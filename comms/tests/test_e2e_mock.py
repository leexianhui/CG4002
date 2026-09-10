"""Integration tests: full relay <-> engine stack over mock transports/simulators
(COMMS_IMPLEMENTATION_PLAN.md §13 "Integration" bullet).

Exercises the real relay/session.py, relay/windowing.py, relay/ultra96.py, relay/engine_link.py,
engine/app.py (via run_engine), engine/game_state_task.py, engine/ws_server.py, mock/firebeetle_sim.py
and transport/loopback.py end to end, driving everything through application-level entry points.
Deliberately black-box with respect to protocol/codec.py, protocol/crypto.py and engine/resolver.py:
this file never imports them and only ever infers their externally-observable behaviour from the
plan's prose and from the modules that call them.

Two of the three tests run the *same* scenario (a single player-1 sword swing against a stationary,
never-guarding player 2) once over a plain in-process LoopbackTransport (the control) and once over
LossyReorderingTransport (a transport-level fault injector built here, since neither transport/ nor
mock/ ships one despite COMMS_IMPLEMENTATION_PLAN.md §11 describing --drop-rate/--reorder-rate CLI
flags for firebeetle_sim.py -- those flags do not actually exist in mock/firebeetle_sim.py as of this
writing; see the docstring on LossyReorderingTransport for how this gap was worked around instead of
silently invented into the mock module itself).
"""

from __future__ import annotations

import asyncio
import random
import socket
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

import websockets

from cg_comms.clock import monotonic_ms
from cg_comms.config import EngineSettings
from cg_comms.engine.app import run_engine
from cg_comms.messages.enums import ActionClass, CombatOutcome, GuardState, Weapon
from cg_comms.messages.models import (
    Action,
    ActionResult,
    CombatEvent,
    Position,
    SensorWindow,
    WeaponChanged,
)
from cg_comms.messages.validate import MessageDropped, parse_app_message
from cg_comms.mock.firebeetle_sim import FireBeetleSim
from cg_comms.mock.scenario import ScenarioStep
from cg_comms.protocol.constants import RELIABLE_PACKET_TYPES, TYPE_MASK
from cg_comms.relay.engine_link import EngineLinkClient
from cg_comms.relay.session import Session, SessionConfig
from cg_comms.relay.ultra96 import Ultra96Client
from cg_comms.relay.windowing import PlayerWindower
from cg_comms.transport.base import ITransport, TransportClosed
from cg_comms.transport.loopback import LoopbackTransport

PSK = bytes(range(16))
BEARER_TOKEN = "integration-test-token"


# ---------------------------------------------------------------------------------------------
# Test-only lossy/reordering transport
# ---------------------------------------------------------------------------------------------


def _packet_type_of(frame: bytes) -> int:
    """Peeks the plaintext wire header's TYPE byte. Per COMMS_IMPLEMENTATION_PLAN.md §4's frame
    layout (MAGIC, VERSION, TYPE, ...) the TYPE byte is frame[2], masked with TYPE_MASK since bit 7
    is the MORE_FRAGMENTS flag. This reads only two public constants from protocol/constants.py and
    never touches protocol/codec.py or protocol/crypto.py."""
    return frame[2] & TYPE_MASK


