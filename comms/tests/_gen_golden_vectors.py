"""Throwaway reference-encoder used ONCE to generate shared/protocol/golden_vectors.json.

Deliberately independent of cg_comms.protocol.codec / crypto: it reimplements AES-128 and GCM mode
from scratch (no `cryptography` package available offline in this environment), header packing,
and CRC-16/CCITT-FALSE (bit-by-bit, per wire_protocol.md §7), straight from wire_protocol.md /
COMMS_IMPLEMENTATION_PLAN.md §4. The AES-GCM implementation below is self-validated against three
independently well-known published vectors before being trusted to produce the golden vectors:
  - FIPS-197 Appendix B/C: AES-128 single-block ECB encryption
  - NIST GCM Test Case 1: AES-128-GCM, zero key/IV, empty plaintext/AAD
  - NIST GCM Test Case 2: AES-128-GCM, zero key/IV, one all-zero plaintext block, empty AAD
This file is not part of the test suite; delete after golden_vectors.json is generated and reviewed.
"""
import json
import struct
import sys

# ---------------------------------------------------------------------------
# AES-128 core (FIPS-197), from scratch.
# ---------------------------------------------------------------------------

_SBOX = [
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
]

_RCON = [0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1b,0x36]


def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _key_expansion(key: bytes) -> list[list[int]]:
    nk = 4  # 128-bit key -> 4 words
    nr = 10
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        temp = list(w[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= _RCON[i // nk - 1]
        w.append([w[i - nk][j] ^ temp[j] for j in range(4)])
    return w


def _add_round_key(state, w, round_):
    for c in range(4):
        for r in range(4):
            state[r][c] ^= w[round_ * 4 + c][r]


def _sub_bytes(state):
    for r in range(4):
        for c in range(4):
            state[r][c] = _SBOX[state[r][c]]


def _shift_rows(state):
    for r in range(1, 4):
        state[r] = state[r][r:] + state[r][:r]


def _mix_columns(state):
    for c in range(4):
        col = [state[r][c] for r in range(4)]
        state[0][c] = _gmul(col[0], 2) ^ _gmul(col[1], 3) ^ col[2] ^ col[3]
        state[1][c] = col[0] ^ _gmul(col[1], 2) ^ _gmul(col[2], 3) ^ col[3]
        state[2][c] = col[0] ^ col[1] ^ _gmul(col[2], 2) ^ _gmul(col[3], 3)
        state[3][c] = _gmul(col[0], 3) ^ col[1] ^ col[2] ^ _gmul(col[3], 2)


def aes128_encrypt_block(key: bytes, block: bytes) -> bytes:
    assert len(key) == 16 and len(block) == 16
    w = _key_expansion(key)
    state = [[block[r + 4 * c] for c in range(4)] for r in range(4)]
    _add_round_key(state, w, 0)
    for round_ in range(1, 10):
        _sub_bytes(state)
        _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, w, round_)
    _sub_bytes(state)
    _shift_rows(state)
    _add_round_key(state, w, 10)
    return bytes(state[r][c] for c in range(4) for r in range(4))


# Validate against the canonical FIPS-197 Appendix B/C AES-128 test vector.
_fips_key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
_fips_pt = bytes.fromhex("00112233445566778899aabbccddeeff")
_fips_ct = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
_got = aes128_encrypt_block(_fips_key, _fips_pt)
assert _got == _fips_ct, f"AES-128 core FAILED FIPS-197 vector: got {_got.hex()}"

# ---------------------------------------------------------------------------
# GCM mode (NIST SP 800-38D), from scratch, built on the validated AES core.
# ---------------------------------------------------------------------------

_R = 0xE1000000000000000000000000000000


def _gf_mult(x: int, y: int) -> int:
    z = 0
    v = y
    for i in range(128):
        if (x >> (127 - i)) & 1:
            z ^= v
        if v & 1:
            v = (v >> 1) ^ _R
        else:
            v = v >> 1
    return z


def _ghash(h_int: int, data: bytes) -> int:
    y = 0
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        block = block + b"\x00" * (16 - len(block))
        y = _gf_mult(y ^ int.from_bytes(block, "big"), h_int)
    return y


def _inc32(block: bytes) -> bytes:
    counter = int.from_bytes(block[12:16], "big")
    counter = (counter + 1) & 0xFFFFFFFF
    return block[:12] + counter.to_bytes(4, "big")


def _ctr_encrypt(key: bytes, icb: bytes, data: bytes) -> bytes:
    out = bytearray()
    counter = icb
    for i in range(0, len(data), 16):
        counter = _inc32(counter) if i > 0 else counter
        ks = aes128_encrypt_block(key, counter)
        chunk = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(chunk, ks))
    return bytes(out)


def aes128_gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """Returns ciphertext || 16-byte tag. `nonce` must be 12 bytes (96-bit IV case)."""
    assert len(nonce) == 12
    h = aes128_encrypt_block(key, b"\x00" * 16)
    h_int = int.from_bytes(h, "big")
    j0 = nonce + b"\x00\x00\x00\x01"

    if plaintext:
        counter = j0
        out = bytearray()
        for i in range(0, len(plaintext), 16):
            counter = _inc32(counter)
            ks = aes128_encrypt_block(key, counter)
            chunk = plaintext[i:i + 16]
            out += bytes(a ^ b for a, b in zip(chunk, ks))
        ciphertext = bytes(out)
    else:
        ciphertext = b""

    aad_padded_len = (len(aad) + 15) // 16 * 16 if aad else 0
    ct_padded_len = (len(ciphertext) + 15) // 16 * 16 if ciphertext else 0
    ghash_input = (
        aad + b"\x00" * (aad_padded_len - len(aad))
        + ciphertext + b"\x00" * (ct_padded_len - len(ciphertext))
        + struct.pack(">QQ", len(aad) * 8, len(ciphertext) * 8)
    )
    s = _ghash(h_int, ghash_input)
    ek_j0 = aes128_encrypt_block(key, j0)
    tag_int = s ^ int.from_bytes(ek_j0, "big")
    tag = tag_int.to_bytes(16, "big")
    return ciphertext + tag


# Validate against NIST GCM Test Case 1 (all-empty, all-zero key/IV).
_tc1_key = bytes(16)
_tc1_nonce = bytes(12)
_tc1_expected_tag = bytes.fromhex("58e2fccefa7e3061367f1d57a4e7455a")
_tc1_got = aes128_gcm_encrypt(_tc1_key, _tc1_nonce, b"", b"")
assert _tc1_got == _tc1_expected_tag, f"GCM FAILED NIST Test Case 1: got {_tc1_got.hex()}"

# Validate against NIST GCM Test Case 2 (zero key/IV, one all-zero plaintext block).
_tc2_pt = bytes(16)
_tc2_expected = bytes.fromhex("0388dace60b6a392f328c2b971b2fe78" "ab6e47d42cec13bdf53a67b21257bddf")
_tc2_got = aes128_gcm_encrypt(_tc1_key, _tc1_nonce, _tc2_pt, b"")
assert _tc2_got == _tc2_expected, f"GCM FAILED NIST Test Case 2: got {_tc2_got.hex()}"

# stderr, not stdout: stdout is the JSON document, and this banner used to corrupt
# `python _gen_golden_vectors.py > golden_vectors.json`.
print("AES-128-GCM from-scratch implementation validated against FIPS-197 + NIST GCM Test Cases 1 & 2.",
      file=sys.stderr)

# ---------------------------------------------------------------------------
# Wire framing (wire_protocol.md), from scratch.
# ---------------------------------------------------------------------------

MAGIC = 0xC9
VERSION = 1

PACKET_TYPES = {
    "HELLO": 0x01,
    "HELLO_ACK": 0x02,
    "TIME_SYNC_REQ": 0x03,
    "TIME_SYNC_RESP": 0x04,
    "SENSOR_FRAME": 0x10,
    "RFID_EVENT": 0x11,
    "HEARTBEAT": 0x20,
    "ACK": 0x21,
    "COMBAT_FEEDBACK": 0x30,
    "ASSESS_ECHO_REQ": 0x40,
    "ASSESS_ECHO_RESP": 0x41,
    "ASSESS_FILE_CHUNK": 0x42,
    "ASSESS_FILE_ACK": 0x43,
}

PSK = bytes.fromhex("000102030405060708090a0b0c0d0e0f")


def crc16_ccitt_false(data: bytes) -> int:
    """Bit-by-bit reference impl, verbatim from wire_protocol.md §7."""
    crc = 0xFFFF
    for b in data:
        crc ^= (b << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


# Sanity-check the CRC against the standard CRC-16/CCITT-FALSE check value for ASCII "123456789".
assert crc16_ccitt_false(b"123456789") == 0x29B1, "CRC-16/CCITT-FALSE reference impl is wrong"


def header_bytes(packet_type: int, seq: int, player_id: int, payload_len: int, more_fragments: bool = False) -> bytes:
    type_byte = packet_type | (0x80 if more_fragments else 0x00)
    return struct.pack("<BBBHBH", MAGIC, VERSION, type_byte, seq, player_id, payload_len)


def build_frame(*, packet_type: int, seq: int, player_id: int, wire_payload: bytes) -> bytes:
    hdr = header_bytes(packet_type, seq, player_id, len(wire_payload))
    body = hdr + wire_payload
    crc = crc16_ccitt_false(body)
    return body + struct.pack("<H", crc)


def derive_nonce(id_field: int, player_id: int, direction: int, ext_seq: int) -> bytes:
    return struct.pack("<IBBIH", id_field, player_id, direction, ext_seq, 0x0000)


def seal(nonce: bytes, aad: bytes, plaintext: bytes) -> bytes:
    return aes128_gcm_encrypt(PSK, nonce, plaintext, aad)


def normal_vector(name, packet_type, *, session_id, player_id, direction, seq, ext_seq, plaintext):
    aad_len = len(plaintext) + 16
    aad = header_bytes(packet_type, seq, player_id, aad_len)
    nonce = derive_nonce(session_id, player_id, direction, ext_seq)
    sealed = seal(nonce, aad, plaintext)
    assert len(sealed) == len(plaintext) + 16
    frame = build_frame(packet_type=packet_type, seq=seq, player_id=player_id, wire_payload=sealed)
    return {
        "name": name, "packet_type": packet_type, "session_id": session_id,
        "player_id": player_id, "direction": direction, "seq": seq, "ext_seq": ext_seq,
        "plaintext_hex": plaintext.hex(), "nonce_hex": nonce.hex(), "aad_hex": aad.hex(),
        "wire_frame_hex": frame.hex(),
    }


def handshake_vector(name, packet_type, *, device_nonce, player_id, direction, seq, ext_seq, plaintext):
    aad_len = 4 + len(plaintext) + 16
    header = header_bytes(packet_type, seq, player_id, aad_len)
    device_nonce_clear = struct.pack("<I", device_nonce)
    aad = header + device_nonce_clear
    nonce = derive_nonce(device_nonce, player_id, direction, ext_seq)
    sealed = seal(nonce, aad, plaintext)
    wire_payload = device_nonce_clear + sealed
    assert len(wire_payload) == aad_len
    frame = build_frame(packet_type=packet_type, seq=seq, player_id=player_id, wire_payload=wire_payload)
    return {
        "name": name, "packet_type": packet_type, "device_nonce": device_nonce,
        "player_id": player_id, "direction": direction, "seq": seq, "ext_seq": ext_seq,
        "plaintext_hex": plaintext.hex(), "nonce_hex": nonce.hex(), "aad_hex": aad.hex(),
        "wire_frame_hex": frame.hex(),
    }


vectors = []

device_nonce = 0xDEADBEEF
hello_plaintext = struct.pack(
    "<6sBBHIH",
    bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF]), 1, 1, 100, device_nonce, 0,
)
vectors.append(handshake_vector(
    "HELLO", PACKET_TYPES["HELLO"],
    device_nonce=device_nonce, player_id=1, direction=0, seq=0, ext_seq=0,
    plaintext=hello_plaintext,
))

