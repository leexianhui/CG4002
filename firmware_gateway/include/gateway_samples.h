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
        // There is no torso stale bit in protocol v1. Torso is not a classifier input.
        if (!weapon) f.flags |= CG_FLAG_CALIBRATING;
        return f;
    }
};
