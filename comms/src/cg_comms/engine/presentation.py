"""Online lifecycle and complete presentation snapshots, called only by the state writer."""

from __future__ import annotations

from dataclasses import dataclass, field

from cg_comms.engine.rules import Rules
from cg_comms.engine.state import MatchState
from cg_comms.messages.enums import GuardState, MatchPhase
from cg_comms.messages.models import StateSnapshot


@dataclass
class Presentation:
    require_ready: bool = False
    ready: dict[int, bool] = field(default_factory=lambda: {1: False, 2: False})
    paused_at: int | None = None
    resume_floor: int = -1
    reason: str | None = None

    def update(self, state: MatchState, rules: Rules, presence: dict[str, bool], now: int) -> bool:
        if not self.require_ready or state.phase == MatchPhase.ENDED:
            return False
        available = all(
            self.ready[pid] and presence.get(str(pid), False)
            and p.weapon is not None and p.position is not None
            and 0 <= now - p.position_ts <= rules.position_max_age_ms
            for pid, p in state.players.items()
        )
        if available and state.phase in (MatchPhase.WAITING, MatchPhase.PAUSED):
            if self.paused_at is not None:
                paused_ms = now - self.paused_at
                for p in state.players.values():
                    for action, expiry in p.cooldowns.items():
                        if expiry > self.paused_at:
                            p.cooldowns[action] = expiry + paused_ms
            self.paused_at = None
            self.resume_floor = now
            state.phase = MatchPhase.IN_PROGRESS
            state.started_ts = state.started_ts or now
            self.reason = None
            return True
        if not available and state.phase == MatchPhase.IN_PROGRESS:
            state.phase = MatchPhase.PAUSED
            self.paused_at = now
            self.reason = "NOT_READY"
            for p in state.players.values():
                p.guard_state = GuardState.IDLE
                p.guard_entered_ts = 0
            return True
        return False

    def snapshot(
        self, state: MatchState, rules: Rules, presence: dict[str, bool], now: int,
    ) -> StateSnapshot:
        cooldown_now = self.paused_at if self.paused_at is not None else now
        players: list[dict[str, object]] = []
        for pid, p in sorted(state.players.items()):
            position = None
            if p.position is not None and 0 <= now - p.position_ts <= rules.position_max_age_ms:
                position = dict(x=p.position.x, y=p.position.y, heading=p.heading_deg)
            cooldowns = [
                dict(action=action, remaining_ms=expiry - cooldown_now,
                     total_ms=max(expiry - cooldown_now, p.cooldown_totals.get(action, 0)))
                for action, expiry in p.cooldowns.items() if expiry > cooldown_now
            ]
            players.append(dict(
                player_id=pid, hp=p.hp, max_hp=rules.starting_hp, weapon=p.weapon,
                guard_state=p.guard_state, connected=presence.get(str(pid), False),
                ready=self.ready[pid], position=position, cooldown_action=p.cooldown_action,
                cooldowns=cooldowns,
            ))
        # Validate the complete wire boundary together, including narrowed action/weapon IDs.
        return StateSnapshot.model_validate(dict(
            timestamp=now, phase=state.phase, winner_id=state.winner_id,
            reason=self.reason, players=players,
        ))
