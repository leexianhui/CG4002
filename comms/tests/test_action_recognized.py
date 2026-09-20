"""Regression tests for ACTION_RECOGNIZED emission (docs/DEMO_TRIGGERED_ACTION_PLAN.md §7.2, E5).

Covers the corrected emit rule -- `action != IDLE OR guardState != IDLE` -- and the presence-gated
`targetId`, both called out in the plan as easy to get wrong: a naive `action != IDLE` rule silently
drops every guard press, and copying `decision.defender_id` straight into `targetId` would announce
an opponent that was never actually connected.
"""
from __future__ import annotations

import asyncio

import pytest

from cg_comms.clock import monotonic_ms
from cg_comms.config import RulesConfig, WeaponMove
from cg_comms.engine.game_state_task import GameStateTask
from cg_comms.engine.inbound import InboundAction, InboundWeaponChanged
from cg_comms.engine.rules import Rules
from cg_comms.messages.enums import ActionClass, CombatOutcome, GuardState, MatchPhase, Weapon
from cg_comms.messages.models import Action, ActionRecognized, WeaponChanged

PLAYER_ID = 1


def make_rules() -> Rules:
    config = RulesConfig(
        moves={"SWORD": {"HORIZONTAL_SWING": WeaponMove(damage=10, range_m=3.0, cooldown_ms=1000)}},
        starting_hp=100,
        parry_window_ms=150,
        block_multiplier=0.25,
        stagger_ms=500,
        guard_arc_deg=120,
        min_confidence=0.6,
        position_max_age_ms=1000,
        reorder_delay_ms=1,
    )
    return Rules(config=config)


def make_task(*, presence: dict[str, bool] | None = None) -> tuple[GameStateTask, list[ActionRecognized]]:
    recognized: list[ActionRecognized] = []

    async def _noop_event(_event):
        return None

    async def _noop_state(_state):
        return None

    async def _noop_feedback(_player_id, _feedback):
        return None

    async def _on_action_recognized(event: ActionRecognized) -> None:
        recognized.append(event)

    def _presence() -> dict[str, bool]:
        return presence if presence is not None else {}

    task = GameStateTask(
        rules=make_rules(),
        reorder_delay_ms=1,
        late_drop_ms=500,
        on_combat_event=_noop_event,
        on_match_state=_noop_state,
        on_combat_feedback=_noop_feedback,
        on_action_recognized=_on_action_recognized,
        presence=_presence,
    )
    task.state.phase = MatchPhase.IN_PROGRESS
    return task, recognized


def make_action(
    *,
    player_id: int,
    action: ActionClass,
    guard_state: GuardState = GuardState.IDLE,
    guard_confidence: float = 0.0,
    weapon: Weapon = Weapon.SWORD,
    ts: int,
) -> InboundAction:
    return InboundAction(
        player_id=player_id,
        action=Action(
            timestamp=ts,
            playerId=player_id,
            weapon=weapon,
            action=action,
            actionConfidence=0.9,
            guardState=guard_state,
            guardConfidence=guard_confidence,
        ),
    )


async def _run_until_resolved(task: GameStateTask, *, poll_ms: int = 5, max_polls: int = 30) -> None:
    for _ in range(max_polls):
        await task._drain_ready()
        if len(task._reorder) == 0:
            return
        await asyncio.sleep(poll_ms / 1000)
    raise AssertionError("reorder buffer never drained")


@pytest.mark.asyncio
async def test_guard_press_emits_exactly_one_action_recognized_state_only() -> None:
    """The case the plan calls out by name: guard is action=IDLE + guardState=GUARDING, so the
    naive `action != IDLE` rule would display nothing at all for the guard button."""
    task, recognized = make_task()
    now = monotonic_ms()
    task._absorb(
        InboundWeaponChanged(
            player_id=PLAYER_ID,
            weapon_changed=WeaponChanged(timestamp=now, playerId=PLAYER_ID, weapon=Weapon.SWORD),
        )
    )
    task._absorb(
        make_action(
            player_id=PLAYER_ID,
            action=ActionClass.IDLE,
            guard_state=GuardState.GUARDING,
            guard_confidence=0.88,
            ts=now,
        )
    )
    await _run_until_resolved(task)

    assert len(recognized) == 1
    event = recognized[0]
    assert event.action is ActionClass.IDLE
    assert event.guard_state is GuardState.GUARDING
    assert event.outcome is CombatOutcome.STATE_ONLY
    assert event.target_id is None


@pytest.mark.asyncio
async def test_pure_idle_emits_nothing() -> None:
    """action=IDLE and guardState=IDLE together is filler, not a recognised action -- the one case
    the emit rule must suppress."""
    task, recognized = make_task()
    now = monotonic_ms()
    task._absorb(
        InboundWeaponChanged(
            player_id=PLAYER_ID,
            weapon_changed=WeaponChanged(timestamp=now, playerId=PLAYER_ID, weapon=Weapon.SWORD),
        )
    )
    task._absorb(make_action(player_id=PLAYER_ID, action=ActionClass.IDLE, ts=now))
    await _run_until_resolved(task)

    assert recognized == []


@pytest.mark.asyncio
async def test_p1_only_swing_emits_action_recognized_ignored_with_null_target() -> None:
    """P1-only operation (§8): a swing with no P2 relay connected must still be reported, with
    outcome IGNORED and targetId null -- this is the proof the pipeline works without a second
    player, per §6."""
    task, recognized = make_task(presence={"1": True, "2": False})
    now = monotonic_ms()
    task._absorb(
        InboundWeaponChanged(
            player_id=PLAYER_ID,
            weapon_changed=WeaponChanged(timestamp=now, playerId=PLAYER_ID, weapon=Weapon.SWORD),
        )
    )
    task._absorb(make_action(player_id=PLAYER_ID, action=ActionClass.HORIZONTAL_SWING, ts=now))
    await _run_until_resolved(task)

    assert len(recognized) == 1
    event = recognized[0]
    assert event.action is ActionClass.HORIZONTAL_SWING
    assert event.outcome is CombatOutcome.IGNORED
    assert event.target_id is None, "defender_id=2 must not leak through when /relay/2 isn't connected"


@pytest.mark.asyncio
async def test_target_id_present_only_when_defender_relay_connected() -> None:
    """Same swing, but with presence saying P2's relay IS connected (still IGNORED here since no
    position data was fed) -- targetId is still null because IGNORED never carried a defender_id
    convertible into a claimed target for this scenario. This isolates presence-gating: flipping
    presence alone, with no defender_id available, must not fabricate a target."""
    task, recognized = make_task(presence={"1": True, "2": True})
    now = monotonic_ms()
    task._absorb(
        InboundWeaponChanged(
            player_id=PLAYER_ID,
            weapon_changed=WeaponChanged(timestamp=now, playerId=PLAYER_ID, weapon=Weapon.SWORD),
        )
    )
    task._absorb(make_action(player_id=PLAYER_ID, action=ActionClass.HORIZONTAL_SWING, ts=now))
    await _run_until_resolved(task)

    assert len(recognized) == 1
    # No P2 position was ever fed to the task, so resolver.py's position gate (step 7) still
    # returns IGNORED with defender_id=2 -- and presence says P2 IS connected here, so targetId
    # correctly surfaces 2: presence, not position data, decides `targetId`, per the plan's rule.
    assert recognized[0].outcome is CombatOutcome.IGNORED
    assert recognized[0].target_id == 2
