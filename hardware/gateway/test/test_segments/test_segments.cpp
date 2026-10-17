#include <unity.h>
#include "gateway_segments.h"
void setUp() {}
void tearDown() {}

static GatewaySegmentQueue q; // ~7 KB: too big for a test function's stack frame

static void fill(uint32_t id, uint16_t source, uint32_t t0 = 0, uint32_t now = 0) {
    q.begin(id, 1, source, now);
    for (uint16_t i = 0; i < source; i++) {
        int16_t axes[6] = {(int16_t)i, 0, 2048, 0, 0, (int16_t)-i};
        q.add(t0 + 10u * i, axes); // 100 Hz source
    }
}

void test_decimates_100hz_to_50hz_and_chunks() {
    q = GatewaySegmentQueue();
    fill(7, 101, 1000);
    TEST_ASSERT_TRUE(q.end());
    int slot = q.take(0, 1500);
    TEST_ASSERT_TRUE(slot >= 0);
    const GatewaySegment &s = q.slots[slot];
    TEST_ASSERT_EQUAL_UINT16(51, s.count); // every other sample of 101
    TEST_ASSERT_EQUAL_INT16(2, s.samples[1][0]);
    TEST_ASSERT_EQUAL_UINT8(4, gatewaySegmentChunks(s));
    cg_payload_segment_chunk_t c;
    gatewaySegmentChunk(s, 3, c);
    TEST_ASSERT_EQUAL_UINT8(3, c.sample_count); // 51 - 48
    TEST_ASSERT_EQUAL_UINT32(1000 + 48 * 20, c.first_sample_ms);
    TEST_ASSERT_EQUAL_INT16(96, c.samples[0][0]);
    TEST_ASSERT_EQUAL_INT16(0, c.samples[3][0]); // unused rows are zero
    TEST_ASSERT_EQUAL_UINT16(GATEWAY_SEGMENT_RATE_HZ, c.sample_rate_hz);
    q.done(slot);
    TEST_ASSERT_EQUAL_INT(-1, q.take(0, 1500));
}

void test_overlong_segment_keeps_its_centre() {
    q = GatewaySegmentQueue();
    fill(1, 2 * GATEWAY_SEGMENT_MAX_SAMPLES + 100);
    q.end();
    const GatewaySegment &s = q.slots[q.take(0, 1500)];
    TEST_ASSERT_EQUAL_UINT16(GATEWAY_SEGMENT_MAX_SAMPLES, s.count);
    TEST_ASSERT_EQUAL_INT16(50, s.samples[0][0]);
}

void test_full_queue_drops_oldest_unsent_never_the_one_sending() {
    q = GatewaySegmentQueue();
    fill(1, 10); q.end();
    int sending = q.take(0, 1500);
    fill(2, 10); q.end();
    fill(3, 10); q.end();
    fill(4, 10); q.end(); // no free slot: segment 2 (oldest READY) is dropped
    TEST_ASSERT_EQUAL_UINT32(1, q.dropped);
    TEST_ASSERT_EQUAL_UINT32(2, q.last_dropped_id);
    TEST_ASSERT_EQUAL_UINT32(1, q.slots[sending].id);
    q.done(sending);
    TEST_ASSERT_EQUAL_UINT32(3, q.slots[q.take(0, 1500)].id);
}

void test_stale_and_empty_segments_are_not_sent() {
    q = GatewaySegmentQueue();
    q.begin(5, 1, 0, 0);
    TEST_ASSERT_FALSE(q.end());
    fill(6, 10, 0, 0); q.end();
    TEST_ASSERT_EQUAL_INT(-1, q.take(2000, 1500));
    TEST_ASSERT_EQUAL_UINT32(6, q.last_dropped_id);
}

int main() {
    UNITY_BEGIN();
    RUN_TEST(test_decimates_100hz_to_50hz_and_chunks);
    RUN_TEST(test_overlong_segment_keeps_its_centre);
    RUN_TEST(test_full_queue_drops_oldest_unsent_never_the_one_sending);
    RUN_TEST(test_stale_and_empty_segments_are_not_sent);
    return UNITY_END();
}
