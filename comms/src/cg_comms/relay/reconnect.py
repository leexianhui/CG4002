"""One owned, cancellation-safe reconnect worker per upstream connection."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class ReconnectWorker:
    name: str
    serve: Callable[[], Awaitable[None]]
    backoff_s: tuple[float, ...] = (1, 2, 4, 8)
    task: asyncio.Task[None] | None = field(default=None, init=False)
    connected: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name=f"{self.name}-reconnect")

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self.connected.clear()

    async def _run(self) -> None:
        attempt = 0
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await self.serve()
            except Exception as exc:
                logger.warning(f"{self.name}_disconnected", error=str(exc))
            finally:
                self.connected.clear()
            if asyncio.get_running_loop().time() - started >= 10:
                attempt = 0
            delay = self.backoff_s[min(attempt, len(self.backoff_s) - 1)]
            attempt += 1
            logger.warning(f"{self.name}_reconnecting", delay_s=delay)
            await asyncio.sleep(delay)
