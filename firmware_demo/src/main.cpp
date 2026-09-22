// CG4002 Comms demo firmware — FireBeetle ESP32, BLE GATT device role.
//
// Speaks the real wire protocol (shared/protocol/wire_protocol.md) to an unmodified cg-relay over
// BLE, generating synthetic sensor data in place of real IMU/RFID hardware (this is a Comms-subsystem
// demo, not the team's real wrist/belt firmware — see firmware_demo/README.md).
//
// Flow: advertise BLE -> laptop connects & subscribes -> HELLO/HELLO_ACK -> 8x TIME_SYNC burst ->
// stream SENSOR_FRAME @50Hz + HEARTBEAT @1Hz, fire RFID_EVENT on button press, ACK incoming
// COMBAT_FEEDBACK. Mirrors comms/src/cg_comms/mock/firebeetle_sim.py's device-side behavior.

#include <Arduino.h>
#include <BLE2902.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>

#include "cg_config.h"
#include "cg_frame.h"
#include "cg_protocol.h"
#include "cg_sequence.h"
#include "cg_reliable.h"
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

namespace {

uint8_t g_psk[CG_PSK_SIZE];
uint32_t g_session_id = 0;
uint32_t g_device_nonce = 0;
CgOutgoingSequenceCounter g_tx_seq;
CgSequenceTracker g_rx_seq;
uint8_t g_weapon_id = 0;  // Weapon.NONE — see comms/src/cg_comms/messages/enums.py WIRE_VALUES

// Motion-trigger capture state (see cg_config.h CG_DEMO_ACTION_BUTTON_*). A capture is atomic: a
// new action-button press while one is already running is ignored, which is what makes one press
// yield exactly one tagged window even on a double-tap.
uint32_t g_capture_remaining = 0;
uint8_t g_capture_trigger_id = 0;

// Reliable retries keep identical authenticated ciphertext, sequence and nonce.
// A duplicate is ACKed again but never applied twice on either side.
CgReliableSend g_pending_rfid;
CgReliableReceiveCache g_feedback_cache;

const uint8_t kActionButtonPins[CG_DEMO_ACTION_BUTTON_COUNT] = CG_DEMO_ACTION_BUTTON_PINS;
const uint8_t kActionButtonTriggerIds[CG_DEMO_ACTION_BUTTON_COUNT] = CG_DEMO_ACTION_BUTTON_TRIGGER_IDS;

// Debounced edge detector shared by the weapon button and every action button. `down` is the
// debounce-confirmed state; a falling edge is reported at most once per confirmed transition.
struct DebouncedButton {
    uint8_t pin;
    bool raw_down = false;
    bool down = false;
    uint32_t last_change_ms = 0;
};

DebouncedButton g_weapon_button;
DebouncedButton g_action_buttons[CG_DEMO_ACTION_BUTTON_COUNT];

// Returns true exactly once per debounced falling edge (button just went down).
bool debounce_falling_edge(DebouncedButton *btn, uint32_t now) {
    bool raw = digitalRead(btn->pin) == LOW;
    if (raw != btn->raw_down) {
        btn->raw_down = raw;
        btn->last_change_ms = now;
        return false;
    }
    if (raw != btn->down && (now - btn->last_change_ms) >= CG_DEMO_BUTTON_DEBOUNCE_MS) {
        bool falling_edge = raw && !btn->down;
        btn->down = raw;
        return falling_edge;
    }
    return false;
}

BLECharacteristic *g_notify_char = nullptr;
BLE2902 *g_notify_cccd = nullptr;  // read to tell whether the relay has subscribed yet
volatile bool g_connected = false;
volatile bool g_handshake_done = false;
volatile uint32_t g_connection_generation = 0;

// BLECharacteristic::notify() returns void: on a controller-level failure it logs
// "esp_ble_gatts_send_notify: rc=-1" and returns, so a caller that just calls notify() cannot tell
// a delivered frame from a discarded one. The status callback below is invoked synchronously from
// inside notify() (BLECharacteristic.cpp), so reading this flag immediately after the call reports
// that send's real outcome. Without it send_packet() reported success for frames Bluedroid dropped on
// the chip, which is what made a 50 Hz stream that never left the radio look healthy on serial
// while the relay saw only a fraction of the frames (2026-09-20 span-suppression investigation).
volatile bool g_last_notify_ok = false;

class NotifyStatusCallback : public BLECharacteristicCallbacks {
    void onStatus(BLECharacteristic *characteristic, Status status, uint32_t code) override {
        g_last_notify_ok = (status == Status::SUCCESS_NOTIFY);
    }
};

// FreeRTOS queue copies writes atomically between the BLE callback and Arduino loop.
// A single mutable slot used to overwrite unread ACK/feedback during bursts.
struct IncomingFrame {
    uint8_t data[CG_MAX_FRAME_SIZE];
    size_t len = 0;
};
QueueHandle_t g_incoming_queue = nullptr;
volatile uint32_t g_rx_dropped = 0;
uint32_t g_notify_dropped = 0;

uint8_t hex_nibble(char c) {
    if (c >= '0' && c <= '9') return (uint8_t)(c - '0');
    if (c >= 'a' && c <= 'f') return (uint8_t)(c - 'a' + 10);
    if (c >= 'A' && c <= 'F') return (uint8_t)(c - 'A' + 10);
    return 0;
}

void hex_to_bytes(const char *hex, uint8_t *out, size_t out_len) {
    for (size_t i = 0; i < out_len; i++) {
        out[i] = (uint8_t)((hex_nibble(hex[i * 2]) << 4) | hex_nibble(hex[i * 2 + 1]));
    }
}

class WriteCallback : public BLECharacteristicCallbacks {
    void onWrite(BLECharacteristic *characteristic) override {
        std::string value = characteristic->getValue();
        if (value.length() > CG_MAX_FRAME_SIZE) {
            return;  // oversized — not a valid frame, drop silently like a bad-length decode
        }
        IncomingFrame incoming;
        memcpy(incoming.data, value.data(), value.length());
        incoming.len = value.length();
        if (xQueueSend(g_incoming_queue, &incoming, 0) != pdTRUE) g_rx_dropped++;

    }
};

class ServerCallbacks : public BLEServerCallbacks {
    void onConnect(BLEServer *server) override {
        g_connection_generation++;
        g_connected = true;
        Serial.println("[ble] connected");
    }
    // Windows picks the connection interval when we don't ask, and its default (~30-45ms) cannot
    // drain a 50Hz (20ms) stream — each interval carries fewer frames than we produce, Bluedroid's
    // tx buffer backs up, and notify() starts failing with rc=-1. Two peripherals sharing one
    // adapter halves the per-link budget again, which is why whichever board connects first (and
    // so streams for longer before the second arrives) shows it worst. Request 7.5-15ms: a
    // peripheral-initiated L2CAP connection-parameter-update, which Windows honours far more often
    // than the passive advertising hint. Only a request — the central may refuse or pick another
    // value, hence the stall counters in send_sensor_frame() rather than trusting it took.
    void onConnect(BLEServer *server, esp_ble_gatts_cb_param_t *param) override {
        server->updateConnParams(param->connect.remote_bda, 0x06, 0x0C, 0, 400);
    }
    void onDisconnect(BLEServer *server) override {
        g_connection_generation++;
        g_connected = false;
        g_handshake_done = false;
        // Bluedroid does not clear the CCCD itself for a non-bonded central, so a stale "subscribed"
        // value would survive into the next connection and re-open the race this gate closes.
        if (g_notify_cccd != nullptr) {
            g_notify_cccd->setNotifications(false);
        }
        Serial.println("[ble] disconnected, advertising again");
        server->startAdvertising();
    }
};

void ble_setup() {
    g_incoming_queue = xQueueCreate(16, sizeof(IncomingFrame));
    configASSERT(g_incoming_queue != nullptr);
    BLEDevice::init(CG_DEMO_BLE_DEVICE_NAME);
    BLEServer *server = BLEDevice::createServer();
    server->setCallbacks(new ServerCallbacks());
    BLEService *service = server->createService(CG_DEMO_BLE_SERVICE_UUID);

    g_notify_char = service->createCharacteristic(
        CG_DEMO_BLE_NOTIFY_CHAR_UUID, BLECharacteristic::PROPERTY_NOTIFY);
    g_notify_char->setCallbacks(new NotifyStatusCallback());
    g_notify_cccd = new BLE2902();
    g_notify_char->addDescriptor(g_notify_cccd);

    BLECharacteristic *write_char = service->createCharacteristic(
        CG_DEMO_BLE_WRITE_CHAR_UUID, BLECharacteristic::PROPERTY_WRITE_NR | BLECharacteristic::PROPERTY_WRITE);
    write_char->setCallbacks(new WriteCallback());

    service->start();
    BLEAdvertising *advertising = BLEDevice::getAdvertising();
    advertising->addServiceUUID(CG_DEMO_BLE_SERVICE_UUID);
    advertising->start();
}

// SUCCESS_NOTIFY means accepted by Bluedroid, NOT delivery over the air.
bool notify_frame(const uint8_t *frame, size_t frame_len) {
    g_notify_char->setValue((uint8_t *)frame, frame_len);
    g_last_notify_ok = false;
    g_notify_char->notify();
    if (!g_last_notify_ok) g_notify_dropped++;
    return g_last_notify_ok;
}

// Encoding always consumes a fresh sequence. Reliable retries reuse these bytes, never reseal.
bool send_packet(uint8_t packet_type, const uint8_t *plaintext, size_t plaintext_len,
                  const uint8_t *cleartext_prefix, size_t cleartext_prefix_len,
                  uint16_t *wire_seq_out = nullptr) {
    uint32_t ext_seq;
    uint16_t seq = g_tx_seq.next(&ext_seq);
    uint32_t nonce_base = (packet_type == CG_PKT_HELLO) ? g_device_nonce : g_session_id;

    uint8_t frame[CG_MAX_FRAME_SIZE];
    size_t frame_len = cg_encode_frame(packet_type, seq, CG_DEMO_PLAYER_ID, plaintext, plaintext_len,
                                        cleartext_prefix, cleartext_prefix_len, g_psk, nonce_base,
                                        CG_DIR_DEVICE_TO_LAPTOP, ext_seq, frame);
    if (frame_len == 0) {
        return false;
    }
    if (wire_seq_out != nullptr) *wire_seq_out = seq;
    if (packet_type == CG_PKT_RFID_EVENT)
        g_pending_rfid.begin(frame, frame_len, seq, millis());
    return notify_frame(frame, frame_len);
}

// Blocks (with a timeout) until a frame of `expect_type` decodes successfully from the laptop.
// Returns true and fills plaintext_out/plaintext_len_out on success.
bool wait_for_packet(uint8_t expect_type, uint8_t *plaintext_out, size_t plaintext_cap,
                      size_t *plaintext_len_out, uint32_t timeout_ms) {
    uint32_t deadline = millis() + timeout_ms;
    uint32_t generation = g_connection_generation;
    while (g_connected && generation == g_connection_generation && (int32_t)(deadline - millis()) > 0) {
        IncomingFrame incoming;
        if (xQueueReceive(g_incoming_queue, &incoming, 0) == pdTRUE) {
            bool is_handshake = (expect_type == CG_PKT_HELLO_ACK);
            uint32_t nonce_base = is_handshake ? g_device_nonce : g_session_id;

            uint8_t packet_type, player_id;
            uint16_t seq;
            uint32_t ext_seq;
            // Validate length/CRC/magic/version/fragmentation before touching the replay window —
            // a frame corrupted in transit must never consume a window slot (matches codec.py's
            // check order: CRC/magic/version first, sequence_tracker.accept() only after).
            uint16_t wire_seq;
            if (!cg_frame_precheck(incoming.data, incoming.len, &wire_seq)) {
                continue;
            }
            CgSequenceTracker candidate = g_rx_seq;
            if (!candidate.accept(wire_seq, &ext_seq)) {
                continue;
            }
            size_t plaintext_len = cg_decode_frame(
                incoming.data, incoming.len, is_handshake ? 4 : 0, g_psk, nonce_base,
                CG_DIR_LAPTOP_TO_DEVICE, ext_seq, &packet_type, &seq, &player_id, plaintext_out,
                plaintext_cap);
            if (plaintext_len == 0 || packet_type != expect_type || player_id != CG_DEMO_PLAYER_ID) {
                continue;
            }
            g_rx_seq = candidate;
            *plaintext_len_out = plaintext_len;
            return true;
        }
        delay(2);
    }
    return false;
}

// Sends an ACK for `acked_seq`. Factored out so both the fresh-frame path and the
// already-seen-duplicate path (below) can answer identically — a duplicate must still be ACKed,
// since its arrival means our previous ACK was the packet that actually got lost.
void send_ack_for(uint16_t acked_seq) {
    uint8_t payload[2];  // cg_payload_ack_t
    memcpy(payload, &acked_seq, 2);
    send_packet(CG_PKT_ACK, payload, sizeof(payload), nullptr, 0);
}

void drain_incoming_non_blocking() {
    IncomingFrame incoming;
    // Bound each turn: sensor/button work continues even under incoming bursts.
    for (unsigned i = 0; i < 16 && xQueueReceive(g_incoming_queue, &incoming, 0) == pdTRUE; i++) {
        uint16_t wire_seq;
        if (!cg_frame_precheck(incoming.data, incoming.len, &wire_seq)) continue;
        if (g_feedback_cache.contains(incoming.data, incoming.len, millis())) {
            send_ack_for(wire_seq); // exact previously authenticated COMBAT_FEEDBACK only
            continue;
        }
        CgSequenceTracker candidate = g_rx_seq;
        uint32_t ext_seq;
        bool accepted = candidate.accept(wire_seq, &ext_seq);
        if (incoming.data[2] == CG_PKT_COMBAT_FEEDBACK) {
            int64_t inferred = g_rx_seq.infer(wire_seq);
            if (inferred <= g_feedback_cache.highest) continue;
            ext_seq = (uint32_t)inferred;
        } else if (!accepted) continue;
        uint8_t plaintext[CG_MAX_FRAME_SIZE], packet_type, player_id;
        uint16_t seq;
        size_t len = cg_decode_frame(incoming.data, incoming.len, 0, g_psk, g_session_id,
            CG_DIR_LAPTOP_TO_DEVICE, ext_seq, &packet_type, &seq, &player_id, plaintext, sizeof(plaintext));
        if (!len || player_id != CG_DEMO_PLAYER_ID) continue;
        if ((packet_type == CG_PKT_ACK && len != 2) ||
            (packet_type == CG_PKT_COMBAT_FEEDBACK && len != sizeof(cg_payload_combat_feedback_t))) continue;
        g_rx_seq = candidate; // commit replay state only after GCM authentication
        if (packet_type == CG_PKT_COMBAT_FEEDBACK) {
            g_feedback_cache.highest = ext_seq;
            g_feedback_cache.remember(incoming.data, incoming.len, millis());
            send_ack_for(seq);
            Serial.printf("[feedback] applied seq=%u code=%u\n", (unsigned)seq, (unsigned)plaintext[0]);
        } else if (packet_type == CG_PKT_ACK) {
            uint16_t acked_seq;
            memcpy(&acked_seq, plaintext, 2);
            bool was_pending = g_pending_rfid.active;
            g_pending_rfid.ack(acked_seq);
            if (was_pending && !g_pending_rfid.active)
                Serial.printf("[weapon] rfid_event acked seq=%u\n", (unsigned)acked_seq);
        }
    }
}

bool do_handshake() {
    g_tx_seq.reset();
    g_rx_seq.reset();
    g_pending_rfid.reset();
    g_feedback_cache.reset();
    g_capture_remaining = 0;
    g_session_id = 0;
    xQueueReset(g_incoming_queue);
    g_device_nonce = esp_random();
    uint8_t device_mac[6];
    esp_read_mac(device_mac, ESP_MAC_BT);

    // cg_payload_hello_t, little-endian, matches PAYLOAD_TABLES[HELLO] field order exactly.
    uint8_t hello[16];
    memcpy(hello, device_mac, 6);
    hello[6] = CG_DEMO_PLAYER_ID;
    hello[7] = CG_PROTOCOL_VERSION;
    hello[8] = 1;
    hello[9] = 0;  // firmware_version = 1 (u16 LE)
    memcpy(hello + 10, &g_device_nonce, 4);
    hello[14] = 0;
    hello[15] = 0;  // reserved

    uint8_t device_nonce_prefix[4];
    memcpy(device_nonce_prefix, &g_device_nonce, 4);

    if (!send_packet(CG_PKT_HELLO, hello, sizeof(hello), device_nonce_prefix, sizeof(device_nonce_prefix))) {
        Serial.println("[handshake] HELLO send failed");
        return false;
    }

    uint8_t ack_plaintext[32];
    size_t ack_len;
    if (!wait_for_packet(CG_PKT_HELLO_ACK, ack_plaintext, sizeof(ack_plaintext), &ack_len, 5000)) {
        Serial.println("[handshake] no HELLO_ACK");
        return false;
    }
    // cg_payload_hello_ack_t: session_id(4) | epoch_lo(4) | epoch_hi(4) | device_nonce_echo(4).
    uint32_t echoed_nonce = 0;
    if (ack_len != 16) return false;
    memcpy(&echoed_nonce, ack_plaintext + 12, 4);
    if (echoed_nonce != g_device_nonce) return false;
    memcpy(&g_session_id, ack_plaintext, 4);
    if (g_session_id == 0) {
        Serial.println("[handshake] refused (version mismatch)");
        return false;
    }
    for (int i = 0; i < 8; i++) {
        uint32_t t0 = millis();
        uint8_t req[4];
        memcpy(req, &t0, 4);
        if (!send_packet(CG_PKT_TIME_SYNC_REQ, req, sizeof(req), nullptr, 0)) {
            Serial.println("[handshake] TIME_SYNC_REQ send failed");
            return false;
        }

        uint8_t resp[16];
        size_t resp_len;
        if (!wait_for_packet(CG_PKT_TIME_SYNC_RESP, resp, sizeof(resp), &resp_len, 2000) ||
            resp_len != 12 || memcmp(resp, &t0, 4) != 0) {
            Serial.println("[handshake] TIME_SYNC_RESP timeout/mismatch");
            return false;
        }
        delay(100);
    }

    Serial.printf("[handshake] complete, session_id=0x%08X\n", g_session_id);
    return true;
}

void send_sensor_frame() {
    static float phase = 0.0f;
    phase += 0.3f;

    // Synthetic "swing" pattern: sine-wave accel/gyro on the dominant wrist, near-zero elsewhere.
    // Scaling per protocol/constants.py: accel 1 LSB = 1/2048 g, gyro 1 LSB = 1/16.4 deg/s.
    int16_t dominant[6] = {
        (int16_t)(sinf(phase) * 1.5f * 2048.0f), 0, (int16_t)(cosf(phase) * 0.5f * 2048.0f),
        (int16_t)(sinf(phase) * 200.0f * 16.4f), 0, 0,
    };
    int16_t offhand[6] = {0, 0, (int16_t)(1.0f * 2048.0f), 0, 0, 0};
    int16_t torso[6] = {0, 0, (int16_t)(1.0f * 2048.0f), 0, 0, 0};

    uint8_t flags = 0;
    if (g_capture_remaining > 0) {
        flags = CG_FLAG_MOTION_ACTIVE | ((g_capture_trigger_id & 0x7) << CG_FLAG_TRIGGER_ID_SHIFT);
        g_capture_remaining--;
    }

    uint8_t payload[42];
    uint32_t ts = millis();
    memcpy(payload, &ts, 4);
    payload[4] = flags;
    payload[5] = g_weapon_id;
    memcpy(payload + 6, dominant, 12);
    memcpy(payload + 18, offhand, 12);
    memcpy(payload + 30, torso, 12);

    // Rate-limited to 1 line/sec: at 50 Hz an unthrottled print would flood the monitor and
    // slow the loop. Without this the 50 Hz path is invisible on serial and a soak test looks
    // healthy while only heartbeats are actually flowing.
    //
    // `dropped` counts frames the controller refused (tx buffer full). It is the number that
    // matters: `sent` alone read a flat 50/s while the relay was receiving far fewer, because the
    // frames were being generated on time and discarded on-chip. sent+dropped is the generated
    // rate; `sent` counts stack acceptance only. The relay sees only
    // accepted frames; radio delivery still requires hardware validation.
    static uint32_t last_log_ms = 0;
    static uint32_t sent_since_log = 0;
    static uint32_t dropped_since_log = 0;
    if (send_packet(CG_PKT_SENSOR_FRAME, payload, sizeof(payload), nullptr, 0)) {
        sent_since_log++;
    } else {
        dropped_since_log++;
    }
    uint32_t now = millis();
    if (now - last_log_ms >= 1000) {
        last_log_ms = now;
        Serial.printf("[tx] sensor_frames=%lu/s dropped=%lu/s weapon=%u generated=%lu/s notify_dropped=%lu rx_dropped=%lu\n",
                      (unsigned long)sent_since_log, (unsigned long)dropped_since_log,
                      (unsigned)g_weapon_id, (unsigned long)(sent_since_log + dropped_since_log),
                      (unsigned long)g_notify_dropped, (unsigned long)g_rx_dropped);
        sent_since_log = 0;
        dropped_since_log = 0;
    }
}

void send_heartbeat() {
    Serial.printf("[tx] heartbeat t=%lu\n", (unsigned long)millis());
    uint8_t payload[12];
    uint32_t ts = millis();
    memcpy(payload, &ts, 4);
    uint16_t vbat = 4000, status = 0;
    memcpy(payload + 4, &vbat, 2);
    memcpy(payload + 6, &vbat, 2);
    memcpy(payload + 8, &vbat, 2);
    memcpy(payload + 10, &status, 2);
    send_packet(CG_PKT_HEARTBEAT, payload, sizeof(payload), nullptr, 0);
}

// Weapon wire values per comms/src/cg_comms/messages/enums.py WIRE_VALUES: NONE=0 SWORD=1 SPEAR=2 MACE=3.
void send_current_weapon() {
    if (g_weapon_id == 0) return;
    uint8_t payload[12];
    uint32_t ts = millis();
    memcpy(payload, &ts, 4);
    payload[4] = g_weapon_id;
    payload[5] = 0;
    uint8_t uid[6] = {0xE0, 0x04, 0x01, 0x50, CG_DEMO_PLAYER_ID, g_weapon_id};
    memcpy(payload + 6, uid, 6);
    // A new selection supersedes the pending old selection, including its ACK identity.
    bool accepted = send_packet(CG_PKT_RFID_EVENT, payload, sizeof(payload), nullptr, 0);
    Serial.printf("[weapon] now %u seq=%u accepted=%u\n",
                  (unsigned)g_weapon_id, (unsigned)g_pending_rfid.seq, (unsigned)accepted);
}

void send_rfid_event_next_weapon() {
    g_weapon_id = (g_weapon_id % 3) + 1;
    send_current_weapon();
}

void service_pending_rfid_event() {
    int due = g_pending_rfid.poll(millis());
    if (due < 0) {
        Serial.printf("[weapon] rfid_event undelivered after 5 attempts, wire_seq=%u\n",
                      (unsigned)g_pending_rfid.seq);
    } else if (due > 0) {
        bool accepted = notify_frame(g_pending_rfid.frame, g_pending_rfid.len);
        Serial.printf("[weapon] rfid_event retransmit attempt=%u wire_seq=%u accepted=%u\n",
                      (unsigned)g_pending_rfid.attempts, (unsigned)g_pending_rfid.seq, (unsigned)accepted);
    }
}

}  // namespace

