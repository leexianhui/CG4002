"""Tutorial (SET_TUTORIAL on /control, a phone's TUTORIAL button): one player practises alone with
the real pipeline, and the engine plays the other player as a dummy. The dummy stands at the arena
origin holding the player's weapon, never loses HP and never attacks; in free practice it raises its
guard now and then. Only the tutorial player has to be ready. Ending it (or a reset) gives the other
player back their real weapon and starts a fresh round.
"""

import asyncio
import json

import websockets
from test_pre_demo_combat import action, position
from test_pre_demo_recovery import free_port

from cg_comms.engine.game_state_task import GameStateTask
from cg_comms.engine.inbound import InboundWeaponChanged
from cg_comms.engine.rules import Rules
from cg_comms.engine.state import Vec2
from cg_comms.messages.enums import CombatOutcome, GuardState, MatchPhase, Weapon
from cg_comms.messages.models import WeaponChanged


def tutorial_task(monkeypatch):
    """An online engine (readiness required) where only player 1's relay is connected and only
    player 1's phone is ready and armed (spear)."""
    clock = [1000]
    monkeypatch.setattr("cg_comms.engine.game_state_task.monotonic_ms", lambda: clock[0])
    combat = []

    async def resolved(event):
        combat.append(event)

    async def noop(*_):
        return None

    task = GameStateTask(Rules.load(), 0, 10000, resolved, noop, noop, noop,
                         lambda: {"1": True}, require_visualizer_ready=True)
    task._clock_anchors = {(pid, ch): 0 for pid in (1, 2) for ch in ("action", "position")}
    task.presentation.ready[1] = True
    task.state.players[1].weapon = Weapon.SPEAR
    return task, clock, combat


def timestamp(event):
    return (getattr(event, "action", None) or getattr(event, "position", None)
            or getattr(event, "weapon_changed", None)).timestamp


async def run(task, clock, until, *events):
    """Runs the engine like its real loop, a tick every 20 ms up to `until`, with player 1's phone
    sending a fresh position every 100 ms (1.5 m from the dummy) and each event arriving at its
    own timestamp (the engine refuses future-stamped events)."""
    pending = sorted(events, key=timestamp)
    while clock[0] < until:
        clock[0] += 20
        if clock[0] % 100 == 0:
            task._absorb(position(1, clock[0], 1.5, 180))
        while pending and timestamp(pending[0]) <= clock[0]:
            task._absorb(pending.pop(0))
        await task._update_presentation()
        await task._drain_ready()
        await task._update_presentation()


async def started(monkeypatch, dummy_guards=False):
    task, clock, combat = tutorial_task(monkeypatch)
    await task.set_tutorial(1, dummy_guards)
    await run(task, clock, 1500)
    return task, clock, combat


def swing(ts, attacker=1):
    return action(attacker, ts, weapon="SPEAR", move="HORIZONTAL_SWING")


async def test_one_ready_player_starts_the_match_against_a_dummy(monkeypatch):
    task, clock, _ = tutorial_task(monkeypatch)
    await run(task, clock, 1500)
    assert task.state.phase is MatchPhase.WAITING, "a normal match waits for player 2"

    await task.set_tutorial(1, False)
    await run(task, clock, 1700)
    assert task.state.phase is MatchPhase.IN_PROGRESS
    dummy = task.state.players[2]
    assert dummy.position == Vec2(0.0, 0.0) and dummy.weapon is Weapon.SPEAR
    assert dummy.heading_deg == 0.0, "facing the player, who stands on +x"
    snapshot = task.current_state_snapshot().model_dump(by_alias=True)
    assert snapshot["tutorialPlayerId"] == 1
    assert snapshot["players"][1]["position"] == {"x": 0.0, "y": 0.0, "heading": 0.0}


async def test_hits_resolve_normally_but_the_dummy_never_loses(monkeypatch):
    task, clock, combat = await started(monkeypatch)
    for n in range(14):  # 14 x 8 damage > 100 HP; 3.1 s apart, past the spear's cooldown
        ts = 2000 + n * 3100
        await run(task, clock, ts + 400, swing(ts))
    assert len(combat) == 14
    assert all(c.outcome is CombatOutcome.HIT and c.damage == 8 for c in combat)
    assert all(c.defender_hp == 100 for c in combat)
    assert task.state.phase is MatchPhase.IN_PROGRESS


