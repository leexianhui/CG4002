"""App-layer enums — mirrors shared/enums/enums.yaml.

NOTE: shared/generated/python (tools/codegen's output, per REPO_PLAN.md §2) does not exist yet —
codegen is a separate, not-yet-built deliverable. Until it lands, this module is the hand-maintained
source of truth on the Python side and must be kept in lockstep with shared/enums/enums.yaml by
hand. Once codegen exists, replace this file's bodies with
`from cg_shared.enums import Weapon, ActionClass, ...` and delete the duplication, per the module's
original design intent ("re-export from shared/generated/python").

Enum values are the UPPER_SNAKE JSON wire strings; `WIRE_VALUES`/`from_wire` give the uint8 binary
mapping for each enum, matching enums.yaml's `wire:` field.
"""

from __future__ import annotations

from enum import Enum
from typing import TypeVar

_E = TypeVar("_E", bound="_WireEnum")


class _WireEnum(str, Enum):
    """A str Enum whose members also carry a uint8 wire value via WIRE_VALUES."""

    @classmethod
    def from_wire(cls: type[_E], wire_value: int) -> _E:
        for member in cls:
            if WIRE_VALUES[member] == wire_value:
                return member
        raise ValueError(f"{wire_value!r} is not a valid wire value for {cls.__name__}")

    @property
    def wire_value(self) -> int:
        return WIRE_VALUES[self]


class Weapon(_WireEnum):
    NONE = "NONE"
    SWORD = "SWORD"
    SPEAR = "SPEAR"
    MACE = "MACE"


class ActionClass(_WireEnum):
    IDLE = "IDLE"
    HORIZONTAL_SWING = "HORIZONTAL_SWING"
    VERTICAL_SWING = "VERTICAL_SWING"
    DIAGONAL_SWING = "DIAGONAL_SWING"
    THRUST = "THRUST"
    CHARGED_ATTACK = "CHARGED_ATTACK"
    CHARGED_THRUST = "CHARGED_THRUST"
    SWING = "SWING"


class GuardState(_WireEnum):
    IDLE = "IDLE"
    GUARDING = "GUARDING"


class CombatOutcome(_WireEnum):
    """Every branch of engine/resolver.py. Only MISS/HIT/BLOCK/PARRY are ever emitted as a
    COMBAT_EVENT.outcome; the rest are internal-logging-only (never on the wire)."""

    IGNORED = "IGNORED"
    STATE_ONLY = "STATE_ONLY"
    ON_COOLDOWN = "ON_COOLDOWN"
    MISS = "MISS"
    HIT = "HIT"
    BLOCK = "BLOCK"
    PARRY = "PARRY"


# The subset of CombatOutcome that is actually emitted on the wire as COMBAT_EVENT.outcome.
WIRE_COMBAT_OUTCOMES: frozenset[CombatOutcome] = frozenset(
    {CombatOutcome.MISS, CombatOutcome.HIT, CombatOutcome.BLOCK, CombatOutcome.PARRY}
)


class MatchPhase(_WireEnum):
    WAITING = "WAITING"
    ALIGNING = "ALIGNING"
    IN_PROGRESS = "IN_PROGRESS"
    PAUSED = "PAUSED"
    ENDED = "ENDED"


class FeedbackCode(_WireEnum):
    HIT_LANDED = "HIT_LANDED"
    HIT_TAKEN = "HIT_TAKEN"
    BLOCKED = "BLOCKED"
    PARRIED = "PARRIED"


WIRE_VALUES: dict[_WireEnum, int] = {
    Weapon.NONE: 0,
    Weapon.SWORD: 1,
    Weapon.SPEAR: 2,
    Weapon.MACE: 3,
    ActionClass.IDLE: 0,
    ActionClass.HORIZONTAL_SWING: 1,
    ActionClass.VERTICAL_SWING: 2,
    ActionClass.DIAGONAL_SWING: 3,
    ActionClass.THRUST: 4,
    ActionClass.CHARGED_ATTACK: 5,
    ActionClass.CHARGED_THRUST: 6,
    ActionClass.SWING: 7,
    GuardState.IDLE: 0,
    GuardState.GUARDING: 1,
    CombatOutcome.IGNORED: 0,
    CombatOutcome.STATE_ONLY: 1,
    CombatOutcome.ON_COOLDOWN: 2,
    CombatOutcome.MISS: 3,
    CombatOutcome.HIT: 4,
    CombatOutcome.BLOCK: 5,
    CombatOutcome.PARRY: 6,
    MatchPhase.WAITING: 0,
    MatchPhase.ALIGNING: 1,
    MatchPhase.IN_PROGRESS: 2,
    MatchPhase.PAUSED: 3,
    MatchPhase.ENDED: 4,
    FeedbackCode.HIT_LANDED: 0,
    FeedbackCode.HIT_TAKEN: 1,
    FeedbackCode.BLOCKED: 2,
    FeedbackCode.PARRIED: 3,
}
