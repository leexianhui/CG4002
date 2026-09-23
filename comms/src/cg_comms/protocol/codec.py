"""Header pack/unpack + bytes <-> Packet — the single entry point for the wire protocol.

Encoding and decoding both go through this module; nothing else in the codebase calls
struct.pack/unpack against a wire layout directly. A malformed packet is always a counted, logged
drop returned as a `DecodeError` — never an exception that reaches the caller's event loop
(§16.5). The one exception the caller must still catch explicitly is
`cryptography.exceptions.InvalidTag`, surfaced here as `DropReason.AUTH_FAILED`.
"""

from __future__ import annotations

import copy
import struct
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Union, cast

from cryptography.exceptions import InvalidTag

from cg_comms.protocol import crc as crc_mod
from cg_comms.protocol.constants import (
    ACK_RETRANSMIT_PACKET_TYPES,
    CRC_SIZE,
    GCM_TAG_SIZE,
    HEADER_SIZE,
    MAGIC,
    PROTOCOL_VERSION,
    TYPE_MASK,
    TYPE_MORE_FRAGMENTS,
    Direction,
    PacketType,
)
from cg_comms.protocol.crypto import SequenceTracker, derive_nonce, open_sealed, seal
from cg_comms.protocol.payloads import PAYLOAD_TABLES, Field, FieldKind

_HEADER_STRUCT = struct.Struct("<BBBHBH")  # magic, version, type, seq, player_id, len
_DEVICE_NONCE_STRUCT = struct.Struct("<I")

# wire_protocol.md §5.1: HELLO/HELLO_ACK have no session yet, so sessionId=0 would repeat nonces
# across devices/reboots. These two carry a cleartext deviceNonce prefix instead.
_HANDSHAKE_TYPES = (PacketType.HELLO, PacketType.HELLO_ACK)

PayloadValue = Union[int, bytes, tuple[int, ...]]
PayloadFields = dict[str, PayloadValue]


@dataclass(frozen=True)
class Header:
    version: int
    packet_type: int
    more_fragments: bool
    seq: int
    player_id: int
    payload_len: int


class DropReason(str, Enum):
    TOO_SHORT = "too_short"
    BAD_MAGIC = "bad_magic"
    BAD_VERSION = "bad_version"
    BAD_CRC = "bad_crc"
    FRAGMENTED = "fragmented"  # MORE_FRAGMENTS set - §4.6 hard error, no reassembly
    UNKNOWN_TYPE = "unknown_type"
    BAD_PAYLOAD_LENGTH = "bad_payload_length"
    REPLAY = "replay"
    AUTH_FAILED = "auth_failed"
    PLAYER_MISMATCH = "player_mismatch"


@dataclass(frozen=True)
class DecodeError:
    reason: DropReason
    detail: str = ""


@dataclass(frozen=True)
class DecodedPacket:
    header: Header
    ext_seq: int
    fields: PayloadFields
    duplicate: bool = False


def _payload_struct_fmt(packet_type: PacketType) -> str:
    fields = PAYLOAD_TABLES[packet_type]
    return "<" + "".join(f.struct_fmt for f in fields)


def pack_payload(packet_type: PacketType, values: PayloadFields) -> bytes:
    """Packs `values` (keyed by field name) into the plaintext payload for `packet_type`,
    per PAYLOAD_TABLES. This is the only place a packet's field order/format is interpreted."""
    fields = PAYLOAD_TABLES[packet_type]
    args: list[int | bytes] = []
    for f in fields:
        value = values[f.name]
        if f.kind is FieldKind.I16_ARRAY:
            args.extend(value)  # type: ignore[arg-type]
        else:
            args.append(value)  # type: ignore[arg-type]
    return struct.pack(_payload_struct_fmt(packet_type), *args)


def unpack_payload(packet_type: PacketType, data: bytes) -> PayloadFields:
    """Inverse of pack_payload."""
    fields = PAYLOAD_TABLES[packet_type]
    fmt = _payload_struct_fmt(packet_type)
    expected = struct.calcsize(fmt)
    if len(data) != expected:
        raise ValueError(f"payload length {len(data)} != expected {expected} for {packet_type!r}")
    raw = struct.unpack(fmt, data)
    result: PayloadFields = {}
    idx = 0
    for f in fields:
        if f.kind is FieldKind.I16_ARRAY:
            result[f.name] = tuple(raw[idx : idx + f.slot_count])
            idx += f.slot_count
        else:
            result[f.name] = raw[idx]
            idx += 1
    return result


