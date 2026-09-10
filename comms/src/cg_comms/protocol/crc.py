"""CRC-16/CCITT-FALSE — standalone, no dependency on the rest of the package.

Kept dependency-free so a firmware parity test (comms/tests/test_golden_vectors.py, a later pass)
can import just this module. Poly 0x1021, init 0xFFFF, no input/output reflection, no final XOR —
must match cg_crc16() in shared/protocol/cg_protocol.h bit-for-bit.
"""

from __future__ import annotations

_POLY: int = 0x1021
_INIT: int = 0xFFFF


def _build_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ _POLY) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
        table.append(crc)
    return tuple(table)


_TABLE: tuple[int, ...] = _build_table()


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE over `data`. Computed over bytes [0, 8+LEN) — header plus payload,
    never including the trailing CRC field itself (wire_protocol.md §7)."""
    crc = _INIT
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ _TABLE[((crc >> 8) ^ byte) & 0xFF]
    return crc
