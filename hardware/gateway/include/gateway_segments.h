#pragma once
#include <stdint.h>
#include <string.h>
#include "cg_protocol.h"

// Event mode: one accepted action segment from the hub's own segmenter, dominant wrist only, in wire
// units (accel 2048 counts/g, gyro 16.4 counts/deg/s), sent once as SEGMENT_CHUNKs.
//
// The segmenter samples at 100 Hz; the classifier consumes 50 Hz. Segments are decimated to 50 Hz
// here (every other sample, by timestamp, as the old 50 Hz stream did), which halves BLE airtime on a
// radio the hub also needs for the torso/left-wrist Wi-Fi. 200 samples = 4 s, above the longest padded
// stroke (a slow spin, ~3.2 s); a longer segment keeps its centre.
#define GATEWAY_SEGMENT_RATE_HZ 50
#define GATEWAY_SEGMENT_MAX_SAMPLES 200
#define GATEWAY_SEGMENT_MIN_GAP_MS 15 // accept a source sample once >= this after the last kept one
// One being sent, one queued, one being filled. A full queue drops its oldest unsent segment.
#define GATEWAY_SEGMENT_SLOTS 3

struct GatewaySegment {
    uint32_t id = 0, first_ms = 0, last_ms = 0, queued_ms = 0;
    uint16_t count = 0, skip = 0, seen = 0; // seen: source samples offered so far
    uint8_t weapon = 0;
    int16_t samples[GATEWAY_SEGMENT_MAX_SAMPLES][6];
};

static_assert(sizeof(cg_payload_segment_chunk_t) == 208, "SEGMENT_CHUNK layout");
static_assert((GATEWAY_SEGMENT_MAX_SAMPLES + CG_SEGMENT_CHUNK_SAMPLES - 1) / CG_SEGMENT_CHUNK_SAMPLES <=
                  CG_SEGMENT_MAX_CHUNKS, "segment needs more chunks than the wire allows");

inline uint8_t gatewaySegmentChunks(const GatewaySegment &s) {
    return (uint8_t)((s.count + CG_SEGMENT_CHUNK_SAMPLES - 1) / CG_SEGMENT_CHUNK_SAMPLES);
}

// Chunk `index` of `s`; sample rows past the segment's end stay zero.
inline void gatewaySegmentChunk(const GatewaySegment &s, uint8_t index, cg_payload_segment_chunk_t &c) {
    memset(&c, 0, sizeof(c));
    uint16_t first = (uint16_t)(index * CG_SEGMENT_CHUNK_SAMPLES);
    uint16_t n = first < s.count ? (uint16_t)(s.count - first) : 0;
    if (n > CG_SEGMENT_CHUNK_SAMPLES) n = CG_SEGMENT_CHUNK_SAMPLES;
    c.segment_id = s.id;
    c.chunk_index = index;
    c.chunk_count = gatewaySegmentChunks(s);
    c.weapon_id = s.weapon;
    c.sample_rate_hz = GATEWAY_SEGMENT_RATE_HZ;
    c.first_sample_ms = s.first_ms + (uint32_t)first * 1000u / GATEWAY_SEGMENT_RATE_HZ;
    c.sample_count = (uint8_t)n;
    if (n) memcpy(c.samples, s.samples[first], n * sizeof(c.samples[0]));
}

// Single producer (hardware loop: begin/add/end) and single consumer (BLE task: take/done). Not
// locked itself: the caller serialises the state changes in begin/end/take/done. add() touches only
// the FILLING slot, which the consumer never reads, so it needs no lock.
class GatewaySegmentQueue {
public:
    enum State : uint8_t { FREE, FILLING, READY, SENDING };
    GatewaySegment slots[GATEWAY_SEGMENT_SLOTS];
    State state[GATEWAY_SEGMENT_SLOTS] = {};
    uint32_t order[GATEWAY_SEGMENT_SLOTS] = {};
    uint32_t next_order = 0, dropped = 0, last_dropped_id = 0;
    int filling = -1;

    // `source_count` is the length of the 100 Hz source range, used only to centre an over-long one.
    void begin(uint32_t id, uint8_t weapon, uint16_t source_count, uint32_t now) {
        if (filling >= 0) state[filling] = FREE; // an unfinished segment is abandoned
        filling = -1;
        for (int i = 0; i < GATEWAY_SEGMENT_SLOTS && filling < 0; i++)
            if (state[i] == FREE) filling = i;
        if (filling < 0) {
            int oldest = -1;
            for (int i = 0; i < GATEWAY_SEGMENT_SLOTS; i++)
                if (state[i] == READY && (oldest < 0 || (int32_t)(order[i] - order[oldest]) < 0)) oldest = i;
            if (oldest < 0) return; // unreachable: only one slot is ever SENDING
            dropped++;
            last_dropped_id = slots[oldest].id;
            filling = oldest;
        }
        GatewaySegment &s = slots[filling];
        state[filling] = FILLING;
        s.id = id;
        s.weapon = weapon;
        s.count = 0;
        s.queued_ms = now;
        const uint16_t source_max = 2 * GATEWAY_SEGMENT_MAX_SAMPLES;
        s.skip = source_count > source_max ? (uint16_t)((source_count - source_max) / 2) : 0;
        s.seen = 0;
    }

    void add(uint32_t t_ms, const int16_t axes[6]) {
        if (filling < 0) return;
        GatewaySegment &s = slots[filling];
        if (s.seen++ < s.skip || s.count >= GATEWAY_SEGMENT_MAX_SAMPLES) return;
        if (s.count && t_ms - s.last_ms < GATEWAY_SEGMENT_MIN_GAP_MS) return;
        if (!s.count) s.first_ms = t_ms;
        s.last_ms = t_ms;
        memcpy(s.samples[s.count++], axes, sizeof(s.samples[0]));
    }

    // Returns false if nothing was queued (no begin, or no samples).
    bool end() {
        if (filling < 0) return false;
        bool ok = slots[filling].count > 0;
        state[filling] = ok ? READY : FREE;
        order[filling] = next_order++;
        filling = -1;
        return ok;
    }

    // Oldest READY segment queued within `max_age_ms`; older ones are discarded (a late action is
    // worse than none). Returns -1 when nothing is ready.
    int take(uint32_t now, uint32_t max_age_ms) {
        for (;;) {
            int oldest = -1;
            for (int i = 0; i < GATEWAY_SEGMENT_SLOTS; i++)
                if (state[i] == READY && (oldest < 0 || (int32_t)(order[i] - order[oldest]) < 0)) oldest = i;
            if (oldest < 0) return -1;
            if (now - slots[oldest].queued_ms <= max_age_ms) {
                state[oldest] = SENDING;
                return oldest;
            }
            state[oldest] = FREE;
            dropped++;
            last_dropped_id = slots[oldest].id;
        }
    }

    void done(int slot) {
        if (slot >= 0 && slot < GATEWAY_SEGMENT_SLOTS && state[slot] == SENDING) state[slot] = FREE;
    }
};
