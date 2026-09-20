"""Regression test for the relay<->engine cross-process clock-mismatch bug.

InboundAction/InboundPosition.timestamp are "engine time" only from the sender's own point of
view: relay/timesync.py anchors ACTION timestamps to the *relay laptop's* time.monotonic(), and
POSITION timestamps come from whatever process runs the visualizer -- both independent of this
engine process's own monotonic_ms() (clock.py). In real deployment (relay laptop + Ultra96 board,
per DEMO_RUNBOOK.md) these three clocks share no common origin.

Before the fix in GameStateTask._anchor, ReorderBuffer.push/drain_ready compared an incoming
relay-anchored timestamp directly against this process's own monotonic_ms(). A laptop with days of
uptime (e.g. offset_ms ~292_308_359 observed on a real rig) makes every incoming timestamp look
"already released" or "impossibly old" relative to the engine's own near-zero monotonic clock,
so `now_engine_ms - timestamp` is deeply negative in both checks: items are neither dropped as
late nor ever released as ready. They pile up in the reorder heap forever with zero logging (
ReorderBuffer.dropped_late is only incremented on the "too late" branch, which never fires) --
a demo that "looks dead" despite the relay classifying every action correctly.

test_e2e_mock.py and test_soak.py never caught this because both run the relay Session and the
engine's GameStateTask/run_engine via asyncio.create_task in the SAME process, so they share one
time.monotonic() and the mismatch cannot occur. This test constructs GameStateTask directly and
feeds it InboundAction/InboundPosition events whose embedded timestamps are deliberately offset by
a large constant from monotonic_ms(), to prove combat still resolves under an arbitrary clock skew.
"""
from __future__ import annotations

import asyncio

import pytest

from cg_comms.clock import monotonic_ms
from cg_comms.config import RulesConfig, WeaponMove
from cg_comms.engine.game_state_task import GameStateTask
from cg_comms.engine.inbound import InboundAction, InboundPosition, InboundWeaponChanged
from cg_comms.engine.rules import Rules
from cg_comms.messages.enums import ActionClass, GuardState, MatchPhase, Weapon
from cg_comms.messages.models import Action, Position, WeaponChanged

# A multi-day laptop uptime, same order of magnitude as the offset_ms observed on the real rig
# that surfaced this bug (~292_308_359 ms).
HUGE_RELAY_CLOCK_SKEW_MS = 300_000_000
# A different, unrelated skew for the visualizer/POSITION channel -- proves per-channel anchoring,
# not just a single global offset applied everywhere.
DIFFERENT_VISUALIZER_CLOCK_SKEW_MS = 50_000_000

ATTACKER_ID = 1
DEFENDER_ID = 2


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
        reorder_delay_ms=60,
    )
    return Rules(config=config)


def make_task() -> GameStateTask:
    async def _noop_event(_event):
        return None

    async def _noop_state(_state):
        return None

    async def _noop_feedback(_player_id, _feedback):
        return None

    async def _noop_action_recognized(_event):
        return None

    def _no_presence() -> dict[str, bool]:
        return {}

    task = GameStateTask(
        rules=make_rules(),
        reorder_delay_ms=60,
        late_drop_ms=500,
        on_combat_event=_noop_event,
        on_match_state=_noop_state,
        on_combat_feedback=_noop_feedback,
        on_action_recognized=_noop_action_recognized,
        presence=_no_presence,
    )
    task.state.phase = MatchPhase.IN_PROGRESS
    return task


def make_action(*, player_id: int, action: ActionClass, relay_ts: int, weapon=Weapon.SWORD) -> InboundAction:
    return InboundAction(
        player_id=player_id,
        action=Action(
            timestamp=relay_ts,
            playerId=player_id,
            weapon=weapon,
            action=action,
            actionConfidence=0.9,
            guardState=GuardState.IDLE,
            guardConfidence=0.9,
        ),
    )


def make_position(*, player_id: int, x: float, y: float, heading: float, visualizer_ts: int) -> InboundPosition:
    return InboundPosition(
        player_id=player_id,
        position=Position(timestamp=visualizer_ts, playerId=player_id, x=x, y=y, heading=heading),
    )


