"""BLE central transport — bleak, notify (device->laptop) + write-without-response (laptop->device).

One characteristic each way. Write-without-response keeps latency low for the 50 Hz SENSOR_FRAME
stream; the reliability tier for packets that need it (§6) is enforced above this layer in
relay/reliability.py, not by requesting a BLE write-with-response here.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import structlog
from bleak import BleakClient

from cg_comms.transport.base import ITransport, TransportClosed

_DEFAULT_MTU = 244  # ATT MTU 247 - 3 bytes of ATT overhead; updated after connect if negotiated higher
_WRITE_TIMEOUT_S = 3.0  # bleak's WinRT backend can hang a write-without-response forever with no
# exception (observed in the field); bound it so a stuck write surfaces as TransportClosed instead of
# silently wedging the caller (the heartbeat sender) for good.
logger = structlog.get_logger(__name__)


class BleTransport(ITransport):
    def __init__(self, address: str, notify_char_uuid: str, write_char_uuid: str) -> None:
        self._address = address
        self._notify_uuid = notify_char_uuid
        self._write_uuid = write_char_uuid
        self._client: BleakClient | None = None
        self._queue: "asyncio.Queue[bytes | None]" = asyncio.Queue(256)
        self._overflow = False
        self._write_lock = asyncio.Lock()
        self._mtu = _DEFAULT_MTU

    async def connect(self) -> None:
        self._client = BleakClient(self._address, disconnected_callback=self._on_disconnect)
        await self._client.connect()
        await self._client.start_notify(self._notify_uuid, self._on_notify)
        negotiated = getattr(self._client, "mtu_size", None)
        if negotiated:
            self._mtu = max(negotiated - 3, 20)
        if self._mtu < 68:
            raise TransportClosed(f"BLE payload MTU {self._mtu} cannot carry a SENSOR_FRAME (68)")
        if self._mtu < 234:
            # Event-mode hardware hubs send 234-byte SEGMENT_CHUNKs; a smaller MTU truncates them
            # on the device, and every chunk then fails CRC here. SENSOR_FRAME-only devices are fine.
            logger.warning("ble_mtu_too_small_for_segments", mtu_payload=self._mtu, needed=234)
        logger.info("ble_connected", mtu_payload=self._mtu)

    def _on_notify(self, _handle: int, data: bytearray) -> None:
        if self._overflow:
            return
        if self._queue.full():
            self._overflow = True
            logger.error("ble_receive_overflow", capacity=self._queue.maxsize)
            self._on_disconnect(self._client)
            return
        self._queue.put_nowait(bytes(data))

    def _on_disconnect(self, _client) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)

    async def disconnect(self) -> None:
        try:
            if self._client is not None and self._client.is_connected:
                await asyncio.wait_for(self._client.disconnect(), 5)
        finally:
            self._client = None
            self._on_disconnect(None)

    async def send(self, frame: bytes) -> None:
        if self._client is None or not self._client.is_connected:
            raise TransportClosed("ble transport not connected")
        try:
            async with self._write_lock:
                await asyncio.wait_for(
                    self._client.write_gatt_char(self._write_uuid, frame, response=False),
                    timeout=_WRITE_TIMEOUT_S,
                )
        except asyncio.TimeoutError as exc:
            raise TransportClosed("ble write timed out") from exc

    async def frames(self) -> AsyncIterator[bytes]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    @property
    def mtu(self) -> int:
        return self._mtu
