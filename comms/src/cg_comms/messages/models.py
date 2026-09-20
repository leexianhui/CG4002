"""Pydantic models for every app-layer JSON message.

Mirrors shared/schemas/*.schema.json field-for-field (see shared/protocol/app_messages.md for the
normative description). `COMBAT_FEEDBACK` has no schema file (app_messages.md §2.7 explains why) but
is still modelled here since relay/engine_link.py and the mock layer both need to agree on its shape.

Field names are snake_case Python attributes with camelCase wire aliases — construct with
`Model(**json_dict)` (aliases) or `Model.model_construct`/keyword args (snake_case), and serialize
with `model_dump(by_alias=True)`.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cg_comms.messages.enums import (
    ActionClass,
    CombatOutcome,
    FeedbackCode,
    GuardState,
    MatchPhase,
    Weapon,
)
from cg_comms.protocol.constants import PROTOCOL_VERSION

PlayerId = Literal[1, 2]

RealWeapon = Literal[Weapon.SWORD, Weapon.SPEAR, Weapon.MACE]
CombatAction = Literal[
    ActionClass.HORIZONTAL_SWING,
    ActionClass.VERTICAL_SWING,
    ActionClass.DIAGONAL_SWING,
    ActionClass.THRUST,
    ActionClass.CHARGED_ATTACK,
    ActionClass.CHARGED_THRUST,
    ActionClass.SWING,
]
WireCombatOutcome = Literal[
    CombatOutcome.MISS, CombatOutcome.HIT, CombatOutcome.BLOCK, CombatOutcome.PARRY
]

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
ImuSample = Annotated[list[float], Field(min_length=6, max_length=6)]


class _Envelope(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    version: int = Field(default=PROTOCOL_VERSION)
    timestamp: int = Field(ge=0)


class Action(_Envelope):
    """relay -> engine. app_messages.md §2.1."""

    type: Literal["ACTION"] = "ACTION"
    player_id: PlayerId = Field(alias="playerId")
    weapon: RealWeapon
    action: ActionClass
    action_confidence: Confidence = Field(alias="actionConfidence")
    guard_state: GuardState = Field(alias="guardState")
    guard_confidence: Confidence = Field(alias="guardConfidence")


class WeaponChanged(_Envelope):
    """relay -> engine. app_messages.md §2.2."""

    type: Literal["WEAPON_CHANGED"] = "WEAPON_CHANGED"
    player_id: PlayerId = Field(alias="playerId")
    weapon: RealWeapon


class Position(_Envelope):
    """visualizer -> engine, echoed engine -> visualizer. app_messages.md §2.3."""

    type: Literal["POSITION"] = "POSITION"
    player_id: PlayerId = Field(alias="playerId")
    x: float
    y: float
    heading: Annotated[float, Field(ge=0.0, lt=360.0)]


class CombatEvent(_Envelope):
    """engine -> both visualizers. app_messages.md §2.4. No single playerId — carries
    attackerId/defenderId since it concerns two players by construction."""

    type: Literal["COMBAT_EVENT"] = "COMBAT_EVENT"
    attacker_id: PlayerId = Field(alias="attackerId")
    defender_id: PlayerId = Field(alias="defenderId")
    weapon: RealWeapon
    action: CombatAction
    outcome: WireCombatOutcome
    damage: int = Field(ge=0)
    attacker_hp: int = Field(alias="attackerHp", ge=0)
    defender_hp: int = Field(alias="defenderHp", ge=0)
    range_m: float = Field(alias="rangeM", ge=0.0)


class MatchState(_Envelope):
    """engine -> both visualizers. app_messages.md §2.5. Global message, no playerId."""

    type: Literal["MATCH_STATE"] = "MATCH_STATE"
    phase: MatchPhase
    hp: dict[str, int]
    winner_id: PlayerId | None = Field(alias="winnerId", default=None)
    reason: str | None = None
    connected: dict[str, bool] | None = None

    @field_validator("hp")
    @classmethod
    def _hp_has_both_players(cls, value: dict[str, int]) -> dict[str, int]:
        if set(value.keys()) != {"1", "2"}:
            raise ValueError(f'hp must have exactly keys "1" and "2", got {sorted(value.keys())}')
        return value


class CooldownEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    action: CombatAction
    remaining_ms: int = Field(alias="remainingMs", ge=0)
    total_ms: int = Field(alias="totalMs", ge=0)


class CooldownState(_Envelope):
    """engine -> both visualizers. app_messages.md §2.6."""

    type: Literal["COOLDOWN_STATE"] = "COOLDOWN_STATE"
    player_id: PlayerId = Field(alias="playerId")
    cooldowns: list[CooldownEntry]


class CombatFeedback(_Envelope):
    """engine -> relay -> FireBeetle. app_messages.md §2.7. Not in shared/schemas/ — translated
    1:1 into the binary CG_PKT_COMBAT_FEEDBACK packet by the relay, never seen by the visualizer
    or Ultra96."""

    type: Literal["COMBAT_FEEDBACK"] = "COMBAT_FEEDBACK"
    player_id: PlayerId = Field(alias="playerId")
    code: FeedbackCode
    intensity_pct: int = Field(alias="intensityPct", ge=0, le=100)
    duration_ms: int = Field(alias="durationMs", ge=0)
    repeat_count: int = Field(alias="repeatCount", ge=0)


class SensorWindow(_Envelope):
    """relay -> Ultra96. app_messages.md §3.1. Row count of dominant/offhand is deliberately
    unconstrained here — it is dictated by the trained model's input shape."""

    type: Literal["SENSOR_WINDOW"] = "SENSOR_WINDOW"
    player_id: PlayerId = Field(alias="playerId")
    request_id: int = Field(alias="requestId", ge=0, le=0xFFFFFFFF)
    weapon: RealWeapon
    window_start_ts: int = Field(alias="windowStartTs", ge=0)
    window_end_ts: int = Field(alias="windowEndTs", ge=0)
    sample_rate_hz: float = Field(alias="sampleRateHz", gt=0.0)
    dominant: list[ImuSample] = Field(min_length=1)
    offhand: list[ImuSample] = Field(min_length=1)
    stale_pct: float = Field(alias="stalePct", ge=0.0, le=100.0)
    trigger_id: int | None = Field(alias="triggerId", default=None, ge=0, le=7)


