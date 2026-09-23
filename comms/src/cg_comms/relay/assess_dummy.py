"""Dummy assessment packet variants — the table the professor will edit during the demo.

Assessment requirement: "You should have multiple dummy packets that will be chosen at random...
Prof will ask you to change the dummy packet data. You may have to recompile and rerun your code."

So the whole point of this module is that it is the ONE place that data lives. Everything else
(assess.py, the firmware transform, the logs) is written against `DUMMY_VARIANTS` generically, so
editing a number below, or adding/removing a whole variant, needs no other file touched.

Requirement: "Dummy data should be in the format of your actual packet... NOT just plain integers
or text messages." These are not free-form blobs: each variant fills `ASSESS_ECHO_REQ`'s declared
fields (protocol/payloads.py) and is sealed with AES-128-GCM + CRC16 by the same codec as every real
packet, so what goes over the air is a genuine protocol frame.

To change the dummy data during the demo:
  1. Edit a `values` tuple, or add a whole `DummyVariant(...)` entry, below.
  2. Restart the relay (no firmware reflash needed -- the device transforms whatever it receives).
Each `values` entry must hold exactly three int16s (-32768..32767); `validate_variants()` is called
at import time, so a typo fails immediately and loudly instead of at the first keystroke.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

I16_MIN = -32768
I16_MAX = 32767


@dataclass(frozen=True)
class DummyVariant:
    """One selectable dummy payload. `kind` goes on the wire as ASSESS_ECHO_REQ.payload_kind and is
    echoed back untouched by the device, so a response can be attributed to its variant even if the
    transformed values are unrecognisable."""

    kind: int
    name: str
    values: tuple[int, int, int]
    note: str = ""


# --- EDIT ME DURING THE DEMO -------------------------------------------------------------------
DUMMY_VARIANTS: tuple[DummyVariant, ...] = (
    DummyVariant(1, "zeros", (0, 0, 0), "identity check -- a transform bug is obvious"),
    DummyVariant(2, "small_mixed", (1000, -2000, 3000), "typical IMU-scale magnitudes"),
    DummyVariant(3, "extremes", (I16_MAX, I16_MIN, 0), "int16 saturation edges"),
    DummyVariant(4, "imu_like", (2048, -1024, 512), "1g, -0.5g, 0.25g at ACCEL_LSB_PER_G"),
    DummyVariant(5, "primes", (7, 13, 31), "small values -- readable raw in a hex dump"),
)
# --- END EDIT ME -------------------------------------------------------------------------------


def validate_variants(variants: tuple[DummyVariant, ...] = DUMMY_VARIANTS) -> None:
    """Fails at import, not at the first keystroke, if the table above was edited badly."""
    if not variants:
        raise ValueError("DUMMY_VARIANTS is empty -- at least one variant is required")
    seen: dict[int, str] = {}
    for v in variants:
        if not 0 <= v.kind <= 255:
            raise ValueError(f"variant {v.name!r}: kind {v.kind} outside u8 range 0..255")
        if v.kind in seen:
            raise ValueError(
                f"variant {v.name!r}: kind {v.kind} already used by {seen[v.kind]!r} -- "
                "payload_kind must be unique or responses cannot be attributed"
            )
        seen[v.kind] = v.name
        if len(v.values) != 3:
            raise ValueError(f"variant {v.name!r}: expected exactly 3 values, got {len(v.values)}")
        for i, value in enumerate(v.values):
            if not I16_MIN <= value <= I16_MAX:
                raise ValueError(
                    f"variant {v.name!r}: values[{i}] = {value} outside int16 range "
                    f"{I16_MIN}..{I16_MAX} (ASSESS_ECHO_REQ.value_* are I16)"
                )


validate_variants()


def choose(rng: random.Random) -> DummyVariant:
    """Picks a variant at random ("chosen at random" per the requirement). Takes an explicit Random
    so a demo run can be seeded and reproduced, and so tests are deterministic."""
    return rng.choice(DUMMY_VARIANTS)
