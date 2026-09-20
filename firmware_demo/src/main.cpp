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

// Retransmit-until-ACKed state for this firmware's one steady-state RELIABLE_PACKET_TYPES send
// (RFID_EVENT — HELLO/TIME_SYNC_REQ already retry via do_handshake's own wait_for_packet loop).
// Mirrors comms/src/cg_comms/relay/reliability.py's ReliabilitySender: the exact same encoded
// frame bytes are resent on timeout (identical wire seq), 200ms base timeout, x2 backoff, 5
// attempts. Without this, one dropped RFID_EVENT notification means the relay's _current_weapon
// never gets set even though the firmware believes the weapon was sent — every action after that
// suppresses at the "weapon chosen" gate (windowing.py) until the button is pressed again.
uint8_t g_pending_frame[CG_MAX_FRAME_SIZE];
size_t g_pending_frame_len = 0;
uint16_t g_pending_wire_seq = 0;
bool g_pending_active = false;
uint8_t g_pending_attempt = 0;
uint32_t g_pending_timeout_ms = 0;
uint32_t g_pending_next_send_ms = 0;

const uint32_t kReliableBaseTimeoutMs = 200;
const uint8_t kReliableMaxAttempts = 5;

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

// Set by the notify-characteristic's write callback (laptop -> device frames arrive there since
// this is a symmetric two-characteristic link, matching comms/src/cg_comms/transport/ble.py).
struct IncomingFrame {
    uint8_t data[CG_MAX_FRAME_SIZE];
    size_t len = 0;
    volatile bool ready = false;
};
IncomingFrame g_incoming;

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
        memcpy(g_incoming.data, value.data(), value.length());
        g_incoming.len = value.length();
        g_incoming.ready = true;
    }
};

