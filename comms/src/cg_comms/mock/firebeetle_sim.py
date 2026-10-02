"""FireBeetle device simulator — speaks the real binary wire protocol as the device side of a
Session, driven by a scenario.py timeline (COMMS_IMPLEMENTATION_PLAN.md §13.1).

Runs as a TCP server (TcpServerTransport) so an unmodified relay (configured with
transport.kind="tcp") connects to it exactly as it would connect to a real device in test rigs.
Does not implement periodic re-sync (§5.4) — only the initial handshake burst; a documented
simplification since nothing in the mock layer needs steady-state clock correction.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import itertools
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from cg_comms.clock import monotonic_ms
from cg_comms.logging_setup import configure_logging
from cg_comms.messages.enums import Weapon
from cg_comms.mock.scenario import ScenarioStep, load_scenario
from cg_comms.protocol import codec
from cg_comms.protocol.codec import DecodedPacket, DecodeError
from cg_comms.protocol.constants import PROTOCOL_VERSION, Direction, PacketType
from cg_comms.protocol.crypto import OutgoingSequenceCounter, SequenceTracker
from cg_comms.protocol.framing import Framer
from cg_comms.timer_resolution import high_resolution_timers
from cg_comms.transport.tcp import TcpServerTransport

logger = structlog.get_logger(__name__)


@dataclass
class FireBeetleSim:
    transport: TcpServerTransport
    psk: bytes
    player_id: int

    session_id: int = field(default=0, init=False)
    # Mirrors the belt's own view of the currently-scanned weapon, so SENSOR_FRAME.weapon_id stays
    # consistent with the last RFID_EVENT the way real firmware's would.
    _weapon_id: int = field(default=0, init=False)
    _tx_counter: OutgoingSequenceCounter = field(default_factory=OutgoingSequenceCounter, init=False)
    _rx_tracker: SequenceTracker = field(default_factory=SequenceTracker, init=False)
    _framer: Framer = field(default_factory=Framer, init=False)

    async def _next_decoded(self, expect: PacketType, direction: Direction) -> DecodedPacket:
        async for raw in self.transport.frames():
            for frame in self._framer.feed(raw):
                decoded = codec.decode_packet(
                    frame,
                    psk=self.psk,
                    session_id=self.session_id,
                    direction=direction,
                    sequence_tracker=self._rx_tracker,
                )
                if isinstance(decoded, DecodeError):
                    logger.warning("sim_decode_drop", reason=decoded.reason.value)
                    continue
                if decoded.header.packet_type == int(expect):
                    return decoded
        raise ConnectionError("transport closed while waiting for a packet")

    async def _send(self, packet_type: PacketType, fields: dict, direction: Direction) -> None:
        seq, ext_seq = self._tx_counter.next()
        frame = codec.encode_packet(
            packet_type=packet_type,
            seq=seq,
            player_id=self.player_id,
            fields=fields,
            psk=self.psk,
            session_id=self.session_id,
            direction=direction,
            ext_seq=ext_seq,
        )
        await self.transport.send(frame)

    async def _handshake(self) -> None:
        device_nonce = random.randint(0, 0xFFFFFFFF)
        await self._send(
            PacketType.HELLO,
            {
                # Locally-administered MAC, last octet = player, so the two sims are distinguishable.
                "device_mac": bytes([0x02, 0x00, 0x00, 0x00, 0x00, self.player_id]),
                "player_id": self.player_id,
                "protocol_version": PROTOCOL_VERSION,
                "firmware_version": 4,
                "device_nonce": device_nonce,
                "reserved": 0,
            },
            Direction.DEVICE_TO_LAPTOP,
        )
        ack = await self._next_decoded(PacketType.HELLO_ACK, Direction.LAPTOP_TO_DEVICE)
        self.session_id = ack.fields["session_id"]

        for _ in range(8):
            t0 = monotonic_ms()
            await self._send(PacketType.TIME_SYNC_REQ, {"t0": t0}, Direction.DEVICE_TO_LAPTOP)
            await self._next_decoded(PacketType.TIME_SYNC_RESP, Direction.LAPTOP_TO_DEVICE)
            await asyncio.sleep(0.1)

    async def _play_step(self, step: ScenarioStep) -> None:
        if step.kind == "sensor_frame":
            await self._send(
                PacketType.SENSOR_FRAME,
                {
                    "device_timestamp_ms": monotonic_ms() & 0xFFFFFFFF,
                    "flags": step.payload.get("flags", 0),
                    "weapon_id": self._weapon_id,
                    "dominant": tuple(step.payload["dominant"]),
                    "offhand": tuple(step.payload["offhand"]),
                    "torso": tuple(step.payload.get("torso", (0, 0, 0, 0, 0, 0))),
                },
                Direction.DEVICE_TO_LAPTOP,
            )
        elif step.kind == "rfid_event":
            weapon = Weapon(step.payload["weapon"])
            self._weapon_id = weapon.wire_value
            await self._send(
                PacketType.RFID_EVENT,
                {
                    "device_timestamp_ms": monotonic_ms() & 0xFFFFFFFF,
                    "weapon_id": weapon.wire_value,
                    "reserved": 0,
                    # Stand-in tag UID: real tags carry a factory-unique 6 bytes, and nothing
                    # downstream reads it, so make it deterministic per weapon for readable traces.
                    "tag_uid": bytes([0xE0, 0x04, 0x01, 0x50, self.player_id, weapon.wire_value]),
                },
                Direction.DEVICE_TO_LAPTOP,
            )
        elif step.kind == "heartbeat":
            await self._send(
                PacketType.HEARTBEAT,
                {
                    "device_timestamp_ms": monotonic_ms() & 0xFFFFFFFF,
                    "vbat_belt_mv": step.payload.get("vbat_belt_mv", 4000),
                    "vbat_dom_mv": step.payload.get("vbat_dom_mv", 4000),
                    "vbat_off_mv": step.payload.get("vbat_off_mv", 4000),
                    "status_flags": step.payload.get("status_flags", 0),
                },
                Direction.DEVICE_TO_LAPTOP,
            )
        else:
            logger.warning("sim_unknown_step_kind", kind=step.kind)

    async def _drain_incoming(self) -> None:
        async for raw in self.transport.frames():
            for frame in self._framer.feed(raw):
                # Match firmware: semantic rejection must not enter the replay cache.
                # Otherwise the retry of a rejected request gets a successful duplicate ACK.
                candidate = copy.deepcopy(self._rx_tracker)
                decoded = codec.decode_packet(
                    frame,
                    psk=self.psk,
                    session_id=self.session_id,
                    direction=Direction.LAPTOP_TO_DEVICE,
                    sequence_tracker=candidate,
                    reliable_duplicates=True,
                    expected_player_id=self.player_id,
                )
                if isinstance(decoded, DecodeError):
                    logger.warning("sim_decode_drop", reason=decoded.reason.value)
                    continue
                logger.info("sim_received", packet_type=decoded.header.packet_type)
                # COMBAT_FEEDBACK is in RELIABLE_PACKET_TYPES, so relay/session.py sends it through
                # ReliabilitySender and waits for an ACK. Only logging it here meant every hit was
                # retransmitted to exhaustion and then reported as link_degraded +
                # combat_feedback_undelivered at ERROR level -- a loud, alarming failure on an
                # otherwise perfectly healthy rig, purely because the device never answered. Real
                # firmware has to ACK this too (firmware_demo/src/main.cpp's incoming handler).
                if decoded.header.packet_type == int(PacketType.COMBAT_FEEDBACK):
                    await self._send(
                        PacketType.ACK,
                        {"acked_seq": decoded.header.seq},
                        Direction.DEVICE_TO_LAPTOP,
                    )
                self._rx_tracker = candidate

    async def run(self, scenario: list[ScenarioStep], loop_scenario: bool = False) -> None:
        await self.transport.connect()
        await self._handshake()
        recv_task = asyncio.create_task(self._drain_incoming())
        try:
            steps = itertools.cycle(scenario) if loop_scenario and scenario else scenario
            next_due = asyncio.get_running_loop().time()
            for step in steps:
                # Absolute deadlines, not per-step asyncio.sleep(delay). Windows' default timer
                # resolution is ~15.6ms, so sleep(0.02) actually returns after ~31ms and a 50 Hz
                # scenario streams at ~32 Hz. relay/windowing.py then measures a 40-sample span of
                # ~1216ms against a nominal 780ms, exceeds max_span_deviation_pct, and silently
                # discards every window: the rig handshakes, streams, and produces no actions at
                # all. Scheduling against a fixed clock keeps the *average* rate correct even when
                # individual wakeups overshoot.
                now = asyncio.get_running_loop().time()
                next_due += step.delay_ms / 1000
                if next_due > now:
                    await asyncio.sleep(next_due - now)
                else:
                    # Fell behind (long GC pause, debugger break). Resync rather than trying to
                    # catch up with a burst, which would itself break the span check.
                    next_due = now
                await self._play_step(step)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Callers run the sim as a fire-and-forget task, so an unlogged failure here surfaces
            # only as "the relay went quiet" — log it before the finally-block disconnect hides it.
            logger.exception("sim_scenario_aborted", player_id=self.player_id)
            raise
        finally:
            recv_task.cancel()
            await self.transport.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="FireBeetle device simulator")
    parser.add_argument("--psk-hex", required=True)
    parser.add_argument("--player-id", type=int, required=True)
    parser.add_argument("--tcp-port", type=int, default=9000)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument(
        "--loop-scenario",
        action="store_true",
        help="Replay the scenario forever instead of exiting after one pass. Real firmware streams "
        "until powered off, so a one-shot sim makes the relay log a spurious link failure the "
        "moment the timeline ends — use this for any run longer than the scenario itself.",
    )
    parser.add_argument("--logs-dir", type=Path, default=Path("logs"))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    configure_logging(
        f"firebeetle-sim-p{args.player_id}",
        args.logs_dir,
        level=getattr(logging, args.log_level.upper()),
    )

    async def _main() -> None:
        transport = TcpServerTransport(host="0.0.0.0", port=args.tcp_port)
        sim = FireBeetleSim(
            transport=transport, psk=bytes.fromhex(args.psk_hex), player_id=args.player_id
        )
        steps = load_scenario(args.scenario)
        logger.info(
            "firebeetle_sim_listening",
            tcp_port=args.tcp_port,
            player_id=args.player_id,
            scenario_steps=len(steps),
            loop_scenario=args.loop_scenario,
        )
        # Held for the whole run: this process paces a 50 Hz stream, which the default ~15.6ms
        # Windows timer tick cannot express (see cg_comms/timer_resolution.py).
        with high_resolution_timers():
            await sim.run(steps, loop_scenario=args.loop_scenario)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
