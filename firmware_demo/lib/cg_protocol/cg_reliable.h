// Bounded reliability state, independent of BLE so retry/duplicate behavior is host-testable.
#pragma once
#include <stdint.h>
#include <string.h>
#include "cg_protocol.h"
#include "cg_frame.h"

class CgReliableSend {
public:
    uint8_t frame[CG_MAX_FRAME_SIZE];
    size_t len = 0;
    uint16_t seq = 0;
    bool active = false;
    uint8_t attempts = 0;
    uint32_t timeout_ms = 500;
    uint32_t next_ms = 0;

    void reset() { active = false; len = 0; }
    void begin(const uint8_t *bytes, size_t size, uint16_t wire_seq, uint32_t now) {
        memcpy(frame, bytes, size);
        len = size;
        seq = wire_seq;
        attempts = 1;
        timeout_ms = 500;
        next_ms = now + timeout_ms;
        active = true; // includes an initial BLE-stack rejection: bytes still need retrying
    }
    void ack(uint16_t acked_seq) { if (active && acked_seq == seq) active = false; }
    // 0: nothing due; 1: resend the SAME ciphertext; -1: exhausted (five sends total).
    int poll(uint32_t now) {
        if (!active || (int32_t)(now - next_ms) < 0) return 0;
        if (attempts >= 5) { active = false; return -1; }
        attempts++;
        timeout_ms *= 2;
        next_ms = now + timeout_ms;
        return 1;
    }
};

class CgReliableReceiveCache {
    struct Entry { uint8_t frame[CG_MAX_FRAME_SIZE]; size_t len = 0; uint32_t at = 0; };
    Entry entries[16];
    unsigned next = 0;
public:
    int64_t highest = -1;
    void reset() { for (auto &entry : entries) entry.len = 0; next = 0; highest = -1; }
    bool contains(const uint8_t *frame, size_t len, uint32_t now) const {
        for (const auto &entry : entries)
            if (entry.len == len && now - entry.at <= 20000 && memcmp(entry.frame, frame, len) == 0)
                return true;
        return false;
    }
    // Only insert after authentication, identity and payload validation.
    void remember(const uint8_t *frame, size_t len, uint32_t now) {
        auto &entry = entries[next++ % 16];
        memcpy(entry.frame, frame, len); entry.len = len; entry.at = now;
    }
};
