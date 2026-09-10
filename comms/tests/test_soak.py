"""Soak tests: compressed/high-iteration-count stand-ins for a sustained mock match run
(COMMS_IMPLEMENTATION_PLAN.md §13 "Soak" bullet).

Every test in this module is marked `soak` (see module-level `pytestmark` below and the
`markers` entry added to comms/pyproject.toml) so it is excluded from the default fast test run
and must be selected explicitly, e.g. `pytest -m soak`.

SCOPE NOTE (read before trusting these as a substitute for a real 15-minute run): a literal
15-minute wall-clock soak is impractical to run routinely, so this module drives a
compressed-time / high-iteration-count stand-in instead. The windower-ring-buffer test below is
pure in-process computation with no I/O, so it runs the *actual* full 45,000-frame count (15
minutes at 50 Hz) since that is cheap. The two I/O-bound tests (Ultra96 concurrency, engine
inbox/memory) instead use a smaller but still high-iteration-count representative sample
(a few hundred to a few thousand iterations), since driving real socket I/O for a literal 15
minutes would make this suite impractical to run even occasionally.

RECOMMENDATION (not implemented here, flagged rather than silently assumed away): a separate,
literal real-time 15-minute soak variant should still exist for periodic/nightly CI, because a
compressed run cannot fully stand in for real-time-scale effects. Most notably,
relay/timesync.py's 30-second periodic EWMA resync cadence (`TimeSyncConfig.resync_interval_s` in
config.py) is not actually wired up anywhere in relay/session.py's streaming loop -- confirmed by
reading session.py, timesync.py and heartbeat.py in full: the offset is seeded once at handshake
and never updated again for the life of the session. That means no compressed *or* real-time test
can currently exercise genuine steady-state clock-drift correction, because the feature does not
exist yet; see `test_clock_offset_stays_within_divergence_tolerance_during_soak`'s docstring for
how that test is scoped as a regression/stability guard instead of a drift-tolerance validation.
"""

from __future__ import annotations

import asyncio
import gc
import socket
import statistics
import tracemalloc
from dataclasses import dataclass, field

import pytest

from cg_comms.clock import monotonic_ms
from cg_comms.engine.broadcaster import broadcast_to_visualizers, send_to_relay
from cg_comms.engine.game_state_task import GameStateTask
from cg_comms.engine.inbound import route_inbound
from cg_comms.engine.rules import Rules
from cg_comms.engine.ws_server import ConnectionRegistry, WsServerConfig, run_ws_server
from cg_comms.messages.enums import ActionClass, GuardState, Weapon
from cg_comms.messages.models import Action, ActionResult, SensorWindow, WeaponChanged
from cg_comms.messages.validate import MessageDropped, parse_app_message
from cg_comms.mock.firebeetle_sim import FireBeetleSim
from cg_comms.mock.scenario import ScenarioStep
from cg_comms.relay.engine_link import EngineLinkClient
from cg_comms.relay.session import Session, SessionConfig
from cg_comms.relay.ultra96 import Ultra96Client
from cg_comms.relay.windowing import PlayerWindower
from cg_comms.transport.loopback import LoopbackTransport

pytestmark = pytest.mark.soak

PSK = bytes(range(16))
BEARER_TOKEN = "soak-test-token"


# ---------------------------------------------------------------------------------------------
# Helpers (deliberately not shared with test_e2e_mock.py's copies -- only two files were asked
# for, so a small amount of helper duplication was accepted here rather than introducing a
# conftest.py neither file explicitly requires).
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


@dataclass
class _EngineHandles:
    task: GameStateTask
    registry: ConnectionRegistry
    server_task: "asyncio.Task"
    task_runner: "asyncio.Task"
    stop_event: asyncio.Event


