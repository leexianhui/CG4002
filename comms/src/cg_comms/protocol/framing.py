"""Byte-stream -> packet boundaries: MAGIC scan + resync.

Needed for stream transports (transport/tcp.py) where one `recv()` may contain zero, one, or many
frames, or a partial frame. BLE notifications (transport/ble.py) arrive one GATT payload per frame
already, but are still fed through a Framer for uniformity — feeding a whole frame at once is a
no-op pass-through.
"""

from __future__ import annotations

import struct

from cg_comms.protocol.constants import CRC_SIZE, HEADER_SIZE, MAGIC, MAX_PAYLOAD_LEN

_MAGIC_BYTES = bytes([MAGIC])


class Framer:
    """Accumulates bytes fed via `feed()` and yields complete, CRC-unverified frames (CRC and all
    other validation happens in codec.decode_packet). Resyncs from arbitrary garbage: if the
    buffer's leading MAGIC byte turns out to be a false positive (implausible LEN), it is skipped
    and the scan continues, rather than stalling forever waiting for bytes that would never
    complete a bogus frame."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        frames: list[bytes] = []
        while True:
            frame = self._try_extract_one()
            if frame is None:
                break
            frames.append(frame)
        return frames

    def _try_extract_one(self) -> bytes | None:
        while True:
            idx = self._buf.find(_MAGIC_BYTES)
            if idx == -1:
                self._buf.clear()
                return None
            if idx > 0:
                del self._buf[:idx]

            if len(self._buf) < HEADER_SIZE:
                return None

            (len_field,) = struct.unpack_from("<H", self._buf, 6)
            if len_field > MAX_PAYLOAD_LEN:
                # This MAGIC byte was a false positive inside other data; drop it and keep
                # scanning rather than waiting for an implausible frame length to arrive.
                del self._buf[:1]
                continue

            total = HEADER_SIZE + len_field + CRC_SIZE
            if len(self._buf) < total:
                return None

            frame = bytes(self._buf[:total])
            del self._buf[:total]
            return frame
