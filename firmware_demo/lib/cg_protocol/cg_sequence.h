// Per-direction extended-sequence tracking, mirroring comms/src/cg_comms/protocol/crypto.py
// (OutgoingSequenceCounter / SequenceTracker). This firmware only needs one of each: one outgoing
// counter for device->laptop packets, one tracker for the laptop->device packets it receives
// (HELLO_ACK, TIME_SYNC_RESP, ACK, COMBAT_FEEDBACK).

#pragma once

#include <stdbool.h>
#include <stdint.h>

// Monotonic per-session outgoing counter. seq16 is what goes on the wire; ext_seq feeds nonce
// derivation and never wraps within a session's lifetime (uint32_t is enough for any demo run).
class CgOutgoingSequenceCounter {
public:
    void reset() { next_ext_seq_ = 0; }

    // Returns the wire seq16 and advances; ext_seq is available via last_ext_seq() right after.
    uint16_t next(uint32_t *ext_seq_out) {
        uint32_t ext_seq = next_ext_seq_++;
        *ext_seq_out = ext_seq;
        return (uint16_t)(ext_seq & 0xFFFF);
    }

private:
    uint32_t next_ext_seq_ = 0;
};

// 64-entry sliding replay window over a 16-bit wire seq, per wire_protocol.md §4. Firmware only
// ever talks to one relay per session, so a fixed-size bitset is enough.
//
// The window is anchored at highest_ext_seq_: bit i of seen_mask_ means "highest_ext_seq_ - i has
// been seen". Advancing the window is a left shift, which evicts everything that fell below the
// floor in one operation — the equivalent of crypto.py's `_seen = {s for s in _seen if s > floor}`.
// (A mask keyed by `ext_seq % 64` instead would never evict anything, so after 64 accepted packets
// every subsequent one reads as a duplicate and the link silently dies one-way. It did.)
class CgSequenceTracker {
public:
    int64_t infer(uint16_t seq) const {
        return has_last_ ? (int64_t)highest_ext_seq_ + signed_delta16(seq, last_seq_) : seq;
    }
    void reset() {
        has_last_ = false;
        highest_ext_seq_ = 0;
        seen_mask_ = 0;
    }

    // Returns true and writes ext_seq_out if `seq` should be accepted (not a replay/duplicate).
    bool accept(uint16_t seq, uint32_t *ext_seq_out) {
        if (!has_last_) {
            has_last_ = true;
            last_seq_ = seq;
            high_ = 0;
            highest_ext_seq_ = seq;
            seen_mask_ = 1ULL;
            *ext_seq_out = seq;
            return true;
        }

        int32_t delta = signed_delta16(seq, last_seq_);
        uint32_t high;
        if (delta > 0) {
            if (seq < last_seq_) {
                high_ += 1;
            }
            last_seq_ = seq;
            high = high_;
        } else if (seq > last_seq_) {
            if (high_ == 0) return false; // no previous epoch; avoid unsigned underflow poisoning RX
            high = high_ - 1;
        } else {
            high = high_;
        }
        uint32_t ext_seq = (high << 16) | seq;

        if (ext_seq > highest_ext_seq_) {
            uint32_t advance = ext_seq - highest_ext_seq_;
            seen_mask_ = (advance >= 64) ? 0 : (seen_mask_ << advance);
            seen_mask_ |= 1ULL;
            highest_ext_seq_ = ext_seq;
        } else {
            uint32_t behind = highest_ext_seq_ - ext_seq;
            if (behind >= 64) {
                return false;  // below the window floor
            }
            uint64_t bit = 1ULL << behind;
            if (seen_mask_ & bit) {
                return false;  // duplicate/replay
            }
            seen_mask_ |= bit;
        }
        *ext_seq_out = ext_seq;
        return true;
    }

private:
    static int32_t signed_delta16(uint16_t new_seq, uint16_t last_seq) {
        int32_t delta = (int32_t)((uint16_t)(new_seq - last_seq));
        if (delta >= 0x8000) {
            delta -= 0x10000;
        }
        return delta;
    }

    bool has_last_ = false;
    uint16_t last_seq_ = 0;
    uint32_t high_ = 0;
    uint32_t highest_ext_seq_ = 0;
    uint64_t seen_mask_ = 0;  // bit i set = "highest_ext_seq_ - i has been seen"
};
