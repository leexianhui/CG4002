#ifdef CG_HARDWARE_GATEWAY
#include "gateway_boundary.h"
#include "gateway_link.h"
#endif
// ============================================================================
// LEFT WRIST NODE. Follows the phase the right-wrist hub broadcasts in
// HubStatus:
//   before PHASE_LOCKED — broadcasts WristPose for weapon detection.
//   PHASE_LOCKED        — the valid-action algorithm doesn't use the left
//                         wrist, so it stops IMU reads + pose broadcasts and
//                         just shows the locked weapon's colour on its LED
//                         (sword&shield=cyan, spear=magenta, mace=orange).
//
// Before the lock:
//   - Reads its own MPU6500 and computes the gravity direction in the
//     board's frame (static tilt only — no gyro integration, so no drift;
//     this is a held-pose reading, not a continuous-motion one).
//   - Gates readings on "stillness" (gyro magnitude below threshold for a
//     sustainedany window) so a reading is only trusted once the player is
//     holding the ready stance still.
//   - Broadcasts a lightweight WristPose UDP packet. The hub combines it
//     with its own pose to classify the weapon.
// ============================================================================

// ============================================================================
// BUILD CONFIG — edit here
// ============================================================================
// ---- WiFi credentials (laptop hotspot) — same on all three boards ----
const char *ssid = "xin hui's laptop";
const char *password = "12345678";

// 1 = WiFi.setSleep(false) once connected: modem sleep off, so broadcasts
//     aren't held until the next DTIM wake-up (lower, steadier latency, more
//     current). 0 = keep the ESP32 default (modem sleep on).
#ifndef WIFI_SLEEP_DISABLED
#define WIFI_SLEEP_DISABLED 1
#endif

// Which player this set of boards belongs to. Must be the same on all three
// boards of one player; packets carrying another PLAYER_ID are dropped.
#ifndef PLAYER_ID
#define PLAYER_ID 1
#endif

// Each player's boards use their own block of UDP ports (player 1:
// 4210-4213, player 2: 4220-4223), so the two sets of boards never see
// each other's broadcasts and the laptop can listen to each separately.
#define PLAYER_PORT_OFFSET ((PLAYER_ID - 1) * 10)

#include <Wire.h>
#include <WiFi.h>
#include <WiFiUdp.h>

// ======================= SHARED WIRE PROTOCOL — BEGIN =======================
// MUST STAY IN SYNC: this whole block is identical in the right wrist, torso
// and left wrist sketches. Change all three together. Declared before
// anything else so Arduino's auto-generated function prototypes can see
// these types.
//
// Every binary packet starts with a PacketHeader. Structs are packed and go
// on the wire as-is (little-endian). Receivers dispatch on hdr.type, reject
// a packet whose length isn't PACKET_SIZE[hdr.type], drop other players'
// packets (hdr.playerId) and drop their own broadcast echoes (hdr.deviceId).
//
// On-wire sizes (bytes):
//   PacketHeader     5 = type(1) playerId(1) deviceId(1) seq(2)
//   HubStatus        8 = hdr(5) phase(1) weapon(1) holding(1)
//   WristPose       18 = hdr(5) ms(4) gravEl(4) gravAz(4) stable(1)
//   TorsoPose       18 = hdr(5) ms(4) pitch(4) roll(4) stable(1)
//   TorsoBeacon     13 = hdr(5) torsoMs(4) gyroMag(4)
//   SegmentRequest  17 = hdr(5) segId(4) startTorsoMs(4) endTorsoMs(4)
//   LatencyProbe     9 = hdr(5) t0Us(4)    PING and PONG
//   SegmentAck       9 = hdr(5) segId(4)

// hdr.deviceId values
#define RIGHT_WRIST_ID 0 // the hub
#define TORSO_ID 1
#define LEFT_WRIST_ID 2
#define DEVICE_COUNT 3

enum WeaponType
{
    WEAPON_UNKNOWN,
    WEAPON_SWORD_SHIELD,
    WEAPON_SPEAR,
    WEAPON_MACE
};

enum GamePhase
{
    PHASE_WAITING,
    PHASE_DETECTING,
    PHASE_LOCKED
};

