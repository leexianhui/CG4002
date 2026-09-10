"""Unit tests for engine/cooldowns.py — per-player, per-action cooldown expiry logic.

Derived from COMMS_IMPLEMENTATION_PLAN.md §10.3 step 6 of the resolver's `resolve()` spec:

    cooldowns[ev.action] > ev.timestamp   -> ON_COOLDOWN

i.e. the comparison is strict: an expiry timestamp that is exactly equal to the action's timestamp
means the cooldown has just elapsed and the action is NOT on cooldown (this is also asserted directly
in tests/test_resolver.py's "cooldown expiring exactly at the action timestamp" boundary case, which
this module backs). §10.3 step 10 (PARRY) additionally specifies that a successful parry adds
`stagger_ms` to *every* currently-running cooldown of the attacker, and that an already-expired
cooldown is not revived by the stagger — both covered below. Also covers the §13 testing-checklist
bullet "cooldown expiry".
"""
from __future__ import annotations

from cg_comms.engine.cooldowns import add_stagger, is_on_cooldown, start_cooldown
from cg_comms.engine.state import PlayerState
from cg_comms.messages.enums import ActionClass

SWING = ActionClass.HORIZONTAL_SWING
THRUST = ActionClass.THRUST


def make_player() -> PlayerState:
    return PlayerState(player_id=1)


# --- start_cooldown: sets an expiry at_ts + cooldown_ms ---------------------------------------------

def test_start_cooldown_sets_expiry_timestamp():
    player = make_player()
    start_cooldown(player, SWING, at_ts=1000, cooldown_ms=800)
    assert player.cooldowns[SWING] == 1800


def test_start_cooldown_on_one_action_does_not_affect_another():
    player = make_player()
    start_cooldown(player, SWING, at_ts=1000, cooldown_ms=800)
    assert THRUST not in player.cooldowns
    assert is_on_cooldown(player, THRUST, at_ts=1000) is False


# --- is_on_cooldown: strict expiry > at_ts, per §10.3 step 6 ----------------------------------------

def test_is_on_cooldown_true_before_expiry():
    player = make_player()
    start_cooldown(player, SWING, at_ts=1000, cooldown_ms=800)  # expiry = 1800
    assert is_on_cooldown(player, SWING, at_ts=1799) is True


def test_is_on_cooldown_false_exactly_at_expiry():
    """§10.3 step 6 boundary: `cooldowns[action] > at_ts`, so an at_ts equal to the expiry must NOT
    be considered on cooldown (the cooldown has just elapsed)."""
    player = make_player()
    start_cooldown(player, SWING, at_ts=1000, cooldown_ms=800)  # expiry = 1800
    assert is_on_cooldown(player, SWING, at_ts=1800) is False


def test_is_on_cooldown_false_after_expiry():
    player = make_player()
    start_cooldown(player, SWING, at_ts=1000, cooldown_ms=800)  # expiry = 1800
    assert is_on_cooldown(player, SWING, at_ts=1801) is False


def test_is_on_cooldown_false_when_action_never_started():
    player = make_player()
    assert is_on_cooldown(player, SWING, at_ts=0) is False


# --- add_stagger: extends only currently-running cooldowns, does not revive expired ones ------------

def test_add_stagger_extends_running_cooldowns():
    player = make_player()
    start_cooldown(player, SWING, at_ts=1000, cooldown_ms=800)  # expiry = 1800, running at ts=1000
    add_stagger(player, stagger_ms=500, at_ts=1000)
    assert player.cooldowns[SWING] == 2300


def test_add_stagger_does_not_revive_an_already_expired_cooldown():
    player = make_player()
    start_cooldown(player, SWING, at_ts=1000, cooldown_ms=800)  # expiry = 1800
    add_stagger(player, stagger_ms=500, at_ts=2000)  # 2000 > 1800: already expired
    assert player.cooldowns[SWING] == 1800  # untouched, not extended


def test_add_stagger_boundary_exactly_at_expiry_is_treated_as_expired():
    """Consistency check: add_stagger's own `expiry > at_ts` guard uses the same strict comparison
    as is_on_cooldown, so a cooldown expiring exactly at `at_ts` must not be extended either."""
    player = make_player()
    start_cooldown(player, SWING, at_ts=1000, cooldown_ms=800)  # expiry = 1800
    add_stagger(player, stagger_ms=500, at_ts=1800)
    assert player.cooldowns[SWING] == 1800


def test_add_stagger_extends_every_running_cooldown_independently():
    player = make_player()
    start_cooldown(player, SWING, at_ts=1000, cooldown_ms=800)  # expiry = 1800, running
    start_cooldown(player, THRUST, at_ts=1000, cooldown_ms=200)  # expiry = 1200, already expired by 1500
    add_stagger(player, stagger_ms=500, at_ts=1500)
    assert player.cooldowns[SWING] == 2300  # running -> extended
    assert player.cooldowns[THRUST] == 1200  # expired -> untouched
