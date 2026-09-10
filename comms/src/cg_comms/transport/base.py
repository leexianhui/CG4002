"""ITransport ABC — the FireBeetle<->laptop link, abstracted over BLE / TCP / loopback.

This is the one abstraction with more than one implementation, so an ABC earns its keep here
(§16.5): three concrete transports (ble.py, tcp.py, loopback.py) share this interface. Do not add
another ABC/factory/registry elsewhere in this codebase "for extensibility" — there is no Week 14.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator


class TransportClosed(Exception):
    """Raised by send()/frames() once the transport has been closed, so callers can distinguish a
    clean shutdown from a real I/O error."""


class ITransport(ABC):
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def send(self, frame: bytes) -> None:
        """Sends one already-encoded wire frame."""

    @abstractmethod
    def frames(self) -> AsyncIterator[bytes]:
        """Yields raw bytes as they arrive off the link. Not guaranteed frame-aligned for stream
        transports (tcp.py) — feed them through protocol.framing.Framer. Ends when the transport
        closes."""

    @property
    @abstractmethod
    def mtu(self) -> int:
        """Maximum payload bytes per send() call the underlying link can carry unfragmented."""