async def _build_engine(host: str, port: int, bearer_token: str) -> _EngineHandles:
    """Inlines the same wiring engine/app.py's run_engine() does, but returns handles to
    task/registry/stop_event for introspection. run_engine() itself never returns until killed and
    exposes no handles, so it cannot be reused as-is by a soak test that needs to sample internal
    queue sizes and then shut down cleanly."""
    rules = Rules.load()
    registry = ConnectionRegistry(send_queue_depth=32)
    stop_event = asyncio.Event()

    async def on_combat_event(event) -> None:
        await broadcast_to_visualizers(registry, event)

    async def on_match_state(state) -> None:
        await broadcast_to_visualizers(registry, state)

    async def on_combat_feedback(player_id: int, feedback) -> None:
        await send_to_relay(registry, player_id, feedback)

    task = GameStateTask(
        rules=rules,
        reorder_delay_ms=rules.config.reorder_delay_ms,
        late_drop_ms=500,
        on_combat_event=on_combat_event,
        on_match_state=on_match_state,
        on_combat_feedback=on_combat_feedback,
    )

    async def on_message(path: str, raw: str) -> None:
        event = route_inbound(path, raw)
        if event is not None:
            await task.submit(event)

    ws_config = WsServerConfig(
        host=host, port=port, bearer_token=bearer_token, send_queue_depth=32, ssl_context=None
    )
    await task.start_match()
    server_task = asyncio.create_task(run_ws_server(ws_config, registry, on_message, stop_event))
    task_runner = asyncio.create_task(task.run(stop_event))
    return _EngineHandles(
        task=task, registry=registry, server_task=server_task, task_runner=task_runner, stop_event=stop_event
    )


