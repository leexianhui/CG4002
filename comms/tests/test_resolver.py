"""Unit tests for engine.resolver.resolve() — the pure combat-decision function.

Derived independently from docs/COMMS_IMPLEMENTATION_PLAN.md §10.3 (the 12-branch combat
decision tree) and §13 (resolver testing requirements), WITHOUT reading engine/resolver.py's
source. Only the public dataclass/function API contract (ActionEvent, Decision, resolve's
signature) was used to call the function; every expected outcome, boundary value, and branch
ordering below comes from the plan document itself.

Fixture design: one small synthetic RulesConfig (one weapon, one move, clean round numbers)
plus a single "everything passes" baseline attacker/defender/event, so that every test below
mutates exactly one field away from that baseline to isolate exactly one §10.3 branch. A
failure should therefore point unambiguously at the branch named in the test.
"""

from __future__ import annotations

import math

import pytest

from cg_comms.config import RulesConfig, WeaponMove
from cg_comms.engine.resolver import ActionEvent, Decision, resolve
from cg_comms.engine.rules import Rules
from cg_comms.engine.state import MatchState, PlayerState, Vec2
from cg_comms.messages.enums import ActionClass, CombatOutcome, GuardState, MatchPhase, Weapon

# ---------------------------------------------------------------------------------------
# Synthetic rules config.
#
# One weapon (SWORD), one move (HORIZONTAL_SWING), clean round numbers so all boundary math
# can be checked by hand:
#   damage = 10, range_m = 3.0, cooldown_ms = 1000
#   block_multiplier = 0.25  ->  block damage = ceil(10 * 0.25) = 3
#   parry_window_ms = 150, guard_arc_deg = 120, min_confidence = 0.6,
#   position_max_age_ms = 1000
#
# THRUST is deliberately *not* defined for SWORD in this synthetic config, so it can stand
# in for "classifier reported a move this weapon doesn't have" (branch 4), without depending
# on the real shared/config/weapons.yaml contents.
# ---------------------------------------------------------------------------------------

ATTACKER_ID = 1
DEFENDER_ID = 2
T = 10_000  # baseline engine-time timestamp, ms


def make_rules(**config_overrides) -> Rules:
    base = dict(
        moves={"SWORD": {"HORIZONTAL_SWING": WeaponMove(damage=10, range_m=3.0, cooldown_ms=1000)}},
        starting_hp=100,
        parry_window_ms=150,
        block_multiplier=0.25,
        stagger_ms=500,
        guard_arc_deg=120,
        min_confidence=0.6,
        position_max_age_ms=1000,
        reorder_delay_ms=60,
    )
    base.update(config_overrides)
    return Rules(config=RulesConfig(**base))


def make_player(**overrides) -> PlayerState:
    base = dict(
        player_id=ATTACKER_ID,
        weapon=Weapon.SWORD,
        hp=100,
        position=Vec2(0.0, 0.0),
        heading_deg=0.0,
        position_ts=T,
        guard_state=GuardState.IDLE,
        guard_entered_ts=0,
        last_action=ActionClass.IDLE,
        cooldowns={},
    )
    base.update(overrides)
    return PlayerState(**base)


def make_attacker(**overrides) -> PlayerState:
    base = dict(player_id=ATTACKER_ID, position=Vec2(0.0, 0.0), heading_deg=0.0)
    base.update(overrides)
    return make_player(**base)


def make_defender(**overrides) -> PlayerState:
    # 2m in +x from the attacker's (0,0); heading 180 deg points back down -x, i.e. this
    # defender is facing the attacker. (Coordinate/heading convention is internal to this
    # file: heading_deg 0 = facing +x, increasing angle turns toward +y. Only internal
    # consistency across these tests matters -- see task note on coordinate convention.)
    base = dict(player_id=DEFENDER_ID, position=Vec2(2.0, 0.0), heading_deg=180.0)
    base.update(overrides)
    return make_player(**base)


def make_state(phase=MatchPhase.IN_PROGRESS, attacker=None, defender=None) -> MatchState:
    attacker = attacker if attacker is not None else make_attacker()
    defender = defender if defender is not None else make_defender()
    return MatchState(phase=phase, players={attacker.player_id: attacker, defender.player_id: defender})


def make_event(**overrides) -> ActionEvent:
    base = dict(
        player_id=ATTACKER_ID,
        action=ActionClass.HORIZONTAL_SWING,
        confidence=0.9,
        timestamp=T,
    )
    base.update(overrides)
    return ActionEvent(**base)


