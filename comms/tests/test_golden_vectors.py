"""Golden-vector tests: assert the Python codec reproduces `shared/protocol/golden_vectors.json`
byte-for-byte, in both directions (encode and decode).

Per COMMS_IMPLEMENTATION_PLAN.md §4.7 / §13, these vectors are the firmware handshake: both the
Python codec (this file) and the C codec (`firmware/test/test_golden_vectors.cpp`) must reproduce
the same fixed PSK/session/seq/plaintext -> exact wire bytes, so the two implementations are
provably compatible before hardware exists.

The vectors themselves were generated independently (a from-scratch, offline AES-128-GCM + CRC16
implementation validated against published NIST/FIPS test vectors — see
`comms/tests/_gen_golden_vectors.py`), not derived from reading `codec.py` or `crypto.py`. This
test file is likewise written without reading either of those modules: it only calls the codec's
public API (`encode_packet`, `decode_packet`, `pack_payload`, `unpack_payload`) per the documented
calling contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cg_comms.protocol.codec import (
    DecodedPacket,
    decode_packet,
    encode_packet,
    unpack_payload,
)
from cg_comms.protocol.constants import Direction, PacketType, PROTOCOL_VERSION
from cg_comms.protocol.crypto import SequenceTracker

# comms/tests/test_golden_vectors.py -> comms/tests -> comms -> <repo root> -> shared/protocol/...
_GOLDEN_VECTORS_PATH = (
    Path(__file__).resolve().parent.parent.parent / "shared" / "protocol" / "golden_vectors.json"
)

with _GOLDEN_VECTORS_PATH.open("r", encoding="utf-8") as _f:
    _GOLDEN_DOC = json.load(_f)

_PSK = bytes.fromhex(_GOLDEN_DOC["psk_hex"])
_VECTORS = _GOLDEN_DOC["vectors"]

# Derived from PacketType rather than a literal, so adding a packet type does not require
# bumping a magic number here as well as in the file. test_golden_vectors_cover_every_packet_type
# checks WHICH types are covered; this only catches duplicates/missing at import time.
assert len(_VECTORS) == len(PacketType), (
    f"expected {len(PacketType)} golden vectors (one per packet type), found {len(_VECTORS)}"
)


def _fields_for(vector: dict) -> dict:
    packet_type = PacketType(vector["packet_type"])
    plaintext = bytes.fromhex(vector["plaintext_hex"])
    return unpack_payload(packet_type, plaintext)


@pytest.mark.parametrize("vector", _VECTORS, ids=[v["name"] for v in _VECTORS])
def test_encode_packet_reproduces_golden_wire_frame(vector: dict):
    packet_type = PacketType(vector["packet_type"])
    fields = _fields_for(vector)
    direction = Direction(vector["direction"])
    # HELLO/HELLO_ACK vectors have no top-level "session_id" (they use "device_nonce" instead, per
    # wire_protocol.md §5.1); the session_id argument is unused for those two types, so 0 is an
    # arbitrary placeholder — see the codec API contract note on this.
    session_id = vector.get("session_id", 0)

    frame = encode_packet(
        packet_type=packet_type,
        seq=vector["seq"],
        player_id=vector["player_id"],
        fields=fields,
        psk=_PSK,
        session_id=session_id,
        direction=direction,
        ext_seq=vector["ext_seq"],
    )

    assert frame == bytes.fromhex(vector["wire_frame_hex"])


@pytest.mark.parametrize("vector", _VECTORS, ids=[v["name"] for v in _VECTORS])
def test_decode_packet_reproduces_golden_plaintext_and_header(vector: dict):
    packet_type = PacketType(vector["packet_type"])
    expected_fields = _fields_for(vector)
    direction = Direction(vector["direction"])
    session_id = vector.get("session_id", 0)
    frame = bytes.fromhex(vector["wire_frame_hex"])

    tracker = SequenceTracker()
    result = decode_packet(
        frame,
        psk=_PSK,
        session_id=session_id,
        direction=direction,
        sequence_tracker=tracker,
    )

    assert isinstance(result, DecodedPacket), (
        f"expected a DecodedPacket for vector {vector['name']!r}, got {result!r}"
    )
    assert result.fields == expected_fields
    assert result.header.seq == vector["seq"]
    assert result.header.player_id == vector["player_id"]
    assert result.header.packet_type == vector["packet_type"]
    assert result.header.version == PROTOCOL_VERSION
    assert result.header.more_fragments is False
    assert result.header.payload_len == len(frame) - 8 - 2  # LEN excludes header and trailing CRC

    # Consistency check (per task instructions): a fresh SequenceTracker decoding a vector's own
    # seq for the first time should establish exactly the vector's recorded ext_seq. If this ever
    # fails, that's a discrepancy between this test's assumption and the real sequence-tracking
    # behaviour worth flagging, not something to silently work around.
    assert result.ext_seq == vector["ext_seq"], (
        f"vector {vector['name']!r}: decode_packet reported ext_seq={result.ext_seq}, "
        f"but the golden vector records ext_seq={vector['ext_seq']}"
    )


def test_golden_vectors_cover_every_packet_type():
    # One vector per packet type, per §4.7 ("for each packet type"). This guards against the
    # golden-vectors file silently losing coverage of a type in a future edit.
    covered = {PacketType(v["packet_type"]) for v in _VECTORS}
    assert covered == set(PacketType)