// hdr.type values. 0 is deliberately unused so a zeroed buffer never parses.
enum PacketType : uint8_t
{
    PKT_INVALID = 0,
    PKT_HUB_STATUS,      // hub -> torso + left wrist, POSE_PORT
    PKT_WRIST_POSE,      // left wrist -> hub, POSE_PORT
    PKT_TORSO_POSE,      // torso -> hub, POSE_PORT (before lock)
    PKT_TORSO_BEACON,    // torso -> hub, LINK_PORT (after lock)
    PKT_SEGMENT_REQUEST, // hub -> torso, LINK_PORT
    PKT_PING,            // hub -> torso, LINK_PORT (LATENCY_TEST builds)
    PKT_PONG,            // torso -> hub, LINK_PORT (LATENCY_TEST builds)
    PKT_SEGMENT_ACK,     // torso -> hub, LINK_PORT (reply to every SEGMENT_REQUEST)
    PKT_TYPE_COUNT
};

struct __attribute__((packed)) PacketHeader
{
    uint8_t type;     // PacketType
    uint8_t playerId; // sender's PLAYER_ID
    uint8_t deviceId; // sender's RIGHT_WRIST_ID / TORSO_ID / LEFT_WRIST_ID
    uint16_t seq;     // per-sender, per-type counter; wraps 65535 -> 0
};

struct __attribute__((packed)) HubStatus
{
    PacketHeader hdr; // PKT_HUB_STATUS
    uint8_t phase;    // GamePhase
    uint8_t weapon;   // WeaponType — current candidate while detecting, locked weapon once locked
    bool holding;     // a known weapon is classified right now (counting toward the lock)
};

struct __attribute__((packed)) WristPose
{
    PacketHeader hdr; // PKT_WRIST_POSE
    uint32_t ms;
    float gravEl, gravAz; // gravity direction in the board's frame, degrees:
                          // el = asin(z), az = atan2(y, x) of the unit accel vector
    bool stable;
};

struct __attribute__((packed)) TorsoPose
{
    PacketHeader hdr; // PKT_TORSO_POSE
    uint32_t ms;
    float pitch, roll;
    bool stable;
};

struct __attribute__((packed)) TorsoBeacon
{
    PacketHeader hdr; // PKT_TORSO_BEACON
    uint32_t torsoMs;
    float gyroMag; // max |omega| over the samples since the previous beacon, dps
};

struct __attribute__((packed)) SegmentRequest
{
    PacketHeader hdr; // PKT_SEGMENT_REQUEST
    uint32_t segId;
    uint32_t startTorsoMs; // window already converted into TORSO's clock domain
    uint32_t endTorsoMs;
};

// PING: hub stamps t0Us with its micros(). PONG: torso sends the same t0Us
// back untouched (only the header changes), so RTT = hub micros() - t0Us.
struct __attribute__((packed)) LatencyProbe
{
    PacketHeader hdr; // PKT_PING or PKT_PONG
    uint32_t t0Us;
};

// Torso -> hub: "SegmentRequest segId arrived". Sent for every copy of a
// request, including retries of one already handled, so a lost ack is
// covered by the hub's next retry.
struct __attribute__((packed)) SegmentAck
{
    PacketHeader hdr; // PKT_SEGMENT_ACK
    uint32_t segId;
};

// Expected on-wire length, indexed by PacketType (order must match the enum).
const uint8_t PACKET_SIZE[PKT_TYPE_COUNT] = {
    0,
    sizeof(HubStatus),
    sizeof(WristPose),
    sizeof(TorsoPose),
    sizeof(TorsoBeacon),
    sizeof(SegmentRequest),
    sizeof(LatencyProbe),
    sizeof(LatencyProbe),
    sizeof(SegmentAck),
};

// Names used in log lines, indexed by PacketType / deviceId.
const char *const PACKET_NAME[PKT_TYPE_COUNT] = {
    "INVALID", "HUB_STATUS", "WRIST_POSE", "TORSO_POSE", "BEACON", "SEG_REQ", "PING", "PONG", "SEG_ACK"};
const char *const DEVICE_NAME[DEVICE_COUNT] = {"right_wrist", "torso", "left_wrist"};

