#include <unity.h>
#include "gateway_segments.h"
void setUp() {}
void tearDown() {}

// Too big for a test function's stack frame.
static GatewayHistory h;
static GatewaySegmentQueue q;
static int16_t win[GATEWAY_WINDOW_ROWS][GATEWAY_AXES];

static void axes(int16_t out[6], int sensor, uint32_t t) {
    for (int a = 0; a < 6; a++) out[a] = (int16_t)(sensor * 1000 + a * 100 + (t / 20) % 50);
}

// All three sensors for [t0, t1): the hub at 100 Hz, nodes at 50 Hz with a 7 ms clock offset.
static void record(uint32_t t0, uint32_t t1, bool left = true, bool torso = true) {
    int16_t v[6];
    for (uint32_t t = t0; t < t1; t += 10) {
        axes(v, 0, t);
        h.put(0, t, v);
        if (t % 20 == 0) {
            axes(v, 1, t);
            if (left) h.put(1, t + 7, v);
            axes(v, 2, t);
            if (torso) h.put(2, t + 3, v);
        }
    }
}

void test_window_has_100_rows_of_all_three_sensors_in_time_order() {
    h = GatewayHistory();
    record(10000, 13000);
    uint32_t end = GatewayHistory::gridOf(12980);
    uint8_t valid = h.window(end, win);
    TEST_ASSERT_EQUAL_UINT8(CG_SEGMENT_FLAG_DOM_VALID | CG_SEGMENT_FLAG_OFF_VALID | CG_SEGMENT_FLAG_TORSO_VALID, valid);
    // row k is t = 11000 + 20k; columns 0-5 right, 6-11 left, 12-17 torso
    for (int k = 0; k < GATEWAY_WINDOW_ROWS; k += 33) {
        uint32_t t = 11000 + 20u * k;
        int16_t r[6], l[6], o[6];
        axes(r, 0, t); axes(l, 1, t); axes(o, 2, t);
        TEST_ASSERT_EQUAL_INT16_ARRAY(r, &win[k][0], 6);
        TEST_ASSERT_EQUAL_INT16_ARRAY(l, &win[k][6], 6);
        TEST_ASSERT_EQUAL_INT16_ARRAY(o, &win[k][12], 6);
    }
}

void test_missing_node_is_flagged_invalid_and_gaps_repeat_the_previous_row() {
    h = GatewayHistory();
    record(10000, 13000, /*left=*/false);
    // knock out one torso step mid-window
    GatewayHistory::Row &gap = h.rows[GatewayHistory::gridOf(12000) % GATEWAY_HISTORY_ROWS];
    gap.present &= (uint8_t)~(1u << 2);
    uint8_t valid = h.window(GatewayHistory::gridOf(12980), win);
    TEST_ASSERT_EQUAL_UINT8(CG_SEGMENT_FLAG_DOM_VALID | CG_SEGMENT_FLAG_TORSO_VALID, valid);
    TEST_ASSERT_EQUAL_INT16_ARRAY(&win[49][12], &win[50][12], 6); // t=12000 repeats t=11980
    TEST_ASSERT_EQUAL_INT16(0, win[10][6]);                        // left never measured: zeros
}

void test_old_steps_are_not_reused_after_the_ring_wraps() {
    h = GatewayHistory();
    record(10000, 14000);
    // a window ending 3.5 s back: its first rows were overwritten by newer steps
    uint8_t valid = h.window(GatewayHistory::gridOf(10500), win);
    TEST_ASSERT_EQUAL_UINT8(0, valid);
}

void test_node_clock_uses_the_smallest_delay_and_forgets_a_reboot() {
    GatewayNodeClock c;
    // node clock = hub clock - 5000; radio delay 30, 12, 45 ms
    TEST_ASSERT_EQUAL_UINT32(1030, c.toHub(1000 - 5000u + 0, 1030, 9));
    // offset is now 5012: the true 5000 plus the smallest delay seen (12 ms), the best it can know
    TEST_ASSERT_EQUAL_UINT32(1032, c.toHub(1020 - 5000u, 1032, 9));
    TEST_ASSERT_EQUAL_UINT32(1052, c.toHub(1040 - 5000u, 1085, 9));   // late sample: still 5012
    TEST_ASSERT_EQUAL_UINT32(2000, c.toHub(100, 2000, 10));            // rebooted node: fresh offset
}

