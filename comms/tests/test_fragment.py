"""Fragmentation tests, per COMMS_IMPLEMENTATION_PLAN.md §4.6 / wire_protocol.md §6:

Reassembly is explicitly NOT implemented. A received frame with the `MORE_FRAGMENTS` bit set must
be treated as a hard error — logged and dropped, not partially decoded or queued for reassembly.
The codec API contract documents this as `DecodeError(reason=DropReason.FRAGMENTED)`.

Written against the codec's public API contract and PAYLOAD_TABLES only, without reading
codec.py's implementation.
"""

from __future__ import annotations

from cg_comms.protocol.codec import DecodedPacket, DecodeError, DropReason, decode_packet, encode_packet
from cg_comms.protocol.constants import Direction, PacketType
from cg_comms.protocol.crypto import SequenceTracker

_PSK = bytes(range(16))
_SESSION_ID = 0xCAFEBABE

_SENSOR_FRAME_FIELDS = {
    "device_timestamp_ms": 1000,
    "flags": 0,
    "weapon_id": 1,
    "dominant": (0, 0, 0, 0, 0, 0),
    "offhand": (0, 0, 0, 0, 0, 0),
    "torso": (0, 0, 0, 0, 0, 0),
}

_HEARTBEAT_FIELDS = {
    "device_timestamp_ms": 2000,
    "vbat_belt_mv": 4100,
    "vbat_dom_mv": 4050,
    "vbat_off_mv": 4000,
    "status_flags": 0,
}


def _decode(frame: bytes, *, direction: Direction) -> DecodedPacket | DecodeError:
    return decode_packet(
        frame,
        psk=_PSK,
        session_id=_SESSION_ID,
        direction=direction,
        sequence_tracker=SequenceTracker(),
    )


def test_more_fragments_bit_is_a_hard_drop_for_sensor_frame():
    frame = encode_packet(
        packet_type=PacketType.SENSOR_FRAME,
        seq=1,
        player_id=1,
        fields=_SENSOR_FRAME_FIELDS,
        psk=_PSK,
        session_id=_SESSION_ID,
        direction=Direction.DEVICE_TO_LAPTOP,
        ext_seq=1,
        more_fragments=True,
    )

    result = _decode(frame, direction=Direction.DEVICE_TO_LAPTOP)

    assert isinstance(result, DecodeError)
    assert result.reason == DropReason.FRAGMENTED


def test_more_fragments_bit_is_a_hard_drop_for_heartbeat():
    # Different packet type and direction, to check the drop isn't special-cased to one type.
    frame = encode_packet(
        packet_type=PacketType.HEARTBEAT,
        seq=1,
        player_id=1,
        fields=_HEARTBEAT_FIELDS,
        psk=_PSK,
        session_id=_SESSION_ID,
        direction=Direction.LAPTOP_TO_DEVICE,
        ext_seq=1,
        more_fragments=True,
    )

    result = _decode(frame, direction=Direction.LAPTOP_TO_DEVICE)

    assert isinstance(result, DecodeError)
    assert result.reason == DropReason.FRAGMENTED


def test_same_packet_without_more_fragments_decodes_normally():
    # Sanity contrast: identical fields/seq/etc. but more_fragments=False must decode fine, so the
    # FRAGMENTED drop above is attributable to the flag alone and not some other mistake in the
    # test's packet construction.
    frame = encode_packet(
        packet_type=PacketType.SENSOR_FRAME,
        seq=1,
        player_id=1,
        fields=_SENSOR_FRAME_FIELDS,
        psk=_PSK,
        session_id=_SESSION_ID,
        direction=Direction.DEVICE_TO_LAPTOP,
        ext_seq=1,
        more_fragments=False,
    )

    result = _decode(frame, direction=Direction.DEVICE_TO_LAPTOP)

    assert isinstance(result, DecodedPacket)
    assert result.fields == _SENSOR_FRAME_FIELDS
    assert result.header.more_fragments is False
