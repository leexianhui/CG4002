#pragma once
#include <stdint.h>
#include <string.h>
#include "cg_protocol.h"

// Event mode, hub: each action the hub's own segmenter accepts goes to the classifier as ONE window
// of all three IMUs, exactly what ai/ACTION_FINAL was trained on: GATEWAY_WINDOW_ROWS rows at 50 Hz
// (2.0 s), 18 axes per row = dominant (right) wrist, off-hand (left) wrist, torso, in wire units
// (accel 2048 counts/g, gyro 16.4 counts/deg/s). The accepted stroke's midpoint sits at row
// GATEWAY_WINDOW_CENTRE_ROW (1.0 s), with real samples on both sides, never padding.
//
// How: every sample of all three boards goes into GatewayHistory, a 4 s ring on a 20 ms grid in hub
// time (the hub's own 100 Hz samples directly; node samples via GatewayNodeClock). When the segmenter
// accepts a stroke, the hub waits until the window's last row is in the past (plus the node-link
// delay), copies the 100 rows out, and queues them for the relay as three-IMU SEGMENT_CHUNKs.
#define GATEWAY_SEGMENT_RATE_HZ 50
#define GATEWAY_GRID_MS (1000 / GATEWAY_SEGMENT_RATE_HZ)
#define GATEWAY_WINDOW_ROWS 100      // ai/config.py WINDOW
#define GATEWAY_WINDOW_CENTRE_ROW 50 // the stroke midpoint's row
#define GATEWAY_HISTORY_ROWS 200     // 4 s: room for the window plus a long candidate's settle time
#define GATEWAY_SENSOR_MIN_ROWS 80   // a sensor measured in fewer rows of a window is sent as invalid
#define GATEWAY_AXES 18
#define GATEWAY_SENSORS 3            // 0 dominant (hub), 1 off-hand (left wrist), 2 torso
// One being sent, one queued, one being filled. A full queue drops its oldest unsent segment.
#define GATEWAY_SEGMENT_SLOTS 3

static_assert(sizeof(cg_payload_segment_chunk_t) == 208, "SEGMENT_CHUNK layout");
static_assert((GATEWAY_WINDOW_ROWS + CG_SEGMENT_CHUNK_SAMPLES_3IMU - 1) / CG_SEGMENT_CHUNK_SAMPLES_3IMU <=
                  CG_SEGMENT_MAX_CHUNKS, "a window needs more chunks than the wire allows");
static_assert(GATEWAY_HISTORY_ROWS > GATEWAY_WINDOW_ROWS, "history must hold a whole window");

// Sensor index -> CG_SEGMENT_FLAG_*_VALID bit.
inline uint8_t gatewaySensorValidFlag(unsigned sensor) {
    return sensor == 0 ? CG_SEGMENT_FLAG_DOM_VALID : sensor == 1 ? CG_SEGMENT_FLAG_OFF_VALID
                                                                 : CG_SEGMENT_FLAG_TORSO_VALID;
}

// The last few seconds of all three sensors on one 20 ms grid. Single thread (hub main loop).
struct GatewayHistory {
    struct Row {
        uint32_t grid = 0;   // which 20 ms step this slot currently holds
        uint8_t present = 0; // bit s: sensor s measured in this step
        int16_t axes[GATEWAY_AXES] = {};
    };
    Row rows[GATEWAY_HISTORY_ROWS];
    uint32_t newest = 0;
    bool any = false;

    static uint32_t gridOf(uint32_t t_ms) { return (t_ms + GATEWAY_GRID_MS / 2) / GATEWAY_GRID_MS; }

    // Sensor `sensor` measured at hub time `t_ms`. A later sample in the same step replaces an earlier.
    void put(unsigned sensor, uint32_t t_ms, const int16_t axes[6]) {
        uint32_t grid = gridOf(t_ms);
        if (any && (int32_t)(newest - grid) >= GATEWAY_HISTORY_ROWS) return; // already overwritten
        Row &r = rows[grid % GATEWAY_HISTORY_ROWS];
        if (r.grid != grid) {
            r.grid = grid;
            r.present = 0;
        }
        r.present |= (uint8_t)(1u << sensor);
        memcpy(&r.axes[sensor * 6], axes, 6 * sizeof(int16_t));
        if (!any || (int32_t)(grid - newest) > 0) newest = grid;
        any = true;
    }

