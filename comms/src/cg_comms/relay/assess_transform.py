"""Assessment transform contract mirrored by cg_assess_transform.h.

The deliberate b/c/d keyboard paths use IDENTITY=0 and verify all returned raw IMU axes.
Identity did not exist in revision 2; updated firmware is required. Legacy arithmetic
IDs 1..3 remain supported by the low-level protocol for existing fixtures only.
The request key selects the transform; there is no separate transform request field.
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
TRANSFORM_IDENTITY = 0
TRANSFORM_NEGATE = 1
TRANSFORM_ADD_SEQ = 2
TRANSFORM_SWAP_SCALE = 3


def apply_transform(
    transform_id: int, values: tuple[int, int, int], keycode: int
) -> tuple[int, int, int]:
    """Recomputes what the device should have produced. Mirrors cg_assess_apply_transform()."""
    a, b, c = values
    if transform_id == TRANSFORM_IDENTITY:
        return values
    if transform_id == TRANSFORM_NEGATE:
        # Negation, not a bit-flip: -INT16_MIN is not representable, so saturation is load-bearing
        # here and this is the case the "extremes" variant exercises.
        return (saturate_i16(-a), saturate_i16(-b), saturate_i16(-c))
    if transform_id == TRANSFORM_ADD_SEQ:
        # Legacy arithmetic demonstration. Trace/session authentication, not this arithmetic,
        # protects correlation against stale replies.
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
    if keycode in (ord("b"), ord("c"), ord("d")):
        return TRANSFORM_IDENTITY
    return (keycode % 3) + 1  # legacy low-level protocol requests only
