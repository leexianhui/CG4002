"""The device-side modification, mirrored on the laptop so the round trip can be VERIFIED.

Assessment requirement: "The FireBeetle will modify the data and send it back to the Ultra96 via the
laptop." A demo that only shows a packet coming back proves the link echoes; it does not prove the
firmware ran. So the device applies a deterministic transform, and the laptop recomputes the expected
result independently and compares. A mismatch is reported loudly rather than counted as success.

This file is the Python half of a two-sided contract. The C half is
`firmware_demo/lib/cg_protocol/cg_assess_transform.h`, and the two must stay identical: if you change
a formula here, change it there in the same commit. `tests/test_assess_transform.py` pins the values
both sides must produce, so a one-sided edit fails the suite instead of failing silently in a demo.

Two arithmetic differences between Python and C are handled explicitly, because both would produce
a demo that reports a mismatch on a link that is actually working correctly:

- **Overflow.** `value_*` are int16 on the wire and the "extremes" dummy variant deliberately sends
  INT16_MAX/INT16_MIN. C signed overflow is undefined behaviour; Python ints just grow. Both sides
  clamp via `saturate_i16`.
- **Division rounding.** Python's `//` floors (`-1001 // 2 == -501`) while C's `/` truncates toward
  zero (`-1001 / 2 == -500`). They differ for every negative odd numerator -- which TRANSFORM_NEGATE
  readily produces from small positive inputs like the "primes" variant. `div2_trunc` below uses C
  semantics on the Python side; do NOT "simplify" it back to `// 2`.
"""

from __future__ import annotations

I16_MIN = -32768
I16_MAX = 32767


def saturate_i16(value: int) -> int:
    """Clamps to int16. Must match cg_assess_saturate_i16() in the firmware header exactly."""
    if value < I16_MIN:
        return I16_MIN
    if value > I16_MAX:
        return I16_MAX
    return value


def div2_trunc(value: int) -> int:
    """Integer division by 2 truncating toward zero, i.e. C's `value / 2` -- NOT Python's `value // 2`,
    which floors and disagrees for every negative odd value."""
    return -((-value) // 2) if value < 0 else value // 2


# Transform ids travel on the wire as ASSESS_ECHO_RESP.transform_id, so the laptop knows which rule
# the device claims to have applied instead of assuming.
TRANSFORM_NEGATE = 1
TRANSFORM_ADD_SEQ = 2
TRANSFORM_SWAP_SCALE = 3


def apply_transform(
    transform_id: int, values: tuple[int, int, int], keycode: int
) -> tuple[int, int, int]:
    """Recomputes what the device should have produced. Mirrors cg_assess_apply_transform()."""
    a, b, c = values
    if transform_id == TRANSFORM_NEGATE:
        # Negation, not a bit-flip: -INT16_MIN is not representable, so saturation is load-bearing
        # here and this is the case the "extremes" variant exercises.
        return (saturate_i16(-a), saturate_i16(-b), saturate_i16(-c))
    if transform_id == TRANSFORM_ADD_SEQ:
        # Mixes the keystroke in, so two presses of different keys cannot produce identical
        # responses -- that would let a stale/duplicated frame pass as a fresh round trip.
        return (saturate_i16(a + keycode), saturate_i16(b + keycode), saturate_i16(c + keycode))
    if transform_id == TRANSFORM_SWAP_SCALE:
        # Reorders as well as scales, so a byte-order mistake on either side shows up as a
        # mismatch rather than as coincidentally-equal values.
        return (saturate_i16(c * 2), saturate_i16(div2_trunc(a)), saturate_i16(-b))
    raise ValueError(f"unknown transform_id {transform_id}")


def transform_for(keycode: int) -> int:
    """Which transform the device will apply for this keystroke. Derived from the keycode so the
    demo can show different transforms by pressing different keys, with no reflash. Must match
    cg_assess_transform_for() in the firmware header."""
    return (keycode % 3) + 1
