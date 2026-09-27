"""Per-device session: handshake -> streaming -> teardown (COMMS_IMPLEMENTATION_PLAN.md §5.1).

Owns the FireBeetle link's protocol state for one connection lifetime: the negotiated sessionId,
both directions' SequenceTracker/OutgoingSequenceCounter, the TimeSyncEstimator, and the
reliability/heartbeat/windowing pipelines built on top of it. A reconnect gets a fresh Session.

HELLO/HELLO_ACK and TIME_SYNC_REQ/RESP are bounded request/response exchanges, not ACK packets.
If an exchange is lost, firmware retries the whole handshake with a new nonce and the relay
reconnects. Streaming RFID/feedback use exact-ciphertext retransmission and duplicate re-ACKs.
"""

from __future__ import annotations

import asyncio
import random
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from cg_comms.clock import monotonic_ms, split_epoch_ms, wall_clock_unix_ms
from cg_comms.messages.enums import Weapon
from cg_comms.messages.models import Action, CombatFeedback, SensorWindow, WeaponChanged
from cg_comms.protocol import codec
from cg_comms.protocol.codec import DecodedPacket, DecodeError
from cg_comms.protocol.constants import PROTOCOL_VERSION, Direction, PacketType
from cg_comms.protocol.crypto import OutgoingSequenceCounter, SequenceTracker
from cg_comms.protocol.framing import Framer
from cg_comms.relay.assess_echo import EchoPipeline, PendingEcho, encode_echo_request, wait_for_echo
from cg_comms.relay.assess_file import FileTransferResult, run_file_transfer
from cg_comms.relay.heartbeat import HeartbeatMonitor, run_heartbeat_sender, run_liveness_watchdog
from cg_comms.relay.reliability import LinkDegraded, ReliabilitySender
from cg_comms.relay.throughput import ThroughputMeter
from cg_comms.relay.timesync import LaptopSideSample, TimeSyncEstimator, estimate_offset_laptop_side
from cg_comms.relay.ultra96 import Ultra96Client, Ultra96Unavailable, passes_confidence_gate
from cg_comms.relay.windowing import PlayerWindower
from cg_comms.transport.base import ITransport, TransportClosed

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
    # Assessment/demo only. When false (the default and the real-game value) the assessment packet
    # types are still decoded and logged, but nothing in this class originates them, so the normal
    # demo path is bit-for-bit unchanged.
    assessment_enabled: bool = False
    # None => draw a fresh random seed per session (the demo default). Set to an int to replay a
    # previous run's dummy-variant sequence exactly.
    assess_seed: int | None = None
    # How long one keystroke's trace waits for its ASSESS_ECHO_RESP (AssessmentConfig.echo_timeout_s).
    assess_echo_timeout_s: float = 6.0
    # Receive-throughput report period, and where the latest summary JSON goes (None = no file).
    # Reporting runs in every mode: one line per interval is cheap and useful for any soak run.
    stats_interval_s: float = 5.0
    throughput_latest_path: Path | None = None


