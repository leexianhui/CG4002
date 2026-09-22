"""Timestamp-ordered release buffer (COMMS_IMPLEMENTATION_PLAN.md §10.2).

Events arrive in *arrival* order but must be processed in *engine-time* order, or the parry window
ends up decided by network jitter rather than by what the players did. Held in a min-heap keyed on
timestamp; released once `now_engine - timestamp > reorder_delay_ms`. Reject events behind an
already released timestamp, beyond the age/future limits, or over capacity, and return the reason.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(order=True)
class _HeapEntry(Generic[T]):
    timestamp: int
    priority: int
    seq: int
    item: T = field(compare=False)


@dataclass
class ReorderBuffer(Generic[T]):
    reorder_delay_ms: int
    late_drop_ms: int
    dropped_late: int = field(default=0, init=False)
    _heap: list = field(default_factory=list, init=False)
    _seq_counter: int = field(default=0, init=False)
    _released_ts: int = field(default=-1, init=False)
    max_events: int = 2048

    @property
    def released_timestamp(self) -> int:
        return self._released_ts

    def push(self, timestamp: int, item: T, now_engine_ms: int, priority: int = 0) -> str | None:
        """Buffer an event, returning its rejection reason or None when accepted."""
        reason = None
        if timestamp < self._released_ts:
            reason = "before_release_watermark"
        elif now_engine_ms - timestamp > self.reorder_delay_ms + self.late_drop_ms:
            reason = "max_age_exceeded"
        elif timestamp > now_engine_ms + 2000:
            reason = "future_timestamp"
        elif len(self._heap) >= self.max_events:
            reason = "buffer_full"
        if reason is not None:
            self.dropped_late += 1
            return reason
        heapq.heappush(self._heap, _HeapEntry(timestamp, priority, self._seq_counter, item))
        self._seq_counter += 1
        return None

    def drain_ready(self, now_engine_ms: int) -> list[T]:
        """Pops and returns every buffered item whose timestamp is now older than
        reorder_delay_ms, in timestamp order."""
        ready: list[T] = []
        while self._heap and now_engine_ms - self._heap[0].timestamp > self.reorder_delay_ms:
            entry = heapq.heappop(self._heap)
            self._released_ts = entry.timestamp
            ready.append(entry.item)
        return ready

    def pending(self) -> list[T]:
        return [entry.item for entry in sorted(self._heap)]

    def __len__(self) -> int:
        return len(self._heap)
