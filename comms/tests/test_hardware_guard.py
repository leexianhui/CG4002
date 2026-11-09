"""Hardware guard (left-wrist pose -> hub GUARD_EVENT -> relay -> engine) and the hub's swing buzz.

Rules under test, shared/config/match.yaml (2026-11-09): a guard stays up guard_duration_ms (3 s)
from when it is raised, blocks every hit in that window for 0 damage from any direction, and an
attack by the guarding player drops it. The relay sends ACTION_CONFIRMED back to the hub only when
the classifier names a real move for a hub segment and it clears the confidence gate.
"""

import asyncio
from unittest.mock import Mock

import pytest
from test_pre_demo_combat import action, position, setup_task
from test_segments import frame, segment_chunks, session

from cg_comms.engine.rules import Rules
from cg_comms.messages.enums import ActionClass, CombatOutcome, FeedbackCode, GuardState, Weapon
from cg_comms.messages.models import ActionResult
from cg_comms.protocol.constants import PacketType


async def play(monkeypatch, events, until):
    """Feeds events in real time: the clock reaches each event's timestamp as it arrives (the
    engine refuses future-stamped events), draining as it goes, then runs on to `until`."""
    task, clock, recognized, combat = setup_task(monkeypatch)
    task.rules = Rules.load()  # the real match.yaml
    for event in events:
        message = getattr(event, "action", None) or getattr(event, "position", None)
        clock[0] = max(clock[0], message.timestamp)
        task._absorb(event)
        await task._drain_ready()
    clock[0] = until
    await task._drain_ready()
    return task, clock, recognized, combat


def attack_at(ts, attacker=1):
    """Fresh positions just before `ts` (position_max_age_ms) and one sword swing at `ts`."""
    return [position(1, ts - 20, 0, 0), position(2, ts - 20, 1, 180),
            action(attacker, ts, move="HORIZONTAL_SWING")]


def test_match_rules_guard_three_seconds_zero_damage_any_direction():
    rules = Rules.load().config
    assert (rules.guard_duration_ms, rules.block_multiplier, rules.guard_arc_deg) == (3000, 0.0, 360)


@pytest.mark.parametrize("attack_ts,outcome,damage", [
    (1500, "BLOCK", 0),   # inside the window
    (4000, "BLOCK", 0),   # exactly 3 s after the guard went up: still up (inclusive)
    (4001, "HIT", 10),    # 1 ms later the guard has dropped
])
async def test_guard_blocks_all_damage_for_three_seconds(monkeypatch, attack_ts, outcome, damage):
    _, _, recognized, combat = await play(
        monkeypatch, [action(2, 1000, guard="GUARDING")] + attack_at(attack_ts), attack_ts + 400)
    assert (combat[-1].outcome, combat[-1].damage) == (CombatOutcome(outcome), damage)
    assert combat[-1].defender_hp == 100 - damage
    # The guard itself reaches the phones as a guard-only ACTION_RECOGNIZED.
    assert any(r.player_id == 2 and r.guard_state is GuardState.GUARDING for r in recognized)


async def test_attacking_drops_your_own_guard(monkeypatch):
    events = ([action(2, 1000, guard="GUARDING")] + attack_at(1500, attacker=2)
              + attack_at(2000, attacker=1))
    _, _, _, combat = await play(monkeypatch, events, 2400)
    assert combat[-1].attacker_id == 1
    assert (combat[-1].outcome, combat[-1].damage) == (CombatOutcome.HIT, 10)


async def test_a_repeat_guard_does_not_extend_but_a_new_one_after_expiry_does(monkeypatch):
    # Raised again while still up (3500): the window still ends at 4000.
    _, _, _, combat = await play(
        monkeypatch, [action(2, 1000, guard="GUARDING"), action(2, 3500, guard="GUARDING")]
        + attack_at(4100), 4500)
    assert combat[-1].outcome is CombatOutcome.HIT
    # Raised again after it dropped (5000): a fresh 3 s window.
    _, _, _, combat = await play(
        monkeypatch, [action(2, 1000, guard="GUARDING"), action(2, 5000, guard="GUARDING")]
        + attack_at(7900), 8300)
    assert (combat[-1].outcome, combat[-1].damage) == (CombatOutcome.BLOCK, 0)


async def test_phones_see_the_guard_drop_after_three_seconds(monkeypatch):
    task, _, _, _ = await play(monkeypatch, [action(2, 1000, guard="GUARDING")], 1400)

    def guard_at(now):
        snap = task.presentation.snapshot(task.state, task.rules, {}, now)
        return next(p.guard_state for p in snap.players if p.player_id == 2)

    assert guard_at(3999) is GuardState.GUARDING
    assert guard_at(4001) is GuardState.IDLE


# -- relay -------------------------------------------------------------------------------------------


def guard_frame(state, ts, seq, weapon=2):
    return frame({"device_timestamp_ms": ts, "guard_state": state, "weapon_id": weapon,
                  "reserved": 0}, seq, kind=PacketType.GUARD_EVENT)


async def test_relay_forwards_guard_up_as_guard_only_action_and_acks_every_edge():
    subject = session()
    await subject._handle_frame(guard_frame(1, 5000, seq=1))
    await subject._handle_frame(guard_frame(0, 5800, seq=2))  # release: logged, not forwarded
    await subject._handle_frame(guard_frame(1, 5400, seq=3))  # an older edge arriving late
    assert subject._send_ack.await_count == 3
    assert subject.on_action_result.await_count == 1
    sent = subject.on_action_result.await_args.args[0]
    assert (sent.action, sent.guard_state, sent.weapon) == (
        ActionClass.IDLE, GuardState.GUARDING, Weapon.SPEAR)
    assert (sent.action_confidence, sent.guard_confidence, sent.player_id) == (1.0, 1.0, 1)


@pytest.mark.parametrize("action,confidence,buzz", [
    ("VERTICAL_SWING", .9, True),
    ("IDLE", .9, False),            # the hub thought it was an action, the AI did not
    ("VERTICAL_SWING", .5, False),  # below the 0.6 gate: never reaches the game, so no buzz
])
async def test_hub_buzzes_only_for_a_confirmed_non_idle_action(action, confidence, buzz):
    subject = session()
    subject.queue_combat_feedback = Mock()

    async def classify(window):
        return ActionResult(timestamp=window.timestamp, player_id=1, request_id=window.request_id,
                            action=action, action_confidence=confidence, guard_state="IDLE",
                            guard_confidence=0)

    subject.ultra96.request = classify
    chunks, _ = segment_chunks(41, 75, weapon=2, first_ms=5000)
    for i, chunk in enumerate(chunks):
        await subject._handle_frame(frame(chunk, seq=i + 1))
    await asyncio.gather(*subject._classification_tasks)
    assert subject.queue_combat_feedback.called is buzz
    if buzz:
        feedback = subject.queue_combat_feedback.call_args.args[0]
        assert (feedback.code, feedback.player_id) == (FeedbackCode.ACTION_CONFIRMED, 1)