# ---------------------------------------------------------------------------------------
# Baseline sanity / branch 12 — otherwise -> HIT (full damage)
#
# This is the "everything passes" fixture every other test in this file mutates exactly one
# field away from: attacker and defender 2m apart, facing each other, both idle-guard, valid
# weapon/move, high confidence, no active cooldown, fresh positions, match IN_PROGRESS.
# ---------------------------------------------------------------------------------------


def test_baseline_sanity_default_hit_branch12():
    """§10.3 branch 12: otherwise -> HIT (full damage). Also exercises the shared baseline
    fixture used by every other test below -- if this one fails, treat every other failure
    in this file as suspect, since it means the baseline itself isn't "everything passes"."""
    d = resolve(make_state(), make_event(), make_rules())
    assert d.outcome == CombatOutcome.HIT
    assert d.attacker_id == ATTACKER_ID
    assert d.defender_id == DEFENDER_ID
    assert d.damage == 10


# ---------------------------------------------------------------------------------------
# Branch 1 — phase != IN_PROGRESS -> IGNORED
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phase",
    [MatchPhase.WAITING, MatchPhase.ALIGNING, MatchPhase.PAUSED, MatchPhase.ENDED],
)
def test_branch1_phase_not_in_progress_is_ignored(phase):
    d = resolve(make_state(phase=phase), make_event(), make_rules())
    assert d.outcome == CombatOutcome.IGNORED


def test_boundary_action_after_match_ended_is_ignored():
    """§13 boundary: an action arriving after ENDED -> IGNORED. Uses the exact baseline
    event that resolves to HIT when IN_PROGRESS (test_baseline_sanity...), changing only the
    phase, so a failure here points unambiguously at phase gating and nothing else."""
    d = resolve(make_state(phase=MatchPhase.ENDED), make_event(), make_rules())
    assert d.outcome == CombatOutcome.IGNORED


# ---------------------------------------------------------------------------------------
# Branch 2 — ev.action == IDLE -> STATE_ONLY (update last_action, no event)
# ---------------------------------------------------------------------------------------


def test_branch2_idle_action_is_state_only():
    d = resolve(make_state(), make_event(action=ActionClass.IDLE), make_rules())
    assert d.outcome == CombatOutcome.STATE_ONLY
    assert d.attacker_id == ATTACKER_ID


# ---------------------------------------------------------------------------------------
# Branch 3 — attacker.weapon is None -> IGNORED
# ---------------------------------------------------------------------------------------


def test_branch3_attacker_unarmed_is_ignored():
    attacker = make_attacker(weapon=None)
    d = resolve(make_state(attacker=attacker), make_event(), make_rules())
    assert d.outcome == CombatOutcome.IGNORED


# ---------------------------------------------------------------------------------------
# Branch 4 — ev.action not in rules.moves[weapon] -> IGNORED (classifier/weapon mismatch)
# ---------------------------------------------------------------------------------------


def test_branch4_action_not_a_move_for_weapon_is_ignored():
    # THRUST is deliberately absent from the synthetic SWORD move set above.
    d = resolve(make_state(), make_event(action=ActionClass.THRUST), make_rules())
    assert d.outcome == CombatOutcome.IGNORED


# ---------------------------------------------------------------------------------------
# Branch 5 — ev.confidence < rules.min_confidence -> IGNORED
# ---------------------------------------------------------------------------------------


def test_branch5_low_confidence_is_ignored():
    d = resolve(make_state(), make_event(confidence=0.5), make_rules())  # min_confidence = 0.6
    assert d.outcome == CombatOutcome.IGNORED


# ---------------------------------------------------------------------------------------
# Branch 6 — cooldowns[ev.action] > ev.timestamp -> ON_COOLDOWN (no COMBAT_EVENT)
# ---------------------------------------------------------------------------------------


def test_branch6_on_cooldown():
    attacker = make_attacker(cooldowns={ActionClass.HORIZONTAL_SWING: T + 1})
    d = resolve(make_state(attacker=attacker), make_event(), make_rules())
    assert d.outcome == CombatOutcome.ON_COOLDOWN


