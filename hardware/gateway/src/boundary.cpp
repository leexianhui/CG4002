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
// Node samples (both modes): Wi-Fi UDP, or the BLE node link (it maps the port to a tag).
#if CG_GATEWAY_NODE_LINK
GatewayLinkUdp udp;
#else
WiFiUDP udp;
#endif
uint8_t playerId, roleId, weaponId;
uint16_t samplePort;
uint32_t bootId, sequence, lastExport;
GatewaySamples samples;
GatewayLowPass gyroLowPass[3]; // this board's gx, gy, gz, as sent to the AI (hardware loop only)
portMUX_TYPE sampleMux = portMUX_INITIALIZER_UNLOCKED;
QueueHandle_t feedbackQueue;
#if CG_GATEWAY_ROLE == 0 && CG_GATEWAY_EVENT_MODE
GatewaySegmentQueue segments;
portMUX_TYPE segmentMux = portMUX_INITIALIZER_UNLOCKED;
// Hub main loop only (gatewaySample, gatewayPoll, gatewaySegment*): no lock needed.
GatewayHistory history;
GatewayNodeClock nodeClock[GATEWAY_SENSORS]; // indexed by history sensor (1 off-hand, 2 torso)
// An accepted stroke whose window is not complete yet: its last row is still in the future, or
// node samples for it may still be on the radio.
struct PendingWindow {
    bool active;
    uint32_t id, end_ms;
    uint8_t weapon;
};
const uint32_t kWindowMaxWaitMs = 400; // wait at most this past acceptance to centre the stroke
const uint32_t kNodeLinkDelayMs = 80;  // node samples arrive this much after they were measured
PendingWindow pending[GATEWAY_SEGMENT_SLOTS] = {};
bool recording;
uint32_t recordId, recordFirst, recordLast;
uint16_t recordCount;
uint8_t recordWeapon;

// GatewaySample role (hub 0, torso 1, left 2) -> history sensor (0 dominant, 1 off-hand, 2 torso).
unsigned sensorOfRole(uint8_t role) { return role == 2 ? 1 : role == 1 ? 2 : 0; }
void buildReadyWindows(uint32_t now);
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