class ServerCallbacks : public BLEServerCallbacks {
    void onConnect(BLEServer *server) override {
        g_connected = true;
        Serial.println("[ble] connected");
    }
    void onDisconnect(BLEServer *server) override {
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
    BLEDevice::init(CG_DEMO_BLE_DEVICE_NAME);
    BLEServer *server = BLEDevice::createServer();
    server->setCallbacks(new ServerCallbacks());
    BLEService *service = server->createService(CG_DEMO_BLE_SERVICE_UUID);

    g_notify_char = service->createCharacteristic(
        CG_DEMO_BLE_NOTIFY_CHAR_UUID, BLECharacteristic::PROPERTY_NOTIFY);
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

// Sends one plaintext payload as a fully-encoded, encrypted wire frame, device -> laptop.
// On success, if `wire_seq_out`/`frame_out`/`frame_len_out` are non-null, also hands back the
// encoded frame and its wire seq so the caller can retransmit the identical bytes later (needed
// for RELIABLE_PACKET_TYPES sends — see g_pending_* above).
bool send_packet(uint8_t packet_type, const uint8_t *plaintext, size_t plaintext_len,
                  const uint8_t *cleartext_prefix, size_t cleartext_prefix_len,
                  uint16_t *wire_seq_out = nullptr, uint8_t *frame_out = nullptr,
                  size_t *frame_len_out = nullptr) {
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
    g_notify_char->setValue(frame, frame_len);
    g_notify_char->notify();
    if (wire_seq_out != nullptr) {
        *wire_seq_out = seq;
    }
    if (frame_out != nullptr && frame_len_out != nullptr) {
        memcpy(frame_out, frame, frame_len);
        *frame_len_out = frame_len;
    }
    return true;
}

// Re-sends the exact previously-encoded frame bytes (same wire seq) — used for retransmits, where
// re-encoding would mint a new seq and defeat the relay's/device's dedup-by-seq ACK matching.
void resend_raw_frame(const uint8_t *frame, size_t frame_len) {
    g_notify_char->setValue(const_cast<uint8_t *>(frame), frame_len);
    g_notify_char->notify();
}

// Blocks (with a timeout) until a frame of `expect_type` decodes successfully from the laptop.
// Returns true and fills plaintext_out/plaintext_len_out on success.
bool wait_for_packet(uint8_t expect_type, uint8_t *plaintext_out, size_t plaintext_cap,
                      size_t *plaintext_len_out, uint32_t timeout_ms) {
    uint32_t deadline = millis() + timeout_ms;
    while ((int32_t)(deadline - millis()) > 0) {
        if (g_incoming.ready) {
            g_incoming.ready = false;
            bool is_handshake = (expect_type == CG_PKT_HELLO_ACK);
            uint32_t nonce_base = is_handshake ? g_device_nonce : g_session_id;

            uint8_t packet_type, player_id;
            uint16_t seq;
            uint32_t ext_seq;
            // Validate length/CRC/magic/version/fragmentation before touching the replay window —
            // a frame corrupted in transit must never consume a window slot (matches codec.py's
            // check order: CRC/magic/version first, sequence_tracker.accept() only after).
            uint16_t wire_seq;
            if (!cg_frame_precheck(g_incoming.data, g_incoming.len, &wire_seq)) {
                continue;
            }
            if (!g_rx_seq.accept(wire_seq, &ext_seq)) {
                continue;
            }
            size_t plaintext_len = cg_decode_frame(
                g_incoming.data, g_incoming.len, is_handshake ? 4 : 0, g_psk, nonce_base,
                CG_DIR_LAPTOP_TO_DEVICE, ext_seq, &packet_type, &seq, &player_id, plaintext_out,
                plaintext_cap);
            if (plaintext_len == 0 || packet_type != expect_type) {
                continue;
            }
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
    if (!g_incoming.ready) {
        return;
    }
    g_incoming.ready = false;
    uint8_t plaintext[CG_MAX_FRAME_SIZE];
    uint8_t packet_type, player_id;
    uint16_t seq;
    uint32_t ext_seq;
    uint16_t wire_seq;
    if (!cg_frame_precheck(g_incoming.data, g_incoming.len, &wire_seq)) {
        // Corrupted-in-transit frame — never got far enough to be a genuine wire seq, must not
        // touch the replay window (see cg_frame_precheck's doc comment).
        return;
    }
    if (!g_rx_seq.accept(wire_seq, &ext_seq)) {
        // A replay-window duplicate is expected steady-state behavior, not a fault: the relay's
        // ReliabilitySender (comms/src/cg_comms/relay/reliability.py) retransmits COMBAT_FEEDBACK
        // with the *same* wire seq whenever our ACK doesn't make it back in time, so this branch
        // fires exactly when the relay is mid-retry. Previously this `return`ed here, before ever
        // reaching the COMBAT_FEEDBACK ACK below — so a single lost ACK meant every retransmit of
        // the same seq kept getting silently dropped as "already seen", guaranteeing attempts 2-5
        // burn their timeouts for nothing and the relay reports link_degraded on a link that is
        // actually fine. We can't re-decrypt a duplicate (its ext_seq was only valid once, for
        // nonce derivation), but we already know its wire seq, and re-ACKing a wire seq we've
        // already accepted is always safe regardless of packet type.
        Serial.printf("[rx] replay-window duplicate, wire_seq=%u, re-acking\n", (unsigned)wire_seq);
        send_ack_for(wire_seq);
        return;
    }
    size_t plaintext_len = cg_decode_frame(g_incoming.data, g_incoming.len, 0, g_psk, g_session_id,
                                            CG_DIR_LAPTOP_TO_DEVICE, ext_seq, &packet_type, &seq,
                                            &player_id, plaintext, sizeof(plaintext));
    if (plaintext_len == 0) {
        return;
    }
    Serial.printf("[rx] packet_type=0x%02X len=%u\n", packet_type, (unsigned)plaintext_len);

    // COMBAT_FEEDBACK is reliable (cg_protocol.h), so the relay sends it through ReliabilitySender
    // and waits for an ACK. Logging it without answering makes the relay retransmit every hit five
    // times and then report link_degraded + combat_feedback_undelivered at ERROR — a loud failure on
    // a perfectly healthy link, caused only by the device staying quiet. ACK is never itself acked,
    // so the laptop's ACK of our RFID_EVENT needs no reply here (it's handled below instead).
    if (packet_type == CG_PKT_COMBAT_FEEDBACK) {
        send_ack_for(seq);
    } else if (packet_type == CG_PKT_ACK) {
        uint16_t acked_seq;
        memcpy(&acked_seq, plaintext, 2);
        if (g_pending_active && acked_seq == g_pending_wire_seq) {
            g_pending_active = false;
        }
    }
}

bool do_handshake() {
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
        Serial.println("[handshake] HELLO encode failed");
        return false;
    }

    uint8_t ack_plaintext[32];
    size_t ack_len;
    if (!wait_for_packet(CG_PKT_HELLO_ACK, ack_plaintext, sizeof(ack_plaintext), &ack_len, 5000)) {
        Serial.println("[handshake] no HELLO_ACK");
        return false;
    }
    // cg_payload_hello_ack_t: session_id(4) | epoch_lo(4) | epoch_hi(4) | device_nonce_echo(4).
    memcpy(&g_session_id, ack_plaintext, 4);
    if (g_session_id == 0) {
        Serial.println("[handshake] refused (version mismatch)");
        return false;
    }
    g_rx_seq.reset();  // fresh session -> fresh replay window, matches Session per-reconnect behavior

    for (int i = 0; i < 8; i++) {
        uint32_t t0 = millis();
        uint8_t req[4];
        memcpy(req, &t0, 4);
        send_packet(CG_PKT_TIME_SYNC_REQ, req, sizeof(req), nullptr, 0);

        uint8_t resp[16];
        size_t resp_len;
        wait_for_packet(CG_PKT_TIME_SYNC_RESP, resp, sizeof(resp), &resp_len, 2000);
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

    if (send_packet(CG_PKT_SENSOR_FRAME, payload, sizeof(payload), nullptr, 0)) {
        // Rate-limited to 1 line/sec: at 50 Hz an unthrottled print would flood the monitor and
        // slow the loop. Without this the 50 Hz path is invisible on serial and a soak test looks
        // healthy while only heartbeats are actually flowing.
        static uint32_t last_log_ms = 0;
        static uint32_t frames_since_log = 0;
        frames_since_log++;
        uint32_t now = millis();
        if (now - last_log_ms >= 1000) {
            last_log_ms = now;
            Serial.printf("[tx] sensor_frames=%lu/s weapon=%u\n", (unsigned long)frames_since_log,
                          (unsigned)g_weapon_id);
            frames_since_log = 0;
        }
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
void send_rfid_event_next_weapon() {
    g_weapon_id = (g_weapon_id % 3) + 1;  // NONE(0) is never selected by a "tag pickup"
    uint8_t payload[12];
    uint32_t ts = millis();
    memcpy(payload, &ts, 4);
    payload[4] = g_weapon_id;
    payload[5] = 0;  // reserved
    uint8_t tag_uid[6] = {0xE0, 0x04, 0x01, 0x50, CG_DEMO_PLAYER_ID, g_weapon_id};
    memcpy(payload + 6, tag_uid, 6);

    // A press during an already-pending (unacked) RFID_EVENT replaces it: the newest weapon is
    // the one that matters, and the old wire seq's ACK (if it arrives late) is simply ignored by
    // the ACK handler below since g_pending_active/g_pending_wire_seq will have moved on.
    uint16_t wire_seq;
    size_t frame_len;
    if (send_packet(CG_PKT_RFID_EVENT, payload, sizeof(payload), nullptr, 0, &wire_seq,
                     g_pending_frame, &frame_len)) {
        g_pending_frame_len = frame_len;
        g_pending_wire_seq = wire_seq;
        g_pending_active = true;
        g_pending_attempt = 1;
        g_pending_timeout_ms = kReliableBaseTimeoutMs;
        g_pending_next_send_ms = millis() + kReliableBaseTimeoutMs;
    }
    Serial.printf("[weapon] now %u\n", g_weapon_id);
}

// Retransmits the pending RFID_EVENT on timeout, exponential backoff, matching
// comms/src/cg_comms/relay/reliability.py's ReliabilitySender (base 200ms, x2 backoff, 5 attempts,
// then give up — logged, not silent, since a stuck weapon looks identical to a "chosen" one on
// this board's own serial monitor).
void service_pending_rfid_event() {
    if (!g_pending_active) {
        return;
    }
    uint32_t now = millis();
    if ((int32_t)(now - g_pending_next_send_ms) < 0) {
        return;
    }
    if (g_pending_attempt >= kReliableMaxAttempts) {
        Serial.printf("[weapon] rfid_event undelivered after %u attempts, wire_seq=%u\n",
                      (unsigned)g_pending_attempt, (unsigned)g_pending_wire_seq);
        g_pending_active = false;
        return;
    }
    g_pending_attempt++;
    resend_raw_frame(g_pending_frame, g_pending_frame_len);
    g_pending_timeout_ms *= 2;
    g_pending_next_send_ms = now + g_pending_timeout_ms;
    Serial.printf("[weapon] rfid_event retransmit attempt=%u wire_seq=%u\n", (unsigned)g_pending_attempt,
                  (unsigned)g_pending_wire_seq);
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
        g_handshake_done = do_handshake();
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
