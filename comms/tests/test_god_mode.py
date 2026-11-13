"""God mode (SET_GOD_MODE on /control, a phone's GOD MODE button): hits still resolve and report their
damage, but nobody's HP drops, so nobody can lose. It stays on through match resets until turned off.
"""

import asyncio
import json

import websockets
from test_hardware_guard import attack_at
from test_pre_demo_combat import setup_task
from test_pre_demo_recovery import free_port

from cg_comms.engine.inbound import SetGodMode
from cg_comms.engine.rules import Rules
from cg_comms.messages.enums import CombatOutcome, MatchPhase


async def hits(monkeypatch, count, god_mode):
    """`count` sword hits by P1 on P2, 3 s apart (past the cooldown), with god mode set as given."""
    task, clock, _, combat = setup_task(monkeypatch)
    task.rules = Rules.load()
    task.god_mode = god_mode
    for n in range(count):
        ts = 2000 + n * 3000
        for event in attack_at(ts):
            message = getattr(event, "action", None) or getattr(event, "position", None)
            clock[0] = max(clock[0], message.timestamp)
            task._absorb(event)
            await task._drain_ready()
        clock[0] = ts + 400
        await task._drain_ready()
    return task, combat


async def test_hits_report_damage_but_hp_never_drops(monkeypatch):
    task, combat = await hits(monkeypatch, 12, god_mode=True)  # 12 x 10 damage > 100 HP
    assert len(combat) == 12
    assert all(c.outcome is CombatOutcome.HIT and c.damage == 10 for c in combat)
    assert all(c.defender_hp == 100 for c in combat)
    assert task.state.players[2].hp == 100
    assert task.state.phase is not MatchPhase.ENDED


async def test_without_god_mode_the_same_hits_win_the_match(monkeypatch):
    task, combat = await hits(monkeypatch, 10, god_mode=False)
    assert combat[-1].defender_hp == 0
    assert task.state.phase is MatchPhase.ENDED and task.state.winner_id == 1


async def test_snapshot_carries_the_flag_and_it_survives_a_reset(monkeypatch):
    task, _ = await hits(monkeypatch, 0, god_mode=False)
    task.on_match_state = lambda state: asyncio.sleep(0)
    stop = asyncio.Event()
    runner = asyncio.create_task(task.run(stop, drain_interval_ms=5))
    try:
        await task.submit(SetGodMode(True))
        await asyncio.sleep(0.05)
        assert task.current_state_snapshot().god_mode is True
        assert task.current_state_snapshot().model_dump(by_alias=True)["godMode"] is True
        await task.reset_match()
        assert task.god_mode is True, "god mode is not part of the match"
        await task.submit(SetGodMode(False))
        await asyncio.sleep(0.05)
        assert task.current_state_snapshot().god_mode is False
    finally:
        stop.set()
        await runner


async def test_control_command_reaches_both_phones_and_malformed_ones_are_rejected():
    from cg_comms.config import EngineSettings
    from cg_comms.engine.app import run_engine

    port = free_port()
    settings = EngineSettings(host="127.0.0.1", port=port, use_tls=False, bearer_token="test",
                              publish_state_snapshots=True)
    engine = asyncio.create_task(run_engine(settings))
    connections = []

    async def connect(path):
        async def attempt():
            while True:
                try:
                    return await websockets.connect(f"ws://127.0.0.1:{port}{path}",
                        additional_headers={"Authorization": "Bearer test"})
                except OSError:
                    await asyncio.sleep(.01)
        ws = await asyncio.wait_for(attempt(), 2)
        connections.append(ws)
        return ws

    async def god_mode_snapshot(ws, enabled):
        while True:
            data = json.loads(await asyncio.wait_for(ws.recv(), 2))
            if data["type"] == "STATE_SNAPSHOT" and data["godMode"] is enabled:
                return data

    try:
        p1 = await connect("/visualizer/1")
        p2 = await connect("/visualizer/2")
        control = await connect("/control")
        # Not a control channel, and malformed commands: all ignored.
        await p1.send('{"type":"SET_GOD_MODE","version":1,"enabled":true}')
        await control.send('{"type":"SET_GOD_MODE","version":1,"enabled":"yes"}')
        await control.send('{"type":"SET_GOD_MODE","version":1}')
        await asyncio.sleep(0.2)
        await god_mode_snapshot(p1, False)
        await control.send('{"type":"SET_GOD_MODE","version":1,"enabled":true}')
        await asyncio.gather(god_mode_snapshot(p1, True), god_mode_snapshot(p2, True))
        await control.send('{"type":"SET_GOD_MODE","version":1,"enabled":false}')
        await asyncio.gather(god_mode_snapshot(p1, False), god_mode_snapshot(p2, False))
    finally:
        for ws in connections:
            await ws.close()
        engine.cancel()
        await asyncio.gather(engine, return_exceptions=True)
