"""The combat decision function — PURE, no I/O (COMMS_IMPLEMENTATION_PLAN.md §10.3, §16.5).

No clock reads, no queue access, no logging. Every input arrives as an argument, so the whole rule
set is testable without a socket. This is the one place purity discipline pays for itself directly:
game balance changes become a pure-function test, not a live-rig experiment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from cg_comms.engine.rules import Rules
from cg_comms.engine.state import MatchState, Vec2
from cg_comms.messages.enums import ActionClass, CombatOutcome, GuardState, MatchPhase


@dataclass(frozen=True)
class ActionEvent:
    player_id: int
    action: ActionClass
    confidence: float
    timestamp: int  # engine time, ms
    # Guard is orthogonal to `action`/combat resolution — `resolve()` never reads these two fields,
    # it only judges `action`. They ride along purely so `_resolve_one` (game_state_task.py) can
    # build ACTION_RECOGNIZED without re-deriving guard state from PlayerState. Defaulted so
    # `resolve()` stays free to ignore them and every existing 4-kwarg ActionEvent construction
    # (comms/tests/test_resolver.py's make_event) keeps working unmodified.
    guard_state: GuardState = GuardState.IDLE
    guard_confidence: float = 0.0


@dataclass(frozen=True)
class Decision:
    outcome: CombatOutcome
    attacker_id: int
    defender_id: int | None = None
    damage: int = 0
    range_m: float = 0.0


def _range_between(a: Vec2, b: Vec2) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _bearing_deg(origin: Vec2, target: Vec2) -> float:
    return math.degrees(math.atan2(target.y - origin.y, target.x - origin.x)) % 360.0


def _angle_diff_deg(a: float, b: float) -> float:
    diff = abs(a - b) % 360.0
    return min(diff, 360.0 - diff)


def _other_player_id(state: MatchState, player_id: int) -> int | None:
    others = [pid for pid in state.players if pid != player_id]
    return others[0] if others else None


def resolve(state: MatchState, ev: ActionEvent, rules: Rules) -> Decision:
    """12-step branch per §10.3/§15: IGNORED / STATE_ONLY / ON_COOLDOWN / MISS / HIT / PARRY / BLOCK."""
    attacker = state.players.get(ev.player_id)
    if attacker is None:
        return Decision(outcome=CombatOutcome.IGNORED, attacker_id=ev.player_id)

    if state.phase != MatchPhase.IN_PROGRESS:
        return Decision(outcome=CombatOutcome.IGNORED, attacker_id=ev.player_id)

    if ev.action == ActionClass.IDLE:
        return Decision(outcome=CombatOutcome.STATE_ONLY, attacker_id=ev.player_id)

    if attacker.weapon is None:
        return Decision(outcome=CombatOutcome.IGNORED, attacker_id=ev.player_id)

    if not rules.is_valid_move(attacker.weapon, ev.action):
        return Decision(outcome=CombatOutcome.IGNORED, attacker_id=ev.player_id)

    if ev.confidence < rules.min_confidence:
        return Decision(outcome=CombatOutcome.IGNORED, attacker_id=ev.player_id)

    expiry = attacker.cooldowns.get(ev.action)
    if expiry is not None and expiry > ev.timestamp:
        return Decision(outcome=CombatOutcome.ON_COOLDOWN, attacker_id=ev.player_id)

    defender_id = _other_player_id(state, ev.player_id)
    defender = state.players.get(defender_id) if defender_id is not None else None

    if (
        defender is None
        or attacker.position is None
        or defender.position is None
        or ev.timestamp - attacker.position_ts > rules.position_max_age_ms
        or ev.timestamp - defender.position_ts > rules.position_max_age_ms
    ):
        return Decision(outcome=CombatOutcome.IGNORED, attacker_id=ev.player_id, defender_id=defender_id)

    move = rules.move_for(attacker.weapon, ev.action)
    assert move is not None  # guaranteed by the is_valid_move check above
    range_m = _range_between(attacker.position, defender.position)

    if range_m > move.range_m:
        return Decision(
            outcome=CombatOutcome.MISS, attacker_id=ev.player_id, defender_id=defender_id, range_m=range_m
        )

    bearing_to_attacker = _bearing_deg(defender.position, attacker.position)
    facing_attacker = _angle_diff_deg(defender.heading_deg, bearing_to_attacker) <= rules.guard_arc_deg / 2

    if facing_attacker and defender.guard_state == GuardState.GUARDING:
        if 0 <= (ev.timestamp - defender.guard_entered_ts) <= rules.parry_window_ms:
            return Decision(
                outcome=CombatOutcome.PARRY,
                attacker_id=ev.player_id,
                defender_id=defender_id,
                damage=0,
                range_m=range_m,
            )
        damage = math.ceil(move.damage * rules.block_multiplier)
        return Decision(
            outcome=CombatOutcome.BLOCK,
            attacker_id=ev.player_id,
            defender_id=defender_id,
            damage=damage,
            range_m=range_m,
        )

    # Not guarding, or not facing the attacker (you cannot block/parry from behind) -> full hit.
    return Decision(
        outcome=CombatOutcome.HIT,
        attacker_id=ev.player_id,
        defender_id=defender_id,
        damage=move.damage,
        range_m=range_m,
    )