def encode_packet(
    *,
    packet_type: PacketType,
    seq: int,
    player_id: int,
    fields: PayloadFields,
    psk: bytes,
    session_id: int,
    direction: Direction,
    ext_seq: int,
    more_fragments: bool = False,
) -> bytes:
    """Builds one complete wire frame: header, encrypted payload, trailing CRC16."""
    plaintext = pack_payload(packet_type, fields)
    type_byte = int(packet_type) | (TYPE_MORE_FRAGMENTS if more_fragments else 0)

    if packet_type in _HANDSHAKE_TYPES:
        device_nonce = fields["device_nonce"]
        assert isinstance(device_nonce, int)
        device_nonce_bytes = _DEVICE_NONCE_STRUCT.pack(device_nonce)
        payload_len = len(device_nonce_bytes) + len(plaintext) + GCM_TAG_SIZE
        header = _HEADER_STRUCT.pack(MAGIC, PROTOCOL_VERSION, type_byte, seq, player_id, payload_len)
        nonce = derive_nonce(device_nonce, player_id, direction, ext_seq)
        sealed = seal(psk, header + device_nonce_bytes, nonce, plaintext)
        body = header + device_nonce_bytes + sealed
    else:
        payload_len = len(plaintext) + GCM_TAG_SIZE
        header = _HEADER_STRUCT.pack(MAGIC, PROTOCOL_VERSION, type_byte, seq, player_id, payload_len)
        nonce = derive_nonce(session_id, player_id, direction, ext_seq)
        sealed = seal(psk, header, nonce, plaintext)
        body = header + sealed

    crc = crc_mod.crc16(body)
    return body + struct.pack("<H", crc)


def reliable_kind(packet_type: PacketType) -> str:
    """Groups ACK-retransmit types that share one replay watermark (SequenceTracker.reliable_high).

    Types in one group must never have more than one packet in flight at a time, so that a retransmit
    is always the newest packet of its group. Both assessment types share a group because the relay
    serializes them behind one lock (Session._assess_send_lock); every other type is its own group.
    """
    if packet_type in (PacketType.ASSESS_ECHO_REQ, PacketType.ASSESS_FILE_CHUNK, PacketType.ASSESS_FILE_READ):
        return "assess"
    return str(packet_type.name)


