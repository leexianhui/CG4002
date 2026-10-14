"""Online presentation contract: real WS clients plus deterministic pause/snapshot checks."""

import asyncio
import json
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock

import pytest
import websockets
from test_pre_demo_recovery import free_port

from cg_comms.clock import monotonic_ms
from cg_comms.config import EngineSettings
from cg_comms.engine.app import run_engine
from cg_comms.engine.cooldowns import start_cooldown
from cg_comms.engine.game_state_task import GameStateTask, TimedInput
from cg_comms.engine.inbound import InboundAction, InboundVisualizerReady, route_inbound
from cg_comms.engine.presentation import Presentation
from cg_comms.engine.rules import Rules
from cg_comms.engine.state import MatchState, PlayerState, Vec2
from cg_comms.messages.enums import ActionClass, GuardState, MatchPhase, Weapon
from cg_comms.messages.models import Action
from cg_comms.messages.validate import MessageDropped, parse_app_message
from cg_comms.timer_resolution import high_resolution_timers
from cg_comms.tools.integration_mock import run_stack


def positioned_state(now):
    return MatchState(players={pid: PlayerState(
        player_id=pid, weapon=Weapon.SWORD, position=Vec2(pid - 1, 0),
        position_ts=now, heading_deg=0 if pid == 1 else 180,
    ) for pid in (1, 2)})


def test_pause_preserves_cooldowns_and_resume_rejects_earlier_actions():
    rules = Rules.load()
    state = positioned_state(1000)
    presentation = Presentation(require_ready=True, ready={1: True, 2: True})
    presence = {"1": True, "2": True}
    assert presentation.update(state, rules, presence, 1000)
    start_cooldown(state.players[1], ActionClass.HORIZONTAL_SWING, 1000, 800)
    state.players[2].guard_state = GuardState.GUARDING
    presentation.ready[2] = False
    assert presentation.update(state, rules, presence, 1100)
    snapshot = presentation.snapshot(state, rules, presence, 3000)
    assert snapshot.phase == MatchPhase.PAUSED
    assert snapshot.players[0].cooldowns[0].remaining_ms == 700
    assert snapshot.players[1].guard_state == GuardState.IDLE
    assert snapshot.players[0].position is None  # stale geometry isn't advertised as valid
    for p in state.players.values():
        p.position_ts = 3000
    presentation.ready[2] = True
    assert presentation.update(state, rules, presence, 3000)
    assert state.players[1].cooldowns[ActionClass.HORIZONTAL_SWING] == 3700
    assert presentation.resume_floor == 3000


@pytest.mark.parametrize("phase", [MatchPhase.PAUSED, MatchPhase.IN_PROGRESS])
async def test_old_queued_guard_and_attack_cannot_cross_online_resume(phase):
    callback = AsyncMock()
    task = GameStateTask(Rules.load(), 0, 10000, callback, callback, callback, callback,
                         lambda: {"1": True, "2": True}, require_visualizer_ready=True)
    now = monotonic_ms()
    task.state = positioned_state(now)
    task.state.phase = phase
    task.presentation.resume_floor = now
    action = Action(timestamp=now - 500, player_id=1, weapon="SWORD",
                    action="HORIZONTAL_SWING", action_confidence=1,
                    guard_state="GUARDING", guard_confidence=1)
    assert task._reorder.push(now - 500, TimedInput(now - 500, InboundAction(1, action)), now) is None
    await task._drain_ready()
    assert task.state.players[1].guard_state == GuardState.IDLE
    assert task.state.players[2].hp == 100
    callback.assert_not_awaited()


@pytest.mark.parametrize("path,pid,accepted", [
    ("/visualizer/1", 1, True), ("/visualizer/1", 2, False), ("/relay/1", 1, False),
])
def test_readiness_is_bound_to_visualizer_identity(path, pid, accepted):
    message = json.dumps(dict(type="VISUALIZER_READY", version=1, timestamp=0,
                              playerId=pid, ready=True))
    assert isinstance(route_inbound(path, message), InboundVisualizerReady) == accepted


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_positions_are_rejected(value):
    raw = json.dumps(dict(type="POSITION", version=1, timestamp=0, playerId=1,
                          x=value, y=0, heading=0))
    assert isinstance(parse_app_message(raw), MessageDropped)


async def receive_until(ws, predicate, timeout=4):
    async def read():
        while True:
            message = json.loads(await ws.recv())
            if predicate(message):
                return message
    return await asyncio.wait_for(read(), timeout)


