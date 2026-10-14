#include <unity.h>
#include "gateway_samples.h"
void setUp() {}
void tearDown() {}
GatewaySample sample(uint8_t role, uint32_t seq = 1) {
    GatewaySample s;
    s.player = 1; s.role = role; s.seq = seq; s.boot = 123;
    s.axes[0] = 100 + role;
    return s;
}
void test_roles_and_missing() {
    GatewaySamples a;
    TEST_ASSERT_TRUE(a.accept(sample(0), 1, 10));
    TEST_ASSERT_TRUE(a.accept(sample(1), 1, 10));
    auto f = a.frame(10, 2);
    TEST_ASSERT_EQUAL(100, f.dominant[0]);
    TEST_ASSERT_EQUAL(101, f.torso[0]);
    TEST_ASSERT_EQUAL(0, f.offhand[0]);
    TEST_ASSERT_EQUAL(CG_FLAG_OFF_STALE, f.flags);
    a.accept(sample(2), 1, 10);
    f = a.frame(10, 2);
    TEST_ASSERT_EQUAL(102, f.offhand[0]);
    TEST_ASSERT_EQUAL(0, f.flags);
}
void test_age_and_clock_wrap() {
    GatewaySamples a;
    a.accept(sample(0), 1, 0xfffffff0u);
    TEST_ASSERT_TRUE(a.fresh(0, 0x10));
    TEST_ASSERT_FALSE(a.fresh(0, 0x100));
    auto f = a.frame(0x100, 0);
    TEST_ASSERT_BITS_HIGH(CG_FLAG_DOM_STALE | CG_FLAG_OFF_STALE | CG_FLAG_CALIBRATING, f.flags);
    TEST_ASSERT_EQUAL(0, f.dominant[0]);
}
void test_duplicate_reorder_restart() {
    GatewaySamples a;
    a.accept(sample(2, 10), 1, 0);
    TEST_ASSERT_FALSE(a.accept(sample(2, 10), 1, 50));
    TEST_ASSERT_FALSE(a.accept(sample(2, 9), 1, 60));
    TEST_ASSERT_FALSE(a.fresh(2, 101)); // duplicates cannot keep a dead sensor fresh
    auto s = sample(2);
    s.boot = 456;
    TEST_ASSERT_FALSE(a.accept(s, 1, 99));
    TEST_ASSERT_TRUE(a.accept(s, 1, 101));
    TEST_ASSERT_FALSE(a.accept(sample(2, 11), 1, 102));
}
void test_sequence_wrap() {
    GatewaySamples a;
    TEST_ASSERT_TRUE(a.accept(sample(2, 0xffffffffu), 1, 0));
    TEST_ASSERT_TRUE(a.accept(sample(2, 0), 1, 20));
    TEST_ASSERT_FALSE(a.accept(sample(2, 0xffffffffu), 1, 30));
}
void test_identity_and_format() {
    GatewaySamples a;
    auto s = sample(2);
    TEST_ASSERT_FALSE(a.accept(s, 2, 0));
    s.role = 3; TEST_ASSERT_FALSE(a.accept(s, 1, 0));
    s.role = 2; s.version = 2; TEST_ASSERT_FALSE(a.accept(s, 1, 0));
    s.version = 1; s.magic = 0; TEST_ASSERT_FALSE(a.accept(s, 1, 0));
    s.magic = 0x4743; s.reserved = 1; TEST_ASSERT_FALSE(a.accept(s, 1, 0));
}
void test_units_and_clipping() {
    TEST_ASSERT_EQUAL(2048, gatewayScale(1, 2048));
    TEST_ASSERT_EQUAL(-1640, gatewayScale(-100, 16.4f));
    TEST_ASSERT_EQUAL(32767, gatewayScale(2000, 16.4f));
    TEST_ASSERT_EQUAL(-32768, gatewayScale(-2000, 16.4f));
}
int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_roles_and_missing);
    RUN_TEST(test_age_and_clock_wrap);
    RUN_TEST(test_duplicate_reorder_restart);
    RUN_TEST(test_sequence_wrap);
    RUN_TEST(test_identity_and_format);
    RUN_TEST(test_units_and_clipping);
    return UNITY_END();
}