static_assert(sizeof(PacketHeader) == 5, "PacketHeader size changed");
static_assert(sizeof(HubStatus) == 8, "HubStatus size changed");
static_assert(sizeof(WristPose) == 18, "WristPose size changed");
static_assert(sizeof(TorsoPose) == 18, "TorsoPose size changed");
static_assert(sizeof(TorsoBeacon) == 13, "TorsoBeacon size changed");
static_assert(sizeof(SegmentRequest) == 17, "SegmentRequest size changed");
static_assert(sizeof(LatencyProbe) == 9, "LatencyProbe size changed");
static_assert(sizeof(SegmentAck) == 9, "SegmentAck size changed");
// ======================= SHARED WIRE PROTOCOL — END =========================

// Declared up here so Arduino's auto-generated prototypes can see it.
enum LedMode
{
    LEDM_CALIBRATING,
    LEDM_WAITING,  // blinking amber — no hub yet / hub waiting for nodes
    LEDM_SAMPLING, // green — detecting, nothing held
    LEDM_HOLDING,  // blue — known weapon held, counting toward lock
    LEDM_LOCKED    // weapon colour
};

// ================= CONFIG =================
#define THIS_DEVICE_ID LEFT_WRIST_ID

IPAddress broadcastIP;                            // subnet broadcast, computed in setup() once connected
const int DEBUG_PORT = 4211 + PLAYER_PORT_OFFSET; // -> laptop listener (debug lines)
const int POSE_PORT = 4212 + PLAYER_PORT_OFFSET;  // <-> hub (WristPose out, HubStatus in)

#define DEBUG_LOG true

#if defined(CG_HARDWARE_GATEWAY) && CG_GATEWAY_NODE_LINK
// Board-to-board packets go over BLE (hardware/gateway/include/gateway_link.h).
GatewayLinkUdp debugUdp;
GatewayLinkUdp poseUdp;
#else
WiFiUDP debugUdp;
WiFiUDP poseUdp;
#endif

// ---------------- RGB LED (status indicator, common cathode) ----------------
#define LED_R_PIN 25
#define LED_G_PIN 26
#define LED_B_PIN 27

void ledSetColor(int r, int g, int b)
{
    analogWrite(LED_R_PIN, r);
    analogWrite(LED_G_PIN, g);
    analogWrite(LED_B_PIN, b);
}
void ledCalibrating() { ledSetColor(255, 255, 255); } // white

// Pre-lock LED mirrors the hub, same scheme as the torso. Tracked so the PWM
// is only rewritten on an actual change.
LedMode ledMode = LEDM_CALIBRATING;

// Waiting blinks amber 500ms on / 500ms off, same as the hub's PAT_BLINK.
const unsigned long WAITING_BLINK_MS = 500;
bool waitingBlinkOn = false;

void setLedMode(LedMode m)
{
    if (m == LEDM_WAITING)
    {
        // Re-evaluated on every call (not just on a mode change) so it blinks.
        bool on = (millis() / WAITING_BLINK_MS) % 2 == 0;
        if (ledMode == LEDM_WAITING && on == waitingBlinkOn)
            return;
        ledMode = m;
        waitingBlinkOn = on;
        if (on)
            ledSetColor(255, 80, 0);
        else
            ledSetColor(0, 0, 0);
        return;
    }
    if (m == ledMode)
        return;
    ledMode = m;
    switch (m)
    {
    case LEDM_SAMPLING:
        ledSetColor(0, 255, 0);
        break;
    case LEDM_HOLDING:
        ledSetColor(0, 0, 255);
        break;
    default:
        break;
    }
}

void ledWeaponColor(WeaponType w)
{
    ledMode = LEDM_LOCKED;
    switch (w)
    {
    case WEAPON_SWORD_SHIELD:
        ledSetColor(0, 255, 255); // cyan
        break;
    case WEAPON_SPEAR:
        ledSetColor(255, 0, 255); // magenta
        break;
    case WEAPON_MACE:
        ledSetColor(255, 40, 0); // orange
        break;
    default:
        ledSetColor(255, 255, 255);
        break;
    }
}

// ---------------- Packet header helpers ----------------
uint16_t txSeq[PKT_TYPE_COUNT]; // next seq to send, per packet type