async def test_two_phone_combat_guard_release_reconnect_and_operator_reset():
    port = free_port()
    token = "online-test-only"
    config = EngineSettings(_env_file=None, host="127.0.0.1", port=port, use_tls=False,
                            bearer_token=token, require_visualizer_ready=True,
                            publish_state_snapshots=True)
    engine = asyncio.create_task(run_engine(config))

    async def connect(path):
        for _ in range(100):
            try:
                return await websockets.connect(f"ws://127.0.0.1:{port}{path}",
                    additional_headers={"Authorization": f"Bearer {token}"})
            except OSError:
                await asyncio.sleep(.01)
        raise AssertionError("engine did not start")

    async def send(ws, kind, pid, **fields):
        await ws.send(json.dumps(dict(type=kind, version=1, timestamp=monotonic_ms(),
                                     playerId=pid, **fields)))

    def snapshot(phase=None):
        return lambda m: m["type"] == "STATE_SNAPSHOT" and (phase is None or m["phase"] == phase)

    def p2guard(value):
        return lambda m: snapshot()(m) and m["players"][1]["guardState"] == value

    connections = []
    try:
        async with AsyncExitStack():
            r1, r2, v1, v2 = [await connect(path) for path in
                              ("/relay/1", "/relay/2", "/visualizer/1", "/visualizer/2")]
            connections.extend((r1, r2, v1, v2))
            assert (await receive_until(v1, snapshot()))["phase"] == "WAITING"
            for pid, relay, phone in ((1, r1, v1), (2, r2, v2)):
                await send(relay, "WEAPON_CHANGED", pid, weapon="SWORD")
                await send(phone, "VISUALIZER_READY", pid, ready=True)
                await send(phone, "POSITION", pid, x=pid - 1, y=0, heading=0 if pid == 1 else 180)
            await receive_until(v1, snapshot("IN_PROGRESS"))
            action_fields = dict(weapon="SWORD", action="IDLE", actionConfidence=1,
                                 guardState="GUARDING", guardConfidence=1)
            await send(r2, "ACTION", 2, **action_fields)
            await receive_until(v1, p2guard("GUARDING"))
            await send(r2, "ACTION", 2, **dict(action_fields, guardState="IDLE"))
            await receive_until(v1, p2guard("IDLE"))
            await send(r1, "ACTION", 1,
                       **dict(action_fields, action="HORIZONTAL_SWING", guardState="IDLE"))
            combat = await receive_until(v1, lambda m: m["type"] == "COMBAT_EVENT")
            assert combat["defenderHp"] == 90 and combat["outcome"] == "HIT"
            state = await receive_until(v1, lambda m: snapshot()(m) and m["players"][1]["hp"] == 90)
            assert state["players"][0]["cooldownAction"] == "HORIZONTAL_SWING"
            assert state["players"][0]["cooldowns"][0]["totalMs"] == 800
            await v1.close()
            await receive_until(v2, snapshot("PAUSED"))
            replacement = await connect("/visualizer/1")
            connections.append(replacement)
            recovered = await receive_until(replacement, snapshot())
            assert recovered["players"][1]["hp"] == 90
            assert recovered["players"][0]["weapon"] == "SWORD"
            assert not recovered["players"][0]["ready"]
            for pid, phone in ((1, replacement), (2, v2)):
                await send(phone, "POSITION", pid, x=pid - 1, y=0, heading=0 if pid == 1 else 180)
                await send(phone, "VISUALIZER_READY", pid, ready=True)
            await receive_until(replacement, snapshot("IN_PROGRESS"))
            # A phone's old START_MATCH_REQUEST is not an engine reset API.
            await send(replacement, "START_MATCH_REQUEST", 1)
            same = await receive_until(replacement, snapshot())
            assert same["players"][1]["hp"] == 90
            control = await connect("/control")
            connections.append(control)
            await control.send('{"type":"RESET_MATCH","version":1}')
            reset = await receive_until(control, lambda m: m.get("reason") == "reset")
            assert reset["hp"] == {"1": 100, "2": 100}
            restored = await receive_until(replacement,
                lambda m: snapshot()(m) and all(p["hp"] == 100 for p in m["players"]))
            assert all(p["weapon"] == "SWORD" and not p["cooldowns"] for p in restored["players"])
    finally:
        for ws in connections:
            await ws.close()
        engine.cancel()
        await asyncio.gather(engine, return_exceptions=True)


@pytest.mark.parametrize("combat_enabled", [True, False])
async def test_mock_launcher_runs_binary_sessions_classifier_and_two_phone_combat(combat_enabled):
    port = free_port()
    config = EngineSettings(_env_file=None, host="127.0.0.1", port=port,
                            use_tls=False, bearer_token="mock-test-token")
    phones = []
    with high_resolution_timers():
        stack = asyncio.create_task(run_stack(config, "127.0.0.1", combat_enabled=combat_enabled))

        async def positions():
            while True:
                for pid, ws in enumerate(phones, 1):
                    await ws.send(json.dumps(dict(type="POSITION", version=1,
                        timestamp=monotonic_ms(), playerId=pid,
                        x=pid - 1, y=0, heading=0 if pid == 1 else 180)))
                await asyncio.sleep(.1)

        position_task = None
        try:
            for pid in (1, 2):
                for _ in range(100):
                    if stack.done():
                        stack.result()
                    try:
                        ws = await websockets.connect(
                            f"ws://127.0.0.1:{port}/visualizer/{pid}",
                            additional_headers={"Authorization": "Bearer mock-test-token"})
                        phones.append(ws)
                        break
                    except OSError:
                        await asyncio.sleep(.02)
                assert len(phones) == pid
                await ws.send(json.dumps(dict(type="VISUALIZER_READY", version=1,
                    timestamp=monotonic_ms(), playerId=pid, ready=True)))
            position_task = asyncio.create_task(positions())
            if combat_enabled:
                combat = await receive_until(phones[0], lambda m: m["type"] == "COMBAT_EVENT", 10)
                assert combat["outcome"] == "HIT" and combat["damage"] == 10
            else:
                await receive_until(phones[0], lambda m: m["type"] == "STATE_SNAPSHOT"
                                    and m["phase"] == "IN_PROGRESS", 10)
                finish = monotonic_ms() + 3500

                def idle_snapshot(message):
                    assert message["type"] != "COMBAT_EVENT"
                    if message["type"] != "STATE_SNAPSHOT":
                        return False
                    assert all(p["hp"] == 100 for p in message["players"])
                    assert message["phase"] == "IN_PROGRESS"
                    return monotonic_ms() >= finish

                await receive_until(phones[0], idle_snapshot, 8)
            assert not stack.done()
        finally:
            if position_task:
                position_task.cancel()
                await asyncio.gather(position_task, return_exceptions=True)
            for ws in phones:
                await ws.close()
            stack.cancel()
            await asyncio.gather(stack, return_exceptions=True)