class ActionResult(_Envelope):
    """Ultra96 -> relay. app_messages.md §3.2. requestId echoes the triggering SensorWindow's."""

    type: Literal["ACTION_RESULT"] = "ACTION_RESULT"
    player_id: PlayerId = Field(alias="playerId")
    request_id: int = Field(alias="requestId", ge=0, le=0xFFFFFFFF)
    action: ActionClass
    action_confidence: Confidence = Field(alias="actionConfidence")
    guard_state: GuardState = Field(alias="guardState")
    guard_confidence: Confidence = Field(alias="guardConfidence")


class ActionRecognized(_Envelope):
    """engine -> both visualizers. app_messages.md §2.8. Reports that an action was *recognised*,
    independent of whether it resolved into combat — see docs/DEMO_TRIGGERED_ACTION_PLAN.md §6/§7.2.
    `action` permits IDLE deliberately: a guard-only report is `action: IDLE` + `guardState: GUARDING`.
    """

    type: Literal["ACTION_RECOGNIZED"] = "ACTION_RECOGNIZED"
    player_id: PlayerId = Field(alias="playerId")
    weapon: RealWeapon
    action: ActionClass
    action_confidence: Confidence = Field(alias="actionConfidence")
    guard_state: GuardState = Field(alias="guardState")
    guard_confidence: Confidence = Field(alias="guardConfidence")
    outcome: CombatOutcome
    target_id: PlayerId | None = Field(alias="targetId", default=None)


AppMessage = (
    Action
    | WeaponChanged
    | Position
    | CombatEvent
    | MatchState
    | CooldownState
    | CombatFeedback
    | SensorWindow
    | ActionResult
    | ActionRecognized
)