hello_ack_plaintext = struct.pack("<IIII", 0xCAFEBABE, 0x5F5E1000, 0x00000001, device_nonce)
vectors.append(handshake_vector(
    "HELLO_ACK", PACKET_TYPES["HELLO_ACK"],
    device_nonce=device_nonce, player_id=1, direction=1, seq=0, ext_seq=0,
    plaintext=hello_ack_plaintext,
))

SESSION_ID = 0xCAFEBABE

vectors.append(normal_vector(
    "TIME_SYNC_REQ", PACKET_TYPES["TIME_SYNC_REQ"],
    session_id=SESSION_ID, player_id=1, direction=0, seq=1, ext_seq=1,
    plaintext=struct.pack("<I", 1000),
))

vectors.append(normal_vector(
    "TIME_SYNC_RESP", PACKET_TYPES["TIME_SYNC_RESP"],
    session_id=SESSION_ID, player_id=1, direction=1, seq=1, ext_seq=1,
    plaintext=struct.pack("<III", 1000, 1050, 1060),
))

sensor_frame_plaintext = struct.pack(
    "<IBB6h6h6h",
    123456, 0x04, 1,
    100, -100, 200, -200, 300, -300,
    10, -10, 20, -20, 30, -30,
    0, 0, 0, 0, 0, 0,
)
vectors.append(normal_vector(
    "SENSOR_FRAME", PACKET_TYPES["SENSOR_FRAME"],
    session_id=SESSION_ID, player_id=1, direction=0, seq=42, ext_seq=42,
    plaintext=sensor_frame_plaintext,
))

