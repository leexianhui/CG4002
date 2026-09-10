"""Ultra96 client — line-delimited JSON over a TCP connection (COMMS_IMPLEMENTATION_PLAN.md §8).

At match time this is a direct connection to the shared Ultra96 over the venue WiFi (no tunnel — the
Ultra96 is whitelisted there). During development the same connection can instead be pointed at a
local SSH-tunnelled port while the Ultra96 sits on the NUS SoC Research Subnet; either way it's just a
different host/port in config, not a different code path.

One long-lived connection held open for the match. Swappable for mock/ultra96_sim.py purely via
config (host/port) — both speak the exact same line-delimited protocol, so relay code never knows
which one it is talking to.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import structlog

from cg_comms.messages.enums import ActionClass
from cg_comms.messages.models import ActionResult, SensorWindow
from cg_comms.messages.validate import MessageDropped, parse_app_message

logger = structlog.get_logger(__name__)


class Ultra96Unavailable(Exception):
    """Raised when a request cannot be sent because the connection is down."""


def passes_confidence_gate(result: ActionResult, min_confidence: float) -> bool:
    """Discards any ACTION_RESULT whose actionConfidence < minConfidence before it reaches the
    engine — except IDLE, which always passes (§8)."""
    if result.action == ActionClass.IDLE:
        return True
    return result.action_confidence >= min_confidence


@dataclass
class Ultra96Client:
    host: str
    port: int
    request_timeout_ms: int
    max_in_flight: int
    _reader: asyncio.StreamReader | None = field(default=None, init=False)
    _writer: asyncio.StreamWriter | None = field(default=None, init=False)
    _pending: dict[int, "asyncio.Future[ActionResult]"] = field(default_factory=dict, init=False)
    _in_flight: asyncio.Semaphore = field(init=False)
    _reader_task: "asyncio.Task[None] | None" = field(default=None, init=False)
    timeouts: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._in_flight = asyncio.Semaphore(self.max_in_flight)

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        self._reader_task = asyncio.create_task(self._read_loop())

    async def disconnect(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        self._reader = None
        self._writer = None
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            line = await self._reader.readline()
            if not line:
                logger.warning("ultra96_connection_closed")
                return
            parsed = parse_app_message(line.decode("utf-8").strip())
            if isinstance(parsed, MessageDropped):
                logger.warning("ultra96_bad_message", reason=parsed.reason.value, detail=parsed.detail)
                continue
            if not isinstance(parsed, ActionResult):
                logger.warning("ultra96_unexpected_type", type=type(parsed).__name__)
                continue
            future = self._pending.pop(parsed.request_id, None)
            if future is not None and not future.done():
                future.set_result(parsed)

    async def request(self, window: SensorWindow) -> ActionResult | None:
        """Sends `window` and awaits its correlated ACTION_RESULT. Returns None on timeout — the
        request is not retried, since by the time a retry would land a newer window already exists
        (§8)."""
        if self._writer is None:
            raise Ultra96Unavailable("not connected")

        async with self._in_flight:
            future: "asyncio.Future[ActionResult]" = asyncio.get_running_loop().create_future()
            self._pending[window.request_id] = future
            line = window.model_dump_json(by_alias=True) + "\n"
            self._writer.write(line.encode("utf-8"))
            await self._writer.drain()
            try:
                return await asyncio.wait_for(future, timeout=self.request_timeout_ms / 1000)
            except asyncio.TimeoutError:
                self.timeouts += 1
                logger.warning("ultra96_timeout", request_id=window.request_id)
                return None
            finally:
                self._pending.pop(window.request_id, None)
