#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include "gateway_boundary.h"
#include "gateway_samples.h"

namespace {
WiFiUDP udp;
uint8_t playerId, roleId, weaponId;
uint16_t samplePort;
uint32_t bootId, sequence, lastExport;
GatewaySamples samples;
portMUX_TYPE sampleMux = portMUX_INITIALIZER_UNLOCKED;
QueueHandle_t feedbackQueue;
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
        IPAddress broadcast((uint32_t)WiFi.localIP() | ~(uint32_t)WiFi.subnetMask());
        udp.beginPacket(broadcast, samplePort);
        udp.write((uint8_t *)&s, sizeof(s));
        udp.endPacket();
    }
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
