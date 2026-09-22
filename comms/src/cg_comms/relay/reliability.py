"""Streaming ACK + identical-ciphertext retransmission (relay COMBAT_FEEDBACK).
Firmware uses the matching mechanism for RFID_EVENT. HELLO/TIME_SYNC are instead bounded
request/response exchanges. SENSOR_FRAME/HEARTBEAT are best-effort; ACK is never ACKed.

Base timeout 200 ms, x2 backoff, max 5 attempts, +-20% jitter, then LinkDegraded.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import structlog

logger = structlog.get_logger(__name__)


class LinkDegraded(Exception):
    """Raised after a reliable packet exhausts every retransmit attempt without being ACKed."""

    def __init__(self, seq: int) -> None:
        super().__init__(f"seq={seq} exhausted retransmit attempts")
        self.seq = seq


@dataclass
class _PendingSend:
    seq: int
    attempt: int = 0
    ack_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class ReliabilitySender:
    """One instance per outbound direction. Call `send_reliable()` for each reliable packet; call
    `on_ack(seq)` whenever an ACK frame arrives for that direction."""

    send_frame: Callable[[bytes], Awaitable[None]]
    base_timeout_ms: int = 200
    backoff_factor: float = 2.0
    max_attempts: int = 5
    jitter_pct: float = 0.2
    _pending: dict[int, _PendingSend] = field(default_factory=dict, init=False)

    def on_ack(self, seq: int) -> None:
        pending = self._pending.get(seq)
        if pending is not None:
            pending.ack_event.set()

    async def send_reliable(self, seq: int, frame: bytes) -> None:
        """Sends `frame` and retries with exponential backoff until ACKed or attempts exhausted.
        Raises LinkDegraded on exhaustion rather than failing silently."""
        pending = _PendingSend(seq=seq)
        self._pending[seq] = pending
        try:
            timeout_ms = float(self.base_timeout_ms)
            for attempt in range(1, self.max_attempts + 1):
                pending.attempt = attempt
                await self.send_frame(frame)
                jitter = 1.0 + random.uniform(-self.jitter_pct, self.jitter_pct)
                try:
                    await asyncio.wait_for(pending.ack_event.wait(), timeout=(timeout_ms * jitter) / 1000)
                    return
                except asyncio.TimeoutError:
                    logger.warning("retransmit", seq=seq, attempt=attempt)
                    timeout_ms *= self.backoff_factor
            logger.error("link_degraded", seq=seq)
            raise LinkDegraded(seq)
        finally:
            self._pending.pop(seq, None)
