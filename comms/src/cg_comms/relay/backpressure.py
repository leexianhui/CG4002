"""Bounded drop-oldest queue used at every stage of the pipeline (COMMS_IMPLEMENTATION_PLAN.md §6).

A slow consumer must never grow memory without bound or apply backpressure upstream — it drops the
oldest queued item and keeps a count so the drop is visible in the heartbeat log line, never silent.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class DropOldestQueue(Generic[T]):
    maxsize: int
    dropped: int = field(default=0, init=False)
    _queue: "asyncio.Queue[T]" = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._queue = asyncio.Queue(maxsize=self.maxsize)

    def put_nowait(self, item: T) -> None:
        """Enqueues `item`, dropping the oldest queued item first if full."""
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    continue

    async def get(self) -> T:
        return await self._queue.get()

    def qsize(self) -> int:
        return self._queue.qsize()