def decode_packet(
    frame: bytes,
    *,
    psk: bytes,
    session_id: int,
    direction: Direction,
    sequence_tracker: SequenceTracker,
    reliable_duplicates: bool = False,
    expected_player_id: int | None = None,
) -> DecodedPacket | DecodeError:
    """Decodes one complete wire frame (as produced by framing.Framer). Verifies CRC before
    decrypting (wire_protocol.md §5 note), checks the replay window, then unpacks the payload."""
    if len(frame) < HEADER_SIZE + CRC_SIZE:
        return DecodeError(DropReason.TOO_SHORT, f"frame is {len(frame)} bytes")

    body, crc_bytes = frame[:-CRC_SIZE], frame[-CRC_SIZE:]
    (expected_crc,) = struct.unpack("<H", crc_bytes)
    if crc_mod.crc16(body) != expected_crc:
        return DecodeError(DropReason.BAD_CRC)

    magic, version, type_byte, seq, player_id, payload_len = _HEADER_STRUCT.unpack(
        body[:HEADER_SIZE]
    )
    if magic != MAGIC:
        return DecodeError(DropReason.BAD_MAGIC, f"got 0x{magic:02X}")
    if version != PROTOCOL_VERSION:
        return DecodeError(DropReason.BAD_VERSION, f"got {version}")
    if expected_player_id is not None and player_id != expected_player_id:
        return DecodeError(DropReason.PLAYER_MISMATCH)
    if type_byte & TYPE_MORE_FRAGMENTS:
        # §4.6: reassembly is not implemented. A set MORE_FRAGMENTS bit is a hard error.
        return DecodeError(DropReason.FRAGMENTED)

    try:
        packet_type = PacketType(type_byte & TYPE_MASK)
    except ValueError:
        return DecodeError(DropReason.UNKNOWN_TYPE, f"got 0x{type_byte & TYPE_MASK:02X}")

    if packet_type in _HANDSHAKE_TYPES:
        device_nonce_bytes = body[HEADER_SIZE : HEADER_SIZE + _DEVICE_NONCE_STRUCT.size]
        if len(device_nonce_bytes) != _DEVICE_NONCE_STRUCT.size:
            return DecodeError(DropReason.TOO_SHORT, "LEN exceeds available frame bytes")
        sealed = body[HEADER_SIZE + _DEVICE_NONCE_STRUCT.size : HEADER_SIZE + payload_len]
        if len(sealed) != payload_len - _DEVICE_NONCE_STRUCT.size:
            return DecodeError(DropReason.TOO_SHORT, "LEN exceeds available frame bytes")
    else:
        sealed = body[HEADER_SIZE : HEADER_SIZE + payload_len]
        if len(sealed) != payload_len:
            return DecodeError(DropReason.TOO_SHORT, "LEN exceeds available frame bytes")

    if payload_len != len(body) - HEADER_SIZE:
        return DecodeError(DropReason.BAD_PAYLOAD_LENGTH)
    if reliable_duplicates:
        cached = sequence_tracker.reliable_frames.get(frame)
        if cached is not None and time.monotonic() - cached[0] <= 20:
            return cast(DecodedPacket, replace(cached[1], duplicate=True))

    # Authentication failures must not advance or poison replay state.
    candidate = copy.copy(sequence_tracker)
    candidate._seen = sequence_tracker._seen.copy()
    ext_seq = candidate.accept(seq)
    reliable = reliable_duplicates and packet_type in ACK_RETRANSMIT_PACKET_TYPES
    if reliable:
        # A first RFID arrival may be >64 sensor packets late after multiple lost sends.
        # Give reliable application effects their own monotonic replay watermark.
        ext_seq = sequence_tracker.infer(seq)
        if ext_seq <= sequence_tracker.reliable_high.get(reliable_kind(packet_type), -1):
            return DecodeError(DropReason.REPLAY, f"reliable seq={seq}")
    if ext_seq is None:
        return DecodeError(DropReason.REPLAY, f"seq={seq}")

    if packet_type in _HANDSHAKE_TYPES:
        (device_nonce,) = _DEVICE_NONCE_STRUCT.unpack(device_nonce_bytes)
        header_aad = body[:HEADER_SIZE] + device_nonce_bytes
        nonce = derive_nonce(device_nonce, player_id, direction, ext_seq)
    else:
        header_aad = body[:HEADER_SIZE]
        nonce = derive_nonce(session_id, player_id, direction, ext_seq)
    try:
        plaintext = open_sealed(psk, header_aad, nonce, sealed)
    except InvalidTag:
        return DecodeError(DropReason.AUTH_FAILED)

    try:
        fields = unpack_payload(packet_type, plaintext)
    except (struct.error, ValueError) as exc:
        return DecodeError(DropReason.BAD_PAYLOAD_LENGTH, str(exc))

    header = Header(
        version=version,
        packet_type=int(packet_type),
        more_fragments=False,
        seq=seq,
        player_id=player_id,
        payload_len=payload_len,
    )
    sequence_tracker._last_seq = candidate._last_seq
    sequence_tracker._high = candidate._high
    sequence_tracker._highest_ext_seq = candidate._highest_ext_seq
    sequence_tracker._seen = candidate._seen
    packet = DecodedPacket(header=header, ext_seq=ext_seq, fields=fields)
    if reliable:
        sequence_tracker.reliable_high[reliable_kind(packet_type)] = ext_seq
        sequence_tracker.reliable_frames[frame] = (time.monotonic(), packet)
        while len(sequence_tracker.reliable_frames) > 16:
            sequence_tracker.reliable_frames.popitem(last=False)
    return packet