    // Copies the GATEWAY_WINDOW_ROWS steps ending at `end_grid` into `out`. A step where a sensor
    // has no sample repeats that sensor's previous row (its first measured row for leading steps;
    // zeros if it never appears). Returns CG_SEGMENT_FLAG_*_VALID for each sensor measured in at
    // least GATEWAY_SENSOR_MIN_ROWS of the rows.
    uint8_t window(uint32_t end_grid, int16_t out[][GATEWAY_AXES]) const {
        uint8_t valid = 0;
        uint32_t start = end_grid - (GATEWAY_WINDOW_ROWS - 1);
        for (unsigned s = 0; s < GATEWAY_SENSORS; s++) {
            int measured = 0, first = -1;
            for (int k = 0; k < GATEWAY_WINDOW_ROWS; k++) {
                uint32_t g = start + (uint32_t)k;
                const Row &r = rows[g % GATEWAY_HISTORY_ROWS];
                int16_t *dst = &out[k][s * 6];
                if (r.grid == g && (r.present & (1u << s))) {
                    memcpy(dst, &r.axes[s * 6], 6 * sizeof(int16_t));
                    if (first < 0) first = k;
                    measured++;
                } else if (k > 0) {
                    memcpy(dst, &out[k - 1][s * 6], 6 * sizeof(int16_t));
                } else {
                    memset(dst, 0, 6 * sizeof(int16_t));
                }
            }
            for (int k = 0; k < first; k++) memcpy(&out[k][s * 6], &out[first][s * 6], 6 * sizeof(int16_t));
            if (measured >= GATEWAY_SENSOR_MIN_ROWS) valid |= gatewaySensorValidFlag(s);
        }
        return valid;
    }
};

// A node's millis() -> the hub's. Radio delivery only ever adds delay, so the smallest
// (arrival - source) seen recently is the best offset; two alternating 4 s epochs follow drift and
// forget a node that rebooted. Arrival is when the hub's loop takes the sample, so an occasional
// busy loop only makes that sample's offset larger, which the minimum ignores.
struct GatewayNodeClock {
    int32_t current = 0, previous = 0;
    bool haveCurrent = false, havePrevious = false;
    uint32_t epochStart = 0, boot = 0;

    uint32_t toHub(uint32_t source_ms, uint32_t arrival_ms, uint32_t boot_id) {
        if (boot_id != boot) {
            boot = boot_id;
            haveCurrent = havePrevious = false;
            epochStart = arrival_ms;
        }
        if (arrival_ms - epochStart >= 4000) {
            previous = current;
            havePrevious = haveCurrent;
            haveCurrent = false;
            epochStart = arrival_ms;
        }
        int32_t offset = (int32_t)(arrival_ms - source_ms);
        if (!haveCurrent || offset < current) current = offset;
        haveCurrent = true;
        int32_t best = havePrevious && previous < current ? previous : current;
        return source_ms + (uint32_t)best;
    }
};

// One accepted action, ready for the relay: the window plus which sensors in it are measurements.
struct GatewaySegment {
    uint32_t id = 0, first_ms = 0, queued_ms = 0; // first_ms: hub clock of row 0
    uint16_t count = 0;                           // rows, GATEWAY_WINDOW_ROWS once filled
    uint8_t weapon = 0, valid = 0;                // valid: CG_SEGMENT_FLAG_*_VALID bits
    int16_t rows[GATEWAY_WINDOW_ROWS][GATEWAY_AXES];
};

inline uint8_t gatewaySegmentChunks(const GatewaySegment &s) {
    return (uint8_t)((s.count + CG_SEGMENT_CHUNK_SAMPLES_3IMU - 1) / CG_SEGMENT_CHUNK_SAMPLES_3IMU);
}

