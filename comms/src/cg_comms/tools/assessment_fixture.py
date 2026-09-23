"""Deterministic binary fixture for the comms-assessment file transfer (relay F key).

    python -m cg_comms.tools.assessment_fixture --output logs/assessment/input.bin [--size 1037]

The default 1037 bytes is deliberately NOT a multiple of the 32-byte chunk, so the final chunk is
short and zero-padded on the wire -- the edge a naive transfer gets wrong. Content, in order:

    a readable ASCII marker  (so a hex dump of the first chunk is recognisably "this file")
    a run of 0x00            (zeros must not be treated as padding or end-of-data)
    0x80..0xFF               (high bytes: catches any signed-char / text-mode mangling)
    a repeating counter      (fills to --size; every byte position differs from its neighbour)

Same --size => byte-identical file on every machine, so the SHA-256 printed here can be written down
before the demo and compared with the `assess_file_start` / `assess_file_verified` log lines.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from cg_comms.protocol.crc import crc16
from cg_comms.relay.assess_file import MAX_FILE_BYTES

DEFAULT_SIZE = 1037
_MARKER = b"CG4002 comms assessment file transfer fixture v1\n"


def build_fixture(size: int = DEFAULT_SIZE) -> bytes:
    if not 1 <= size <= MAX_FILE_BYTES:
        raise ValueError(f"size must be 1..{MAX_FILE_BYTES} bytes, got {size}")
    body = bytearray(_MARKER)
    body += bytes(64)
    body += bytes(range(0x80, 0x100))
    counter = 0
    while len(body) < size:
        body.append(counter & 0xFF)
        counter += 7  # coprime with 256: cycles through every byte value
    return bytes(body[:size])


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the deterministic assessment transfer fixture")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE,
                        help=f"Bytes, 1..{MAX_FILE_BYTES} (default {DEFAULT_SIZE})")
    args = parser.parse_args()
    data = build_fixture(args.size)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)
    print(f"wrote {args.output} ({len(data)} B, {-(-len(data) // 32)} chunks)")
    print(f"sha256    {hashlib.sha256(data).hexdigest()}")
    print(f"crc16     0x{crc16(data):04X}  (CRC-16/CCITT-FALSE, what the FireBeetle reports)")


if __name__ == "__main__":
    main()