@dataclass
class Session:
    """One FireBeetle connection's lifetime. `on_action_result`/`on_weapon_changed` forward
    classified windows/weapon changes up to the engine link. Completed SENSOR_WINDOWs go to the
    injected Ultra96 client without blocking the BLE receive loop."""

    transport: ITransport
    config: SessionConfig
    windower: PlayerWindower
    ultra96: Ultra96Client
    on_weapon_changed: Callable[[WeaponChanged], Awaitable[None]]
    on_action_result: Callable[[Action], Awaitable[None]]

    session_id: int = field(default=0, init=False)
    _rx_tracker: SequenceTracker = field(default_factory=SequenceTracker, init=False)
    _tx_counter: OutgoingSequenceCounter = field(default_factory=OutgoingSequenceCounter, init=False)
    _framer: Framer = field(default_factory=Framer, init=False)
    _time_sync: TimeSyncEstimator = field(init=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _reliability_out: ReliabilitySender = field(init=False)
    _heartbeat_monitor: HeartbeatMonitor = field(init=False)
    _seen_device_nonces: dict[int, float] = field(default_factory=dict, init=False)
    _frames: deque[bytes] = field(default_factory=deque, init=False)
    _classification_tasks: set[asyncio.Task] = field(default_factory=set, init=False)
    _feedback: asyncio.Queue[CombatFeedback] = field(
        default_factory=lambda: asyncio.Queue(8), init=False
    )
    _streaming: bool = field(default=False, init=False)
    _last_weapon_ts: int = field(default=-1, init=False)
    echo: EchoPipeline = field(init=False)
    _file_acks: asyncio.Queue[dict] = field(default_factory=lambda: asyncio.Queue(64), init=False)
    # At most ONE assessment reliable packet (ASSESS_ECHO_REQ / ASSESS_FILE_CHUNK) in flight. Not a
    # throughput choice -- a correctness one. If two were in flight and the earlier was lost, its
    # retransmit would arrive after the later packet had already advanced the receiver's replay
    # state; serializing makes "the retransmit is the newest assessment frame" an invariant the
    # firmware can rely on (firmware_demo/src/main.cpp, handle_assess_reliable).
    _assess_tasks: set[asyncio.Task] = field(default_factory=set, init=False)
    _file_data: asyncio.Queue[dict] = field(default_factory=lambda: asyncio.Queue(64), init=False)
    _assess_send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    meter: ThroughputMeter = field(init=False)
    _ack_fault_armed: bool = field(default=False, init=False)
    _ack_fault_target: int | None = field(default=None, init=False)
    _firmware_version: int = field(default=0, init=False)
    _weapon_reply: tuple[int, asyncio.Future] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.meter = ThroughputMeter(
            player_id=self.config.player_id, latest_path=self.config.throughput_latest_path
        )
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
        # Keep the legacy seed option accepted by existing commands. Keyboard selection ignores it;
        # only a/b/c/d are dispatched, and B/C/D read the saved table on every press.
        seed = self.config.assess_seed
        if seed is None:
            seed = random.SystemRandom().getrandbits(64)
        self.echo = EchoPipeline(player_id=self.config.player_id, rng=random.Random(seed))
        if self.config.assessment_enabled:
            logger.info(
                "assess_mapping_ready",
                player_id=self.config.player_id,
                mapping="a=weapon b=B c=C d=D; saved dummy table reloaded per key; seed unused",
            )

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        """Runs the full session lifetime: handshake, then streaming until the transport closes or
        `stop()` is called. Raises HandshakeFailed if the device never completes the handshake."""
        try:
            await self.transport.connect()
            if self.config.assessment_enabled and self.transport.mtu < 76:
                raise TransportClosed("assessment revision 2 needs BLE payload capacity >=76 bytes")
            await self._handshake()
            logger.info("session_streaming", player_id=self.config.player_id, session_id=self.session_id)
            await self._stream()
        finally:
            self._streaming = False
            # Release every keystroke still waiting on a response: the transport is going away, so
            # those traces cannot be resolved and waiting out each individual timeout is pointless.
            self.echo.abandon_all("session ended")
            for task in list(self._assess_tasks):
                task.cancel()
            await asyncio.gather(*self._assess_tasks, return_exceptions=True)
            for task in self._classification_tasks:
                task.cancel()
            await asyncio.gather(*self._classification_tasks, return_exceptions=True)
            await self.transport.disconnect()

    # -- decode helpers -------------------------------------------------------------------

    async def _next_decoded(self, expect: PacketType, timeout_s: float) -> DecodedPacket:
        async def receive() -> DecodedPacket:
            async for frame in self._wire_frames():
                restarting = len(frame) > 2 and frame[2] == int(PacketType.HELLO)
                decoded = codec.decode_packet(
                    frame,
                    psk=self.config.psk,
                    session_id=self.session_id,
                    direction=Direction.DEVICE_TO_LAPTOP,
                    expected_player_id=self.config.player_id,
                    sequence_tracker=(
                        SequenceTracker()
                        if restarting and expect != PacketType.HELLO
                        else self._rx_tracker
                    ),
                )
                if isinstance(decoded, DecodeError):
                    logger.warning("handshake_decode_drop", reason=decoded.reason.value)
                    continue
                if decoded.header.player_id != self.config.player_id:
                    raise HandshakeFailed("player identity mismatch")
                if decoded.header.packet_type == int(expect):
                    return decoded
                if decoded.header.packet_type == int(PacketType.HELLO):
                    raise HandshakeFailed("device restarted handshake; reconnect for fresh session")
                logger.warning("handshake_unexpected_packet", got=decoded.header.packet_type)
            raise HandshakeFailed("transport closed during handshake")

        try:
            return await asyncio.wait_for(receive(), timeout_s)
        except asyncio.TimeoutError as exc:
            logger.warning("handshake_timeout", player_id=self.config.player_id, expected=expect.name)
            raise HandshakeFailed(f"timed out waiting for {expect.name}") from exc

    async def _wire_frames(self):
        # Preserve every frame in a coalesced TCP read across handshake calls and streaming.
        while self._frames:
            yield self._frames.popleft()
        async for raw in self.transport.frames():
            self.meter.on_transport_bytes(len(raw))  # no-op until streaming starts
            self._frames.extend(self._framer.feed(raw))
            while self._frames:
                yield self._frames.popleft()

    def _prune_nonces(self, now: float) -> None:
        cutoff = now - _HANDSHAKE_NONCE_TTL_S
        self._seen_device_nonces = {n: t for n, t in self._seen_device_nonces.items() if t > cutoff}

    # -- handshake --------------------------------------------------------------------------

    async def _handshake(self) -> None:
        hello = await self._next_decoded(PacketType.HELLO, timeout_s=30.0)
        self._firmware_version = hello.fields["firmware_version"]
        device_nonce = hello.fields["device_nonce"]
        now = time.monotonic()
        self._prune_nonces(now)
        if device_nonce in self._seen_device_nonces:
            logger.error("hello_replay_rejected", device_nonce=device_nonce)
            raise HandshakeFailed("HELLO replayed within nonce TTL")
        self._seen_device_nonces[device_nonce] = now

        protocol_version = hello.fields["protocol_version"]
        version_ok = protocol_version == PROTOCOL_VERSION
        self.session_id = (secrets.randbits(32) or 1) if version_ok else 0

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
                fields={"t0": t0, "t1": t1 & 0xFFFFFFFF, "t2": t2 & 0xFFFFFFFF},
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
        offsets = [s.t1 - s.t0 for s in samples]
        logger.info(
            "timesync_seeded",
            player_id=self.config.player_id,
            offset_ms=offset,
            samples=len(samples),
            sample_spread_ms=max(offsets) - min(offsets),
            accuracy="one_way_delay_unknown",
        )

    # -- streaming ----------------------------------------------------------------------------

    async def _stream(self) -> None:
        # The liveness clock starts here, not at construction: the handshake carries no heartbeats,
        # so a monitor seeded in __post_init__ is already past its timeout by the time the watchdog
        # below first polls.
        self._heartbeat_monitor.reset()
        self._streaming = True
        self.meter.start()
        tasks = [
            asyncio.create_task(self._stats_loop()),
            asyncio.create_task(self._read_loop()),
            asyncio.create_task(self._feedback_loop()),
            asyncio.create_task(self._stop_event.wait()),
            asyncio.create_task(
                run_heartbeat_sender(
                    self._send_heartbeat, self.config.heartbeat_interval_ms, self._stop_event
                )
            ),
            asyncio.create_task(
                run_liveness_watchdog(
                    self._heartbeat_monitor,
                    self._on_link_stale,
                    poll_interval_ms=500,
                    stop_event=self._stop_event,
                )
            ),
        ]
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._stop_event.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Whole-run figure for this connection: the number to quote for ">1 min of dummy data".
            self.meter.report(final=True)
        for task in done:
            exc = None if task.cancelled() else task.exception()
            if exc is not None:
                raise exc

    async def _stats_loop(self) -> None:
        # Never returns on its own: a return would complete the FIRST_COMPLETED wait in _stream and
        # tear the session down. Absolute deadlines so the period does not drift under load.
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        while True:
            deadline += self.config.stats_interval_s
            if deadline < loop.time():  # stalled for more than a period: resync, never burst
                deadline = loop.time() + self.config.stats_interval_s
            await asyncio.sleep(max(deadline - loop.time(), 0))
            self.meter.report()
            if self.config.assessment_enabled and self.echo.sent:
                logger.info("assess_echo_stats", player_id=self.config.player_id, **self.echo.stats())

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

    async def send_echo_request(self, keycode: int) -> PendingEcho:
        """Sends one ASSESS_ECHO_REQ for a keystroke and returns its pending trace.

        Sent reliably, reusing the existing ReliabilitySender: the assessment's "show reliability
        features" item then covers this path too, and a dropped request is retransmitted with the
        identical ciphertext rather than re-encrypted (which would change the nonce and break the
        device's duplicate suppression).

        `send_reliable` does not return until the device ACKs or attempts are exhausted, and sends
        are serialized by `_assess_send_lock`. A direct caller queues behind the lock; the keyboard
        (relay/assess_keyboard.py) checks `assess_busy()` first and rejects with BUSY instead, so
        typing never builds an invisible backlog.
        """
        async with self._assess_send_lock:
            return await self._send_echo_request_locked(keycode)

    async def echo_keystroke(self, keycode: int) -> tuple[PendingEcho, bool]:
        """Own the round trip through classification; recover a lost reply even if its ACK arrived."""
        owner = asyncio.current_task()
        assert owner is not None
        self._assess_tasks.add(owner)
        pending = None
        try:
            async with self._assess_send_lock:
                pending = await self._send_echo_request_locked(keycode)
                # Retransmit the SAME request, not a new trace, when response alone was lost.
                for attempt in range(3):
                    if await wait_for_echo(pending, self.config.assess_echo_timeout_s / 3):
                        break
                    if pending.event.is_set():
                        break
                    if attempt < 2:
                        logger.warning(
                            "assess_echo_response_retry", trace_id=pending.trace_id, attempt=attempt + 1
                        )
                        await self._reliability_out.send_reliable(
                            pending.request_seq, pending.request_frame
                        )
                if not pending.event.is_set():
                    self.echo.expire(pending.trace_id, self.config.assess_echo_timeout_s)
                if pending.matched:
                    await self._forward_echo(pending)
                return pending, pending.matched
        finally:
            if pending is not None and not pending.event.is_set():
                self.echo.expire(pending.trace_id, self.config.assess_echo_timeout_s)
            self._assess_tasks.discard(owner)

    def assess_busy(self) -> bool:
        return self._assess_send_lock.locked()

    @property
    def streaming(self) -> bool:
        return self._streaming

    def arm_assessment_ack_loss(self) -> None:
        self._ack_fault_armed = True
        logger.warning(
            "assess_fault_armed",
            player_id=self.config.player_id,
            fault="ignore exactly one received upload ACK; next F only",
        )

    async def _assessment_send(self, kind: PacketType, fields: dict) -> Callable[[], Awaitable[None]]:
        seq, ext_seq = self._tx_counter.next()
        frame = codec.encode_packet(
            packet_type=kind,
            seq=seq,
            player_id=self.config.player_id,
            fields=fields,
            psk=self.config.psk,
            session_id=self.session_id,
            direction=Direction.LAPTOP_TO_DEVICE,
            ext_seq=ext_seq,
        )
        if kind is PacketType.ASSESS_FILE_CHUNK and self._ack_fault_armed:
            self._ack_fault_armed = False
            self._ack_fault_target = seq

        async def resend() -> None:
            await self._reliability_out.send_reliable(seq, frame)

        await resend()
        return resend

    async def cycle_weapon(self) -> None:
        """Board owns the selection. Serialize/retry exactly like other assessment operations."""
        self._require_keyboard_firmware()
        owner = asyncio.current_task()
        assert owner is not None
        self._assess_tasks.add(owner)
        future = None
        try:
            async with self._assess_send_lock:
                trace = self.echo._allocate_trace_id()
                previous_selection = (self._last_weapon_ts, self.windower.current_weapon)
                label = f"P{self.config.player_id}-{self.session_id:08x}-{trace}"
                future = asyncio.get_running_loop().create_future()
                self._weapon_reply = (trace, future)
                logger.info("assess_weapon_sent", key="a", demo_trace_id=label,
                            player_id=self.config.player_id)
                try:
                    resend = await self._assessment_send(PacketType.ASSESS_WEAPON_REQ,
                                                         {"trace_id": trace})
                except LinkDegraded:
                    if not future.done():
                        raise
                    resend = None  # authenticated reply already proves receipt
                for attempt in range(3):
                    try:
                        fields = await asyncio.wait_for(asyncio.shield(future),
                                                       self.config.assess_echo_timeout_s / 3)
                        break
                    except asyncio.TimeoutError:
                        if attempt == 2 or resend is None:
                            raise
                        await resend()  # identical ciphertext: must NOT cycle again
                # Distinct commands can complete in the same device millisecond. Accept that
                # tie only if no RFID/physical selection advanced while this request waited.
                # A delayed cached reply must never undo a newer physical selection.
                await self._handle_weapon_fields(
                    fields, allow_same_timestamp=previous_selection ==
                    (self._last_weapon_ts, self.windower.current_weapon))
                logger.info("assess_weapon_verified", key="a", demo_trace_id=label,
                            player_id=self.config.player_id,
                            weapon=Weapon.from_wire(fields["weapon_id"]).value,
                            current_weapon=self.windower.current_weapon.value
                            if self.windower.current_weapon else None)
        finally:
            if self._weapon_reply is not None and self._weapon_reply[1] is future:
                self._weapon_reply[1].cancel()
                self._weapon_reply = None
            self._assess_tasks.discard(owner)

    def _require_keyboard_firmware(self) -> None:
        if self._firmware_version < 3:
            raise ValueError("keyboard a/b/c/d requires firmware version 3; flash both normal targets")

    async def send_file(self, data: bytes) -> FileTransferResult:
        """Upload and reconstruct actual board RAM bytes under one session/operation lock."""
        owner = asyncio.current_task()
        assert owner is not None
        self._assess_tasks.add(owner)
        returned = bytearray()

        async def send_chunk(fields: dict) -> Callable[[], Awaitable[None]]:
            return await self._assessment_send(PacketType.ASSESS_FILE_CHUNK, fields)

        async def readback(transfer_id: int, size: int) -> bytes:
            while not self._file_data.empty():
                self._file_data.get_nowait()
            for index in range((size + 31) // 32):
                resend = await self._assessment_send(
                    PacketType.ASSESS_FILE_READ, {"transfer_id": transfer_id, "chunk_index": index}
                )
                reply = None
                for attempt in range(3):
                    deadline = asyncio.get_running_loop().time() + 1.0
                    while asyncio.get_running_loop().time() < deadline:
                        try:
                            candidate = await asyncio.wait_for(
                                self._file_data.get(),
                                max(0.001, deadline - asyncio.get_running_loop().time()),
                            )
                        except asyncio.TimeoutError:
                            break
                        if candidate["transfer_id"] == transfer_id and candidate["chunk_index"] == index:
                            reply = candidate
                            break
                    if reply is not None:
                        break
                    if attempt < 2:
                        await resend()
                if reply is None:
                    raise TimeoutError(f"readback chunk {index}")
                n = min(32, size - index * 32)
                if (
                    reply["total_bytes"] != size
                    or reply["chunk_len"] != n
                    or reply["reserved"] != 0
                    or any(reply["data"][n:])
                ):
                    raise ValueError(f"invalid readback chunk {index}")
                returned.extend(reply["data"][:n])
            return bytes(returned)

        try:
            async with self._assess_send_lock:
                result = await run_file_transfer(
                    data,
                    player_id=self.config.player_id,
                    send_chunk=send_chunk,
                    acks=self._file_acks,
                    readback=readback,
                )
                if result.ok and self.config.throughput_latest_path is not None:
                    directory = self.config.throughput_latest_path.parent / "assessment"
                    directory.mkdir(parents=True, exist_ok=True)
                    path = (
                        directory
                        / f"received-p{self.config.player_id}-{self.session_id:08x}-{time.time_ns()}.bin"
                    )
                    path.write_bytes(returned)
                    result.received_path = str(path)
                    logger.info(
                        "assess_file_saved",
                        player_id=self.config.player_id,
                        path=str(path),
                        returned_sha256=result.returned_sha256,
                    )
                return result
        finally:
            self._ack_fault_target = None
            self._assess_tasks.discard(owner)

    async def _send_echo_request_locked(self, keycode: int) -> PendingEcho:
        self._require_keyboard_firmware()
        pending, fields = self.echo.build_request(keycode, self.windower.current_weapon or Weapon.SWORD)
        seq, ext_seq = self._tx_counter.next()
        frame = encode_echo_request(
            fields=fields,
            seq=seq,
            player_id=self.config.player_id,
            psk=self.config.psk,
            session_id=self.session_id,
            ext_seq=ext_seq,
        )
        pending.request_frame, pending.request_seq = frame, seq
        pending.demo_trace_id = f"P{self.config.player_id}-{self.session_id:08x}-{pending.trace_id}"
        logger.info(
            "assess_echo_sent",
            demo_trace_id=pending.demo_trace_id,
            player_id=self.config.player_id,
            trace_id=pending.trace_id,
            seq=seq,
            keycode=keycode,
            key=chr(keycode) if 32 <= keycode < 127 else None,
            variant=pending.variant.name,
            values=pending.variant.values,
            sensor_raw={"dominant": pending.variant.values + fields["dominant_gyro"],
                        "offhand": fields["offhand"], "torso": fields["torso"]},
            transform=pending.transform_id,
            transform_name="IDENTITY",
            expected=pending.expected,
            frame_bytes=len(frame),
        )
        self.echo.mark_sent()
        try:
            await self._reliability_out.send_reliable(seq, frame)
        except LinkDegraded:
            if pending.matched:
                return pending  # the authenticated response itself proves request receipt
            # The device never ACKed. Retire the trace now rather than leaving the caller to wait out
            # the full echo timeout for a response that provably cannot arrive.
            self.echo.forget(pending.trace_id)
            self.echo.lost += 1
            pending.mismatch = "link degraded: request never ACKed"
            pending.event.set()
            logger.error(
                "assess_echo_undelivered",
                player_id=self.config.player_id,
                trace_id=pending.trace_id,
                seq=seq,
            )
        return pending

    async def _handle_echo_response(self, decoded: DecodedPacket) -> None:
        self.echo.on_response(decoded.fields)

    async def _forward_echo(self, pending: PendingEcho) -> None:
        # A window needs a real weapon (RealWeapon excludes NONE), and the engine only emits
        # ActionRecognized when the attacker has a confirmed weapon. Without one the pipeline would
        # stop dead at the engine with every link healthy, so say so explicitly instead.
        weapon = self.windower.current_weapon
        if weapon is None or weapon is Weapon.NONE:
            logger.warning(
                "assess_echo_no_weapon",
                player_id=self.config.player_id,
                trace_id=pending.trace_id,
                hint="tap the RFID tag / press the weapon button first -- the engine "
                "cannot emit ActionRecognized without a confirmed weapon",
            )
            return

        window = self.echo.synthetic_window(
            pending, weapon=pending.weapon, request_id=self.windower.next_request_id()
        )
        if len(self._classification_tasks) >= self.ultra96.max_in_flight:
            logger.warning(
                "assess_echo_dropped", trace_id=pending.trace_id, reason="classifier_capacity"
            )
            return
        logger.info(
            "assess_pipeline_forward",
            player_id=self.config.player_id,
            demo_trace_id=pending.demo_trace_id,
            request_id=window.request_id,
            dominant_first=window.dominant[0],
            key=chr(pending.keycode), variant=pending.variant.name,
            offhand_first=window.offhand[0], weapon=window.weapon.value,
        )
        await self._classify(window)

    def queue_combat_feedback(self, feedback: CombatFeedback) -> None:
        if not self._streaming or feedback.player_id != self.config.player_id:
            logger.warning("combat_feedback_dropped", reason="no_matching_session")
            return
        try:
            self._feedback.put_nowait(feedback)
        except asyncio.QueueFull:
            logger.warning("combat_feedback_dropped", reason="capacity")

    async def _feedback_loop(self) -> None:
        while True:
            await self.send_combat_feedback(await self._feedback.get())

    async def _read_loop(self) -> None:
        async for frame in self._wire_frames():
            await self._handle_frame(frame)

    async def _handle_frame(self, frame: bytes) -> None:
        restarting = len(frame) > 2 and frame[2] == int(PacketType.HELLO)
        decoded = codec.decode_packet(
            frame,
            psk=self.config.psk,
            session_id=self.session_id,
            direction=Direction.DEVICE_TO_LAPTOP,
            expected_player_id=self.config.player_id,
            sequence_tracker=SequenceTracker() if restarting else self._rx_tracker,
            reliable_duplicates=True,
        )
        if isinstance(decoded, DecodeError):
            self.meter.on_drop(decoded.reason.value)
            logger.warning("stream_decode_drop", reason=decoded.reason.value)
            return

        packet_type = decoded.header.packet_type
        if restarting:
            raise HandshakeFailed("device restarted handshake during streaming")
        if decoded.header.player_id != self.config.player_id:
            logger.warning("stream_player_mismatch", actual=decoded.header.player_id)
            return
        self.meter.on_frame(frame, decoded)
        if packet_type == int(PacketType.HEARTBEAT):
            self._heartbeat_monitor.mark_received()
        elif packet_type == int(PacketType.ACK):
            if self._ack_fault_target == decoded.fields["acked_seq"]:
                logger.warning(
                    "assess_fault_consumed",
                    player_id=self.config.player_id,
                    acked_seq=self._ack_fault_target,
                    fault="local ACK suppression",
                )
                self._ack_fault_target = None
            else:
                self._reliability_out.on_ack(decoded.fields["acked_seq"])
        elif packet_type == int(PacketType.SENSOR_FRAME):
            self._heartbeat_monitor.mark_received()
            await self._handle_sensor_frame(decoded)
        elif packet_type == int(PacketType.RFID_EVENT):
            self._heartbeat_monitor.mark_received()
            await self._send_ack(decoded.header.seq)
            if not decoded.duplicate:
                await self._handle_rfid_event(decoded)
        elif packet_type == int(PacketType.TIME_SYNC_REQ):
            # Request/response exchange, not ACK-of-ACK reliability. Current demo firmware only
            # initiates sync at handshake, but a delayed/future resync request must receive RESP.
            t1 = monotonic_ms()
            seq, ext_seq = self._tx_counter.next()
            response = codec.encode_packet(
                packet_type=PacketType.TIME_SYNC_RESP,
                seq=seq,
                player_id=self.config.player_id,
                fields={
                    "t0": decoded.fields["t0"],
                    "t1": t1 & 0xFFFFFFFF,
                    "t2": monotonic_ms() & 0xFFFFFFFF,
                },
                psk=self.config.psk,
                session_id=self.session_id,
                direction=Direction.LAPTOP_TO_DEVICE,
                ext_seq=ext_seq,
            )
            await self.transport.send(response)
        elif packet_type == int(PacketType.ASSESS_WEAPON_RESP):
            waiting = self._weapon_reply
            if (waiting is not None and decoded.fields["trace_id"] == waiting[0]
                    and not waiting[1].done() and decoded.fields["weapon_id"] in (1, 2, 3)):
                waiting[1].set_result(decoded.fields)
        elif packet_type == int(PacketType.ASSESS_ECHO_RESP):
            # Best-effort inbound (the device does not expect an ACK for it); the REQ's own
            # retransmit is what recovers a lost round trip. Duplicates are tolerated: a repeated
            # RESP finds its trace already retired and is logged as assess_echo_duplicate_response
            # (info), never applied twice.
            self._heartbeat_monitor.mark_received()
            await self._handle_echo_response(decoded)
        elif packet_type == int(PacketType.ASSESS_FILE_DATA):
            self._heartbeat_monitor.mark_received()
            if not self._file_data.full():
                self._file_data.put_nowait(decoded.fields)
        elif packet_type == int(PacketType.ASSESS_FILE_ACK):
            self._heartbeat_monitor.mark_received()
            logger.debug(
                "assess_file_ack",
                player_id=self.config.player_id,
                chunk_index=decoded.fields["chunk_index"],
                running_crc=f"0x{decoded.fields['running_crc']:04X}",
                received_bytes=decoded.fields["received_bytes"],
            )
            try:
                self._file_acks.put_nowait(decoded.fields)
            except asyncio.QueueFull:
                # The file sender is not waiting on acks (or is behind). Dropping is correct: the
                # ack is already logged above, and the sender's own retransmit is what guarantees
                # delivery -- blocking the BLE receive loop here would stall every other packet.
                logger.warning("assess_file_ack_dropped", chunk_index=decoded.fields["chunk_index"])
        else:
            logger.warning("stream_unexpected_packet", packet_type=packet_type)

    async def _handle_rfid_event(self, decoded: DecodedPacket) -> None:
        await self._handle_weapon_fields(decoded.fields)

    async def _handle_weapon_fields(self, fields: dict, *, allow_same_timestamp: bool = False) -> None:
        # Both physical RFID/GPIO and authenticated keyboard replies use this one state path.
        engine_ts = self._time_sync.to_engine_ms(fields["device_timestamp_ms"])
        if (engine_ts < self._last_weapon_ts or
                (engine_ts == self._last_weapon_ts and not allow_same_timestamp)):
            return  # tolerate legacy firmware retries with fresh seq, without rolling state back
        self._last_weapon_ts = engine_ts
        weapon = Weapon.from_wire(fields["weapon_id"])
        self.windower.on_weapon_confirmed(weapon if weapon is not Weapon.NONE else None)
        if weapon is Weapon.NONE:
            return
        logger.info(
            "weapon_changed",
            player_id=self.config.player_id,
            relay_timestamp_ms=engine_ts,
            weapon=weapon.value,
        )
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
        logger.info(
            "window_ready",
            player_id=window.player_id,
            request_id=window.request_id,
            trigger_id=window.trigger_id,
            capture_id=self.windower._capture_id,
            window_timestamp_ms=window.timestamp,
            weapon=window.weapon.value,
        )
        if len(self._classification_tasks) >= self.ultra96.max_in_flight:
            logger.warning("window_dropped", request_id=window.request_id, reason="classifier_capacity")
            return
        task = asyncio.create_task(self._classify(window))
        self._classification_tasks.add(task)
        task.add_done_callback(self._classification_done)

    def _classification_done(self, task: asyncio.Task) -> None:
        self._classification_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("classification_failed", error=str(task.exception()))

    async def _classify(self, window: SensorWindow) -> None:
        try:
            result = await self.ultra96.request(window)
        except Ultra96Unavailable as exc:
            logger.warning(
                "window_dropped",
                player_id=window.player_id,
                request_id=window.request_id,
                reason=str(exc),
                demo_trace_id=window.demo_trace_id,
            )
            return
        if result is None:
            logger.warning(
                "window_dropped", player_id=window.player_id, request_id=window.request_id,
                demo_trace_id=window.demo_trace_id, reason="classifier_timeout",
                hint="BLE echo verification does not mean the action reached the engine or phone",
            )
            return
        if not passes_confidence_gate(result, self.config.min_action_confidence):
            logger.info(
                "action_below_confidence_gate",
                action=result.action.value,
                confidence=result.action_confidence,
                request_id=window.request_id,
            )
            return
        logger.info(
            "action_classified",
            player_id=result.player_id,
            request_id=result.request_id,
            demo_trace_id=window.demo_trace_id,
            trigger_id=window.trigger_id,
            window_timestamp_ms=window.timestamp,
            action=result.action.value,
            confidence=result.action_confidence,
        )
        await self.on_action_result(
            Action(
                timestamp=window.timestamp,
                player_id=window.player_id,
                weapon=window.weapon,
                action=result.action,
                action_confidence=result.action_confidence,
                guard_state=result.guard_state,
                guard_confidence=result.guard_confidence,
                demo_trace_id=window.demo_trace_id,
            )
        )
