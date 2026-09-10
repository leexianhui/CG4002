"""HEARTBEAT send + liveness watchdog (COMMS_IMPLEMENTATION_PLAN.md §6).

Heartbeat is 1 Hz, best-effort; liveness timeout is 3x the interval. On timeout, the caller (
relay/session.py) marks the link stale and begins reconnect-with-backoff — this module only detects
the edge and reports it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import structlog

from cg_comms.clock import monotonic_ms

logger = structlog.get_logger(__name__)


@dataclass
class HeartbeatMonitor:
    """Tracks the last-received-heartbeat timestamp for one link and declares it stale after
    `interval_ms * liveness_timeout_factor` of silence."""

    interval_ms: int
    liveness_timeout_factor: int
    _last_rx_ms: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._last_rx_ms = monotonic_ms()

    def mark_received(self) -> None:
        self._last_rx_ms = monotonic_ms()

    def reset(self) -> None:
        """Restart the liveness clock. Call this when streaming begins, not at construction: the
        handshake's 8-sample time-sync burst (~800 ms at the specified 100 ms spacing) carries no
        heartbeats, so a monitor started at construction is already stale by the time the watchdog
        first polls, and every device would disconnect itself right after a successful handshake."""
        self._last_rx_ms = monotonic_ms()

    def is_stale(self) -> bool:
        return monotonic_ms() - self._last_rx_ms > self.interval_ms * self.liveness_timeout_factor


async def run_heartbeat_sender(
    send: Callable[[], Awaitable[None]],
    interval_ms: int,
    stop_event: asyncio.Event,
) -> None:
    """Sends a HEARTBEAT every `interval_ms` until `stop_event` is set."""
    while not stop_event.is_set():
        await send()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_ms / 1000)
        except asyncio.TimeoutError:
            pass


async def run_liveness_watchdog(
    monitor: HeartbeatMonitor,
    on_stale: Callable[[], Awaitable[None]],
    poll_interval_ms: int,
    stop_event: asyncio.Event,
) -> None:
    """Polls `monitor` and calls `on_stale()` once when it transitions to stale (edge-triggered, so
    the caller's reconnect logic runs exactly once per outage rather than on every poll tick)."""
    was_stale = False
    while not stop_event.is_set():
        stale = monitor.is_stale()
        if stale and not was_stale:
            logger.warning("heartbeat_stale")
            await on_stale()
        was_stale = stale
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_ms / 1000)
        except asyncio.TimeoutError:
            pass
