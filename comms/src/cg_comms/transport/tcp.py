"""TCP transport — WiFi fallback for the FireBeetle<->laptop link, and the transport
mock/firebeetle_sim.py uses when driven with `--transport tcp` (no real radio available).
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from cg_comms.transport.base import ITransport, TransportClosed

_RECV_CHUNK = 4096


class TcpTransport(ITransport):
    def __init__(self, host: str, port: int, mtu: int = 4096) -> None:
        self._host = host
        self._port = port
        self._mtu = mtu
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self._host, self._port)

    async def disconnect(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        self._reader = None
        self._writer = None

    async def send(self, frame: bytes) -> None:
        if self._writer is None:
            raise TransportClosed("tcp transport not connected")
        self._writer.write(frame)
        await self._writer.drain()

    async def frames(self) -> AsyncIterator[bytes]:
        if self._reader is None:
            raise TransportClosed("tcp transport not connected")
        while True:
            chunk = await self._reader.read(_RECV_CHUNK)
            if not chunk:
                return
            yield chunk

    @property
    def mtu(self) -> int:
        return self._mtu


class TcpServerTransport(ITransport):
    """Server-side counterpart: accepts exactly one connection and behaves as that connection's
    transport thereafter. Used where the laptop is the listener rather than the connector."""

    def __init__(self, host: str, port: int, mtu: int = 4096) -> None:
        self._host = host
        self._port = port
        self._mtu = mtu
        self._server: asyncio.base_events.Server | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._client_connected = asyncio.Event()

    async def connect(self) -> None:
        self._server = await asyncio.start_server(self._on_client, self._host, self._port)
        await self._client_connected.wait()

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._client_connected.set()

    async def disconnect(self) -> None:
        if self._writer is not None:
            self._writer.close()
        if self._server is not None:
            self._server.close()
        self._reader = None
        self._writer = None

    async def send(self, frame: bytes) -> None:
        if self._writer is None:
            raise TransportClosed("tcp server transport has no client yet")
        self._writer.write(frame)
        await self._writer.drain()

    async def frames(self) -> AsyncIterator[bytes]:
        await self._client_connected.wait()
        assert self._reader is not None
        while True:
            chunk = await self._reader.read(_RECV_CHUNK)
            if not chunk:
                return
            yield chunk

    @property
    def mtu(self) -> int:
        return self._mtu