void test_window_end_centres_the_stroke_within_the_wait_limit() {
    // stroke 1000..1600 accepted at 2000: centre 1300, end 1300+980=2280 (within 2000+400)
    TEST_ASSERT_EQUAL_UINT32(2280, gatewayWindowEndMs(1000, 1600, 2000, 400));
    // accepted at 1700: the end would be 580 ms away, so it is pulled in to 2100
    TEST_ASSERT_EQUAL_UINT32(2100, gatewayWindowEndMs(1000, 1600, 1700, 400));
    // a stroke long past: the window is pushed later so it is still in the 4 s history
    TEST_ASSERT_EQUAL_UINT32(10000 - 1800, gatewayWindowEndMs(1000, 1600, 10000, 400));
}

void test_three_imu_chunks_carry_5_rows_of_18_and_the_valid_flags() {
    q = GatewaySegmentQueue();
    GatewaySegment *s = q.begin(77, 2, 0);
    for (int k = 0; k < GATEWAY_WINDOW_ROWS; k++)
        for (int a = 0; a < GATEWAY_AXES; a++) s->rows[k][a] = (int16_t)(k * 100 + a);
    s->count = GATEWAY_WINDOW_ROWS;
    s->first_ms = 5000;
    s->valid = CG_SEGMENT_FLAG_DOM_VALID | CG_SEGMENT_FLAG_TORSO_VALID;
    TEST_ASSERT_TRUE(q.end());
    const GatewaySegment &r = q.slots[q.take(0, 1500)];
    TEST_ASSERT_EQUAL_UINT8(20, gatewaySegmentChunks(r));
    cg_payload_segment_chunk_t c;
    gatewaySegmentChunk(r, 19, c);
    TEST_ASSERT_EQUAL_UINT8(CG_SEGMENT_FLAG_THREE_IMU | CG_SEGMENT_FLAG_DOM_VALID | CG_SEGMENT_FLAG_TORSO_VALID, c.flags);
    TEST_ASSERT_EQUAL_UINT8(5, c.sample_count);
    TEST_ASSERT_EQUAL_UINT8(20, c.chunk_count);
    TEST_ASSERT_EQUAL_UINT8(2, c.weapon_id);
    TEST_ASSERT_EQUAL_UINT32(5000 + 95 * 20, c.first_sample_ms);
    TEST_ASSERT_EQUAL_INT16(9500, c.samples3[0][0]);  // row 95, right ax
    TEST_ASSERT_EQUAL_INT16(9917, c.samples3[4][17]); // row 99, torso gz
    // the same bytes read as the legacy layout: row 0 col 6 is the 7th int16
    TEST_ASSERT_EQUAL_INT16(9506, c.samples[1][0]);
}

void test_full_queue_drops_oldest_unsent_never_the_one_sending() {
    q = GatewaySegmentQueue();
    for (uint32_t id = 1; id <= 4; id++) {
        GatewaySegment *s = q.begin(id, 1, 0);
        TEST_ASSERT_NOT_NULL(s);
        s->count = 1;
        q.end();
        if (id == 1) TEST_ASSERT_EQUAL_UINT32(1, q.slots[q.take(0, 1500)].id); // 1 is sending
    }
    TEST_ASSERT_EQUAL_UINT32(1, q.dropped);
    TEST_ASSERT_EQUAL_UINT32(2, q.last_dropped_id);
}

void test_stale_and_empty_segments_are_not_sent() {
    q = GatewaySegmentQueue();
    q.begin(1, 1, 0);
    TEST_ASSERT_FALSE(q.end()); // no rows
    GatewaySegment *s = q.begin(2, 1, 0);
    s->count = 1;
    q.end();
    TEST_ASSERT_EQUAL_INT(-1, q.take(2000, 1500)); // queued 2 s ago
    TEST_ASSERT_EQUAL_UINT32(1, q.dropped);
}

int main() {
    UNITY_BEGIN();
    RUN_TEST(test_window_has_100_rows_of_all_three_sensors_in_time_order);
    RUN_TEST(test_missing_node_is_flagged_invalid_and_gaps_repeat_the_previous_row);
    RUN_TEST(test_old_steps_are_not_reused_after_the_ring_wraps);
    RUN_TEST(test_node_clock_uses_the_smallest_delay_and_forgets_a_reboot);
    RUN_TEST(test_window_end_centres_the_stroke_within_the_wait_limit);
    RUN_TEST(test_three_imu_chunks_carry_5_rows_of_18_and_the_valid_flags);
    RUN_TEST(test_full_queue_drops_oldest_unsent_never_the_one_sending);
    RUN_TEST(test_stale_and_empty_segments_are_not_sent);
    return UNITY_END();
}
