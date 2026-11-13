"""Weapon selection waits for the phone's calibration (2026-11-12).

Players handle the phone before the headset goes on, often in a weapon pose. So the hub may lock a
weapon only after that player's phone has calibrated (VISUALIZER_READY): the engine tells the relay
WEAPON_SELECT_ALLOWED / _BLOCKED (COMBAT_FEEDBACK codes 5/6, no buzz), the relay replays the latest
one to every new hub session, and the hub treats every pose as "no weapon" while blocked.
"""

import asyncio
from unittest.mock import AsyncMock, Mock

from test_segments import session

from cg_comms.engine.game_state_task import GameStateTask
from cg_comms.engine.inbound import LinkReset
from cg_comms.engine.rules import Rules
from cg_comms.messages.enums import FeedbackCode
from cg_comms.messages.models import CombatFeedback

ALLOWED, BLOCKED = FeedbackCode.WEAPON_SELECT_ALLOWED, FeedbackCode.WEAPON_SELECT_BLOCKED


def engine(require_ready=True, presence=None):
    sent = []

    async def feedback(player_id, message):
        assert message.player_id == player_id and message.intensity_pct == 0  # never a buzz
        sent.append((player_id, message.code))

    links = {"1": True, "2": True} if presence is None else presence
    noop = AsyncMock()
    task = GameStateTask(Rules.load(), 0, 10000, noop, noop, feedback, noop, lambda: links,
                         require_visualizer_ready=require_ready)
    return task, sent, links


async def test_blocked_until_that_players_phone_calibrates_then_follows_it():
    task, sent, _ = engine()
    await task._update_presentation()
    assert sent == [(1, BLOCKED), (2, BLOCKED)]
    task.presentation.ready[1] = True  # P1's phone: look-and-hold calibration done
    await task._update_presentation()
    await task._update_presentation()  # no repeats while nothing changes
    assert sent[2:] == [(1, ALLOWED)]
    task.presentation.ready[1] = False  # tracking lost > 3 s, or re-placing the dummy
    await task._update_presentation()
    assert sent[3:] == [(1, BLOCKED)]


async def test_a_reconnecting_relay_is_told_again():
    task, sent, _ = engine()
    task.presentation.ready[2] = True
    await task._update_presentation()
    stop = asyncio.Event()
    runner = asyncio.create_task(task.run(stop, drain_interval_ms=5))
    await task.submit(LinkReset(2, "action"))  # P2's relay connected again
    await asyncio.sleep(0.05)
    stop.set()
    await runner
    assert sent.count((2, ALLOWED)) == 2 and sent.count((1, BLOCKED)) == 1


async def test_nothing_is_sent_to_a_relay_that_is_not_connected():
    task, sent, links = engine(presence={"1": False, "2": True})
    await task._update_presentation()
    assert sent == [(2, BLOCKED)]
    links["1"] = True
    await task._update_presentation()
    assert sent == [(2, BLOCKED), (1, BLOCKED)]


async def test_without_phones_weapon_selection_is_always_allowed():
    task, sent, _ = engine(require_ready=False)
    await task._update_presentation()
    assert sent == [(1, ALLOWED), (2, ALLOWED)]


async def test_relay_replays_the_latest_selection_when_a_hub_session_starts_streaming():
    """The hub starts blocked after every boot or reconnect; the engine only speaks on change."""
    latest = CombatFeedback(timestamp=1, player_id=1, code=ALLOWED, intensity_pct=0,
                            duration_ms=1, repeat_count=1)
    subject = session()
    subject.startup_feedback = lambda: [latest]
    subject.queue_combat_feedback = Mock()
    subject._stop_event.set()  # stream starts, then stops at once
    await subject._stream()
    subject.queue_combat_feedback.assert_called_once_with(latest)


# -- match reset unlocks the hub's weapon (2026-11-13) ------------------------------------------------

UNLOCK = FeedbackCode.WEAPON_UNLOCK


async def test_reset_clears_weapons_and_unlocks_both_hubs_before_the_selection_state():
    from cg_comms.messages.enums import Weapon
    task, sent, _ = engine()
    for pid in (1, 2):
        task.presentation.ready[pid] = True
        task.state.players[pid].weapon = Weapon.SWORD
    await task._update_presentation()
    sent.clear()
    await task.reset_match()
    assert all(p.weapon is None for p in task.state.players.values())  # phones show weapon select
    await task._update_presentation()
    await task._update_presentation()  # sent once, not on every tick
    assert sent == [(1, UNLOCK), (2, UNLOCK)]


async def test_an_unlock_waits_for_a_relay_that_was_offline_at_reset():
    task, sent, links = engine(presence={"1": True, "2": False})
    await task.reset_match()
    await task._update_presentation()
    assert (1, UNLOCK) in sent and (2, UNLOCK) not in sent
    links["2"] = True  # P2's relay comes back
    await task._update_presentation()
    assert sent.count((2, UNLOCK)) == 1


async def test_relay_sends_a_pending_unlock_before_the_selection_state_when_the_hub_returns():
    """relay/app.py startup_feedback: an unlock that arrived while the hub was away goes first."""
    unlock = CombatFeedback(timestamp=1, player_id=1, code=UNLOCK, intensity_pct=0,
                            duration_ms=1, repeat_count=1)
    allowed = CombatFeedback(timestamp=2, player_id=1, code=ALLOWED, intensity_pct=0,
                             duration_ms=1, repeat_count=1)
    subject = session()
    subject.startup_feedback = lambda: [unlock, allowed]
    subject.queue_combat_feedback = Mock()
    subject._stop_event.set()
    await subject._stream()
    assert [c.args[0].code for c in subject.queue_combat_feedback.call_args_list] == [UNLOCK, ALLOWED]
