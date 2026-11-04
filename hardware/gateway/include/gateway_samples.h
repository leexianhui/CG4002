#pragma once
#include <stdint.h>
#include <string.h>
#include <math.h>
#include "cg_protocol.h"

// Internal UDP boundary only. Roles retain the hardware team's IDs: hub=0, torso=1, left=2.
struct __attribute__((packed)) GatewaySample {
    uint16_t magic = 0x4743;
    uint8_t version = 1, player = 0, role = 0, reserved = 0;
    uint32_t boot = 0, seq = 0, source_ms = 0;
    int16_t axes[6] = {};
};
static_assert(sizeof(GatewaySample) == 30, "UDP sample layout");

// The AI's copy of the gyro must look like ACTION_FINAL's training data. Those recordings came from
// the same MPU6500 boards with the chip's DLPF_CFG=4 (gyro ~20 Hz bandwidth, ~9.9 ms delay) read at
// 50 Hz; the accelerometer was unfiltered (on the MPU6500 that register leaves it alone, and the
// training data's accel noise is flat to 25 Hz). The game sketches leave the DLPF off for their own
// swing detection and read at 100 Hz, so the gateway filters the gyro it exports: a 2nd-order
// Butterworth low-pass, fc 20 Hz at fs 100 Hz (bilinear), group delay ~9.8 ms. Taking every other
// filtered sample then matches the chip's filter-and-read-at-50-Hz. Feed it every 100 Hz sample.
struct GatewayLowPass {
    static constexpr float b0 = 0.206572084f, b1 = 0.413144168f, b2 = 0.206572084f;
    static constexpr float a1 = -0.369527377f, a2 = 0.195815713f;
    float z1 = 0, z2 = 0;
    bool primed = false;
    float step(float x) {
        if (!primed) { // start in steady state at the first value: no start-up transient
            z2 = (b2 - a2) * x;
            z1 = (b1 - a1) * x + z2;
            primed = true;
        }
        float y = b0 * x + z1; // direct form II transposed
        z1 = b1 * x - a1 * y + z2;
        z2 = b2 * x - a2 * y;
        return y;
    }
};

inline int16_t gatewayScale(float value, float scale) {
    if (!isfinite(value)) return 0;
    float n = roundf(value * scale);
    return (int16_t)(n < -32768 ? -32768 : n > 32767 ? 32767 : n);
}

struct GatewaySamples {
    GatewaySample latest[3] = {};
    uint32_t received[3] = {};
    bool seen[3] = {};
    bool accept(const GatewaySample &s, uint8_t player, uint32_t now) {
        if (s.magic != 0x4743 || s.version != 1 || s.player != player ||
            s.role > 2 || s.reserved) return false;
        unsigned r = s.role;
        if (seen[r]) {
            if (s.boot == latest[r].boot) {
                if ((int32_t)(s.seq - latest[r].seq) <= 0) return false;
            } else if (now - received[r] <= 100) {
                // A restarted node may take over only after the old stream becomes stale.
                return false;
            }
        }
        latest[r] = s;
        received[r] = now;
        seen[r] = true;
        return true;
    }
    bool fresh(unsigned role, uint32_t now) const {
        return seen[role] && now - received[role] <= 100;
    }
    cg_payload_sensor_frame_t frame(uint32_t now, uint8_t weapon) const {
        cg_payload_sensor_frame_t f = {};
        f.device_timestamp_ms = now;
        f.weapon_id = weapon;
        if (fresh(0, now)) memcpy(f.dominant, latest[0].axes, 12);
        else f.flags |= CG_FLAG_DOM_STALE;
        if (fresh(2, now)) memcpy(f.offhand, latest[2].axes, 12);
        else f.flags |= CG_FLAG_OFF_STALE;
        if (fresh(1, now)) memcpy(f.torso, latest[1].axes, 12);
        // There is no torso stale bit in protocol v1: a missing torso is all zeros, which the
        // classifier treats as an absent sensor (a constant block).
        if (!weapon) f.flags |= CG_FLAG_CALIBRATING;
        return f;
    }
};
