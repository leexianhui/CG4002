"""Reproduce the sustained 1230 ms POSITION offset seen in the September 30 log."""

import pytest
import structlog
from test_game_state_task_clock_skew import make_action, make_position, make_task

from cg_comms.engine import game_state_task
from cg_comms.messages.enums import ActionClass


@pytest.mark.parametrize("clock_step_ms", [0, -1230, 3000])
async def test_position_clock_step_blocks_combat_but_monotonic_clock_keeps_damage(
    monkeypatch, clock_step_ms,
):
    task = make_task()
    task._reorder.reorder_delay_ms = 650
    now = [10000]
    monkeypatch.setattr(game_state_task, "monotonic_ms", lambda: now[0])
    recognized = []

    async def recognize(event):
        recognized.append(event)

    task.on_action_recognized = recognize

    def position(pid, step=0):
        return make_position(player_id=pid, x=pid - 1, y=0, heading=0,
                             visualizer_ts=1_790_000_000_000 + now[0] + step)

    for pid in (1, 2):
        task._absorb(position(pid))
    task._absorb(make_action(player_id=1, action=ActionClass.IDLE, relay_ts=now[0]))
    with structlog.testing.capture_logs() as logs:
        for tick in range(1, 13):
            now[0] = 10000 + tick * 500
            task._absorb(position(1))
            task._absorb(position(2, clock_step_ms))
            await task._drain_ready()
        task._absorb(make_action(player_id=1, action=ActionClass.HORIZONTAL_SWING,
                                 relay_ts=now[0]))
        now[0] += 700
        await task._drain_ready()

    assert recognized[-1].action == ActionClass.HORIZONTAL_SWING
    if clock_step_ms == 0:
        assert task.state.players[2].hp == 90
        assert task._reorder.dropped_late == 0
    else:
        assert task.state.players[2].hp == 100
        ignored = next(row for row in logs if row["event"] == "action_not_resolved"
                       and row["action"] == "HORIZONTAL_SWING")
        assert ignored["reason"] == "stale_position"
        assert ignored["position_age_ms"] == {"1": 0, "2": 6000}
        assert ignored["position_max_age_ms"] == 1000
        warnings = [row for row in logs if row["event"] == "event_dropped"]
        assert len(warnings) == 2
        assert warnings[-1]["suppressed_since_last_warning"] == 9
        assert task._reorder.dropped_late == 12


async def test_missing_positions_are_explicit_in_action_log(monkeypatch):
    task = make_task()
    now = [10000]
    monkeypatch.setattr(game_state_task, "monotonic_ms", lambda: now[0])
    task._absorb(make_action(player_id=1, action=ActionClass.HORIZONTAL_SWING,
                             relay_ts=now[0]))
    now[0] += 700
    with structlog.testing.capture_logs() as logs:
        await task._drain_ready()
    ignored = next(row for row in logs if row["event"] == "action_not_resolved")
    assert ignored["reason"] == "missing_position"
    assert ignored["position_age_ms"] == {"1": None, "2": None}