rfid_plaintext = struct.pack(
    "<IBB6s", 200000, 2, 0, bytes([0xDE, 0xAD, 0xBE, 0xEF, 0xCA, 0xFE]),
)
vectors.append(normal_vector(
    "RFID_EVENT", PACKET_TYPES["RFID_EVENT"],
    session_id=SESSION_ID, player_id=1, direction=0, seq=7, ext_seq=7,
    plaintext=rfid_plaintext,
))

heartbeat_plaintext = struct.pack("<IHHHH", 50000, 3700, 3650, 3600, 0)
vectors.append(normal_vector(
    "HEARTBEAT", PACKET_TYPES["HEARTBEAT"],
    session_id=SESSION_ID, player_id=1, direction=0, seq=3, ext_seq=3,
    plaintext=heartbeat_plaintext,
))

ack_plaintext = struct.pack("<H", 42)
vectors.append(normal_vector(
    "ACK", PACKET_TYPES["ACK"],
    session_id=SESSION_ID, player_id=1, direction=1, seq=9, ext_seq=9,
    plaintext=ack_plaintext,
))

combat_feedback_plaintext = struct.pack("<BBHH", 1, 90, 180, 1)
vectors.append(normal_vector(
    "COMBAT_FEEDBACK", PACKET_TYPES["COMBAT_FEEDBACK"],
    session_id=SESSION_ID, player_id=1, direction=1, seq=2, ext_seq=2,
    plaintext=combat_feedback_plaintext,
))

