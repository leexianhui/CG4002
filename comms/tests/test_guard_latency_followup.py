"""Controlled avatar geometry, real JSON diagnostics, and classifier tail measurements."""

import asyncio
import json

import pytest
import structlog
from test_pre_demo_combat import action, position, setup_task
from test_pre_demo_recovery import window

from cg_comms.config import RelaySettings
from cg_comms.engine import game_state_task
from cg_comms.engine.coordinates import unity_heading_to_engine
from cg_comms.engine.inbound import InboundWeaponChanged
from cg_comms.engine.rules import Rules
from cg_comms.messages.models import ActionResult, SensorWindow, WeaponChanged
from cg_comms.mock.ultra96_sim import _handle_client, load_trigger_map
from cg_comms.relay.ultra96 import Ultra96Client
from cg_comms.tools.classifier_probe import probe, summarize


def json_log_sink(monkeypatch):
    sink = structlog.testing.CapturingLogger()
    # Include the production timestamp processor: capture_logs alone would hide key collisions.
    logger = structlog.wrap_logger(sink, processors=[structlog.processors.TimeStamper(fmt="iso"),
                                                   structlog.processors.JSONRenderer()])
    monkeypatch.setattr(game_state_task, "logger", logger)
    return sink


@pytest.mark.parametrize("attacker", [1, 2])
@pytest.mark.parametrize("yaws,guard_ts,expected", [
    ((0, 0), 700, "HIT"),
    ((90, 270), 700, "BLOCK"),
    ((90, 270), 950, "PARRY"),
    ((90, 270), 1050, "BLOCK"),
    ((90, 270), 1051, "HIT"),
])
async def test_unity_geometry_through_engine_state_and_guard(monkeypatch, attacker, yaws,
                                                           guard_ts, expected):
    task, clock, recognized, combat = setup_task(monkeypatch)
    task.rules = Rules.load()
    sink = json_log_sink(monkeypatch)
    for pid in (1, 2):
        task._absorb(position(pid, 750, pid - 1, unity_heading_to_engine(yaws[pid - 1])))
    # Deliberately deliver attack before guard to exercise actual event ordering/lookahead.
    task._absorb(action(attacker, 1000, move="HORIZONTAL_SWING"))
    task._absorb(action(3 - attacker, guard_ts, guard="GUARDING"))
    clock[0] = 1400
    await task._drain_ready()
    assert combat[0].outcome.value == expected
    assert combat[0].damage == {"HIT": 10, "BLOCK": 3, "PARRY": 0}[expected]
    assert any(e.guard_state.value == "GUARDING" for e in recognized)
    entry = next(json.loads(c.args[0]) for c in sink.calls
                 if json.loads(c.args[0])["event"] == "combat_resolved")
    assert entry["engine_timestamp_ms"] == 1000
    assert isinstance(entry["timestamp"], str)
    assert entry["facing_attacker"] == (yaws == (90, 270))
    assert entry["facing_error_deg"] == (0 if yaws == (90, 270) else 90)


@pytest.mark.parametrize("message_type", ["ACTION", "POSITION", "WEAPON_CHANGED"])
@pytest.mark.parametrize("reason,normalized", [
    ("before_release_watermark", 9700), ("max_age_exceeded", 9000),
    ("future_timestamp", 12001), ("buffer_full", 10000), ("before_match_reset", 10000),
])
async def test_rejection_json_has_type_reason_and_both_clocks(monkeypatch, message_type,
                                                            reason, normalized):
    task, clock, _, _ = setup_task(monkeypatch)
    clock[0] = 10000
    task._clock_anchors = {(pid, channel): 2000 for pid in (1, 2)
                           for channel in ("action", "position")}
    # Discard setup inputs to isolate this rejection branch.
    task._reorder.drain_ready(2000)
    if reason == "before_release_watermark":
        task._reorder.push(9800, None, 9800)
        task._reorder.drain_ready(10000)
    elif reason == "buffer_full":
        task._reorder.max_events = 1
        task._reorder.push(9950, None, 10000)
    elif reason == "before_match_reset":
        task._reset_floor = 10001
    source = normalized - 2000
    event = {"ACTION": action(1, source), "POSITION": position(1, source, 0, 0),
             "WEAPON_CHANGED": InboundWeaponChanged(
                 1, WeaponChanged(timestamp=source, player_id=1, weapon="SWORD"))}[message_type]
    sink = json_log_sink(monkeypatch)
    task._absorb(event)
    entry = json.loads(sink.calls[-1].args[0])
    assert entry["player_id"] == 1
    assert entry["message_type"] == message_type
    assert entry["reason"] == reason
    assert entry["source_timestamp"] == source
    assert entry["engine_timestamp_ms"] == normalized
    assert entry["event_age_ms"] == 10000 - normalized
    assert isinstance(entry["timestamp"], str)


