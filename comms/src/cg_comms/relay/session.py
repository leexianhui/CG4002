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
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

from cg_comms.clock import monotonic_ms, split_epoch_ms, wall_clock_unix_ms
from cg_comms.messages.enums import Weapon
from cg_comms.messages.models import Action, CombatFeedback, SensorWindow, WeaponChanged
from cg_comms.protocol import codec
from cg_comms.protocol.codec import DecodedPacket, DecodeError
from cg_comms.protocol.constants import PROTOCOL_VERSION, Direction, PacketType
from cg_comms.protocol.crypto import OutgoingSequenceCounter, SequenceTracker
from cg_comms.protocol.framing import Framer
from cg_comms.relay.heartbeat import HeartbeatMonitor, run_heartbeat_sender, run_liveness_watchdog
from cg_comms.relay.reliability import LinkDegraded, ReliabilitySender
from cg_comms.relay.segments import OFFHAND_PLACEHOLDER, Segment, SegmentAssembler, classifier_rows
from cg_comms.relay.timesync import LaptopSideSample, TimeSyncEstimator, estimate_offset_laptop_side
from cg_comms.relay.ultra96 import Ultra96Client, Ultra96Unavailable, passes_confidence_gate
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
    segment_mode: bool = False
    segment_timeout_ms: float = 2000.0


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
    _segments: SegmentAssembler = field(init=False)
    # request_id -> segment_id for in-flight segment-mode windows, for the segment_classified log.
    _segment_requests: dict[int, int] = field(default_factory=dict, init=False)

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
        self._segments = SegmentAssembler(timeout_ms=self.config.segment_timeout_ms)

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        """Runs the full session lifetime: handshake, then streaming until the transport closes or
        `stop()` is called. Raises HandshakeFailed if the device never completes the handshake."""
        try:
            await self.transport.connect()
            await self._handshake()
            logger.info("session_streaming", player_id=self.config.player_id, session_id=self.session_id)
            await self._stream()
        finally:
            self._streaming = False
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
            self._frames.extend(self._framer.feed(raw))
            while self._frames:
                yield self._frames.popleft()

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
        tasks = [
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
        for task in done:
            exc = None if task.cancelled() else task.exception()
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
            logger.warning("stream_decode_drop", reason=decoded.reason.value)
            return

        packet_type = decoded.header.packet_type
        if restarting:
            raise HandshakeFailed("device restarted handshake during streaming")
        if decoded.header.player_id != self.config.player_id:
            logger.warning("stream_player_mismatch", actual=decoded.header.player_id)
            return
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
            if not decoded.duplicate:
                await self._handle_rfid_event(decoded)
        elif packet_type == int(PacketType.SEGMENT_CHUNK):
            self._heartbeat_monitor.mark_received()
            await self._send_ack(decoded.header.seq)
            if not decoded.duplicate:
                self._handle_segment_chunk(decoded)
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
        else:
            logger.warning("stream_unexpected_packet", packet_type=packet_type)

    async def _handle_rfid_event(self, decoded: DecodedPacket) -> None:
        fields = decoded.fields
        engine_ts = self._time_sync.to_engine_ms(fields["device_timestamp_ms"])
        if engine_ts <= self._last_weapon_ts:
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
        self._start_classification(window)

    def _handle_segment_chunk(self, decoded: DecodedPacket) -> None:
        if not self.config.segment_mode:
            logger.warning("segment_ignored", reason="segment_mode_off",
                           segment_id=decoded.fields["segment_id"])
            return
        segment = self._segments.add(decoded.fields)
        if segment is None:
            return
        window = self._segment_window(segment)
        if window is None:
            return
        self._start_classification(window)

    def _segment_window(self, segment: Segment) -> SensorWindow | None:
        windower = self.windower
        weapon = windower.current_weapon
        try:
            sent_weapon = Weapon.from_wire(segment.weapon_id)
        except ValueError:
            sent_weapon = Weapon.NONE
        if sent_weapon is not Weapon.NONE:
            weapon = sent_weapon  # the weapon locked when the hub accepted this action
        start_ts = self._time_sync.to_engine_ms(segment.first_sample_ms)
        span_ms = round(1000.0 * (len(segment.samples) - 1) / segment.sample_rate_hz)
        log = dict(player_id=self.config.player_id, segment_id=segment.segment_id,
                   samples=len(segment.samples), sample_rate_hz=segment.sample_rate_hz,
                   chunks=segment.chunk_count, assembly_ms=round(segment.assembly_ms, 1),
                   span_ms=span_ms)
        if weapon is None or weapon is Weapon.NONE:
            logger.info("segment_suppressed", reason="no_weapon", **log)
            return None
        n = windower.window_samples
        dominant = classifier_rows(segment, windower.frame_rate_hz, n)
        window = SensorWindow(
            # Arrival time, not the segment's end: the hub only sends a segment after its stroke has
            # settled (~0.4-1 s later), and the engine drops actions older than its reorder window
            # (max_age_exceeded). The device-time span stays in windowStartTs/windowEndTs.
            timestamp=max(monotonic_ms(), start_ts + span_ms),
            player_id=self.config.player_id,  # type: ignore[arg-type]
            request_id=windower.next_request_id(),
            weapon=weapon,  # type: ignore[arg-type]
            window_start_ts=start_ts,
            window_end_ts=start_ts + span_ms,
            sample_rate_hz=windower.frame_rate_hz,
            dominant=dominant,
            offhand=[list(OFFHAND_PLACEHOLDER) for _ in range(n)],
            # Only the dominant half is measured; the placeholder off-hand half counts as stale.
            stale_pct=50.0,
        )
        logger.info("segment_received", request_id=window.request_id, weapon=weapon.value, **log)
        self._segment_requests[window.request_id] = segment.segment_id
        return window

    def _start_classification(self, window: SensorWindow) -> None:
        if len(self._classification_tasks) >= self.ultra96.max_in_flight:
            logger.warning("window_dropped", request_id=window.request_id, reason="classifier_capacity")
            self._segment_requests.pop(window.request_id, None)
            return
        task = asyncio.create_task(self._classify(window))
        self._classification_tasks.add(task)
        task.add_done_callback(self._classification_done)
        task.add_done_callback(lambda _t: self._segment_requests.pop(window.request_id, None))

    def _classification_done(self, task: asyncio.Task) -> None:
        self._classification_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("classification_failed", error=str(task.exception()))

    async def _classify(self, window: SensorWindow) -> None:
        segment_id = self._segment_requests.get(window.request_id)
        try:
            result = await self.ultra96.request(window)
        except Ultra96Unavailable as exc:
            logger.warning(
                "window_dropped",
                player_id=window.player_id,
                request_id=window.request_id,
                reason=str(exc),
            )
            return
        if result is None:
            logger.warning(
                "window_dropped", player_id=window.player_id, request_id=window.request_id,
                reason="classifier_timeout",
            )
            return
        if segment_id is not None:
            logger.info(
                "segment_classified",
                player_id=result.player_id,
                request_id=result.request_id,
                segment_id=segment_id,
                action=result.action.value,
                confidence=result.action_confidence,
                passes_gate=passes_confidence_gate(result, self.config.min_action_confidence),
            )
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
            )
        )
