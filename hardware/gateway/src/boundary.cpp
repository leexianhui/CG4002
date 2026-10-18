#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include "gateway_boundary.h"
#include "gateway_link.h"
#include "gateway_samples.h"
#include "gateway_segments.h"

namespace {
// Continuous-mode node samples: Wi-Fi UDP, or the BLE node link (it maps the port to a tag).
#if CG_GATEWAY_NODE_LINK
GatewayLinkUdp udp;
#else
WiFiUDP udp;
#endif
uint8_t playerId, roleId, weaponId;
uint16_t samplePort;
uint32_t bootId, sequence, lastExport;
GatewaySamples samples;
portMUX_TYPE sampleMux = portMUX_INITIALIZER_UNLOCKED;
QueueHandle_t feedbackQueue;
#if CG_GATEWAY_ROLE == 0 && CG_GATEWAY_EVENT_MODE
GatewaySegmentQueue segments;
portMUX_TYPE segmentMux = portMUX_INITIALIZER_UNLOCKED;
#endif
}

#if CG_GATEWAY_ROLE == 0
void gatewayTransportSetup();
void gatewayTransportLoop();
static void transportTask(void *) {
    gatewayTransportSetup();
    for (;;) {
        gatewayTransportLoop();
        vTaskDelay(1);
    }
}
#endif

void gatewayBegin(uint8_t player, uint8_t role) {
    playerId = player;
    roleId = role;
    bootId = esp_random();
    samplePort = 4215 + (player - 1) * 10;
    // Node sockets use an ephemeral source port; only the hub receives on samplePort.
    configASSERT(udp.begin(role == 0 ? samplePort : 0));
#if CG_GATEWAY_ROLE == 0
    feedbackQueue = xQueueCreate(16, sizeof(cg_payload_combat_feedback_t));
    configASSERT(feedbackQueue);
    configASSERT(xTaskCreate(transportTask, "relay-ble", 8192, nullptr, 1, nullptr) == pdPASS);
#endif
}

void gatewaySample(float ax, float ay, float az, float gx, float gy, float gz) {
#if CG_GATEWAY_EVENT_MODE
    // Event mode sends segments, not a stream: no node broadcast competes with the hub's BLE link.
    (void)ax; (void)ay; (void)az; (void)gx; (void)gy; (void)gz;
#else
    uint32_t now = millis();
    if (roleId && now - lastExport < 20) return;
    lastExport = now;
    GatewaySample s;
    s.player = playerId;
    s.role = roleId;
    s.boot = bootId;
    s.seq = ++sequence;
    s.source_ms = now;
    const float values[] = {ax, ay, az, gx, gy, gz};
    for (unsigned i = 0; i < 6; ++i) {
        if (!isfinite(values[i])) return; // missing/invalid samples age out instead of becoming zeros
        s.axes[i] = gatewayScale(values[i], i < 3 ? 2048.0f : 16.4f);
    }
    if (roleId == 0) {
        portENTER_CRITICAL(&sampleMux);
        samples.accept(s, playerId, now);
        portEXIT_CRITICAL(&sampleMux);
    } else {
#if CG_GATEWAY_NODE_LINK
        IPAddress broadcast;
#else
        IPAddress broadcast((uint32_t)WiFi.localIP() | ~(uint32_t)WiFi.subnetMask());
#endif
        udp.beginPacket(broadcast, samplePort);
        udp.write((uint8_t *)&s, sizeof(s));
        udp.endPacket();
    }
#endif
}

void gatewayPoll(uint8_t locked_weapon) {
    if (roleId != 0) return;
    portENTER_CRITICAL(&sampleMux);
    weaponId = locked_weapon <= 3 ? locked_weapon : 0;
    portEXIT_CRITICAL(&sampleMux);
    // Bound network work so other hardware tasks retain their sampling time.
    for (unsigned i = 0; i < 12; ++i) {
        int n = udp.parsePacket();
        if (!n) break;
        GatewaySample s;
        if (n != sizeof(s) || udp.read((uint8_t *)&s, sizeof(s)) != sizeof(s)) {
            udp.flush();
            continue;
        }
        if (s.role != 1 && s.role != 2) continue;
        portENTER_CRITICAL(&sampleMux);
        samples.accept(s, playerId, millis());
        portEXIT_CRITICAL(&sampleMux);
    }
}

