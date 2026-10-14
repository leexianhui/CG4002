"""Authoritative game state — plain dataclasses (COMMS_IMPLEMENTATION_PLAN.md §10.1).

Written by exactly one coroutine (game_state_task.py); everyone else reads immutable snapshots
(`.clone()`). Do not hand out a live PlayerState/MatchState reference outside game_state_task.py —
that is the entire justification for the single-writer, no-locks design (§10.1, §16.5).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cg_comms.messages.enums import ActionClass, GuardState, MatchPhase, Weapon


@dataclass(frozen=True)
class Vec2:
    x: float
    y: float


@dataclass
class PlayerState:
    player_id: int
    weapon: Weapon | None = None
    hp: int = 100
    position: Vec2 | None = None
    heading_deg: float = 0.0
    position_ts: int = 0
    guard_state: GuardState = GuardState.IDLE
    guard_entered_ts: int = 0
    last_action: ActionClass = ActionClass.IDLE
    cooldowns: dict[ActionClass, int] = field(default_factory=dict)  # action -> expiry engine-ts-ms
    cooldown_totals: dict[ActionClass, int] = field(default_factory=dict)
    cooldown_action: ActionClass | None = None

    def clone(self) -> "PlayerState":
        return PlayerState(
            player_id=self.player_id,
            weapon=self.weapon,
            hp=self.hp,
            position=self.position,
            heading_deg=self.heading_deg,
            position_ts=self.position_ts,
            guard_state=self.guard_state,
            guard_entered_ts=self.guard_entered_ts,
            last_action=self.last_action,
            cooldowns=dict(self.cooldowns),
            cooldown_totals=dict(self.cooldown_totals),
            cooldown_action=self.cooldown_action,
        )


@dataclass
class MatchState:
    phase: MatchPhase = MatchPhase.WAITING
    players: dict[int, PlayerState] = field(default_factory=dict)
    winner_id: int | None = None
    started_ts: int = 0

    def clone(self) -> "MatchState":
        return MatchState(
            phase=self.phase,
            players={pid: p.clone() for pid, p in self.players.items()},
            winner_id=self.winner_id,
            started_ts=self.started_ts,
        )
