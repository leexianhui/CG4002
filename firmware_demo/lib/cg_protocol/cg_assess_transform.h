/* Assessment transform contract mirrored by relay/assess_transform.py.
 * Revision 3: b/c/d select explicit IDENTITY=0; all raw axes remain unchanged.
 * Legacy IDs 1..3 remain available only to low-level protocol fixtures. Arithmetic
 * uses int32 intermediates, saturation and C division truncating toward zero.
 * Python tests mirror/check the formulas; firmware builds compile this header.
 */

#ifndef CG_ASSESS_TRANSFORM_H
#define CG_ASSESS_TRANSFORM_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CG_ASSESS_TRANSFORM_IDENTITY    0
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
    if (keycode == 'b' || keycode == 'c' || keycode == 'd') return CG_ASSESS_TRANSFORM_IDENTITY;
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
        case CG_ASSESS_TRANSFORM_IDENTITY:
            return 1;  // retain every raw axis exactly; metadata still correlates the exchange
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