async def test_the_dummy_players_real_inputs_are_ignored(monkeypatch):
    task, clock, combat = await started(monkeypatch)
    await run(task, clock, 2400, position(2, 1980, 5, 0), action(2, 2000, move="HORIZONTAL_SWING"))
    assert combat == []
    assert task.state.players[2].position == Vec2(0.0, 0.0)


async def test_free_practice_dummy_guards_now_and_then(monkeypatch):
    task, clock, combat = await started(monkeypatch)
    await task.set_tutorial(1, True)  # lessons done: free practice
    await run(task, clock, 1700)
    assert task.state.players[2].guard_state is GuardState.GUARDING
    await run(task, clock, 2400, swing(2000))
    assert (combat[-1].outcome, combat[-1].damage) == (CombatOutcome.BLOCK, 0)
    # The guard lasts guard_duration_ms (3 s), then the dummy waits before guarding again.
    await run(task, clock, 5600, swing(5200))
    assert combat[-1].outcome is CombatOutcome.HIT


async def test_ending_gives_back_the_real_weapon_and_starts_a_fresh_round(monkeypatch):
    task, clock, combat = tutorial_task(monkeypatch)
    task.state.players[2].weapon = Weapon.MACE
    await task.set_tutorial(1, False)
    await run(task, clock, 2400, swing(2000))
    assert combat[-1].outcome is CombatOutcome.HIT
    assert task.state.players[2].weapon is Weapon.SPEAR, "holds the player's weapon meanwhile"
    # Player 2's hub locks a new weapon during the tutorial: kept for afterwards.
    await run(task, clock, 2600,
              InboundWeaponChanged(2, WeaponChanged(timestamp=2500, player_id=2, weapon="SWORD")))

    await task.set_tutorial(None, False)
    p1, p2 = task.state.players[1], task.state.players[2]
    assert task.tutorial_player is None
    assert p2.weapon is Weapon.SWORD and p2.position is None
    assert p1.hp == p2.hp == 100 and not p1.cooldowns
    assert task.state.phase is MatchPhase.WAITING, "a normal match waits for both players again"
    assert task.current_state_snapshot().tutorial_player_id is None


async def test_a_reset_ends_the_tutorial(monkeypatch):
    task, _, _ = await started(monkeypatch)
    task.on_match_state = lambda state: asyncio.sleep(0)
    await task.reset_match()
    assert task.tutorial_player is None


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

    async def tutorial_snapshot(ws, player):
        while True:
            data = json.loads(await asyncio.wait_for(ws.recv(), 2))
            if data["type"] == "STATE_SNAPSHOT" and data["tutorialPlayerId"] == player:
                return data

    try:
        p1 = await connect("/visualizer/1")
        p2 = await connect("/visualizer/2")
        control = await connect("/control")
        for bad in ('{"type":"SET_TUTORIAL","version":1,"playerId":3,"dummyGuards":false}',
                    '{"type":"SET_TUTORIAL","version":1,"playerId":true,"dummyGuards":false}',
                    '{"type":"SET_TUTORIAL","version":1,"playerId":1}'):
            await control.send(bad)
        await p1.send('{"type":"SET_TUTORIAL","version":1,"playerId":1,"dummyGuards":false}')
        await asyncio.sleep(0.2)
        await tutorial_snapshot(p1, None)
        await control.send('{"type":"SET_TUTORIAL","version":1,"playerId":1,"dummyGuards":false}')
        await asyncio.gather(tutorial_snapshot(p1, 1), tutorial_snapshot(p2, 1))
        await control.send('{"type":"SET_TUTORIAL","version":1,"playerId":null,"dummyGuards":false}')
        await asyncio.gather(tutorial_snapshot(p1, None), tutorial_snapshot(p2, None))
    finally:
        for ws in connections:
            await ws.close()
        engine.cancel()
        await asyncio.gather(engine, return_exceptions=True)