void setup() {
    Serial.begin(115200);
    hex_to_bytes(CG_DEMO_PSK_HEX, g_psk, CG_PSK_SIZE);
    pinMode(CG_DEMO_WEAPON_BUTTON_PIN, INPUT_PULLUP);
    g_weapon_button.pin = CG_DEMO_WEAPON_BUTTON_PIN;
    for (int i = 0; i < CG_DEMO_ACTION_BUTTON_COUNT; i++) {
        pinMode(kActionButtonPins[i], INPUT_PULLUP);
        g_action_buttons[i].pin = kActionButtonPins[i];
    }
    ble_setup();
    Serial.println("[boot] advertising, waiting for relay connection");
}

void loop() {
    static uint32_t last_sensor_ms = 0;
    static uint32_t last_heartbeat_ms = 0;

    if (!g_connected) {
        delay(50);
        return;
    }
    // A BLE connection alone does not mean the relay can hear us: transport/ble.py calls
    // start_notify() only after client.connect() returns, so a HELLO sent in that gap is notified
    // into a void and the handshake burns its full 5s HELLO_ACK timeout before retrying. Wait for
    // the central to write the CCCD (which is what start_notify does) before speaking.
    if (g_notify_cccd == nullptr || !g_notify_cccd->getNotifications()) {
        delay(10);
        return;
    }
    if (!g_handshake_done) {
        static bool announced = false;
        if (!announced) {
            Serial.println("[ble] relay subscribed, starting handshake");
            announced = true;
        }
        uint32_t generation = g_connection_generation;
        bool completed = do_handshake();
        g_handshake_done = completed && g_connected && generation == g_connection_generation;
        if (g_handshake_done) send_current_weapon(); // reannounce held selection after BLE/relay restart
        if (!g_handshake_done) {
            announced = false;
            delay(1000);  // let the relay retry its own reconnect loop rather than spamming HELLOs
        }
        return;
    }

    uint32_t now = millis();
    if (now - last_sensor_ms >= CG_DEMO_SENSOR_FRAME_INTERVAL_MS) {
        last_sensor_ms = now;
        send_sensor_frame();
    }
    if (now - last_heartbeat_ms >= CG_DEMO_HEARTBEAT_INTERVAL_MS) {
        last_heartbeat_ms = now;
        send_heartbeat();
    }

    if (debounce_falling_edge(&g_weapon_button, now)) {
        send_rfid_event_next_weapon();
    }
    for (int i = 0; i < CG_DEMO_ACTION_BUTTON_COUNT; i++) {
        if (debounce_falling_edge(&g_action_buttons[i], now)) {
            // A press during an active capture is ignored — the capture is atomic (§3.2), so a
            // double-tap still yields exactly one window.
            if (g_capture_remaining == 0) {
                g_capture_remaining = CG_DEMO_CAPTURE_FRAMES;
                g_capture_trigger_id = kActionButtonTriggerIds[i];
                Serial.printf("[capture] start trigger_id=%u frames=%lu\n",
                              (unsigned)g_capture_trigger_id, (unsigned long)g_capture_remaining);
            }
        }
    }

    drain_incoming_non_blocking();
    service_pending_rfid_event();
}
