"""Deterministic event-time tests; converted clocks are fixed to isolate gameplay semantics."""

import asyncio
from itertools import permutations

import pytest
from test_game_state_task_clock_skew import make_task

from cg_comms.engine.coordinates import unity_heading_to_engine
from cg_comms.engine.inbound import InboundAction, InboundPosition, InboundWeaponChanged
from cg_comms.engine.resolver import _bearing_deg
from cg_comms.engine.state import Vec2
from cg_comms.messages.enums import CombatOutcome, Weapon
from cg_comms.messages.models import Action, Position, WeaponChanged


def action(pid, ts, guard="IDLE", weapon="SWORD", move="IDLE"):
    return InboundAction(pid, Action(timestamp=ts, player_id=pid, weapon=weapon, action=move,
                                    action_confidence=.9, guard_state=guard, guard_confidence=1))


def position(pid, ts, x, heading):
    return InboundPosition(pid, Position(timestamp=ts, player_id=pid, x=x, y=0, heading=heading))


def setup_task(monkeypatch, grace=50):
    task = make_task()
    task.rules.config.guard_grace_ms = grace
    task._reorder.reorder_delay_ms = task.reorder_delay_ms + grace
    task._clock_anchors = {(pid, channel): 0 for pid in (1, 2) for channel in ("action", "position")}
    clock = [1000]
    monkeypatch.setattr("cg_comms.engine.game_state_task.monotonic_ms", lambda: clock[0])
    recognized, combat = [], []

    async def recognize(event):
        recognized.append(event)

    async def resolved(event):
        combat.append(event)

    task.on_action_recognized = recognize
    task.on_combat_event = resolved
    task._absorb(position(1, 700, 0, 0))
    task._absorb(position(2, 700, 1, 180))
    return task, clock, recognized, combat


@pytest.mark.parametrize("guard_ts,outcome", [(700, "BLOCK"), (950, "PARRY"),
                                             (1049, "BLOCK"), (1050, "BLOCK"),
                                             (1051, "HIT"), (1200, "HIT")])
@pytest.mark.parametrize("reverse", [False, True])
async def test_guard_grace_boundaries_and_arrival_order(monkeypatch, guard_ts, outcome, reverse):
    task, clock, _, combat = setup_task(monkeypatch)
    events = [action(1, 1000, move="HORIZONTAL_SWING"), action(2, guard_ts, guard="GUARDING")]
    for event in reversed(events) if reverse else events:
        task._absorb(event)
    clock[0] = 1400
    await task._drain_ready()
    assert combat[0].outcome == CombatOutcome(outcome)


async def test_guard_lookahead_does_not_apply_future_weapon_or_geometry(monkeypatch):
    events = [action(1, 1000, move="HORIZONTAL_SWING"),
              InboundWeaponChanged(1, WeaponChanged(timestamp=1020, player_id=1, weapon="MACE")),
              position(2, 1020, 100, 0)]
    for arrival in permutations(events):
        task, clock, recognized, combat = setup_task(monkeypatch)
        for event in arrival:
            task._absorb(event)
        clock[0] = 1200
        await task._drain_ready()
        assert combat[0].outcome == CombatOutcome.HIT
        assert combat[0].weapon == recognized[0].weapon == Weapon.SWORD
        assert combat[0].damage == 10 and combat[0].range_m == 1
        assert task.state.players[1].weapon == Weapon.MACE
        assert task.state.players[2].position.x == 100


async def test_grace_release_waits_for_reorder_plus_grace(monkeypatch):
    task, clock, _, combat = setup_task(monkeypatch)
    task._absorb(action(1, 1000, move="HORIZONTAL_SWING"))
    clock[0] = 1061
    await task._drain_ready()
    assert not combat
    task._absorb(action(2, 1050, guard="GUARDING"))
    clock[0] = 1111
    await task._drain_ready()
    assert combat[0].outcome == CombatOutcome.BLOCK
    # Grace only affected the resolution snapshot; real guard event has not released yet.
    assert task.state.players[2].guard_state.value == "IDLE"


async def test_late_event_cannot_rewrite_already_resolved_state(monkeypatch):
    task, clock, _, combat = setup_task(monkeypatch, grace=0)
    task._absorb(action(1, 1000, move="HORIZONTAL_SWING"))
    clock[0] = 1100
    await task._drain_ready()
    task._absorb(action(2, 990, guard="GUARDING"))
    await task._drain_ready()
    assert combat[0].outcome == CombatOutcome.HIT
    assert task._reorder.dropped_late == 1
    assert task.state.players[2].guard_state.value == "IDLE"


async def test_relay_reconnect_does_not_change_guard_for_earlier_queued_attack(monkeypatch):
    from test_pre_demo_recovery import until

    from cg_comms.engine.inbound import LinkReset

    task, clock, _, combat = setup_task(monkeypatch)
    task._absorb(action(2, 700, guard="GUARDING"))
    await task._drain_ready()
    task._absorb(action(1, 1000, move="HORIZONTAL_SWING"))
    await task.submit(LinkReset(2, "action"))
    stop = asyncio.Event()
    runner = asyncio.create_task(task.run(stop, drain_interval_ms=5))
    try:
        await until(lambda: (2, "action") not in task._clock_anchors)
        assert task.state.players[2].guard_state.value == "GUARDING"
        clock[0] = 1200
        await until(lambda: bool(combat))
        assert combat[0].outcome == CombatOutcome.BLOCK
    finally:
        stop.set()
        await runner


async def test_reset_clears_gameplay_and_pending_attacks_preserves_presence_and_clocks(monkeypatch):
    task, clock, _, combat = setup_task(monkeypatch)
    task.presence = lambda: {"1": True, "2": True}
    states = []

    async def state(value):
        states.append(value)

    task.on_match_state = state
    task._absorb(action(1, 1000, move="HORIZONTAL_SWING"))
    task.state.players[2].hp = 1
    anchors = dict(task._clock_anchors)
    clock[0] = 1100
    await task.reset_match()
    assert not task._reorder
    assert task._clock_anchors == anchors
    assert states[-1].reason == "reset"
    assert states[-1].hp == {"1": 100, "2": 100}
    assert states[-1].connected == {"1": True, "2": True}
    task._absorb(action(1, 1099, move="HORIZONTAL_SWING"))
    clock[0] = 1400
    await task._drain_ready()
    assert not combat


@pytest.mark.parametrize("yaw,target,heading", [(0, Vec2(0, 1), 90), (90, Vec2(1, 0), 0),
                                              (180, Vec2(0, -1), 270), (270, Vec2(-1, 0), 180),
                                              (-90, Vec2(-1, 0), 180), (450, Vec2(1, 0), 0)])
def test_unity_heading_agrees_with_engine_bearing(yaw, target, heading):
    assert unity_heading_to_engine(yaw) == heading == _bearing_deg(Vec2(0, 0), target)