// Chunk `index` of `s` in the three-IMU layout; row slots past the segment's end stay zero.
inline void gatewaySegmentChunk(const GatewaySegment &s, uint8_t index, cg_payload_segment_chunk_t &c) {
    memset(&c, 0, sizeof(c));
    uint16_t first = (uint16_t)(index * CG_SEGMENT_CHUNK_SAMPLES_3IMU);
    uint16_t n = first < s.count ? (uint16_t)(s.count - first) : 0;
    if (n > CG_SEGMENT_CHUNK_SAMPLES_3IMU) n = CG_SEGMENT_CHUNK_SAMPLES_3IMU;
    c.segment_id = s.id;
    c.chunk_index = index;
    c.chunk_count = gatewaySegmentChunks(s);
    c.weapon_id = s.weapon;
    c.flags = (uint8_t)(CG_SEGMENT_FLAG_THREE_IMU | s.valid);
    c.sample_rate_hz = GATEWAY_SEGMENT_RATE_HZ;
    c.first_sample_ms = s.first_ms + (uint32_t)first * GATEWAY_GRID_MS;
    c.sample_count = (uint8_t)n;
    if (n) memcpy(c.samples3, s.rows[first], n * sizeof(c.samples3[0]));
}

// Single producer (hardware loop: begin/end) and single consumer (BLE task: take/done). Not locked
// itself: the caller serialises begin/end/take/done. Between begin and end the producer writes the
// FILLING slot, which the consumer never reads, so filling needs no lock.
class GatewaySegmentQueue {
public:
    enum State : uint8_t { FREE, FILLING, READY, SENDING };
    GatewaySegment slots[GATEWAY_SEGMENT_SLOTS];
    State state[GATEWAY_SEGMENT_SLOTS] = {};
    uint32_t order[GATEWAY_SEGMENT_SLOTS] = {};
    uint32_t next_order = 0, dropped = 0, last_dropped_id = 0;
    int filling = -1;

    // A slot to fill (count/rows/valid/first_ms are the caller's), or nullptr (unreachable: only one
    // slot is ever SENDING). When none is free, the oldest unsent segment is dropped.
    GatewaySegment *begin(uint32_t id, uint8_t weapon, uint32_t now) {
        if (filling >= 0) state[filling] = FREE; // an unfinished segment is abandoned
        filling = -1;
        for (int i = 0; i < GATEWAY_SEGMENT_SLOTS && filling < 0; i++)
            if (state[i] == FREE) filling = i;
        if (filling < 0) {
            int oldest = -1;
            for (int i = 0; i < GATEWAY_SEGMENT_SLOTS; i++)
                if (state[i] == READY && (oldest < 0 || (int32_t)(order[i] - order[oldest]) < 0)) oldest = i;
            if (oldest < 0) return nullptr;
            dropped++;
            last_dropped_id = slots[oldest].id;
            filling = oldest;
        }
        GatewaySegment &s = slots[filling];
        state[filling] = FILLING;
        s.id = id;
        s.weapon = weapon;
        s.count = 0;
        s.valid = 0;
        s.queued_ms = now;
        return &s;
    }

    // Returns false if nothing was queued (no begin, or no rows).
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

// Where the window for one accepted stroke ends. `first_ms`/`last_ms` span the segmenter's padded
// stroke; its midpoint becomes row GATEWAY_WINDOW_CENTRE_ROW. The end is pulled in so the hub waits
// at most `max_wait_ms` past `now`, and pushed out so the whole window is still in the history.
inline uint32_t gatewayWindowEndMs(uint32_t first_ms, uint32_t last_ms, uint32_t now, uint32_t max_wait_ms) {
    uint32_t centre = first_ms + (last_ms - first_ms) / 2;
    uint32_t end = centre + (uint32_t)(GATEWAY_WINDOW_ROWS - 1 - GATEWAY_WINDOW_CENTRE_ROW) * GATEWAY_GRID_MS;
    if ((int32_t)(end - (now + max_wait_ms)) > 0) end = now + max_wait_ms;
    // Oldest end whose window still fits: history span minus the window, minus 10 rows of slack.
    const uint32_t oldest = (uint32_t)(GATEWAY_HISTORY_ROWS - GATEWAY_WINDOW_ROWS - 10) * GATEWAY_GRID_MS;
    if ((int32_t)(now - end) > (int32_t)oldest) end = now - oldest;
    return end;
}