def test_boundary_cooldown_expiring_exactly_at_action_timestamp_is_not_on_cooldown():
    """§13 boundary: cooldown expiring exactly at the action timestamp. Branch 6's
    condition is strict '>' (cooldowns[action] > ev.timestamp), so an expiry equal to the
    action's own timestamp must NOT count as still-on-cooldown -- it has just elapsed --
    and the event falls through to a plain HIT under the rest of the baseline."""
    attacker = make_attacker(cooldowns={ActionClass.HORIZONTAL_SWING: T})
    d = resolve(make_state(attacker=attacker), make_event(), make_rules())
    assert d.outcome == CombatOutcome.HIT
    assert d.damage == 10


# ---------------------------------------------------------------------------------------
# Branch 7 — either position is None, or age(position) > position_max_age_ms -> IGNORED
#
# Judgment call: the plan does not spell out what "age" is measured against. resolve() is a
# pure function with no clock of its own, so ev.timestamp is the only available "now" --
# age is taken here as ev.timestamp - position_ts. Both attacker's and defender's positions
# are tested (missing and stale), since range/facing need both to "adjudicate fairly", which
# is the plan's own phrase for why this branch exists.
# ---------------------------------------------------------------------------------------


def test_branch7_attacker_position_missing_is_ignored():
    attacker = make_attacker(position=None)
    d = resolve(make_state(attacker=attacker), make_event(), make_rules())
    assert d.outcome == CombatOutcome.IGNORED


def test_branch7_defender_position_missing_is_ignored():
    defender = make_defender(position=None)
    d = resolve(make_state(defender=defender), make_event(), make_rules())
    assert d.outcome == CombatOutcome.IGNORED


def test_branch7_attacker_position_stale_is_ignored():
    # position_max_age_ms = 1000; age = T - (T - 1001) = 1001 > 1000
    attacker = make_attacker(position_ts=T - 1001)
    d = resolve(make_state(attacker=attacker), make_event(), make_rules())
    assert d.outcome == CombatOutcome.IGNORED


def test_branch7_defender_position_stale_is_ignored():
    defender = make_defender(position_ts=T - 1001)
    d = resolve(make_state(defender=defender), make_event(), make_rules())
    assert d.outcome == CombatOutcome.IGNORED


# ---------------------------------------------------------------------------------------
# Branch 8 — range = |attacker.pos - defender.pos|; range > weapon range -> MISS
# ---------------------------------------------------------------------------------------


def test_branch8_out_of_range_is_miss():
    defender = make_defender(position=Vec2(10.0, 0.0))  # 10m; weapon range is 3.0m
    d = resolve(make_state(defender=defender), make_event(), make_rules())
    assert d.outcome == CombatOutcome.MISS
    assert d.damage == 0


def test_boundary_range_exactly_at_weapon_limit_is_not_a_miss():
    """§13 boundary: range at exactly the weapon limit. Branch 8's condition is strict '>'
    (range > limit), so range == limit must NOT miss -- it falls through the rest of the
    chain to a plain HIT under baseline (unguarded, facing) conditions."""
    defender = make_defender(position=Vec2(3.0, 0.0))  # exactly 3.0m == weapon range_m
    d = resolve(make_state(defender=defender), make_event(), make_rules())
    assert d.outcome == CombatOutcome.HIT
    assert d.damage == 10
    assert d.range_m == pytest.approx(3.0)


# ---------------------------------------------------------------------------------------
# Branch 9 — defender not facing attacker within guard_arc_deg of heading -> HIT (full
# damage); "you cannot block behind you". Uses a clearly-behind 180 degree case (unambiguous
# under either a half-angle or full-angle cone interpretation of guard_arc_deg), and
# additionally puts the defender in GUARDING with a freshly-raised guard so that, if the arc
# check did not run before the parry/block checks, this would incorrectly come back PARRY.
# ---------------------------------------------------------------------------------------


def test_branch9_attack_from_outside_guard_arc_is_full_hit_even_while_guarding():
    defender = make_defender(
        heading_deg=0.0,  # facing +x -- i.e. facing away from the attacker at (0,0)
        guard_state=GuardState.GUARDING,
        guard_entered_ts=T - 10,  # fresh guard; would qualify for PARRY if arc didn't gate first
    )
    d = resolve(make_state(defender=defender), make_event(), make_rules())
    assert d.outcome == CombatOutcome.HIT
    assert d.damage == 10


# ---------------------------------------------------------------------------------------
# Branch 10 — GUARDING and 0 <= (attack_ts - guard_entered_ts) <= parry_window_ms -> PARRY
# (0 damage, attacker staggered)
# ---------------------------------------------------------------------------------------