@pytest.mark.parametrize("reorder_ms,expected,dropped", [(60, "HIT", 1), (250, "PARRY", 0)])
async def test_longer_classifier_deadline_alone_cannot_recover_late_guard(
        monkeypatch, reorder_ms, expected, dropped):
    task, clock, _, combat = setup_task(monkeypatch)
    task.reorder_delay_ms = reorder_ms
    task._reorder.reorder_delay_ms = reorder_ms + 50
    clock[0] = 1070
    task._absorb(action(1, 1050, move="HORIZONTAL_SWING"))
    clock[0] = 1180
    await task._drain_ready()
    clock[0] = 1200  # guard classification delayed 200 ms after its event time
    task._absorb(action(2, 1000, guard="GUARDING"))
    clock[0] = 1400
    await task._drain_ready()
    assert combat[0].outcome.value == expected
    assert task._reorder.dropped_late == dropped


async def test_classifier_probe_measures_tail_without_changing_live_deadline():
    handlers = set()

    async def delayed(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            while line := await reader.readline():
                msg = SensorWindow.model_validate_json(line)
                await asyncio.sleep(.18)
                result = ActionResult(timestamp=msg.timestamp, player_id=msg.player_id,
                                      request_id=msg.request_id, action="IDLE", action_confidence=1,
                                      guard_state="GUARDING", guard_confidence=1)
                writer.write((result.model_dump_json(by_alias=True) + "\n").encode())
                await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            handlers.discard(asyncio.current_task())

    async with await asyncio.start_server(delayed, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        settings = RelaySettings(_env_file=None, player_id=2,
                                 ultra96={"host": "127.0.0.1", "port": port,
                                          "request_timeout_ms": 120})
        live = Ultra96Client("127.0.0.1", port, 120, 1)
        try:
            with structlog.testing.capture_logs() as logs:
                await live.connect()
                assert await live.request(window(2)) is None
                await live.disconnect()
                result = await probe(settings, samples=2, timeout_ms=500, interval_ms=0)
            assert result["completed"] == 2 and result["timeouts"] == 0
            assert result["request_call_ms"]["p50"] >= 170
            assert not result["forwarded_to_engine"]
            assert settings.ultra96.request_timeout_ms == 120
            timeout = next(d for d in logs if d["event"] == "ultra96_timeout")
            assert timeout["player_id"] == 2 and timeout["phase"] == "response_wait"
            assert timeout["timeout_ms"] == 120 and timeout["elapsed_ms"] >= 120
            responses = [d for d in logs if d["event"] == "classifier_response"]
            assert len(responses) == 2
            assert all(d["response_wait_ms"] >= 170 and d["request_bytes"] > 1000
                       and d["encode_ms"] >= 0 and d["write_drain_ms"] >= 0 for d in responses)
        finally:
            await live.disconnect()
            if handlers:
                await asyncio.gather(*list(handlers))


async def test_simulator_reports_its_own_processing_and_write_time():
    from pathlib import Path

    trigger_map = load_trigger_map(Path("configs/scenarios/demo_trigger_map.yaml"))
    client_done = asyncio.Event()

    async def serve(reader, writer):
        try:
            await _handle_client(reader, writer, [], trigger_map)
        finally:
            client_done.set()

    with structlog.testing.capture_logs() as logs:
        async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
            client = Ultra96Client("127.0.0.1", server.sockets[0].getsockname()[1], 500, 1)
            try:
                await client.connect()
                msg = window().model_copy(update={"trigger_id": 3})
                result = await client.request(msg)
                assert result.guard_state.value == "GUARDING"
            finally:
                await client.disconnect()
                await asyncio.wait_for(client_done.wait(), 2)
    entry = next(d for d in logs if d["event"] == "ultra96_sim_classified")
    assert entry["window_timestamp_ms"] == msg.timestamp
    assert entry["processing_ms"] >= 0 and entry["write_drain_ms"] >= 0
    assert entry["service_ms"] >= entry["processing_ms"]


def test_probe_percentiles_do_not_hide_missing_samples():
    assert summarize([]) == {}
    assert summarize([120, 20, 180, 10]) == {"p50": 20, "p95": 180, "p99": 180, "max": 180}
