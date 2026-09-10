"""Unit tests for `cg_comms.protocol.crc.crc16`.

Derived from `shared/protocol/wire_protocol.md` §7 (CRC-16/CCITT-FALSE: poly `0x1021`, init
`0xFFFF`, no input/output reflection, no final XOR) — NOT from reading `codec.py` or `crypto.py`.

`crc.py` itself is a small, dependency-free, table-based implementation and is explicitly called
out as safe to import directly (it is the standalone module the plan says exists precisely so a
firmware-parity test can import it in isolation). We both use it directly *and* reimplement the
bit-by-bit reference algorithm from the spec's pseudocode independently, then cross-check the two
agree — this way a bug shared between the table-builder and the reference implementation is far
less likely to hide the same bug in both.
"""

from __future__ import annotations

import pytest

from cg_comms.protocol.crc import crc16

# Bit-by-bit reference implementation, transcribed directly from wire_protocol.md §7:
#
#   uint16_t crc = 0xFFFF;
#   for each byte b in message:
#       crc ^= (uint16_t)b << 8;
#       for i in 0..7:
#           if crc & 0x8000: crc = (crc << 1) ^ 0x1021;
#           else:            crc = crc << 1;
#   return crc;
#
# Written independently of crc.py's table-based approach so the two implementations can be
# cross-checked against each other, not just against a single published constant.

_POLY = 0x1021
_INIT = 0xFFFF


def _reference_crc16(data: bytes) -> int:
    crc = _INIT
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ _POLY) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


# The standard published CRC-16/CCITT-FALSE "check value": running the algorithm over the ASCII
# bytes of "123456789" must yield 0x29B1. This is a well-known, codebase-independent constant for
# this exact CRC variant (used to validate implementations across many languages/projects), not
# something specific to this repo.
CCITT_FALSE_CHECK_INPUT = b"123456789"
CCITT_FALSE_CHECK_VALUE = 0x29B1


def test_reference_implementation_matches_published_check_value():
    # Sanity-check our own reimplementation against the published constant before trusting it to
    # validate crc.py below.
    assert _reference_crc16(CCITT_FALSE_CHECK_INPUT) == CCITT_FALSE_CHECK_VALUE


def test_crc16_matches_published_check_value():
    assert crc16(CCITT_FALSE_CHECK_INPUT) == CCITT_FALSE_CHECK_VALUE


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00",
        b"\xff",
        b"\x00\x00\x00\x00",
        b"\xc9\x01\x10\x2a\x00\x01\x3a\x00",  # a plausible 8-byte wire header, arbitrary content
        b"the quick brown fox jumps over the lazy dog",
        bytes(range(256)),
    ],
)
def test_crc16_matches_reference_bit_by_bit_implementation(data: bytes):
    assert crc16(data) == _reference_crc16(data)


def test_empty_input_returns_the_initial_value():
    # With zero bytes processed, the running CRC is never touched and must equal the algorithm's
    # initial value (0xFFFF) — this falls directly out of the pseudocode's structure (the loop
    # body simply never executes), not out of any special-cased behaviour in an implementation.
    assert crc16(b"") == _INIT


def test_single_byte_inputs_are_well_defined_and_nonconstant():
    # Every distinct single byte should (almost always) produce a distinct CRC from every other
    # distinct single byte, and none of them should coincide with the empty-input value.
    values = {b: crc16(bytes([b])) for b in range(256)}
    assert len(set(values.values())) == 256
    assert _INIT not in values.values()


@pytest.mark.parametrize(
    ("data", "byte_index"),
    [
        (b"\x00\x00\x00\x00", 0),
        (b"\x00\x00\x00\x00", 3),
        (b"hello world", 0),
        (b"hello world", 5),
        (b"hello world", 10),
        (CCITT_FALSE_CHECK_INPUT, 4),
    ],
)
def test_changing_one_byte_changes_the_crc(data: bytes, byte_index: int):
    # Spot check, not a formal proof: flip every bit of one byte in turn and confirm the CRC
    # changes each time. (A CRC is a checksum, not a hash — collisions are possible in general —
    # but CRC-16/CCITT-FALSE is specifically designed to catch every single-bit error in messages
    # far longer than the handful of bytes used here, so no collision is expected in this check.)
    original = crc16(data)
    for bit in range(8):
        mutated = bytearray(data)
        mutated[byte_index] ^= 1 << bit
        assert crc16(bytes(mutated)) != original