def test_branch10_parry_within_window():
    defender = make_defender(guard_state=GuardState.GUARDING, guard_entered_ts=T - 100)
    d = resolve(make_state(defender=defender), make_event(), make_rules())
    assert d.outcome == CombatOutcome.PARRY
    assert d.damage == 0


def test_boundary_parry_at_exactly_150ms():
    """§13 boundary: parry at exactly 150ms. The window comparison is inclusive on both
    ends ('0 <= ... <= parry_window_ms'), so attack_ts - guard_entered_ts == 150 must still
    PARRY, not fall through to BLOCK."""
    defender = make_defender(guard_state=GuardState.GUARDING, guard_entered_ts=T - 150)
    d = resolve(make_state(defender=defender), make_event(), make_rules())
    assert d.outcome == CombatOutcome.PARRY


def test_late_guard_requires_explicit_grace_and_never_parries():
    defender = make_defender(guard_state=GuardState.GUARDING, guard_entered_ts=T + 50)
    rules = make_rules()
    assert resolve(make_state(defender=defender), make_event(), rules).outcome == CombatOutcome.HIT
    rules.config.guard_grace_ms = 50
    assert resolve(make_state(defender=defender), make_event(), rules).outcome == CombatOutcome.BLOCK


def test_branch11_block_outside_parry_window():
    # Guard has been held since well before the parry window opened (10s, not 150ms).
    defender = make_defender(guard_state=GuardState.GUARDING, guard_entered_ts=T - 10_000)
    d = resolve(make_state(defender=defender), make_event(), make_rules())
    assert d.outcome == CombatOutcome.BLOCK
    assert d.damage == math.ceil(10 * 0.25)  # == 3


def test_boundary_hp_crossing_zero_from_a_block_is_not_clamped_by_the_resolver():
    """§13 boundary: HP crossing 0 from a block. resolve() is a pure function; §10.3's
    closing note says damage application and HP clamping happen OUTSIDE the pure function,
    in the caller. So even when the defender's remaining HP (1) is less than the raw block
    damage (3), resolve() must still return the full, unclamped block damage, and must not
    itself mutate the defender's stored hp."""
    defender = make_defender(hp=1, guard_state=GuardState.GUARDING, guard_entered_ts=T - 10_000)
    state = make_state(defender=defender)
    d = resolve(state, make_event(), make_rules())
    assert d.outcome == CombatOutcome.BLOCK
    assert d.damage == 3
    assert state.players[DEFENDER_ID].hp == 1  # untouched by the pure function


# ---------------------------------------------------------------------------------------
# Branch 12 — otherwise -> HIT (full damage)
#
# Covered directly by test_baseline_sanity_default_hit_branch12 above; the cooldown- and
# range-boundary tests also fall through to this branch, which is expected overlap.
# ---------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------
# §13 boundary — simultaneous mutual hits: resolve() called twice against the SAME
# unmodified MatchState snapshot, once per player as attacker, confirming the pure function
# doesn't get confused resolving both directions from one shared snapshot (no mutation of
# `state` leaks from one call into the other).
# ---------------------------------------------------------------------------------------


def test_boundary_simultaneous_mutual_hits_resolve_independently():
    # Player 1 at (0,0) facing +x (toward player 2); player 2 at (2,0) facing -x (toward
    # player 1) -- each faces the other, both idle-guard, so each direction independently
    # resolves to a plain HIT.
    p1 = make_player(player_id=1, position=Vec2(0.0, 0.0), heading_deg=0.0)
    p2 = make_player(player_id=2, position=Vec2(2.0, 0.0), heading_deg=180.0)
    state = MatchState(phase=MatchPhase.IN_PROGRESS, players={1: p1, 2: p2})
    rules = make_rules()

    ev1 = make_event(player_id=1, timestamp=T)
    ev2 = make_event(player_id=2, timestamp=T)

    d1 = resolve(state, ev1, rules)
    d2 = resolve(state, ev2, rules)

    assert d1.outcome == CombatOutcome.HIT
    assert d1.attacker_id == 1
    assert d1.defender_id == 2

    assert d2.outcome == CombatOutcome.HIT
    assert d2.attacker_id == 2
    assert d2.defender_id == 1

    # The shared snapshot must not have been mutated by either call.
    assert state.players[1].hp == 100
    assert state.players[2].hp == 100
