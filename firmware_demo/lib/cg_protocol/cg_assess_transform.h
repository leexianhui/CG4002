/*
 * Device-side modification applied to ASSESS_ECHO_REQ, echoed back in ASSESS_ECHO_RESP.
 *
 * Assessment requirement: "The FireBeetle will modify the data and send it back to the Ultra96 via
 * the laptop." The laptop recomputes the same transform and compares, so the demo proves the
 * firmware actually ran rather than that the link echoes bytes.
 *
 * This is one half of a two-sided contract. The Python half is
 * comms/src/cg_comms/relay/assess_transform.py; the two must stay identical, and a change to one
 * without the other must be made in the same commit. comms/tests/test_assess_transform_c.py
 * compiles THIS header and diffs it against the Python across the whole int16 range, so a one-sided
 * edit fails the test suite rather than showing up as a mismatch during the demo.
 *
 * Two portability traps are handled deliberately -- do not "simplify" either away:
 *   1. Signed overflow is undefined behaviour in C, and -INT16_MIN is not representable. All
 *      arithmetic is done in int32_t and then saturated.
 *   2. C's `/` truncates toward zero while Python's `//` floors; they differ for every negative odd
 *      numerator. The Python side matches C here (div2_trunc), so plain `/` is correct below.
 */

#ifndef CG_ASSESS_TRANSFORM_H
#define CG_ASSESS_TRANSFORM_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CG_ASSESS_TRANSFORM_NEGATE      1
#define CG_ASSESS_TRANSFORM_ADD_SEQ     2
#define CG_ASSESS_TRANSFORM_SWAP_SCALE  3

/* Clamps a 32-bit intermediate into int16. Mirrors saturate_i16() in assess_transform.py. */
static inline int16_t cg_assess_saturate_i16(int32_t value) {
    if (value < INT16_MIN) return INT16_MIN;
    if (value > INT16_MAX) return INT16_MAX;
    return (int16_t)value;
}

/* Which transform this keystroke selects. Mirrors transform_for(). Lets the demo show different
 * transforms by pressing different keys, with no reflash. */
static inline uint8_t cg_assess_transform_for(uint8_t keycode) {
    return (uint8_t)((keycode % 3) + 1);
}

/* Applies the transform in place. Mirrors apply_transform(). Returns 1 on success, 0 if
 * transform_id is unknown (in which case the values are left untouched). */
static inline int cg_assess_apply_transform(uint8_t transform_id, uint8_t keycode,
                                            int16_t *value_a, int16_t *value_b, int16_t *value_c) {
    const int32_t a = (int32_t)*value_a;
    const int32_t b = (int32_t)*value_b;
    const int32_t c = (int32_t)*value_c;
    switch (transform_id) {
        case CG_ASSESS_TRANSFORM_NEGATE:
            *value_a = cg_assess_saturate_i16(-a);
            *value_b = cg_assess_saturate_i16(-b);
            *value_c = cg_assess_saturate_i16(-c);
            return 1;
        case CG_ASSESS_TRANSFORM_ADD_SEQ:
            *value_a = cg_assess_saturate_i16(a + (int32_t)keycode);
            *value_b = cg_assess_saturate_i16(b + (int32_t)keycode);
            *value_c = cg_assess_saturate_i16(c + (int32_t)keycode);
            return 1;
        case CG_ASSESS_TRANSFORM_SWAP_SCALE:
            /* `a / 2` truncates toward zero; the Python side matches this deliberately. */
            *value_a = cg_assess_saturate_i16(c * 2);
            *value_b = cg_assess_saturate_i16(a / 2);
            *value_c = cg_assess_saturate_i16(-b);
            return 1;
        default:
            return 0;
    }
}

#ifdef __cplusplus
}
#endif

#endif /* CG_ASSESS_TRANSFORM_H */
