"""Unit tests for engine/reorder.py — the timestamp-ordered release buffer.

Derived from COMMS_IMPLEMENTATION_PLAN.md §10.2 ("hold every inbound event in a min-heap keyed on
timestamp and release it only once `now_engine - timestamp > reorderDelayMs` (default 60); drop
anything arriving more than `lateDropMs: 500` behind the release watermark, and count it") and
cg_comms.config.EngineSettings, whose `reorder_delay_ms`/`late_drop_ms` defaults must match those
numbers (reorder_delay_ms is also pinned in shared/config/match.yaml). This backs the reorder-buffer
behavior called out in §13's testing checklist alongside cooldown expiry and windowing edge cases,
even though §13's bullet list does not name it explicitly by string — §10.2's "do not skip this"
framing and the resolver's reliance on trustworthy timestamps make it a de facto required unit under
test.

On the "arriving more than lateDropMs behind the release watermark" drop condition: for an item with
timestamp T pushed when the buffer's clock has reached now_engine, the release watermark for a
hypothetical item at the current instant is `now_engine - reorder_delay_ms`; being more than
late_drop_ms behind that watermark means `now_engine - T > reorder_delay_ms + late_drop_ms`, which is
exactly ReorderBuffer.push's drop condition. Tests below derive expected numbers from that reading.
"""
from __future__ import annotations

import pytest

from cg_comms.config import EngineSettings
from cg_comms.engine.reorder import ReorderBuffer

REORDER_DELAY_MS = 60
LATE_DROP_MS = 500


def make_buffer(**overrides) -> ReorderBuffer:
    defaults = dict(reorder_delay_ms=REORDER_DELAY_MS, late_drop_ms=LATE_DROP_MS)
    defaults.update(overrides)
    return ReorderBuffer(**defaults)


# --- config parity: §10.2's stated defaults ---------------------------------------------------------

def test_config_defaults_match_plan_ss10_2():
    settings = EngineSettings()
    assert settings.reorder_delay_ms == REORDER_DELAY_MS
    assert settings.late_drop_ms == LATE_DROP_MS


# --- hold-until-delay-elapses release behavior -------------------------------------------------------

def test_item_held_until_reorder_delay_elapses():
    buf = make_buffer()
    buf.push(timestamp=1000, item="a", now_engine_ms=1000)
    assert len(buf) == 1
    assert buf.drain_ready(now_engine_ms=1050) == []  # 1050-1000=50, not > 60
    assert len(buf) == 1


def test_item_released_once_strictly_past_reorder_delay():
    buf = make_buffer()
    buf.push(timestamp=1000, item="a", now_engine_ms=1000)
    assert buf.drain_ready(now_engine_ms=1061) == ["a"]  # 1061-1000=61 > 60
    assert len(buf) == 0


def test_release_boundary_exactly_at_reorder_delay_is_not_yet_released():
    """Boundary: release requires `now_engine - timestamp > reorder_delay_ms`, strictly. A gap of
    exactly reorder_delay_ms must not release yet."""
    buf = make_buffer()
    buf.push(timestamp=1000, item="a", now_engine_ms=1000)
    assert buf.drain_ready(now_engine_ms=1060) == []  # 1060-1000=60, not > 60
    assert buf.drain_ready(now_engine_ms=1061) == ["a"]  # one ms later, now released


# --- release order follows timestamp, not arrival/push order ----------------------------------------

def test_items_released_in_timestamp_order_regardless_of_push_order():
    buf = make_buffer()
    # Each pushed with now_engine_ms == its own timestamp (gap 0) so none trip the late-drop check;
    # only the push *order* is scrambled relative to timestamp order.
    buf.push(timestamp=3000, item="third", now_engine_ms=3000)
    buf.push(timestamp=1000, item="first", now_engine_ms=1000)
    buf.push(timestamp=2000, item="second", now_engine_ms=2000)
    released = buf.drain_ready(now_engine_ms=10_000)  # far past every item's release point
    assert released == ["first", "second", "third"]
    assert len(buf) == 0


def test_drain_only_releases_items_whose_delay_has_elapsed_so_far():
    buf = make_buffer()
    buf.push(timestamp=1000, item="early", now_engine_ms=1000)
    buf.push(timestamp=1100, item="late", now_engine_ms=1000)
    # Only "early" (pushed 61ms behind now) has crossed the reorder delay; "late" has not (1061-1100<0).
    released = buf.drain_ready(now_engine_ms=1061)
    assert released == ["early"]
    assert len(buf) == 1


# --- late-drop: arriving far enough behind is dropped rather than buffered stale --------------------

def test_item_dropped_when_arriving_beyond_late_drop_window():
    buf = make_buffer()
    # now_engine - timestamp = 561 > reorder_delay_ms(60) + late_drop_ms(500) = 560 -> dropped.
    buf.push(timestamp=1000, item="ancient", now_engine_ms=1561)
    assert len(buf) == 0
    assert buf.dropped_late == 1
    assert buf.drain_ready(now_engine_ms=1_000_000) == []  # never enters the heap at all


def test_late_drop_boundary_exactly_at_threshold_is_not_dropped():
    """Boundary: the drop condition is `now_engine - timestamp > reorder_delay_ms + late_drop_ms`,
    strictly. A gap of exactly reorder_delay_ms + late_drop_ms (560) must still be buffered."""
    buf = make_buffer()
    buf.push(timestamp=1000, item="just_in_time", now_engine_ms=1560)
    assert len(buf) == 1
    assert buf.dropped_late == 0
    # It is already well past its own release point, so it drains immediately.
    assert buf.drain_ready(now_engine_ms=1560) == ["just_in_time"]


def test_dropped_late_counter_only_increments_on_actual_drops():
    buf = make_buffer()
    buf.push(timestamp=1000, item="fine", now_engine_ms=1000)  # not late
    buf.drain_ready(now_engine_ms=1000)  # nothing ready yet, no drop either
    assert buf.dropped_late == 0
    buf.push(timestamp=1000, item="ancient", now_engine_ms=2000)  # 1000 > 560 -> dropped
    assert buf.dropped_late == 1


@pytest.mark.parametrize("gap", [561, 700, 10_000])
def test_various_gaps_beyond_threshold_are_all_dropped(gap):
    buf = make_buffer()
    buf.push(timestamp=1000, item="x", now_engine_ms=1000 + gap)
    assert buf.dropped_late == 1
    assert len(buf) == 0