void fillHeader(PacketHeader &h, PacketType type)
{
    h.type = type;
    h.playerId = PLAYER_ID;
    h.deviceId = THIS_DEVICE_ID;
    h.seq = txSeq[type]++; // uint16_t, wraps 65535 -> 0
}

// True if buf holds a well-formed packet (len = the datagram's real length)
// from another board of this player; copies its header into hdr.
bool acceptPacket(const uint8_t *buf, int len, PacketHeader &hdr)
{
    if (len < (int)sizeof(PacketHeader))
        return false;
    memcpy(&hdr, buf, sizeof(hdr));
    if (hdr.type >= PKT_TYPE_COUNT || PACKET_SIZE[hdr.type] == 0 || len != PACKET_SIZE[hdr.type])
        return false; // unknown type, or wrong length for its type
    if (hdr.playerId != PLAYER_ID)
        return false; // another player's boards
    if (hdr.deviceId == THIS_DEVICE_ID)
        return false; // our own broadcast looping back
    return true;
}

// ---------------- Link stats (packet loss) ----------------
// For each sender + packet type this board consumes: packets received and
// packets missing from the seq sequence. Printed and reset once a second as
//   [stats] to=<this board> from=<sender> type=<TYPE> rx=<n> lost=<n>
// Loss is counted when the next packet arrives, so an outage shows up as
// rx=0 lines followed by one line with the whole gap in lost=.
struct LinkStats
{
    bool seen;
    uint16_t lastSeq;
    unsigned long lastRxMs;
    uint32_t rx;   // this window
    uint32_t lost; // this window
};
LinkStats linkStats[DEVICE_COUNT][PKT_TYPE_COUNT];

const unsigned long STATS_INTERVAL_MS = 1000;
const unsigned long STATS_STALE_MS = 5000;   // stop printing a stream that stopped (e.g. poses after lock)
const unsigned long MIN_TX_INTERVAL_MS = 10; // nothing is sent faster than 100Hz
unsigned long lastStatsMs = 0;

// Call once for every accepted packet this board acts on.
void noteRx(const PacketHeader &h)
{
    if (h.deviceId >= DEVICE_COUNT)
        return;
    LinkStats &s = linkStats[h.deviceId][h.type];
    unsigned long now = millis();
    if (s.seen)
    {
        // 16-bit unsigned subtraction handles the 65535 -> 0 wrap (gap 1).
        uint16_t gap = (uint16_t)(h.seq - s.lastSeq);
        if (gap == 0)
            return; // duplicate
        // A gap larger than the sender could have produced since the last
        // packet means its counter restarted (reboot) — resync, don't count.
        uint32_t maxGap = (now - s.lastRxMs) / MIN_TX_INTERVAL_MS + 16;
        if (gap <= maxGap)
            s.lost += gap - 1;
    }
    s.seen = true;
    s.lastSeq = h.seq;
    s.lastRxMs = now;
    s.rx++;
}

void printLinkStats(unsigned long now)
{
    if (now - lastStatsMs < STATS_INTERVAL_MS)
        return;
    lastStatsMs = now;
    for (int d = 0; d < DEVICE_COUNT; d++)
    {
        for (int t = 0; t < PKT_TYPE_COUNT; t++)
        {
            LinkStats &s = linkStats[d][t];
            if (!s.seen || (s.rx == 0 && s.lost == 0 && now - s.lastRxMs > STATS_STALE_MS))
                continue;
            debugLog(String("[stats] to=") + DEVICE_NAME[THIS_DEVICE_ID] + " from=" + DEVICE_NAME[d] +
                     " type=" + PACKET_NAME[t] + " rx=" + String(s.rx) + " lost=" + String(s.lost));
            s.rx = 0;
            s.lost = 0;
        }
    }
}

// ---------------- MPU6500 ----------------
#define MPU6500_ADDR 0x68
#define REG_PWR_MGMT_1 0x6B
#define REG_GYRO_CONFIG 0x1B
#define REG_ACCEL_CONFIG 0x1C
#define REG_ACCEL_XOUT_H 0x3B // accel(6) + temp(2) + gyro(6) = 14 contiguous bytes

const float ACCEL_SCALE = 4096.0; // +/-8g
const float GYRO_SCALE = 16.4;    // +/-2000dps