// Every board calls this for every IMU sample (100 Hz), in every phase. Nodes send one every 20 ms
// to the hub in both modes (event mode needs them for the classifier window). The hub keeps its own
// every sample: in event mode in the history, in continuous mode as the latest SENSOR_FRAME value.
void gatewaySample(float ax, float ay, float az, float gx, float gy, float gz) {
    uint32_t now = millis();
    float values[] = {ax, ay, az, gx, gy, gz};
    for (unsigned i = 0; i < 6; ++i)
        if (!isfinite(values[i])) return; // missing/invalid samples age out instead of becoming zeros
    // Every 100 Hz sample goes through the gyro low-pass (gateway_samples.h), before the 20 ms
    // export throttle, so the AI sees the gyro the way the training boards' chip filter shaped it.
    for (unsigned i = 0; i < 3; ++i) values[3 + i] = gyroLowPass[i].step(values[3 + i]);
    if (roleId && now - lastExport < 20) return;
    lastExport = now;
    GatewaySample s;
    s.player = playerId;
    s.role = roleId;
    s.boot = bootId;
    s.seq = ++sequence;
    s.source_ms = now;
    for (unsigned i = 0; i < 6; ++i) s.axes[i] = gatewayScale(values[i], i < 3 ? 2048.0f : 16.4f);
    if (roleId == 0) {
#if CG_GATEWAY_ROLE == 0 && CG_GATEWAY_EVENT_MODE
        history.put(0, now, s.axes);
#else
        portENTER_CRITICAL(&sampleMux);
        samples.accept(s, playerId, now);
        portEXIT_CRITICAL(&sampleMux);
#endif
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
        uint32_t arrival = millis();
        portENTER_CRITICAL(&sampleMux);
        bool fresh = samples.accept(s, playerId, arrival);
        portEXIT_CRITICAL(&sampleMux);
#if CG_GATEWAY_ROLE == 0 && CG_GATEWAY_EVENT_MODE
        if (fresh) {
            unsigned sensor = sensorOfRole(s.role);
            history.put(sensor, nodeClock[sensor].toHub(s.source_ms, arrival, s.boot), s.axes);
        }
#else
        (void)fresh;
#endif
    }
#if CG_GATEWAY_ROLE == 0 && CG_GATEWAY_EVENT_MODE
    buildReadyWindows(millis());
#endif
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
// The segmenter hands over the samples of the stroke it accepted (plus its padding). Only their time
// span is used: the window itself comes from the history, so it has all three sensors and real
// samples on both sides of the stroke.
void gatewaySegmentBegin(uint32_t segment_id, uint8_t weapon, uint16_t source_samples) {
    (void)source_samples;
    recording = true;
    recordId = segment_id;
    recordWeapon = weapon <= 3 ? weapon : 0;
    recordCount = 0;
}
void gatewaySegmentSample(uint32_t t_ms, float, float, float, float, float, float) {
    if (!recording) return;
    if (!recordCount++) recordFirst = t_ms;
    recordLast = t_ms;
}
bool gatewaySegmentEnd() {
    if (!recording) return false;
    recording = false;
    if (!recordCount) return false;
    uint32_t now = millis();
    int slot = -1;
    for (int i = 0; i < GATEWAY_SEGMENT_SLOTS; i++)
        if (!pending[i].active && slot < 0) slot = i;
    if (slot < 0) { // three strokes still waiting: the oldest gives way
        slot = 0;
        for (int i = 1; i < GATEWAY_SEGMENT_SLOTS; i++)
            if ((int32_t)(pending[i].end_ms - pending[slot].end_ms) < 0) slot = i;
        Serial.printf("[segment] too many waiting, dropped seg=%lu\n", (unsigned long)pending[slot].id);
    }
    pending[slot] = {true, recordId, gatewayWindowEndMs(recordFirst, recordLast, now, kWindowMaxWaitMs),
                     recordWeapon};
    return true;
}

namespace {
// Main loop: a window whose last row (plus the node-link delay) is in the past is copied out of the
// history into the relay queue, oldest first.
void buildReadyWindows(uint32_t now) {
    for (;;) {
        int next = -1;
        for (int i = 0; i < GATEWAY_SEGMENT_SLOTS; i++)
            if (pending[i].active && (int32_t)(now - (pending[i].end_ms + kNodeLinkDelayMs)) >= 0 &&
                (next < 0 || (int32_t)(pending[i].end_ms - pending[next].end_ms) < 0))
                next = i;
        if (next < 0) return;
        PendingWindow p = pending[next];
        pending[next].active = false;

        portENTER_CRITICAL(&segmentMux);
        uint32_t dropped = segments.dropped;
        GatewaySegment *seg = segments.begin(p.id, p.weapon, now);
        bool lostOne = segments.dropped != dropped;
        uint32_t lost = segments.last_dropped_id;
        portEXIT_CRITICAL(&segmentMux);
        if (lostOne)
            Serial.printf("[segment] queue full, dropped unsent seg=%lu\n", (unsigned long)lost);
        if (!seg) continue;
        // FILLING: the BLE task never reads this slot, so it is filled outside the lock.
        uint32_t endGrid = GatewayHistory::gridOf(p.end_ms);
        seg->valid = history.window(endGrid, seg->rows);
        seg->first_ms = (endGrid - (GATEWAY_WINDOW_ROWS - 1)) * GATEWAY_GRID_MS;
        seg->count = GATEWAY_WINDOW_ROWS;
        portENTER_CRITICAL(&segmentMux);
        segments.end();
        portEXIT_CRITICAL(&segmentMux);
        Serial.printf("[segment] window seg=%lu right=%d left=%d torso=%d\n", (unsigned long)p.id,
                      !!(seg->valid & CG_SEGMENT_FLAG_DOM_VALID), !!(seg->valid & CG_SEGMENT_FLAG_OFF_VALID),
                      !!(seg->valid & CG_SEGMENT_FLAG_TORSO_VALID));
    }
}
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