uint8_t gatewayWeapon() {
    portENTER_CRITICAL(&sampleMux);
    uint8_t result = weaponId;
    portEXIT_CRITICAL(&sampleMux);
    return result;
}
void gatewayReadFrame(cg_payload_sensor_frame_t &frame) {
    portENTER_CRITICAL(&sampleMux);
    frame = samples.frame(millis(), weaponId);
    portEXIT_CRITICAL(&sampleMux);
}
bool gatewayQueueFeedback(const uint8_t *payload) {
    cg_payload_combat_feedback_t f;
    memcpy(&f, payload, sizeof(f));
    if (f.feedback_code > 3 || f.intensity_pct > 100 || !f.duration_ms ||
        f.duration_ms > 2000 || !f.repeat_count || f.repeat_count > 10) return false;
    return feedbackQueue && xQueueSend(feedbackQueue, &f, 0) == pdTRUE;
}
bool gatewayTakeFeedback(cg_payload_combat_feedback_t &f) {
    return feedbackQueue && xQueueReceive(feedbackQueue, &f, 0) == pdTRUE;
}
int gatewayFeedbackDuty() {
    static cg_payload_combat_feedback_t current = {};
    static uint32_t start;
    static bool active;
    uint32_t now = millis();
    if (!active) {
        if (!gatewayTakeFeedback(current)) return -1;
        start = now;
        active = true;
    }
    uint32_t cycle = current.duration_ms + 150;
    uint32_t elapsed = now - start;
    if (elapsed >= cycle * current.repeat_count - 150) {
        active = false;
        return 0;
    }
    return elapsed % cycle < current.duration_ms ? current.intensity_pct * 255 / 100 : 0;
}

#if CG_GATEWAY_ROLE == 0 && CG_GATEWAY_EVENT_MODE
void gatewaySegmentBegin(uint32_t segment_id, uint8_t weapon, uint16_t source_samples) {
    portENTER_CRITICAL(&segmentMux);
    uint32_t dropped = segments.dropped;
    segments.begin(segment_id, weapon <= 3 ? weapon : 0, source_samples, millis());
    bool lostOne = segments.dropped != dropped;
    uint32_t lost = segments.last_dropped_id;
    portEXIT_CRITICAL(&segmentMux);
    if (lostOne)
        Serial.printf("[segment] queue full, dropped unsent seg=%lu\n", (unsigned long)lost);
}
// Touches only the FILLING slot, which the BLE task never reads: no lock needed.
void gatewaySegmentSample(uint32_t t_ms, float ax, float ay, float az, float gx, float gy, float gz) {
    const float values[] = {ax, ay, az, gx, gy, gz};
    int16_t axes[6];
    for (unsigned i = 0; i < 6; ++i) axes[i] = gatewayScale(values[i], i < 3 ? 2048.0f : 16.4f);
    segments.add(t_ms, axes);
}
bool gatewaySegmentEnd() {
    portENTER_CRITICAL(&segmentMux);
    bool queued = segments.end();
    portEXIT_CRITICAL(&segmentMux);
    return queued;
}
const GatewaySegment *gatewaySegmentTake(uint32_t now, uint32_t max_age_ms, int &slot) {
    portENTER_CRITICAL(&segmentMux);
    uint32_t dropped = segments.dropped;
    slot = segments.take(now, max_age_ms);
    bool lostOne = segments.dropped != dropped;
    uint32_t lost = segments.last_dropped_id;
    portEXIT_CRITICAL(&segmentMux);
    if (lostOne)
        Serial.printf("[segment] dropped stale unsent seg=%lu\n", (unsigned long)lost);
    return slot < 0 ? nullptr : &segments.slots[slot];
}
void gatewaySegmentDone(int slot) {
    portENTER_CRITICAL(&segmentMux);
    segments.done(slot);
    portEXIT_CRITICAL(&segmentMux);
}
#else
void gatewaySegmentBegin(uint32_t, uint8_t, uint16_t) {}
void gatewaySegmentSample(uint32_t, float, float, float, float, float, float) {}
bool gatewaySegmentEnd() { return false; }
const GatewaySegment *gatewaySegmentTake(uint32_t, uint32_t, int &slot) { slot = -1; return nullptr; }
void gatewaySegmentDone(int) {}
#endif