void writeRegister(uint8_t reg, uint8_t value)
{
    Wire.beginTransmission(MPU6500_ADDR);
    Wire.write(reg);
    Wire.write(value);
    Wire.endTransmission();
}

// out7 layout: [ax,ay,az,temp,gx,gy,gz] as raw int16.
bool readIMUBurst(int16_t *out7)
{
    Wire.beginTransmission(MPU6500_ADDR);
    Wire.write(REG_ACCEL_XOUT_H);
    if (Wire.endTransmission(false) != 0)
        return false;
    uint8_t n = Wire.requestFrom((int)MPU6500_ADDR, 14);
    if (n != 14)
        return false;
    for (int i = 0; i < 7; i++)
    {
        int16_t hi = Wire.read();
        int16_t lo = Wire.read();
        out7[i] = (hi << 8) | lo;
    }
    return true;
}

// ---------------- Gyro offset calibration (keep still) ----------------
float gyroXoffset = 0, gyroYoffset = 0, gyroZoffset = 0;
int gyroCalOk = 0; // successful reads out of 500 during calibration (logged at boot)

void calibrateGyro()
{
    Serial.println("Calibrating gyro... keep still!");
    long sumX = 0, sumY = 0, sumZ = 0;
    const int samples = 500;
    int16_t raw[7];

    int ok = 0;
    for (int i = 0; i < samples; i++)
    {
        if (readIMUBurst(raw))
        {
            sumX += raw[4];
            sumY += raw[5];
            sumZ += raw[6];
            ok++;
        }
        delay(3);
    }
    gyroCalOk = ok;
    if (ok == 0)
        ok = 1;

    gyroXoffset = (sumX / (float)ok) / GYRO_SCALE;
    gyroYoffset = (sumY / (float)ok) / GYRO_SCALE;
    gyroZoffset = (sumZ / (float)ok) / GYRO_SCALE;
    Serial.println("Gyro calibration done.");
}

// ---------------- Pose (gravity direction) ----------------
// The pose is the direction gravity points in the board's own frame: the
// accel vector, normalised and EMA-smoothed as a vector, sent as
// elevation/azimuth (see WristPose). No boot-time zero, so it only depends
// on how the board is strapped on.
//
// Not pitch = atan2(ay, az) / roll = atan2(-ax, az): both divide by az, so
// with gravity near the board's x/y plane (the sword & shield stance: az
// ~ -0.09g) pitch swings -158..-172 for a ~1 degree real tilt. el/az are
// only singular with the board flat (el = +/-90), where az stops mattering.
float gravX = 0, gravY = 0, gravZ = 1; // EMA of the unit accel vector

float gravElDeg()
{
    float n = sqrt(gravX * gravX + gravY * gravY + gravZ * gravZ);
    return asin(constrain(gravZ / n, -1.0f, 1.0f)) * 180.0 / PI;
}

float gravAzDeg() { return atan2(gravY, gravX) * 180.0 / PI; }

// ---------------- Debug logging ----------------
void debugLog(String msg)
{
    if (!DEBUG_LOG)
        return;
    debugUdp.beginPacket(broadcastIP, DEBUG_PORT);
    debugUdp.print("[left_wrist] " + msg);
    debugUdp.endPacket();
}

// ---------------- Smoothing + stillness gate ----------------
const float EMA_ALPHA = 0.2; // smoothing for gravity vector + gyroMag
// Holding the stance reads 20-65 dps from hand tremor alone; moving between
// stances reads 150-2000. Same value as the right wrist.
const float STILLNESS_GYRO_THRESHOLD_DPS = 100.0;
const unsigned long STILLNESS_HOLD_MS = 700;
const unsigned long POSE_BROADCAST_INTERVAL_MS = 100;

// Live pose print while the weapon isn't locked yet (independent of the
// stable/unstable edge). Stops once locked.
#define DEBUG_POSE_STREAM true
const unsigned long POSE_LOG_INTERVAL_MS = 300;
unsigned long lastPoseLogMs = 0;

float gyroMagEMA = 0; // smoothed before the stillness check, so one noisy
                      // 100Hz sample (hand tremor) doesn't reset the hold timer
