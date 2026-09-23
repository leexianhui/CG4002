"""Recovering classifier TCP client. Requests are bounded and never replayed after an outage."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import structlog

from cg_comms.clock import monotonic_ms
from cg_comms.messages.enums import ActionClass
from cg_comms.messages.models import ActionResult, SensorWindow
from cg_comms.messages.validate import MessageDropped, parse_app_message
from cg_comms.relay.reconnect import ReconnectWorker

logger = structlog.get_logger(__name__)


class Ultra96Unavailable(Exception):
    """The current window cannot be classified; a future window may try again."""


def passes_confidence_gate(result: ActionResult, min_confidence: float) -> bool:
    return result.action == ActionClass.IDLE or result.action_confidence >= min_confidence


@dataclass
class Ultra96Client:
    host: str
    port: int
    request_timeout_ms: int
    max_in_flight: int
    _writer: asyncio.StreamWriter | None = field(default=None, init=False)
    _pending: dict[int, asyncio.Future[ActionResult]] = field(default_factory=dict, init=False)
    _worker: ReconnectWorker = field(init=False)
    timeouts: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._worker = ReconnectWorker("classifier", self._serve)

    def start(self) -> None:
        self._worker.start()

    @property
    def is_connected(self) -> bool:
        return self._worker.connected.is_set()

    async def connect(self) -> None:
        self.start()
        try:
            await asyncio.wait_for(self._worker.connected.wait(), 10)
        except BaseException:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        await self._worker.stop()

    async def _serve(self) -> None:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 5)
        self._writer = writer
        self._worker.connected.set()
        logger.info("classifier_connected", host=self.host, port=self.port)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    raise ConnectionError("classifier EOF")
                if not line.endswith(b"\n"):
                    raise ConnectionError("classifier unterminated line")
                parsed = parse_app_message(line.decode("utf-8").strip())
                if isinstance(parsed, MessageDropped) or not isinstance(parsed, ActionResult):
                    logger.warning("ultra96_bad_message")
                    continue
                future = self._pending.get(parsed.request_id)
                if future is not None and not future.done():
                    future.set_result(parsed)
        finally:
            self._writer = None
            self._worker.connected.clear()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(Ultra96Unavailable("classifier disconnected"))
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (OSError, asyncio.TimeoutError):
                pass

    async def request(self, window: SensorWindow) -> ActionResult | None:
        writer = self._writer
        if writer is None or not self._worker.connected.is_set():
            raise Ultra96Unavailable("classifier offline")
        if len(self._pending) >= self.max_in_flight:
            raise Ultra96Unavailable("classifier capacity")
        if window.request_id in self._pending:
            raise Ultra96Unavailable("duplicate request ID")
        future: asyncio.Future[ActionResult] = asyncio.get_running_loop().create_future()
        self._pending[window.request_id] = future
        loop = asyncio.get_running_loop()
        sent_at: float | None = None
        phase = "scheduled"
        encode_ms = write_drain_ms = 0.0
        request_bytes = 0

        async def exchange() -> ActionResult:
            nonlocal sent_at, phase, encode_ms, write_drain_ms, request_bytes
            phase = "encoding"
            began = loop.time()
            payload = (window.model_dump_json(by_alias=True, exclude={"demo_trace_id"} if window.demo_trace_id is None else set()) + "\n").encode()
            request_bytes = len(payload)
            encode_ms = (loop.time() - began) * 1000
            phase = "write_drain"
            began = loop.time()
            writer.write(payload)
            await writer.drain()
            sent_at = loop.time()
            write_drain_ms = (sent_at - began) * 1000
            phase = "response_wait"
            return await future

        started = loop.time()

        def timing() -> dict[str, Any]:
            now = loop.time()
            return dict(player_id=window.player_id, request_id=window.request_id,
                        window_timestamp_ms=window.timestamp,
                        elapsed_ms=round((now - started) * 1000, 3),
                        encode_ms=round(encode_ms, 3), write_drain_ms=round(write_drain_ms, 3),
                        response_wait_ms=(round((now - sent_at) * 1000, 3)
                                          if sent_at is not None else None),
                        window_age_ms=monotonic_ms() - window.timestamp,
                        request_bytes=request_bytes, timeout_ms=self.request_timeout_ms)

        try:
            logger.info("classifier_request", player_id=window.player_id,
                        request_id=window.request_id, trigger_id=window.trigger_id,
                        window_timestamp_ms=window.timestamp,
                        window_age_ms=monotonic_ms() - window.timestamp,
                        timeout_ms=self.request_timeout_ms)
            # Measure against the same monotonic interval as wait_for; logging before admission
            # and cross-machine wall-clock offsets are not part of this RTT measurement.
            started = loop.time()
            result = await asyncio.wait_for(exchange(), self.request_timeout_ms / 1000)
            if (result.player_id != window.player_id or result.timestamp != window.timestamp
                    or result.demo_trace_id != window.demo_trace_id):
                raise Ultra96Unavailable("classifier correlation mismatch")
            logger.info("classifier_response", **timing())
            return result
        except asyncio.TimeoutError:
            self.timeouts += 1
            metrics = timing()
            logger.warning("ultra96_timeout", **metrics, phase=phase,
                           deadline_overrun_ms=round(max(0, metrics["elapsed_ms"]
                                                        - self.request_timeout_ms), 3))
            writer.close()
            return None
        except (OSError, ConnectionError) as exc:
            writer.close()
            raise Ultra96Unavailable(str(exc)) from exc
        finally:
            self._pending.pop(window.request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()