async def _start_slow_ultra96_stub(host: str, port: int, delay_s: float) -> "asyncio.base_events.Server":
    """Deliberately slow canned Ultra96 stub -- the artificial per-request delay is what makes
    concurrent load actually pile up against Ultra96Client's max_in_flight semaphore instead of
    draining faster than requests arrive."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            line = await reader.readline()
            if not line:
                return
            parsed = parse_app_message(line.decode("utf-8").strip())
            if isinstance(parsed, MessageDropped) or not isinstance(parsed, SensorWindow):
                continue
            await asyncio.sleep(delay_s)
            result = ActionResult(
                timestamp=parsed.timestamp,
                player_id=parsed.player_id,
                request_id=parsed.request_id,
                action=ActionClass.IDLE,
                action_confidence=1.0,
                guard_state=GuardState.IDLE,
                guard_confidence=1.0,
            )
            writer.write((result.model_dump_json(by_alias=True) + "\n").encode("utf-8"))
            await writer.drain()

    return await asyncio.start_server(handle, host, port)


def _dummy_window(request_id: int) -> SensorWindow:
    sample = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    return SensorWindow(
        timestamp=0,
        player_id=1,
        request_id=request_id,
        weapon=Weapon.SWORD,
        window_start_ts=0,
        window_end_ts=0,
        sample_rate_hz=50.0,
        dominant=[sample],
        offhand=[sample],
        stale_pct=0.0,
    )


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------


async def test_windower_ring_buffers_stay_bounded_over_a_full_matchs_worth_of_frames() -> None:
    """Pure computation, no I/O: runs the literal full 15-minutes-at-50Hz frame count (45,000
    frames) since it is cheap enough to run at real scale rather than compressed. Spot-checks
    PlayerWindower's internal ring-buffer sizes at intervals and asserts they never exceed
    window_samples -- i.e. bounded, not monotonically growing (COMMS_IMPLEMENTATION_PLAN.md §13
    Soak bullet)."""
    window_samples = 40
    windower = PlayerWindower(
        player_id=1,
        window_samples=window_samples,
        stride_frames=5,
        max_stale_pct=20.0,
        max_span_deviation_pct=15.0,
        frame_rate_hz=50.0,
    )
    windower.on_weapon_confirmed(Weapon.SWORD)

    n_frames = 45_000  # 15 minutes at 50 Hz -- literal, not compressed (see module docstring)
    sample = (0, 0, 0, 0, 0, 0)
    dominant_sizes: list[int] = []
    offhand_sizes: list[int] = []
    windows_emitted = 0
    ts = 0
    for i in range(n_frames):
        ts += 20  # 50 Hz -> 20ms/frame, matching frame_rate_hz exactly (zero span deviation)
        window = windower.ingest_frame(engine_ts_ms=ts, flags=0, dominant_raw=sample, offhand_raw=sample)
        if window is not None:
            windows_emitted += 1
        if i % 500 == 0:
            dominant_sizes.append(len(windower._dominant))
            offhand_sizes.append(len(windower._offhand))
            await asyncio.sleep(0)  # yield periodically even though this loop is pure CPU work

    assert dominant_sizes and offhand_sizes
    assert max(dominant_sizes) <= window_samples
    assert max(offhand_sizes) <= window_samples
    assert len(windower._dominant) <= window_samples
    assert len(windower._offhand) <= window_samples
    assert windows_emitted > 0


async def test_ultra96_client_respects_max_in_flight_bound_under_concurrent_load() -> None:
    """Launches concurrent Ultra96Client.request() calls against a deliberately slow stub server,
    with a background monitor sampling len(client._pending) frequently, to genuinely stress the
    max_in_flight semaphore bound (a sequential loop would never let `_pending` grow past 1).
    Compressed representative sample (300 requests) standing in for a match's worth of Ultra96
    traffic -- see module docstring."""
    port = _free_tcp_port()
    server = await _start_slow_ultra96_stub("127.0.0.1", port, delay_s=0.02)
    max_in_flight = 2
    client = Ultra96Client(
        host="127.0.0.1", port=port, request_timeout_ms=5000, max_in_flight=max_in_flight
    )
    await client.connect()

    samples: list[int] = []
    stop = asyncio.Event()

    async def monitor() -> None:
        while not stop.is_set():
            samples.append(len(client._pending))
            await asyncio.sleep(0.002)

    monitor_task = asyncio.create_task(monitor())
    n_requests = 300
    try:
        results = await asyncio.gather(*(client.request(_dummy_window(i)) for i in range(n_requests)))
    finally:
        stop.set()
        await monitor_task
        await client.disconnect()
        server.close()
        await server.wait_closed()

    assert all(r is not None for r in results), "some requests timed out under bounded concurrency"
    assert samples, "no samples collected"
    assert max(samples) <= max_in_flight
    assert len(client._pending) == 0


async def test_engine_inbox_and_reorder_buffer_stay_bounded_and_memory_does_not_grow_unbounded() -> None:
    """Compressed representative soak (COMMS_IMPLEMENTATION_PLAN.md §13 Soak bullet): spams a few
    thousand Action messages at the engine over a real websocket connection (one relay's worth of
    EngineLinkClient), sampling GameStateTask._inbox.qsize(), len(task._reorder), and
    tracemalloc'd process memory at intervals. Confirms these stay bounded rather than growing
    without limit.

    KNOWN GAP (flagged, not silently worked around): GameStateTask._inbox is currently a plain
    unbounded `asyncio.Queue()` with no maxsize or DropOldestQueue wrapper (see
    engine/game_state_task.py's `_inbox` field), which does not match
    COMMS_IMPLEMENTATION_PLAN.md §6's specified 512-depth bounded+logged engine event queue. This
    test asserts the inbox stays small in practice -- because the engine's drain loop keeps up
    with this soak's send rate -- but that is a property of the drain loop being fast enough here,
    not a structural guarantee the queue itself enforces. A producer that outpaces the drain loop
    (not exercised by this compressed run) could still grow `_inbox` without bound today.
    """
    port = _free_tcp_port()
    handles = await _build_engine("127.0.0.1", port, BEARER_TOKEN)
    await _wait_until_connectable("127.0.0.1", port)

    async def on_combat_feedback(_feedback) -> None:
        return None

    engine_link = EngineLinkClient(
        url=f"ws://127.0.0.1:{port}/relay/1", bearer_token=BEARER_TOKEN, on_combat_feedback=on_combat_feedback
    )
    await engine_link.connect()

    n_actions = 4000
    inbox_samples: list[int] = []
    reorder_samples: list[int] = []
    mem_samples: list[float] = []

    # Every action sent is IDLE, deliberately: no RFID_EVENT/WeaponChanged is sent first, and no
    # Position is ever sent for either player, so this loop never triggers a real combat
    # resolution (which resolver.py's off-limits-to-read internals may assume has position/weapon
    # data available). The reorder buffer/GameStateTask._inbox drain pipeline is exercised
    # identically regardless of the action payload's content, so IDLE-only traffic is sufficient
    # for a queue/memory soak without needing to guess resolver.py's preconditions.
    tracemalloc.start()
    try:
        for i in range(n_actions):
            await engine_link.send_action(
                Action(
                    timestamp=monotonic_ms(),
                    player_id=1,
                    weapon=Weapon.SWORD,
                    action=ActionClass.IDLE,
                    action_confidence=0.9,
                    guard_state=GuardState.IDLE,
                    guard_confidence=0.9,
                )
            )
            if i % 200 == 0:
                await asyncio.sleep(0)  # let the engine's drain loop run
                gc.collect()
                current, _peak = tracemalloc.get_traced_memory()
                inbox_samples.append(handles.task._inbox.qsize())
                reorder_samples.append(len(handles.task._reorder))
                mem_samples.append(current)
    finally:
        tracemalloc.stop()
        await engine_link.disconnect()
        handles.stop_event.set()
        for t in (handles.server_task, handles.task_runner):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    assert inbox_samples and reorder_samples and mem_samples
    assert max(inbox_samples) < 500, "GameStateTask._inbox grew large -- see docstring re: unbounded queue gap"
    assert max(reorder_samples) < 500
    first_half = mem_samples[: len(mem_samples) // 2]
    second_half = mem_samples[len(mem_samples) // 2 :]
    assert statistics.mean(second_half) < statistics.mean(first_half) * 3, (
        "traced memory grew more than 3x between the first and second half of the soak run"
    )


async def test_clock_offset_stays_within_divergence_tolerance_during_soak() -> None:
    """Regression/stability guard, NOT a genuine drift-tolerance validation -- see this module's
    docstring: relay/session.py has no periodic clock resync task (TimeSyncConfig.resync_interval_s
    is declared in config.py but never consumed anywhere in session.py/timesync.py/heartbeat.py, as
    confirmed by reading all three in full). The offset is seeded once at handshake and then never
    updated for the rest of the session, so this test only confirms the seeded offset remains
    numerically stable across a sustained heartbeat-only run (as it structurally must, absent any
    resync) -- it does not, and currently cannot, confirm the system tolerates real device clock
    drift accumulating over a 15-minute match, because nothing in the implementation attempts to
    correct for that yet.
    """
    relay_transport, device_transport = LoopbackTransport.pair()

    async def _noop_weapon_changed(_msg: WeaponChanged) -> None:
        return None

    async def _noop_action_result(_result: ActionResult) -> None:
        return None

    # window_samples deliberately huge: the heartbeat-only scenario below never sends a
    # SENSOR_FRAME, so no window should ever complete regardless.
    windower = PlayerWindower(
        player_id=1,
        window_samples=10_000,
        stride_frames=1,
        max_stale_pct=100.0,
        max_span_deviation_pct=1_000_000.0,
        frame_rate_hz=50.0,
    )
    ultra96 = Ultra96Client(host="127.0.0.1", port=1, request_timeout_ms=100, max_in_flight=1)

    divergence_warn_ms = 20.0
    session_config = SessionConfig(
        player_id=1,
        psk=PSK,
        handshake_samples=8,
        handshake_spacing_ms=10,
        ewma_alpha=0.1,
        outlier_rtt_multiplier=2.0,
        divergence_warn_ms=divergence_warn_ms,
        heartbeat_interval_ms=50,
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

    heartbeat_steps = [ScenarioStep(delay_ms=20, kind="heartbeat", payload={}) for _ in range(500)]
    device_sim = FireBeetleSim(transport=device_transport, psk=PSK, player_id=1)
    device_task = asyncio.create_task(device_sim.run(heartbeat_steps))

    try:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 5.0
        while session._time_sync.offset_ms is None and loop.time() < deadline:
            await asyncio.sleep(0.02)
        assert session._time_sync.offset_ms is not None, "handshake never seeded a clock offset"

        baseline_offset = session._time_sync.offset_ms
        samples: list[float] = []
        for _ in range(20):
            await asyncio.sleep(0.02)
            samples.append(session._time_sync.offset_ms)

        assert samples
        for offset in samples:
            assert abs(offset - baseline_offset) <= divergence_warn_ms, (
                "seeded clock offset moved beyond divergence_warn_ms with no resync in flight"
            )
    finally:
        session.stop()
        session_task.cancel()
        device_task.cancel()
        for t in (session_task, device_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