bool emaInitialized = false;
unsigned long stillSinceMs = 0;
bool wasStill = false;
unsigned long lastPoseBroadcastMs = 0;

// ---------------- HubStatus from right-wrist hub ----------------
bool hubSeen = false;
GamePhase hubPhase = PHASE_WAITING;
bool hubHolding = false;

void checkIncomingHubStatus()
{
    uint8_t buf[32];
    int packetSize = poseUdp.parsePacket();
    if (packetSize <= 0)
        return;

    poseUdp.read(buf, sizeof(buf));
    PacketHeader hdr;
    if (!acceptPacket(buf, packetSize, hdr))
        return; // bad length, another player, or our own WristPose looping back
    if (hdr.type != PKT_HUB_STATUS || hdr.deviceId != RIGHT_WRIST_ID)
        return; // e.g. the torso's TorsoPose
    noteRx(hdr);

    HubStatus status;
    memcpy(&status, buf, sizeof(status));

    GamePhase newPhase = (GamePhase)status.phase;
    if (newPhase == PHASE_LOCKED && hubPhase != PHASE_LOCKED)
    {
        debugLog("Weapon locked by hub: weapon=" + String(status.weapon) + " — going idle");
        ledWeaponColor((WeaponType)status.weapon);
    }
    hubSeen = true;
    hubPhase = newPhase;
    hubHolding = status.holding;
}

const unsigned long SAMPLE_PERIOD_US = 10000UL; // 100Hz
unsigned long lastSampleUs = 0;
uint32_t imuReadFails = 0; // failed readIMUBurst() calls since the last pose log line

void setup()
{
    Serial.begin(115200);
    Wire.begin();
    Wire.setClock(400000);

    pinMode(LED_R_PIN, OUTPUT);
    pinMode(LED_G_PIN, OUTPUT);
    pinMode(LED_B_PIN, OUTPUT);
    ledCalibrating();

#if defined(CG_HARDWARE_GATEWAY) && CG_GATEWAY_NODE_LINK
    gatewayNodeLinkStart(); // no Wi-Fi: the radio is BLE only
#else
    Serial.print("Connecting to WiFi");
    WiFi.begin(ssid, password);
    while (WiFi.status() != WL_CONNECTED)
    {
        delay(500);
        Serial.print(".");
    }
    Serial.println();
    Serial.print("Connected! IP: ");
    Serial.println(WiFi.localIP());
    broadcastIP = IPAddress((uint32_t)WiFi.localIP() | ~(uint32_t)WiFi.subnetMask());
    Serial.print("Broadcast: ");
    Serial.println(broadcastIP);
#if WIFI_SLEEP_DISABLED
    WiFi.setSleep(false);
#endif
#endif

    debugUdp.begin(DEBUG_PORT);
    poseUdp.begin(POSE_PORT);

    writeRegister(REG_PWR_MGMT_1, 0x00);
    delay(100);
    writeRegister(REG_GYRO_CONFIG, 0x18);  // +/-2000dps
    writeRegister(REG_ACCEL_CONFIG, 0x10); // +/-8g

    calibrateGyro();
    delay(100);

    setLedMode(LEDM_WAITING);
    debugLog("Left-wrist game node initialized at IP " + WiFi.localIP().toString());
    debugLog(String("[cfg] wifi_sleep=") + (WIFI_SLEEP_DISABLED ? "off" : "on") +
             " player=" + String(PLAYER_ID));
    debugLog("[cal] gyro offset x=" + String(gyroXoffset, 1) + " y=" + String(gyroYoffset, 1) +
             " z=" + String(gyroZoffset, 1) + " dps, good reads=" + String(gyroCalOk) + "/500");
#ifdef CG_HARDWARE_GATEWAY
    gatewayBegin(PLAYER_ID, 2);
#endif
}

