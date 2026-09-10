"""Per-device session: handshake -> streaming -> teardown (COMMS_IMPLEMENTATION_PLAN.md §5.1).

Owns the FireBeetle link's protocol state for one connection lifetime: the negotiated sessionId,
both directions' SequenceTracker/OutgoingSequenceCounter, the TimeSyncEstimator, and the
reliability/heartbeat/windowing pipelines built on top of it. A reconnect gets a fresh Session.

The handshake's own HELLO_ACK/TIME_SYNC_RESP sends are best-effort (not wrapped in
ReliabilitySender) — the 8-sample timesync burst already gives the exchange its own repetition,
and full ACK/retry plumbing is reserved for the streaming phase below, where a concurrent read loop
actually exists to observe the resulting ACK frames.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import structlog

from cg_comms.clock import monotonic_ms, split_epoch_ms, wall_clock_unix_ms
from cg_comms.messages.enums import Weapon
from cg_comms.messages.models import CombatFeedback, WeaponChanged
from cg_comms.protocol import codec
from cg_comms.protocol.codec import DecodeError, DecodedPacket
from cg_comms.protocol.constants import PROTOCOL_VERSION, Direction, PacketType
from cg_comms.protocol.crypto import OutgoingSequenceCounter, SequenceTracker
from cg_comms.protocol.framing import Framer
from cg_comms.relay.heartbeat import HeartbeatMonitor, run_heartbeat_sender, run_liveness_watchdog
from cg_comms.relay.reliability import LinkDegraded, ReliabilitySender
from cg_comms.relay.timesync import LaptopSideSample, TimeSyncEstimator, estimate_offset_laptop_side
from cg_comms.relay.ultra96 import Ultra96Client, passes_confidence_gate
from cg_comms.relay.windowing import PlayerWindower
from cg_comms.transport.base import ITransport

logger = structlog.get_logger(__name__)

_HANDSHAKE_NONCE_TTL_S = 60.0


class HandshakeFailed(Exception):
    pass


@dataclass
class SessionConfig:
    player_id: int
    psk: bytes
    handshake_samples: int
    handshake_spacing_ms: int
    ewma_alpha: float
    outlier_rtt_multiplier: float
    divergence_warn_ms: float
    heartbeat_interval_ms: int
    heartbeat_liveness_factor: int
    reliability_base_timeout_ms: int
    reliability_backoff_factor: float
    reliability_max_attempts: int
    reliability_jitter_pct: float
    min_action_confidence: float


@dataclass
class Session:
    """One FireBeetle connection's lifetime. `on_action_result`/`on_weapon_changed` forward
    classified windows/weapon changes up to the engine link; `send_window` hands a completed
    SENSOR_WINDOW to the Ultra96 client (both injected so this module never imports websockets)."""

    transport: ITransport
    config: SessionConfig
    windower: PlayerWindower
    ultra96: Ultra96Client
    on_weapon_changed: Callable[[WeaponChanged], Awaitable[None]]
    on_action_result: Callable[[object], Awaitable[None]]  # ActionResult, kept loosely typed here

    session_id: int = field(default=0, init=False)
    _rx_tracker: SequenceTracker = field(default_factory=SequenceTracker, init=False)
    _tx_counter: OutgoingSequenceCounter = field(default_factory=OutgoingSequenceCounter, init=False)
    _framer: Framer = field(default_factory=Framer, init=False)
    _time_sync: TimeSyncEstimator = field(init=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _reliability_out: ReliabilitySender = field(init=False)
    _heartbeat_monitor: HeartbeatMonitor = field(init=False)
    _seen_device_nonces: dict[int, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._time_sync = TimeSyncEstimator(ewma_alpha=self.config.ewma_alpha)
        self._reliability_out = ReliabilitySender(
            send_frame=self.transport.send,
            base_timeout_ms=self.config.reliability_base_timeout_ms,
            backoff_factor=self.config.reliability_backoff_factor,
            max_attempts=self.config.reliability_max_attempts,
            jitter_pct=self.config.reliability_jitter_pct,
        )
        self._heartbeat_monitor = HeartbeatMonitor(
            interval_ms=self.config.heartbeat_interval_ms,
            liveness_timeout_factor=self.config.heartbeat_liveness_factor,
        )

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        """Runs the full session lifetime: handshake, then streaming until the transport closes or
        `stop()` is called. Raises HandshakeFailed if the device never completes the handshake."""
        await self.transport.connect()
        try:
            await self._handshake()
            await self._stream()
        finally:
            await self.transport.disconnect()

    # -- decode helpers -------------------------------------------------------------------

    async def _next_decoded(self, expect: PacketType, timeout_s: float) -> DecodedPacket:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        async for raw in self.transport.frames():
            for frame in self._framer.feed(raw):
                decoded = codec.decode_packet(
                    frame,
                    psk=self.config.psk,
                    session_id=self.session_id,
                    direction=Direction.DEVICE_TO_LAPTOP,
                    sequence_tracker=self._rx_tracker,
                )
                if isinstance(decoded, DecodeError):
                    logger.warning("handshake_decode_drop", reason=decoded.reason.value)
                    continue
                if decoded.header.packet_type == int(expect):
                    return decoded
                logger.warning("handshake_unexpected_packet", got=decoded.header.packet_type)
            if loop.time() > deadline:
                break
        raise HandshakeFailed(f"timed out waiting for packet type {expect!r}")

    def _prune_nonces(self, now: float) -> None:
        cutoff = now - _HANDSHAKE_NONCE_TTL_S
        self._seen_device_nonces = {n: t for n, t in self._seen_device_nonces.items() if t > cutoff}

    # -- handshake --------------------------------------------------------------------------

    async def _handshake(self) -> None:
        hello = await self._next_decoded(PacketType.HELLO, timeout_s=30.0)
        device_nonce = hello.fields["device_nonce"]
        now = time.monotonic()
        self._prune_nonces(now)
        if device_nonce in self._seen_device_nonces:
            logger.error("hello_replay_rejected", device_nonce=device_nonce)
            raise HandshakeFailed("HELLO replayed within nonce TTL")
        self._seen_device_nonces[device_nonce] = now

        protocol_version = hello.fields["protocol_version"]
        version_ok = protocol_version == PROTOCOL_VERSION
        self.session_id = random.randint(1, 0xFFFFFFFF) if version_ok else 0

        epoch_lo, epoch_hi = split_epoch_ms(wall_clock_unix_ms())
        seq, _ = self._tx_counter.next()
        ack_frame = codec.encode_packet(
            packet_type=PacketType.HELLO_ACK,
            seq=seq,
            player_id=self.config.player_id,
            fields={
                "session_id": self.session_id,
                "server_epoch_unix_ms_lo": epoch_lo,
                "server_epoch_unix_ms_hi": epoch_hi,
                "device_nonce": device_nonce,
            },
            psk=self.config.psk,
            session_id=0,
            direction=Direction.LAPTOP_TO_DEVICE,
            ext_seq=0,
        )
        await self.transport.send(ack_frame)
        if not version_ok:
            logger.error("hello_version_mismatch", got=protocol_version, expected=PROTOCOL_VERSION)
            raise HandshakeFailed("protocol version mismatch, HELLO refused")

        await self._run_timesync_burst()

    async def _run_timesync_burst(self) -> None:
        samples: list[LaptopSideSample] = []
        for _ in range(self.config.handshake_samples):
            req = await self._next_decoded(PacketType.TIME_SYNC_REQ, timeout_s=5.0)
            t0 = req.fields["t0"]
            t1 = monotonic_ms()
            seq, ext_seq = self._tx_counter.next()
            t2 = monotonic_ms()
            resp_frame = codec.encode_packet(
                packet_type=PacketType.TIME_SYNC_RESP,
                seq=seq,
                player_id=self.config.player_id,
                fields={"t0": t0, "t1": t1, "t2": t2},
                psk=self.config.psk,
                session_id=self.session_id,
                direction=Direction.LAPTOP_TO_DEVICE,
                ext_seq=ext_seq,
            )
            await self.transport.send(resp_frame)
            samples.append(LaptopSideSample(t0=t0, t1=t1, t2=t2))
            await asyncio.sleep(self.config.handshake_spacing_ms / 1000)

        offset = estimate_offset_laptop_side(samples, self.config.outlier_rtt_multiplier)
        self._time_sync.seed(offset)
        logger.info("timesync_seeded", offset_ms=offset, samples=len(samples))

    # -- streaming ----------------------------------------------------------------------------

    async def _stream(self) -> None:
        # The liveness clock starts here, not at construction: the handshake carries no heartbeats,
        # so a monitor seeded in __post_init__ is already past its timeout by the time the watchdog
        # below first polls.
        self._heartbeat_monitor.reset()
        tasks = [
            asyncio.create_task(self._read_loop()),
            asyncio.create_task(
                run_heartbeat_sender(self._send_heartbeat, self.config.heartbeat_interval_ms, self._stop_event)
            ),
            asyncio.create_task(
                run_liveness_watchdog(
                    self._heartbeat_monitor, self._on_link_stale, poll_interval_ms=500, stop_event=self._stop_event
                )
            ),
        ]
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._stop_event.set()
            for task in tasks:
                task.cancel()
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc

    async def _on_link_stale(self) -> None:
        logger.error("link_lost", player_id=self.config.player_id)
        self._stop_event.set()

    async def _send_heartbeat(self) -> None:
        seq, ext_seq = self._tx_counter.next()
        frame = codec.encode_packet(
            packet_type=PacketType.HEARTBEAT,
            seq=seq,
            player_id=self.config.player_id,
            fields={
                "device_timestamp_ms": wall_clock_unix_ms() & 0xFFFFFFFF,
                "vbat_belt_mv": 0,
                "vbat_dom_mv": 0,
                "vbat_off_mv": 0,
                "status_flags": 0,
            },
            psk=self.config.psk,
            session_id=self.session_id,
            direction=Direction.LAPTOP_TO_DEVICE,
            ext_seq=ext_seq,
        )
        await self.transport.send(frame)
        logger.debug("heartbeat_sent", seq=seq)

    async def _send_ack(self, acked_seq: int) -> None:
        seq, ext_seq = self._tx_counter.next()
        frame = codec.encode_packet(
            packet_type=PacketType.ACK,
            seq=seq,
            player_id=self.config.player_id,
            fields={"acked_seq": acked_seq},
            psk=self.config.psk,
            session_id=self.session_id,
            direction=Direction.LAPTOP_TO_DEVICE,
            ext_seq=ext_seq,
        )
        await self.transport.send(frame)

    async def send_combat_feedback(self, feedback: CombatFeedback) -> None:
        """Translates one COMBAT_FEEDBACK app message 1:1 into the binary packet and sends it
        reliably (§6 — this packet type is on RELIABLE_PACKET_TYPES)."""
        from cg_comms.messages.enums import FeedbackCode

        seq, ext_seq = self._tx_counter.next()
        frame = codec.encode_packet(
            packet_type=PacketType.COMBAT_FEEDBACK,
            seq=seq,
            player_id=self.config.player_id,
            fields={
                "feedback_code": FeedbackCode(feedback.code).wire_value,
                "intensity_pct": feedback.intensity_pct,
                "duration_ms": feedback.duration_ms,
                "repeat_count": feedback.repeat_count,
            },
            psk=self.config.psk,
            session_id=self.session_id,
            direction=Direction.LAPTOP_TO_DEVICE,
            ext_seq=ext_seq,
        )
        try:
            await self._reliability_out.send_reliable(seq, frame)
        except LinkDegraded:
            logger.error("combat_feedback_undelivered", player_id=self.config.player_id)

    async def _read_loop(self) -> None:
        async for raw in self.transport.frames():
            for frame in self._framer.feed(raw):
                await self._handle_frame(frame)

    async def _handle_frame(self, frame: bytes) -> None:
        decoded = codec.decode_packet(
            frame,
            psk=self.config.psk,
            session_id=self.session_id,
            direction=Direction.DEVICE_TO_LAPTOP,
            sequence_tracker=self._rx_tracker,
        )
        if isinstance(decoded, DecodeError):
            logger.warning("stream_decode_drop", reason=decoded.reason.value)
            return

        packet_type = decoded.header.packet_type
        if packet_type == int(PacketType.HEARTBEAT):
            self._heartbeat_monitor.mark_received()
        elif packet_type == int(PacketType.ACK):
            self._reliability_out.on_ack(decoded.fields["acked_seq"])
        elif packet_type == int(PacketType.SENSOR_FRAME):
            self._heartbeat_monitor.mark_received()
            await self._handle_sensor_frame(decoded)
        elif packet_type == int(PacketType.RFID_EVENT):
            self._heartbeat_monitor.mark_received()
            await self._send_ack(decoded.header.seq)
            await self._handle_rfid_event(decoded)
        elif packet_type == int(PacketType.TIME_SYNC_REQ):
            await self._send_ack(decoded.header.seq)
        else:
            logger.warning("stream_unexpected_packet", packet_type=packet_type)

    async def _handle_rfid_event(self, decoded: DecodedPacket) -> None:
        weapon = Weapon.from_wire(decoded.fields["weapon_id"])
        self.windower.on_weapon_confirmed(weapon if weapon is not Weapon.NONE else None)
        if weapon is Weapon.NONE:
            return
        engine_ts = self._time_sync.to_engine_ms(decoded.fields["device_timestamp_ms"])
        await self.on_weapon_changed(
            WeaponChanged(timestamp=engine_ts, player_id=self.config.player_id, weapon=weapon)  # type: ignore[arg-type]
        )

    async def _handle_sensor_frame(self, decoded: DecodedPacket) -> None:
        engine_ts = self._time_sync.to_engine_ms(decoded.fields["device_timestamp_ms"])
        window = self.windower.ingest_frame(
            engine_ts_ms=engine_ts,
            flags=decoded.fields["flags"],
            dominant_raw=decoded.fields["dominant"],
            offhand_raw=decoded.fields["offhand"],
        )
        if window is None:
            return
        logger.info("window_ready", request_id=window.request_id, weapon=window.weapon.value)
        result = await self.ultra96.request(window)
        if result is None:
            return
        if not passes_confidence_gate(result, self.config.min_action_confidence):
            logger.info(
                "action_below_confidence_gate",
                action=result.action.value,
                confidence=result.action_confidence,
            )
            return
        logger.info("action_classified", action=result.action.value, confidence=result.action_confidence)
        await self.on_action_result(result)
