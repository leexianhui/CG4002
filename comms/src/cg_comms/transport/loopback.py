"""In-process transport pair — no I/O. Used by mock/firebeetle_sim.py paired against a relay
session (and by any future test) to exercise the entire stack without a radio.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from cg_comms.transport.base import ITransport, TransportClosed


class LoopbackTransport(ITransport):
    """One endpoint of an in-process pair. Construct both ends with `LoopbackTransport.pair()`."""

    def __init__(
        self,
        inbox: "asyncio.Queue[bytes | None]",
        outbox: "asyncio.Queue[bytes | None]",
        mtu: int = 244,
    ) -> None:
        self._inbox = inbox
        self._outbox = outbox
        self._mtu = mtu
        self._closed = False

    @staticmethod
    def pair(mtu: int = 244) -> tuple["LoopbackTransport", "LoopbackTransport"]:
        a_to_b: "asyncio.Queue[bytes | None]" = asyncio.Queue()
        b_to_a: "asyncio.Queue[bytes | None]" = asyncio.Queue()
        return LoopbackTransport(b_to_a, a_to_b, mtu), LoopbackTransport(a_to_b, b_to_a, mtu)

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        if not self._closed:
            self._closed = True
            await self._outbox.put(None)

    async def send(self, frame: bytes) -> None:
        if self._closed:
            raise TransportClosed("loopback transport closed")
        await self._outbox.put(frame)

    async def frames(self) -> AsyncIterator[bytes]:
        while True:
            item = await self._inbox.get()
            if item is None:
                return
            yield item

    @property
    def mtu(self) -> int:
        return self._mtu
