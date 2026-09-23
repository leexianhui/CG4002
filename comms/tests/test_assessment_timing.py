"""Hardware-follow-up regressions: censored classifier replies and paired engine hold."""

import asyncio

import pytest
import structlog
from test_pre_demo_combat import action, setup_task
from test_pre_demo_recovery import window

from cg_comms.assessment_timing import relay_settings_for_profile, rules_for_profile
from cg_comms.config import RelaySettings, RulesConfig
from cg_comms.messages.models import ActionResult, SensorWindow
from cg_comms.relay.ultra96 import Ultra96Client


def test_profile_is_explicit_and_does_not_mutate_gameplay_settings():
    configured = RelaySettings(_env_file=None)
    assert relay_settings_for_profile(configured) is configured
    assert configured.ultra96.request_timeout_ms == 120
    rules = RulesConfig.load()
    assert rules_for_profile(rules, "configured") is rules
    assessed = configured.model_copy(
        update={
            "latency_profile": "assessment",
            "assessment": configured.assessment.model_copy(update={"enabled": True}),
        }
    )
    assert relay_settings_for_profile(assessed).ultra96.request_timeout_ms == 500
    assert assessed.ultra96.request_timeout_ms == 120
    profile_rules = rules_for_profile(rules, "assessment")
    assert profile_rules.reorder_delay_ms == 600
    assert rules.reorder_delay_ms == 60
    assert profile_rules.guard_grace_ms == rules.guard_grace_ms == 50
    assert profile_rules.model_dump(exclude={"reorder_delay_ms"}) == rules.model_dump(
        exclude={"reorder_delay_ms"}
    )
    with pytest.raises(ValueError, match="requires --assessment"):
        relay_settings_for_profile(configured.model_copy(update={"latency_profile": "assessment"}))


@pytest.mark.parametrize(
    "profile,expected,dropped",
    [
        ("configured", "HIT", 1),
        ("assessment", "PARRY", 0),
    ],
)
async def test_450ms_delayed_guard_requires_paired_engine_profile(
    monkeypatch, profile, expected, dropped
):
    task, clock, _, combat = setup_task(monkeypatch)
    config = rules_for_profile(task.rules.config, profile)
    task.reorder_delay_ms = config.reorder_delay_ms
    task._reorder.reorder_delay_ms = config.reorder_delay_ms + config.guard_grace_ms
    clock[0] = 1070
    task._absorb(action(1, 1050, move="HORIZONTAL_SWING"))
    clock[0] = 1300
    await task._drain_ready()
    clock[0] = 1450
    task._absorb(action(2, 1000, guard="GUARDING"))
    clock[0] = 1800
    await task._drain_ready()
    assert combat[0].outcome.value == expected
    assert task._reorder.dropped_late == dropped


@pytest.mark.parametrize(
    "profile,delay,accepted",
    [
        ("configured", 0.25, False),
        ("assessment", 0.25, True),
        ("assessment", 0.65, False),
    ],
)
async def test_real_tcp_reply_deadline_and_trace_diagnostics(profile, delay, accepted):
    tasks = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            request = SensorWindow.model_validate_json(await reader.readline())
            await asyncio.sleep(delay)
            response = ActionResult(
                timestamp=request.timestamp,
                player_id=request.player_id,
                request_id=request.request_id,
                demo_trace_id=request.demo_trace_id,
                action="THRUST",
                action_confidence=0.9,
                guard_state="IDLE",
                guard_confidence=1,
            )
            writer.write((response.model_dump_json(by_alias=True) + "\n").encode())
            await writer.drain()
            await reader.read()
        except OSError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            tasks.discard(task)

    settings = relay_settings_for_profile(
        RelaySettings(_env_file=None, assessment={"enabled": True}, latency_profile=profile)
    )
    async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
        client = Ultra96Client(
            "127.0.0.1", server.sockets[0].getsockname()[1], settings.ultra96.request_timeout_ms, 1
        )
        try:
            await client.connect()
            with structlog.testing.capture_logs() as logs:
                result = await client.request(window().model_copy(update={"demo_trace_id": "P1-tail"}))
            assert (result is not None) == accepted
            event = next(
                e
                for e in logs
                if e["event"] == ("classifier_response" if accepted else "ultra96_timeout")
            )
            assert event["demo_trace_id"] == "P1-tail"
            assert event["timeout_ms"] == settings.ultra96.request_timeout_ms
            assert not client._pending  # expired replies cannot fulfill another request
        finally:
            await client.disconnect()
            for task in list(tasks):
                task.cancel()
            await asyncio.gather(*list(tasks), return_exceptions=True)