class LossyReorderingTransport(ITransport):
    """Test-only ITransport that drops and reorders frames, to exercise the reliability/backoff
    and reorder-buffer paths end to end (COMMS_IMPLEMENTATION_PLAN.md §13's Integration bullet).

    Gap this works around: neither transport/ nor mock/ ships a lossy/reordering transport, and
    mock/firebeetle_sim.py's CLI has no --drop-rate/--reorder-rate/--latency-ms flags despite
    COMMS_IMPLEMENTATION_PLAN.md §11 describing them -- confirmed absent by reading the actual
    file. This class is a local substitute built for this test file rather than a change to the
    mock/transport layer itself.

    Only drops packet types that are NOT in RELIABLE_PACKET_TYPES (i.e. SENSOR_FRAME, HEARTBEAT,
    ACK). Nothing in this codebase retries HELLO/HELLO_ACK/TIME_SYNC_*/RFID_EVENT/COMBAT_FEEDBACK
    sends at the *handshake* layer -- relay/session.py's own docstring says its HELLO_ACK/
    TIME_SYNC_RESP sends are "best-effort (not wrapped in ReliabilitySender)", and
    mock/firebeetle_sim.py's HELLO/RFID_EVENT sends have no retry loop either. Unconditionally
    dropping those packet types would therefore produce permanent hangs that have nothing to do
    with the reliability/reordering behaviour this test wants to exercise. Reordering (a bounded
    random delivery delay) applies to every packet type uniformly, since correct handling of
    out-of-order arrival is exactly what's under test and every consumer here already tolerates it
    (the Framer/handshake loops match on packet type, not arrival order).
    """

    def __init__(
        self,
        inbox: "asyncio.Queue[bytes | None]",
        outbox: "asyncio.Queue[bytes | None]",
        rng: random.Random,
        drop_rate: float,
        reorder_rate: float,
        max_reorder_delay_ms: int,
        mtu: int = 244,
    ) -> None:
        self._inbox = inbox
        self._outbox = outbox
        self._rng = rng
        self._drop_rate = drop_rate
        self._reorder_rate = reorder_rate
        self._max_reorder_delay_ms = max_reorder_delay_ms
        self._mtu = mtu
        self._closed = False
        self._pending_deliveries: list[asyncio.Task] = []

    @staticmethod
    def pair(
        drop_rate: float,
        reorder_rate: float,
        max_reorder_delay_ms: int,
        seed: int,
        mtu: int = 244,
    ) -> tuple["LossyReorderingTransport", "LossyReorderingTransport"]:
        a_to_b: "asyncio.Queue[bytes | None]" = asyncio.Queue()
        b_to_a: "asyncio.Queue[bytes | None]" = asyncio.Queue()
        # Distinct seeds per end so both directions get independent (but still reproducible) fault
        # patterns rather than accidentally-correlated ones.
        rng_a = random.Random(seed)
        rng_b = random.Random(seed + 1)
        a = LossyReorderingTransport(
            b_to_a, a_to_b, rng_a, drop_rate, reorder_rate, max_reorder_delay_ms, mtu
        )
        b = LossyReorderingTransport(
            a_to_b, b_to_a, rng_b, drop_rate, reorder_rate, max_reorder_delay_ms, mtu
        )
        return a, b

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        if self._closed:
            return
        # Gather every scheduled delayed-delivery task BEFORE flipping _closed and pushing the
        # sentinel -- otherwise a reordered frame that hasn't been delivered yet would look
        # indistinguishable from "closed" and be silently lost, rather than being a deliberate
        # drop-by-design.
        if self._pending_deliveries:
            await asyncio.gather(*self._pending_deliveries, return_exceptions=True)
        self._closed = True
        await self._outbox.put(None)

    async def send(self, frame: bytes) -> None:
        if self._closed:
            raise TransportClosed("lossy transport closed")
        packet_type = _packet_type_of(frame)
        if packet_type not in RELIABLE_PACKET_TYPES and self._rng.random() < self._drop_rate:
            return
        if self._rng.random() < self._reorder_rate:
            delay_s = self._rng.uniform(0, self._max_reorder_delay_ms) / 1000
            task = asyncio.create_task(self._delayed_put(frame, delay_s))
            self._pending_deliveries.append(task)
            task.add_done_callback(self._on_delivery_done)
        else:
            await self._outbox.put(frame)

    def _on_delivery_done(self, task: "asyncio.Task") -> None:
        if task in self._pending_deliveries:
            self._pending_deliveries.remove(task)

    async def _delayed_put(self, frame: bytes, delay_s: float) -> None:
        await asyncio.sleep(delay_s)
        await self._outbox.put(frame)

    async def frames(self) -> AsyncIterator[bytes]:
        while True:
            item = await self._inbox.get()
            if item is None:
                return
            yield item

    @property
    def mtu(self) -> int:
        return self._mtu


