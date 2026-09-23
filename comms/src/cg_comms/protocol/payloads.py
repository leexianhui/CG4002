"""Declarative payload field tables — one table of (field, type) per packet type, consumed by the
generic packer in codec.py.

§16.3 rule 2: "Declare payload layouts as data, not code... A firmware layout change then edits one
table and regenerates the golden vectors." No `struct.pack` call for a specific packet type may
appear anywhere else in this codebase — codec.py's packer is the only thing that reads these tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from cg_comms.protocol.constants import PacketType


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
    # --- Assessment/demo extension types (0x40-0x4F) -----------------------------------------
    # `trace_id` is a wire field on purpose. The obvious alternative was a `demoTraceId` key on the
    # relay -> Ultra96 -> visualizer JSON messages, but every shared/schemas/*.json sets
    # "additionalProperties": false and every model in messages/models.py inherits extra="forbid",
    # so that would be a breaking change requiring relay + engine + Ultra96 + both APKs to be
    # redeployed in lockstep. Carrying the id in the packet keeps the correlation evidence entirely
    # inside the BLE hop being demonstrated, which is the hop the requirement is about.
    PacketType.ASSESS_ECHO_REQ: (
        Field("trace_id", FieldKind.U32),
        Field("sent_ms", FieldKind.U32),
        Field("keycode", FieldKind.U8),  # ASCII of the keystroke that generated this packet
        Field("payload_kind", FieldKind.U8),  # which dummy-packet variant (see assess_dummy.py)
        Field("value_a", FieldKind.I16),
        Field("value_b", FieldKind.I16),
        Field("value_c", FieldKind.I16),
    ),
    PacketType.ASSESS_ECHO_RESP: (
        Field("trace_id", FieldKind.U32),  # echoed unchanged — correlates response to request
        Field("device_timestamp_ms", FieldKind.U32),
        Field("keycode", FieldKind.U8),  # echoed unchanged
        Field("payload_kind", FieldKind.U8),  # echoed unchanged
        Field("transform_id", FieldKind.U8),  # which modification the device applied
        Field("reserved", FieldKind.U8),
        Field("value_a", FieldKind.I16),  # modified by the device
        Field("value_b", FieldKind.I16),
        Field("value_c", FieldKind.I16),
    ),
    # Fixed 32-byte data field with an explicit chunk_len, rather than a variable-length payload:
    # FieldKind has no variable-length kind (BYTES is fixed via Field.length), and adding one would
    # change the generic packer that every other type depends on. The final chunk is zero-padded and
    # chunk_len says how much of it counts. Frame size = 8 + 38 + 16 + 2 = 64 B, under the 68 B
    # SENSOR_FRAME frame the BLE MTU floor in transport/ble.py is already validated against.
    PacketType.ASSESS_FILE_CHUNK: (
        Field("chunk_index", FieldKind.U16),
        Field("total_chunks", FieldKind.U16),
        Field("chunk_len", FieldKind.U8),  # 1..32 valid bytes in `data`
        Field("reserved", FieldKind.U8),
        Field("data", FieldKind.BYTES, 32),
    ),
    PacketType.ASSESS_FILE_ACK: (
        Field("chunk_index", FieldKind.U16),
        Field("running_crc", FieldKind.U16),  # CRC-16/CCITT-FALSE over all file bytes so far
        Field("received_bytes", FieldKind.U32),
    ),
}

# Longest valid `data` field in an ASSESS_FILE_CHUNK. Kept next to the table it constrains so the
# sender, the firmware and the tests all quote one number.
ASSESS_FILE_CHUNK_DATA_LEN: int = 32
