// Replay-window regression test for CgSequenceTracker. Runs natively (env:native_test).
//
// Guards the bug that silently killed the laptop->device direction of a live link ~57s in: the
// window's seen-mask was keyed by `ext_seq % 64` and never evicted anything, so the 64th accepted
// packet aliased onto bit 0 and every packet after it was rejected as a duplicate. Nothing
// detected it — the relay's write-without-response still "succeeded", no BLE disconnect fired, and
// device->laptop kept streaming, so the link looked healthy from both ends while being half dead.
//
// Behaviour here must stay identical to comms/src/cg_comms/protocol/crypto.py's SequenceTracker,
// which is the normative implementation (wire_protocol.md §4).

#include <unity.h>

#include <stdint.h>

#include "cg_sequence.h"

namespace {

// The real-world failure: a long monotonic stream (8 handshake packets, then 1 heartbeat/sec).
// Pre-fix this passed for exactly 64 packets and then rejected everything, forever.
void test_long_monotonic_stream_all_accepted() {
    CgSequenceTracker t;
    t.reset();
    uint32_t ext_seq = 0;
    for (uint32_t i = 1; i <= 5000; i++) {
        uint16_t seq = (uint16_t)(i & 0xFFFF);
        TEST_ASSERT_TRUE_MESSAGE(t.accept(seq, &ext_seq), "monotonic packet rejected as replay");
        TEST_ASSERT_EQUAL_UINT32(i, ext_seq);
    }
}

void test_duplicate_is_rejected() {
    CgSequenceTracker t;
    t.reset();
    uint32_t ext_seq = 0;
    for (uint16_t i = 1; i <= 10; i++) {
        TEST_ASSERT_TRUE(t.accept(i, &ext_seq));
    }
    TEST_ASSERT_FALSE(t.accept(7, &ext_seq));   // in-window replay
    TEST_ASSERT_FALSE(t.accept(10, &ext_seq));  // most recent, re-sent
}

// A packet that fell off the back of the 64-entry window is rejected rather than re-accepted.
void test_below_window_floor_is_rejected() {
    CgSequenceTracker t;
    t.reset();
    uint32_t ext_seq = 0;
    for (uint16_t i = 1; i <= 200; i++) {
        TEST_ASSERT_TRUE(t.accept(i, &ext_seq));
    }
    TEST_ASSERT_FALSE(t.accept(100, &ext_seq));
    TEST_ASSERT_TRUE(t.accept(180, &ext_seq));  // still inside the window
    TEST_ASSERT_EQUAL_UINT32(180, ext_seq);
}

// Out-of-order delivery inside the window is accepted once, in any order.
void test_reordered_within_window() {
    CgSequenceTracker t;
    t.reset();
    uint32_t ext_seq = 0;
    TEST_ASSERT_TRUE(t.accept(10, &ext_seq));
    TEST_ASSERT_TRUE(t.accept(13, &ext_seq));
    TEST_ASSERT_TRUE(t.accept(11, &ext_seq));
    TEST_ASSERT_EQUAL_UINT32(11, ext_seq);
    TEST_ASSERT_TRUE(t.accept(12, &ext_seq));
    TEST_ASSERT_FALSE(t.accept(11, &ext_seq));
}

// A jump of >=64 clears the window wholesale instead of shifting stale bits into range.
void test_large_jump_clears_window() {
    CgSequenceTracker t;
    t.reset();
    uint32_t ext_seq = 0;
    for (uint16_t i = 1; i <= 20; i++) {
        TEST_ASSERT_TRUE(t.accept(i, &ext_seq));
    }
    TEST_ASSERT_TRUE(t.accept(500, &ext_seq));
    TEST_ASSERT_EQUAL_UINT32(500, ext_seq);
    TEST_ASSERT_TRUE(t.accept(501, &ext_seq));
    TEST_ASSERT_FALSE(t.accept(500, &ext_seq));
}

// 16-bit wire seq wrap must produce a continuous ext_seq across the 0xFFFF boundary.
void test_wrap_produces_continuous_ext_seq() {
    CgSequenceTracker t;
    t.reset();
    uint32_t ext_seq = 0;
    TEST_ASSERT_TRUE(t.accept(0xFFFE, &ext_seq));
    uint32_t base = ext_seq;
    TEST_ASSERT_TRUE(t.accept(0xFFFF, &ext_seq));
    TEST_ASSERT_EQUAL_UINT32(base + 1, ext_seq);
    TEST_ASSERT_TRUE(t.accept(0x0000, &ext_seq));
    TEST_ASSERT_EQUAL_UINT32(base + 2, ext_seq);
    TEST_ASSERT_TRUE(t.accept(0x0001, &ext_seq));
    TEST_ASSERT_EQUAL_UINT32(base + 3, ext_seq);
}

// reset() must fully clear the window — a fresh session reuses low seq numbers.
void test_reset_clears_window() {
    CgSequenceTracker t;
    t.reset();
    uint32_t ext_seq = 0;
    for (uint16_t i = 1; i <= 100; i++) {
        TEST_ASSERT_TRUE(t.accept(i, &ext_seq));
    }
    t.reset();
    TEST_ASSERT_TRUE(t.accept(1, &ext_seq));
    TEST_ASSERT_EQUAL_UINT32(1, ext_seq);
}

}  // namespace

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_long_monotonic_stream_all_accepted);
    RUN_TEST(test_duplicate_is_rejected);
    RUN_TEST(test_below_window_floor_is_rejected);
    RUN_TEST(test_reordered_within_window);
    RUN_TEST(test_large_jump_clears_window);
    RUN_TEST(test_wrap_produces_continuous_ext_seq);
    RUN_TEST(test_reset_clears_window);
    return UNITY_END();
}