# --- Assessment/demo extension types (0x40-0x4F) -------------------------------------------------
# Directions matter here and are easy to get backwards: _REQ/_CHUNK are laptop -> device
# (direction 1), _RESP/_ACK are device -> laptop (direction 0). Getting one wrong still produces a
# self-consistent vector, so it would pass a round-trip test while disagreeing with the firmware.

# trace_id, sent_ms, keycode ('a'), payload_kind, value_a..c
assess_echo_req_plaintext = struct.pack("<IBBIBB18h", 0x11223344, 0x61, 2, 777000, 0, 1,
    1000, -2000, 3000, 164, 0, -82, 0, 0, 2048, 0, 0, 0, 0, 0, 2048, 0, 0, 0)
vectors.append(normal_vector(
    "ASSESS_ECHO_REQ", PACKET_TYPES["ASSESS_ECHO_REQ"],
    session_id=SESSION_ID, player_id=1, direction=1, seq=11, ext_seq=11,
    plaintext=assess_echo_req_plaintext,
))

# trace_id echoed, device_timestamp_ms, keycode echoed, payload_kind echoed, transform_id, reserved,
# transformed value_a..c (here: negate, transform_id 1)
assess_echo_resp_plaintext = struct.pack(
    "<IBBBBIBB18h", 0x11223344, 0x61, 2, 1, 0, 777123, 0, 1,
    -1000, 2000, -3000, 164, 0, -82, 0, 0, 2048, 0, 0, 0, 0, 0, 2048, 0, 0, 0,
)
vectors.append(normal_vector(
    "ASSESS_ECHO_RESP", PACKET_TYPES["ASSESS_ECHO_RESP"],
    session_id=SESSION_ID, player_id=1, direction=0, seq=12, ext_seq=12,
    plaintext=assess_echo_resp_plaintext,
))

# chunk_index, total_chunks, chunk_len, reserved, data[32] (11 valid bytes, zero-padded -- exercises
# the padding path, which is the part a fixed-length BYTES field makes easy to get wrong)
assess_file_chunk_plaintext = struct.pack(
    "<IHHBB32s", 123, 3, 7, 11, 0, b"hello world",
)
assert len(assess_file_chunk_plaintext) == 42
vectors.append(normal_vector(
    "ASSESS_FILE_CHUNK", PACKET_TYPES["ASSESS_FILE_CHUNK"],
    session_id=SESSION_ID, player_id=1, direction=1, seq=13, ext_seq=13,
    plaintext=assess_file_chunk_plaintext,
))

# chunk_index, running_crc, received_bytes
assess_file_ack_plaintext = struct.pack("<IHHI", 123, 3, 0x29B1, 107)
vectors.append(normal_vector(
    "ASSESS_FILE_ACK", PACKET_TYPES["ASSESS_FILE_ACK"],
    session_id=SESSION_ID, player_id=1, direction=0, seq=14, ext_seq=14,
    plaintext=assess_file_ack_plaintext,
))

vectors.append(normal_vector(
    "ASSESS_FILE_READ", 0x44, session_id=SESSION_ID, player_id=1, direction=1, seq=15, ext_seq=15,
    plaintext=struct.pack("<IH", 123, 0),
))
vectors.append(normal_vector(
    "ASSESS_FILE_DATA", 0x45, session_id=SESSION_ID, player_id=1, direction=0, seq=16, ext_seq=16,
    plaintext=struct.pack("<IHHBB32s", 123, 0, 11, 11, 0, b"hello world"),
))
output = {"psk_hex": PSK.hex(), "vectors": vectors}
print(json.dumps(output, indent=2))