void loop()
{
    checkIncomingHubStatus();
    printLinkStats(millis());

    // Locked until reboot: no IMU reads or pose broadcasts. The LED
    // already shows the weapon colour (set on the lock edge).
#ifndef CG_HARDWARE_GATEWAY
    if (hubPhase == PHASE_LOCKED)
        return;
#endif

    if (hubPhase != PHASE_LOCKED) {
    if (!hubSeen || hubPhase == PHASE_WAITING)
        setLedMode(LEDM_WAITING);
    else
        setLedMode(hubHolding ? LEDM_HOLDING : LEDM_SAMPLING);
    }

    unsigned long nowUs = micros();
    if (nowUs - lastSampleUs < SAMPLE_PERIOD_US)
        return;
    lastSampleUs = nowUs;

    int16_t raw[7];
    if (!readIMUBurst(raw))
    {
        imuReadFails++;
        // A failed read counts as "not still": stable then needs
        // STILLNESS_HOLD_MS of unbroken good reads, so a flaky IMU link can't
        // keep reporting a stale pose as stable.
        stillSinceMs = millis();
        wasStill = false;
        return;
    }

    float ax_g = raw[0] / ACCEL_SCALE;
    float ay_g = raw[1] / ACCEL_SCALE;
    float az_g = raw[2] / ACCEL_SCALE;
    float gx_dps = (raw[4] / GYRO_SCALE) - gyroXoffset;
    float gy_dps = (raw[5] / GYRO_SCALE) - gyroYoffset;
    float gz_dps = (raw[6] / GYRO_SCALE) - gyroZoffset;
#ifdef CG_HARDWARE_GATEWAY
    gatewaySample(ax_g, ay_g, az_g, gx_dps, gy_dps, gz_dps);
    // Keep raw export alive after lock, without restarting pose detection.
    if (hubPhase == PHASE_LOCKED) return;
#endif

    float gyroMag = sqrt(gx_dps * gx_dps + gy_dps * gy_dps + gz_dps * gz_dps);

    float accelMag = sqrt(ax_g * ax_g + ay_g * ay_g + az_g * az_g);
    if (accelMag < 0.1f)
        accelMag = 0.1f; // free-fall / bad read: keep the division sane
    float ux = ax_g / accelMag, uy = ay_g / accelMag, uz = az_g / accelMag;

    if (!emaInitialized)
    {
        gravX = ux;
        gravY = uy;
        gravZ = uz;
        gyroMagEMA = gyroMag;
        emaInitialized = true;
    }
    else
    {
        gravX += EMA_ALPHA * (ux - gravX);
        gravY += EMA_ALPHA * (uy - gravY);
        gravZ += EMA_ALPHA * (uz - gravZ);
        gyroMagEMA = EMA_ALPHA * gyroMag + (1 - EMA_ALPHA) * gyroMagEMA;
    }
    float el = gravElDeg(), az = gravAzDeg();

    unsigned long now = millis();
    if (gyroMagEMA > STILLNESS_GYRO_THRESHOLD_DPS)
    {
        stillSinceMs = now; // reset the hold timer, still moving
        wasStill = false;
    }
    bool stable = (now - stillSinceMs) >= STILLNESS_HOLD_MS;

    if (now - lastPoseBroadcastMs >= POSE_BROADCAST_INTERVAL_MS)
    {
        lastPoseBroadcastMs = now;

        WristPose pose;
        fillHeader(pose.hdr, PKT_WRIST_POSE);
        pose.ms = now;
        pose.gravEl = el;
        pose.gravAz = az;
        pose.stable = stable;

        poseUdp.beginPacket(broadcastIP, POSE_PORT);
        poseUdp.write((uint8_t *)&pose, sizeof(pose));
        poseUdp.endPacket();

        if (stable && !wasStill)
            debugLog("stable el=" + String(el, 1) + " az=" + String(az, 1));
        wasStill = stable;
    }

#if DEBUG_POSE_STREAM
    if (now - lastPoseLogMs >= POSE_LOG_INTERVAL_MS)
    {
        lastPoseLogMs = now;
        // gx/gy/gz (offset-corrected) and imuFails (failed I2C reads since the
        // last line) are diagnostics for a stuck/biased gyro or loose wiring.
        debugLog("raw el=" + String(el, 1) + " az=" + String(az, 1) +
                 " gyroMag=" + String(gyroMagEMA, 1) + " stable=" + String(stable ? 1 : 0) +
                 " gx=" + String(gx_dps, 1) + " gy=" + String(gy_dps, 1) + " gz=" + String(gz_dps, 1) +
                 " imuFails=" + String(imuReadFails));
        imuReadFails = 0;
    }
#endif
}