# ---------------------------------------------------------------------------------------------
# Small harness helpers
# ---------------------------------------------------------------------------------------------


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_until_connectable(host: str, port: int, timeout: float = 5.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last_exc: Exception | None = None
    while loop.time() < deadline:
        try:
            _reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError as exc:
            last_exc = exc
            await asyncio.sleep(0.05)
    raise TimeoutError(f"{host}:{port} not connectable within {timeout}s") from last_exc


async def _start_ultra96_stub(
    host: str, port: int, action: ActionClass, confidence: float = 0.95
) -> asyncio.base_events.Server:
    """Canned Ultra96 stub: answers every SENSOR_WINDOW with the same classified action, echoing
    request_id/player_id/timestamp -- mirrors mock/ultra96_sim.py's own echo pattern."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            line = await reader.readline()
            if not line:
                return
            parsed = parse_app_message(line.decode("utf-8").strip())
            if isinstance(parsed, MessageDropped) or not isinstance(parsed, SensorWindow):
                continue
            result = ActionResult(
                timestamp=parsed.timestamp,
                player_id=parsed.player_id,
                request_id=parsed.request_id,
                action=action,
                action_confidence=confidence,
                guard_state=GuardState.IDLE,
                guard_confidence=1.0,
            )
            writer.write((result.model_dump_json(by_alias=True) + "\n").encode("utf-8"))
            await writer.drain()

    return await asyncio.start_server(handle, host, port)


@dataclass
class _VisualizerProbe:
    """Stands in for a visualizer UI: sends periodic POSITION updates and records every broadcast
    the engine sends back, mirroring mock/visualizer_sim.py's own recv/send loop pattern."""

    url: str
    token: str
    player_id: int
    x: float
    y: float
    heading: float
    received: list = field(default_factory=list, init=False)
    _ws: "websockets.WebSocketClientProtocol | None" = field(default=None, init=False, repr=False)
    _recv_task: "asyncio.Task | None" = field(default=None, init=False, repr=False)
    _send_task: "asyncio.Task | None" = field(default=None, init=False, repr=False)

    async def start(self) -> None:
        self._ws = await websockets.connect(
            self.url, additional_headers={"Authorization": f"Bearer {self.token}"}
        )
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._send_task = asyncio.create_task(self._send_loop())

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        async for raw in self._ws:
            parsed = parse_app_message(raw)
            if isinstance(parsed, MessageDropped):
                continue
            self.received.append(parsed)

    async def _send_loop(self) -> None:
        assert self._ws is not None
        while True:
            position = Position(
                timestamp=monotonic_ms(),
                player_id=self.player_id,  # type: ignore[arg-type]
                x=self.x,
                y=self.y,
                heading=self.heading,
            )
            await self._ws.send(position.model_dump_json(by_alias=True))
            await asyncio.sleep(0.15)

    async def stop(self) -> None:
        for task in (self._send_task, self._recv_task):
            if task is not None:
                task.cancel()
        if self._ws is not None:
            await self._ws.close()


@dataclass
class _RelayHarness:
    """Wires Ultra96Client + PlayerWindower + EngineLinkClient + Session together for one player,
    mirroring relay/app.py's own run_relay() wiring (inlined here rather than calling run_relay()
    directly, since that function loops forever supervising reconnects and has no clean single-shot
    entry point for tests)."""

    player_id: int
    transport: ITransport
    engine_url: str
    ultra96_host: str
    ultra96_port: int
    bearer_token: str
    window_samples: int = 4
    stride_frames: int = 2

    _session: "Session | None" = field(default=None, init=False)
    _session_task: "asyncio.Task | None" = field(default=None, init=False)
    _ultra96: "Ultra96Client | None" = field(default=None, init=False)
    _engine_link: "EngineLinkClient | None" = field(default=None, init=False)
    _windower: "PlayerWindower | None" = field(default=None, init=False)

    async def start(self) -> None:
        self._ultra96 = Ultra96Client(
            host=self.ultra96_host, port=self.ultra96_port, request_timeout_ms=2000, max_in_flight=2
        )
        await self._ultra96.connect()

        self._windower = PlayerWindower(
            player_id=self.player_id,
            window_samples=self.window_samples,
            stride_frames=self.stride_frames,
            max_stale_pct=100.0,
            max_span_deviation_pct=1_000_000.0,
            frame_rate_hz=50.0,
        )

        async def on_combat_feedback(feedback) -> None:
            if self._session is not None:
                await self._session.send_combat_feedback(feedback)

        self._engine_link = EngineLinkClient(
            url=self.engine_url, bearer_token=self.bearer_token, on_combat_feedback=on_combat_feedback
        )
        await self._engine_link.connect()

        windower = self._windower
        engine_link = self._engine_link

        async def on_weapon_changed(msg: WeaponChanged) -> None:
            await engine_link.send_weapon_changed(msg)

        async def on_action_result(result: ActionResult) -> None:
            weapon = windower.current_weapon
            if weapon is None:
                return
            await engine_link.send_action(
                Action(
                    timestamp=result.timestamp,
                    player_id=result.player_id,
                    weapon=weapon,  # type: ignore[arg-type]
                    action=result.action,
                    action_confidence=result.action_confidence,
                    guard_state=result.guard_state,
                    guard_confidence=result.guard_confidence,
                )
            )

        session_config = SessionConfig(
            player_id=self.player_id,
            psk=PSK,
            handshake_samples=8,
            handshake_spacing_ms=20,
            ewma_alpha=0.1,
            outlier_rtt_multiplier=2.0,
            divergence_warn_ms=20.0,
            heartbeat_interval_ms=200,
            heartbeat_liveness_factor=3,
            reliability_base_timeout_ms=100,
            reliability_backoff_factor=2.0,
            reliability_max_attempts=5,
            reliability_jitter_pct=0.2,
            min_action_confidence=0.6,
        )
        self._session = Session(
            transport=self.transport,
            config=session_config,
            windower=self._windower,
            ultra96=self._ultra96,
            on_weapon_changed=on_weapon_changed,
            on_action_result=on_action_result,
        )
        self._session_task = asyncio.create_task(self._session.run())

    async def stop(self) -> None:
        if self._session is not None:
            self._session.stop()
        if self._session_task is not None:
            self._session_task.cancel()
            try:
                await self._session_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._engine_link is not None:
            await self._engine_link.disconnect()
        if self._ultra96 is not None:
            await self._ultra96.disconnect()


def _attack_scenario_steps(
    weapon: str, n_frames: int = 20, frame_delay_ms: int = 15
) -> list[ScenarioStep]:
    """One RFID_EVENT (confirms the weapon) followed by a burst of all-zero SENSOR_FRAMEs, enough
    to fill several SENSOR_WINDOWs at the harness's window_samples=4/stride_frames=2 config."""
    zero_sample = [0, 0, 0, 0, 0, 0]
    steps = [ScenarioStep(delay_ms=50, kind="rfid_event", payload={"weapon": weapon})]
    for _ in range(n_frames):
        steps.append(
            ScenarioStep(
                delay_ms=frame_delay_ms,
                kind="sensor_frame",
                payload={"dominant": zero_sample, "offhand": zero_sample},
            )
        )
    return steps


TransportPairFactory = Callable[[], tuple[ITransport, ITransport]]


async def _run_attack_scenario(
    transport_pair_factory: TransportPairFactory, event_timeout: float = 8.0
) -> CombatEvent:
    """Full-stack orchestration shared by the lossless and lossy tests: only player 1 gets a real
    relay/device/Ultra96 chain (attacker); player 2 (defender) is fed purely via a visualizer
    Position feed and never sends an Action, so its GuardState stays IDLE for the whole test --
    per COMMS_IMPLEMENTATION_PLAN.md §10.3's prose this makes the outcome deterministically HIT
    without this test needing to know resolver.py's heading/facing convention (resolver.py is
    intentionally not read for this task)."""
    engine_port = _free_tcp_port()
    ultra96_port = _free_tcp_port()

    engine_settings = EngineSettings(
        host="127.0.0.1",
        port=engine_port,
        use_tls=False,
        bearer_token=BEARER_TOKEN,
    )
    engine_task = asyncio.create_task(run_engine(engine_settings))
    ultra96_server = await _start_ultra96_stub(
        "127.0.0.1", ultra96_port, ActionClass.HORIZONTAL_SWING, confidence=0.95
    )

    relay_transport, device_transport = transport_pair_factory()
    harness = _RelayHarness(
        player_id=1,
        transport=relay_transport,
        engine_url=f"ws://127.0.0.1:{engine_port}/relay/1",
        ultra96_host="127.0.0.1",
        ultra96_port=ultra96_port,
        bearer_token=BEARER_TOKEN,
    )
    viz1 = _VisualizerProbe(
        url=f"ws://127.0.0.1:{engine_port}/visualizer/1",
        token=BEARER_TOKEN,
        player_id=1,
        x=0.0,
        y=0.0,
        heading=0.0,
    )
    viz2 = _VisualizerProbe(
        url=f"ws://127.0.0.1:{engine_port}/visualizer/2",
        token=BEARER_TOKEN,
        player_id=2,
        x=1.0,
        y=0.0,
        heading=0.0,
    )
    device_sim = FireBeetleSim(transport=device_transport, psk=PSK, player_id=1)
    scenario = _attack_scenario_steps(weapon="SWORD")
    device_task: "asyncio.Task | None" = None

    try:
        await _wait_until_connectable("127.0.0.1", engine_port)
        await harness.start()
        await viz1.start()
        await viz2.start()
        await asyncio.sleep(0.2)  # let both players' POSITION updates land before combat starts

        device_task = asyncio.create_task(device_sim.run(scenario))

        loop = asyncio.get_event_loop()
        deadline = loop.time() + event_timeout
        event: CombatEvent | None = None
        while loop.time() < deadline:
            for msg in viz2.received:
                if isinstance(msg, CombatEvent):
                    event = msg
                    break
            if event is not None:
                break
            await asyncio.sleep(0.1)

        assert event is not None, "no CombatEvent observed within timeout -- system hung or diverged"
        return event
    finally:
        if device_task is not None and not device_task.done():
            device_task.cancel()
        if device_task is not None:
            try:
                await device_task
            except (asyncio.CancelledError, Exception):
                pass
        await viz1.stop()
        await viz2.stop()
        await harness.stop()
        ultra96_server.close()
        await ultra96_server.wait_closed()
        if not engine_task.done():
            engine_task.cancel()
        try:
            await engine_task
        except (asyncio.CancelledError, Exception):
            pass


def _assert_expected_hit(event: CombatEvent) -> None:
    assert event.outcome == CombatOutcome.HIT
    assert event.weapon == Weapon.SWORD
    assert event.action == ActionClass.HORIZONTAL_SWING
    assert event.attacker_id == 1
    assert event.defender_id == 2
    assert event.damage == 10  # shared/config/weapons.yaml SWORD.HORIZONTAL_SWING.damage
    assert event.attacker_hp == 100
    assert event.defender_hp == 90  # starting_hp (100) - damage (10)


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------


async def test_baseline_lossless_duel_reaches_expected_hit_outcome() -> None:
    """Control: the same scenario as the lossy test below, over a plain in-process
    LoopbackTransport with no faults injected. Establishes the expected end-to-end outcome that
    the lossy variant must still reach."""
    event = await _run_attack_scenario(lambda: LoopbackTransport.pair(), event_timeout=8.0)
    _assert_expected_hit(event)


async def test_lossy_reordering_duel_still_reaches_expected_hit_outcome() -> None:
    """Same scenario as the baseline, but the FireBeetle<->relay link drops ~30% of best-effort
    packets (SENSOR_FRAME/HEARTBEAT/ACK) and reorders ~40% of all packets by up to 120ms. Verifies
    the system still converges to the correct end-to-end outcome rather than hanging, crashing, or
    silently diverging -- COMMS_IMPLEMENTATION_PLAN.md §13's Integration requirement."""
    event = await _run_attack_scenario(
        lambda: LossyReorderingTransport.pair(
            drop_rate=0.3, reorder_rate=0.4, max_reorder_delay_ms=120, seed=42
        ),
        event_timeout=12.0,
    )
    _assert_expected_hit(event)


async def test_handshake_and_timesync_complete_over_lossy_reordering_transport() -> None:
    """Standalone check (no engine/Ultra96/visualizer involved) that a full HELLO/HELLO_ACK +
    8-sample TIME_SYNC handshake burst completes over a lossy/reordering transport before any
    gameplay traffic starts. Every handshake packet type (HELLO, HELLO_ACK, TIME_SYNC_REQ,
    TIME_SYNC_RESP) is in RELIABLE_PACKET_TYPES and is therefore never dropped by
    LossyReorderingTransport -- only reordered/delayed -- so this specifically exercises
    reordering tolerance, not the (separately tested) drop/retry path."""
    relay_transport, device_transport = LossyReorderingTransport.pair(
        drop_rate=0.0, reorder_rate=0.5, max_reorder_delay_ms=150, seed=7
    )

    async def _noop_weapon_changed(_msg: WeaponChanged) -> None:
        return None

    async def _noop_action_result(_result: ActionResult) -> None:
        return None

    # window_samples deliberately huge: the scenario below never sends a SENSOR_FRAME, so no
    # window should ever complete regardless.
    windower = PlayerWindower(
        player_id=1,
        window_samples=10_000,
        stride_frames=1,
        max_stale_pct=100.0,
        max_span_deviation_pct=1_000_000.0,
        frame_rate_hz=50.0,
    )
    ultra96 = Ultra96Client(host="127.0.0.1", port=1, request_timeout_ms=100, max_in_flight=1)

    session_config = SessionConfig(
        player_id=1,
        psk=PSK,
        handshake_samples=8,
        handshake_spacing_ms=20,
        ewma_alpha=0.1,
        outlier_rtt_multiplier=2.0,
        divergence_warn_ms=20.0,
        heartbeat_interval_ms=200,
        heartbeat_liveness_factor=3,
        reliability_base_timeout_ms=100,
        reliability_backoff_factor=2.0,
        reliability_max_attempts=5,
        reliability_jitter_pct=0.2,
        min_action_confidence=0.6,
    )
    session = Session(
        transport=relay_transport,
        config=session_config,
        windower=windower,
        ultra96=ultra96,
        on_weapon_changed=_noop_weapon_changed,
        on_action_result=_noop_action_result,
    )
    session_task = asyncio.create_task(session.run())
    device_sim = FireBeetleSim(transport=device_transport, psk=PSK, player_id=1)
    device_task = asyncio.create_task(device_sim.run([]))  # empty scenario: handshake only

    try:
        # device_sim.run() disconnects its end once its (empty) scenario loop finishes, which
        # propagates a sentinel through the loopback-style queue pair and lets session.run()'s
        # read loop -- and therefore the whole session -- end cleanly on its own.
        await asyncio.wait_for(device_task, timeout=8.0)
        await asyncio.wait_for(session_task, timeout=8.0)
    finally:
        if not session_task.done():
            session.stop()
            session_task.cancel()
        if not device_task.done():
            device_task.cancel()
        for task in (session_task, device_task):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    assert session_task.done()
    assert not session_task.cancelled()
    assert session_task.exception() is None
    assert session._time_sync.offset_ms is not None
