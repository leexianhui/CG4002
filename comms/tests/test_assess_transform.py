"""Pins the laptop<->firmware transform contract (relay/assess_transform.py and
lib/cg_protocol/cg_assess_transform.h). These expected values are the shared contract: if a change
here is not mirrored in the C header, the round-trip verification in the demo silently reports
mismatches, so this test failing is the intended early warning.
"""

from __future__ import annotations

import pytest

from cg_comms.relay.assess_transform import (
    I16_MAX,
    I16_MIN,
    TRANSFORM_ADD_SEQ,
    TRANSFORM_NEGATE,
    TRANSFORM_SWAP_SCALE,
    apply_transform,
    saturate_i16,
    transform_for,
)


def test_saturate_clamps_both_ends():
    assert saturate_i16(I16_MAX + 1) == I16_MAX
    assert saturate_i16(I16_MIN - 1) == I16_MIN
    assert saturate_i16(0) == 0


def test_negate_saturates_int16_min():
    # -(-32768) is not representable in int16. Python would return 32768 and C is undefined; both
    # ends must clamp to 32767. This is exactly the "extremes" dummy variant.
    # Note only the first element saturates: -I16_MAX is -32767, which is perfectly representable.
    assert apply_transform(TRANSFORM_NEGATE, (I16_MIN, I16_MAX, 0), 0x61) == (I16_MAX, -I16_MAX, 0)


def test_add_seq_mixes_keycode_and_saturates():
    assert apply_transform(TRANSFORM_ADD_SEQ, (1000, -2000, 3000), 0x61) == (1097, -1903, 3097)
    assert apply_transform(TRANSFORM_ADD_SEQ, (I16_MAX, 0, 0), 0x01) == (I16_MAX, 1, 1)


def test_swap_scale_reorders_and_scales():
    assert apply_transform(TRANSFORM_SWAP_SCALE, (1000, -2000, 3000), 0x61) == (6000, 500, 2000)
    assert apply_transform(TRANSFORM_SWAP_SCALE, (0, 0, I16_MAX), 0x61) == (I16_MAX, 0, 0)


def test_transform_for_covers_every_transform_and_stays_in_range():
    seen = {transform_for(k) for k in range(32, 127)}  # printable ASCII
    assert seen == {TRANSFORM_NEGATE, TRANSFORM_ADD_SEQ, TRANSFORM_SWAP_SCALE}


def test_unknown_transform_is_rejected():
    with pytest.raises(ValueError):
        apply_transform(99, (0, 0, 0), 0x61)


def test_div2_truncates_toward_zero_like_c():
    # Python's // floors; C's / truncates. They differ for every negative odd numerator, and
    # TRANSFORM_NEGATE readily produces those from small positive inputs (the "primes" variant).
    # If this regresses, the demo reports a transform mismatch on a perfectly healthy link.
    from cg_comms.relay.assess_transform import div2_trunc

    assert div2_trunc(-1001) == -500  # Python // would give -501
    assert div2_trunc(-1) == 0  # Python // would give -1
    assert div2_trunc(1001) == 500
    assert div2_trunc(-1000) == -500
    assert apply_transform(TRANSFORM_SWAP_SCALE, (-7, 0, 0), 0x61) == (0, -3, 0)