async def _run_until_resolved(task: GameStateTask, *, poll_ms: int = 5, max_polls: int = 30) -> None:
    """Drains the reorder buffer until it's empty, standing in for GameStateTask.run's loop
    without needing a real stop_event/background task."""
    for _ in range(max_polls):
        await task._drain_ready()
        if len(task._reorder) == 0:
            return
        await asyncio.sleep(poll_ms / 1000)
    raise AssertionError("reorder buffer never drained -- items are stuck (the bug this test guards)")


@pytest.mark.asyncio
async def test_action_resolves_despite_huge_relay_clock_skew() -> None:
    """The core regression: a relay-anchored timestamp offset by ~300M ms (day-scale laptop
    uptime) from this process's own monotonic_ms() must not permanently strand the action in
    ReorderBuffer -- it must still resolve once anchored to engine-local time."""
    task = make_task()
    now = monotonic_ms()
    relay_now = now + HUGE_RELAY_CLOCK_SKEW_MS

    task._absorb(
        make_position(player_id=ATTACKER_ID, x=0.0, y=0.0, heading=0.0, visualizer_ts=now)
    )
    task._absorb(
        make_position(player_id=DEFENDER_ID, x=2.0, y=0.0, heading=180.0, visualizer_ts=now)
    )
    task._absorb(
        InboundWeaponChanged(
            player_id=ATTACKER_ID,
            weapon_changed=WeaponChanged(timestamp=relay_now, playerId=ATTACKER_ID, weapon=Weapon.SWORD),
        )
    )
    task._absorb(
        make_action(player_id=ATTACKER_ID, action=ActionClass.HORIZONTAL_SWING, relay_ts=relay_now)
    )

    assert len(task._reorder) == 1, "action should be buffered, not dropped, despite the clock skew"
    await _run_until_resolved(task)

    assert task.state.players[DEFENDER_ID].hp == 90, "attack should have resolved as a full HIT"


@pytest.mark.asyncio
async def test_action_and_position_channels_anchor_independently() -> None:
    """ACTION arrives via the relay link, POSITION via the visualizer link -- a different sender
    that may run on yet another machine with its own unrelated clock offset. Anchoring one channel
    must not affect the other's offset."""
    task = make_task()
    now = monotonic_ms()

    task._absorb(
        make_position(
            player_id=ATTACKER_ID, x=0.0, y=0.0, heading=0.0,
            visualizer_ts=now + DIFFERENT_VISUALIZER_CLOCK_SKEW_MS,
        )
    )
    task._absorb(
        make_position(
            player_id=DEFENDER_ID, x=2.0, y=0.0, heading=180.0,
            visualizer_ts=now + DIFFERENT_VISUALIZER_CLOCK_SKEW_MS,
        )
    )
    task._absorb(
        InboundWeaponChanged(
            player_id=ATTACKER_ID,
            weapon_changed=WeaponChanged(
                timestamp=now + HUGE_RELAY_CLOCK_SKEW_MS, playerId=ATTACKER_ID, weapon=Weapon.SWORD
            ),
        )
    )
    task._absorb(
        make_action(
            player_id=ATTACKER_ID,
            action=ActionClass.HORIZONTAL_SWING,
            relay_ts=now + HUGE_RELAY_CLOCK_SKEW_MS,
        )
    )

    assert len(task._reorder) == 1
    await _run_until_resolved(task)
    assert task.state.players[DEFENDER_ID].hp == 90


@pytest.mark.asyncio
async def test_dropped_late_is_logged(caplog) -> None:
    """An action that is genuinely too late (even after anchoring) must still be dropped and
    logged -- ReorderBuffer.dropped_late used to be incremented and never surfaced anywhere."""
    import structlog

    task = make_task()
    now = monotonic_ms()

    # Seed the anchor with a first, on-time action so the offset is known...
    task._absorb(make_action(player_id=ATTACKER_ID, action=ActionClass.IDLE, relay_ts=now))
    await task._drain_ready()

    # ...then a second action on the same relay clock, but far enough behind (in relay-clock terms)
    # to be genuinely late once anchored: reorder_delay_ms(60) + late_drop_ms(500) = 560.
    late_relay_ts = now - 600
    with structlog.testing.capture_logs() as logs:
        task._absorb(make_action(player_id=ATTACKER_ID, action=ActionClass.HORIZONTAL_SWING, relay_ts=late_relay_ts))

    assert task._reorder.dropped_late == 1
    assert any(entry.get("event") == "action_dropped_late" for entry in logs)
