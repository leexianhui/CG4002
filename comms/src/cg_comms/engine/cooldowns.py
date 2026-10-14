"""Per-player, per-action cooldown table helpers — operate on PlayerState.cooldowns (action ->
expiry engine-ts-ms), which state.py declares but does not manage.

Cooldowns pause (do not tick) while MatchPhase is PAUSED (COMMS_IMPLEMENTATION_PLAN.md §15 decision
#10) — enforced by game_state_task.py only advancing `now_engine_ms` into these checks while the
match is IN_PROGRESS, not by anything in this module.
"""

from __future__ import annotations

from cg_comms.engine.state import PlayerState
from cg_comms.messages.enums import ActionClass


def is_on_cooldown(player: PlayerState, action: ActionClass, at_ts: int) -> bool:
    expiry = player.cooldowns.get(action)
    return expiry is not None and expiry > at_ts


def start_cooldown(player: PlayerState, action: ActionClass, at_ts: int, cooldown_ms: int) -> None:
    player.cooldowns[action] = at_ts + cooldown_ms
    player.cooldown_totals[action] = cooldown_ms
    player.cooldown_action = action


def add_stagger(player: PlayerState, stagger_ms: int, at_ts: int) -> None:
    """Extends every one of `player`'s currently-running cooldowns by `stagger_ms` (resolver's
    PARRY penalty, §10.3 step 10). A cooldown that already expired is not revived."""
    for action, expiry in list(player.cooldowns.items()):
        if expiry > at_ts:
            player.cooldowns[action] = expiry + stagger_ms
            previous_total = player.cooldown_totals.get(action, expiry - at_ts)
            player.cooldown_totals[action] = previous_total + stagger_ms
