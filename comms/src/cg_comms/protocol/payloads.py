"""Declarative payload field tables — one table of (field, type) per packet type, consumed by the
generic packer in codec.py.

§16.3 rule 2: "Declare payload layouts as data, not code... A firmware layout change then edits one
table and regenerates the golden vectors." No `struct.pack` call for a specific packet type may
appear anywhere else in this codebase — codec.py's packer is the only thing that reads these tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from cg_comms.protocol.constants import SEGMENT_CHUNK_SAMPLES, PacketType


class FieldKind(Enum):
    U8 = auto()
    U16 = auto()
    U32 = auto()
    I16 = auto()
    BYTES = auto()  # fixed-length raw bytes -> python `bytes`
    I16_ARRAY = auto()  # fixed-length array of int16 -> python tuple[int, ...]


# struct format character(s) for one element of this kind, and how many struct-unpack "slots"
# (flat positional values) one field of this kind consumes.
_STRUCT_CHAR: dict[FieldKind, str] = {
    FieldKind.U8: "B",
    FieldKind.U16: "H",
    FieldKind.U32: "I",
    FieldKind.I16: "h",
    FieldKind.BYTES: "s",  # prefixed with length, e.g. "6s"
    FieldKind.I16_ARRAY: "h",  # repeated `length` times, e.g. "6h"
}


@dataclass(frozen=True)
class Field:
    name: str
    kind: FieldKind
    length: int = 1  # byte length for BYTES; element count for I16_ARRAY; ignored otherwise

    @property
    def struct_fmt(self) -> str:
        char = _STRUCT_CHAR[self.kind]
        if self.kind in (FieldKind.BYTES, FieldKind.I16_ARRAY):
            return f"{self.length}{char}"
        return char

    @property
    def slot_count(self) -> int:
        """Number of flat positional values this field occupies in struct.pack/unpack args."""
        return self.length if self.kind is FieldKind.I16_ARRAY else 1


PAYLOAD_TABLES: dict[PacketType, tuple[Field, ...]] = {
    PacketType.HELLO: (
        Field("device_mac", FieldKind.BYTES, 6),
        Field("player_id", FieldKind.U8),
        Field("protocol_version", FieldKind.U8),
        Field("firmware_version", FieldKind.U16),
        Field("device_nonce", FieldKind.U32),
        Field("reserved", FieldKind.U16),
    ),
    PacketType.HELLO_ACK: (
        Field("session_id", FieldKind.U32),
        Field("server_epoch_unix_ms_lo", FieldKind.U32),
        Field("server_epoch_unix_ms_hi", FieldKind.U32),
        Field("device_nonce", FieldKind.U32),
    ),
    PacketType.TIME_SYNC_REQ: (Field("t0", FieldKind.U32),),
    PacketType.TIME_SYNC_RESP: (
        Field("t0", FieldKind.U32),
        Field("t1", FieldKind.U32),
        Field("t2", FieldKind.U32),
    ),
    PacketType.SENSOR_FRAME: (
        Field("device_timestamp_ms", FieldKind.U32),
        Field("flags", FieldKind.U8),
        Field("weapon_id", FieldKind.U8),
        Field("dominant", FieldKind.I16_ARRAY, 6),
        Field("offhand", FieldKind.I16_ARRAY, 6),
        Field("torso", FieldKind.I16_ARRAY, 6),
    ),
    PacketType.RFID_EVENT: (
        Field("device_timestamp_ms", FieldKind.U32),
        Field("weapon_id", FieldKind.U8),
        Field("reserved", FieldKind.U8),
        Field("tag_uid", FieldKind.BYTES, 6),
    ),
    PacketType.SEGMENT_CHUNK: (
        Field("segment_id", FieldKind.U32),
        Field("chunk_index", FieldKind.U8),
        Field("chunk_count", FieldKind.U8),
        Field("weapon_id", FieldKind.U8),
        Field("flags", FieldKind.U8),
        Field("sample_rate_hz", FieldKind.U16),
        Field("first_sample_ms", FieldKind.U32),
        Field("sample_count", FieldKind.U8),
        Field("reserved", FieldKind.U8),
        # SEGMENT_CHUNK_SAMPLES rows of (ax, ay, az, gx, gy, gz), flattened; unused rows are zero.
        Field("samples", FieldKind.I16_ARRAY, SEGMENT_CHUNK_SAMPLES * 6),
    ),
    PacketType.HEARTBEAT: (
        Field("device_timestamp_ms", FieldKind.U32),
        Field("vbat_belt_mv", FieldKind.U16),
        Field("vbat_dom_mv", FieldKind.U16),
        Field("vbat_off_mv", FieldKind.U16),
        Field("status_flags", FieldKind.U16),
    ),
    PacketType.ACK: (Field("acked_seq", FieldKind.U16),),
    PacketType.COMBAT_FEEDBACK: (
        Field("feedback_code", FieldKind.U8),
        Field("intensity_pct", FieldKind.U8),
        Field("duration_ms", FieldKind.U16),
        Field("repeat_count", FieldKind.U16),
    ),
}
