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

    def update(self, state: MatchState, rules: Rules, presence: dict[str, bool], now: int,
               players: set[int] | None = None) -> bool:
        """`players`: whose readiness the match waits for (all by default; the tutorial player alone
        in a tutorial, since the engine plays the dummy)."""
        if not self.require_ready or state.phase == MatchPhase.ENDED:
            return False
        available = all(
            self.ready[pid] and presence.get(str(pid), False)
            and p.weapon is not None and p.position is not None
            and 0 <= now - p.position_ts <= rules.position_max_age_ms
            for pid, p in state.players.items() if players is None or pid in players
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
        god_mode: bool = False, tutorial_player: int | None = None,
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
                # The phones see a guard drop when guard_duration_ms runs out, not just on release.
                guard_state=GuardState.GUARDING if rules.guard_active(p, now) else GuardState.IDLE,
                connected=presence.get(str(pid), False),
                ready=self.ready[pid], position=position, cooldown_action=p.cooldown_action,
                cooldowns=cooldowns,
                weapon_lock=None if p.weapon_candidate is None else dict(
                    weapon=p.weapon_candidate, total_ms=p.candidate_lock_ms,
                    held_ms=min(max(0, now - p.candidate_since_ts), p.candidate_lock_ms)),
            ))
        # Validate the complete wire boundary together, including narrowed action/weapon IDs.
        return StateSnapshot.model_validate(dict(
            timestamp=now, phase=state.phase, winner_id=state.winner_id,
            reason=self.reason, god_mode=god_mode, tutorial_player_id=tutorial_player, players=players,
        ))
