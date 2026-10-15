#include <unity.h>
#include "cg_reliable.h"
#include "cg_sequence.h"

void test_stack_rejection_keeps_pending_and_retries_identical_ciphertext() {
    CgReliableSend sender;
    uint8_t encrypted[] = {1, 2, 3, 4};
    sender.begin(encrypted, sizeof(encrypted), 42, 0); // first notify rejected
    TEST_ASSERT_TRUE(sender.active);
    TEST_ASSERT_EQUAL_INT(0, sender.poll(499));
    TEST_ASSERT_EQUAL_INT(1, sender.poll(500));
    TEST_ASSERT_EQUAL_MEMORY(encrypted, sender.frame, sizeof(encrypted));
    TEST_ASSERT_EQUAL_UINT16(42, sender.seq);
    sender.ack(41);
    TEST_ASSERT_TRUE(sender.active);
    sender.ack(42); // delayed ACK from the FIRST attempt still matches
    TEST_ASSERT_FALSE(sender.active);
}

void test_retry_budget_and_new_selection_supersedes_old_ack() {
    CgReliableSend sender;
    uint8_t bytes[] = {42};
    sender.begin(bytes, 1, 100, 0);
    uint32_t deadlines[] = {500, 1500, 3500, 7500};
    for (uint32_t now : deadlines) TEST_ASSERT_EQUAL_INT(1, sender.poll(now));
    TEST_ASSERT_EQUAL_INT(-1, sender.poll(15500));
    TEST_ASSERT_FALSE(sender.active);
    sender.begin(bytes, 1, 101, 16000);
    sender.ack(100);
    TEST_ASSERT_TRUE(sender.active);
    sender.reset();
    TEST_ASSERT_FALSE(sender.active);
}

void test_authenticated_duplicate_cache_identity_expiry_and_reset() {
    CgReliableReceiveCache cache;
    uint8_t authentic[] = {1, 2, 3};
    uint8_t forged[] = {1, 2, 4};
    cache.remember(authentic, 3, 0);
    TEST_ASSERT_TRUE(cache.contains(authentic, 3, 20000));
    TEST_ASSERT_FALSE(cache.contains(forged, 3, 10));
    TEST_ASSERT_FALSE(cache.contains(authentic, 3, 20001));
    cache.reset();
    TEST_ASSERT_FALSE(cache.contains(authentic, 3, 100));
}

void test_dropped_generated_sequence_and_two_player_session_isolation() {
    CgOutgoingSequenceCounter p1, p2;
    CgSequenceTracker rx1, rx2;
    uint32_t ext, received;
    TEST_ASSERT_EQUAL_UINT16(0, p1.next(&ext)); // encoded but rejected by stack
    TEST_ASSERT_EQUAL_UINT16(1, p1.next(&ext));
    TEST_ASSERT_TRUE(rx1.accept(1, &received));
    TEST_ASSERT_EQUAL_UINT32(ext, received);
    TEST_ASSERT_EQUAL_UINT16(0, p2.next(&ext));
    TEST_ASSERT_TRUE(rx2.accept(0, &received));
    for (uint16_t i = 2; i < 1000; i++) TEST_ASSERT_TRUE(rx1.accept(i, &received));
    p1.reset(); rx1.reset();
    TEST_ASSERT_EQUAL_UINT16(0, p1.next(&ext));
    TEST_ASSERT_TRUE(rx1.accept(0, &received));
    TEST_ASSERT_FALSE(rx2.accept(0, &received)); // P1 reset did not reset P2
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_stack_rejection_keeps_pending_and_retries_identical_ciphertext);
    RUN_TEST(test_retry_budget_and_new_selection_supersedes_old_ack);
    RUN_TEST(test_authenticated_duplicate_cache_identity_expiry_and_reset);
    RUN_TEST(test_dropped_generated_sequence_and_two_player_session_isolation);
    return UNITY_END();
}
