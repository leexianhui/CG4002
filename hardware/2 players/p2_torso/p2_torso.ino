#ifdef CG_HARDWARE_GATEWAY
#include "gateway_boundary.h"
#include "gateway_link.h"
#endif
// ============================================================================
// TORSO NODE. Follows the phase the right-wrist hub broadcasts in HubStatus:
//   before PHASE_LOCKED — broadcasts TorsoPose on POSE_PORT for weapon
//                         detection.
//   PHASE_LOCKED        — stops pose broadcasts; sends TorsoBeacon on
//                         LINK_PORT and answers SegmentRequests.
// The ring buffer is filled in every phase so it's already full at lock.
//
// Valid-action phase:
//   - Does NOT run its own attack state machine. The hub decides what counts
//     as an action; the torso just supplies the data it needs to decide.
//   - Broadcasts a small TorsoBeacon (~50Hz) with the peak rotation magnitude
//     since the previous beacon. The hub uses it to check the torso's
//     engagement during a candidate swing (not too little, not too much
//     unless it's a likely Spin Attack) and to keep its clock in sync with
//     this board.
//   - Continuously ring-buffers its own raw samples. When the hub accepts a
//     segment, it sends a SegmentRequest with a time window; the torso
//     slices that window out of its ring buffer for the Ultra96, tagged with
//     the same segId so the classifier can join both streams.
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
#define PLAYER_ID 2
#endif

// Each player's boards use their own block of UDP ports (player 1:
// 4210-4214, player 2: 4220-4224), so the two sets of boards never see
// each other's broadcasts and the laptop can listen to each separately.
#define PLAYER_PORT_OFFSET ((PLAYER_ID - 1) * 10)

// 1 = answer the hub's latency-test PINGs with PONGs. Must match the right
// wrist sketch. 0 for normal play.
#ifndef LATENCY_TEST
#define LATENCY_TEST 0
#endif

// 1 = stream every IMU sample to the laptop on RAW_PORT, in every phase, for
// offline waveform analysis (listener writes raw_<time>_<label>.csv). Must
// match the right wrist sketch to get both streams. 0 for normal play.
#ifndef RAW_STREAM
#define RAW_STREAM 0
#endif

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

// ================= CONFIG =================
#define THIS_DEVICE_ID TORSO_ID

IPAddress broadcastIP;                            // subnet broadcast, computed in setup() once connected
const int LINK_PORT = 4210 + PLAYER_PORT_OFFSET;  // <-> hub (beacons + acks out, SegmentRequest in), used once LOCKED
const int DEBUG_PORT = 4211 + PLAYER_PORT_OFFSET; // -> laptop listener (debug lines)
const int POSE_PORT = 4212 + PLAYER_PORT_OFFSET;  // <-> hub (TorsoPose out, HubStatus in)
const int RAW_PORT = 4214 + PLAYER_PORT_OFFSET;   // -> laptop listener (RawBatch, RAW_STREAM builds only)

#define DEBUG_LOG true

// Live pose print while the weapon isn't locked yet. Stops once locked.
#define DEBUG_POSE_STREAM true
const unsigned long POSE_LOG_INTERVAL_MS = 300;

#if defined(CG_HARDWARE_GATEWAY) && CG_GATEWAY_NODE_LINK
// Board-to-board packets go over BLE (hardware/gateway/include/gateway_link.h).
GatewayLinkUdp debugUdp;
GatewayLinkUdp linkUdp;
GatewayLinkUdp poseUdp;
#else
WiFiUDP debugUdp;
WiFiUDP linkUdp;
WiFiUDP poseUdp;
#endif

// ---------------- Local types ----------------
struct Sample
{
    uint32_t t; // this device's local millis()
    float ax, ay, az, gx, gy, gz;
};

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

// ---------------- RGB LED ----------------
#define LED_R_PIN 25
#define LED_G_PIN 26
#define LED_B_PIN 27

enum LedState
{
    LED_BOOTING,
    LED_WAITING,  // weapon phase: no hub yet / hub waiting for nodes
    LED_SAMPLING, // weapon phase: detecting, nothing held
    LED_HOLDING,  // weapon phase: known weapon held, counting toward lock
    LED_IDLE,        // locked, torso still: weapon colour
    LED_ACTIVE,      // locked, torso rotating
    LED_FLASH_ACCEPT // locked, SegmentRequest just served
};
LedState ledState = LED_BOOTING;
unsigned long ledFlashStart = 0;
const unsigned long LED_FLASH_DURATION_MS = 200;

void ledSetColor(int r, int g, int b)
{
    analogWrite(LED_R_PIN, r);
    analogWrite(LED_G_PIN, g);
    analogWrite(LED_B_PIN, b);
}
void ledBooting()
{
    ledState = LED_BOOTING;
    ledSetColor(255, 255, 255);
}
// Waiting blinks amber 500ms on / 500ms off, same as the hub's PAT_BLINK.
// Call it repeatedly while waiting; it only rewrites the PWM on a toggle.
const unsigned long WAITING_BLINK_MS = 500;
bool waitingBlinkOn = false;

void ledWaiting()
{
    bool on = (millis() / WAITING_BLINK_MS) % 2 == 0;
    if (ledState == LED_WAITING && on == waitingBlinkOn)
        return;
    ledState = LED_WAITING;
    waitingBlinkOn = on;
    if (on)
        ledSetColor(255, 80, 0); // amber
    else
        ledSetColor(0, 0, 0);
}
void ledSampling()
{
    ledState = LED_SAMPLING;
    ledSetColor(0, 255, 0); // green
}
void ledHolding()
{
    ledState = LED_HOLDING;
    ledSetColor(0, 0, 255); // blue
}
// Colour shown while still after the lock — the locked weapon's colour,
// same as both wrists. Set by setIdleWeaponColor() on the lock edge.
uint8_t idleR = 0, idleG = 40, idleB = 0;

void setIdleWeaponColor(WeaponType w)
{
    switch (w)
    {
    case WEAPON_SWORD_SHIELD:
        idleR = 0, idleG = 255, idleB = 255; // cyan
        break;
    case WEAPON_SPEAR:
        idleR = 255, idleG = 0, idleB = 255; // magenta
        break;
    case WEAPON_MACE:
        idleR = 255, idleG = 40, idleB = 0; // orange
        break;
    default:
        idleR = 0, idleG = 40, idleB = 0; // dim green
        break;
    }
}

void ledIdle()
{
    ledState = LED_IDLE;
    ledSetColor(idleR, idleG, idleB);
}
// Torso doesn't decide "active" the way wrist does; this is just a soft
// visual hint that rotation is currently elevated (e.g. during Spin Attack).
void ledLocalActive()
{
    ledState = LED_ACTIVE;
    ledSetColor(0, 0, 120);
}
void ledFlashAccept()
{
    ledState = LED_FLASH_ACCEPT;
    ledFlashStart = millis();
    ledSetColor(0, 255, 0);
}
void updateLed()
{
    if (ledState == LED_FLASH_ACCEPT && millis() - ledFlashStart > LED_FLASH_DURATION_MS)
        ledIdle();
}

// ---------------- Haptic motor ----------------
// Fitted but unused on the torso; held off so the switch doesn't float.
#define MOTOR_PIN 4

void motorSetup()
{
    pinMode(MOTOR_PIN, OUTPUT);
    analogWrite(MOTOR_PIN, 0);
}

// ---------------- MPU6500 ----------------
#define MPU6500_ADDR 0x68
#define REG_PWR_MGMT_1 0x6B
#define REG_GYRO_CONFIG 0x1B
#define REG_ACCEL_CONFIG 0x1C
#define REG_ACCEL_XOUT_H 0x3B // accel(6) + temp(2) + gyro(6) = 14 contiguous bytes

// +/-8g, +/-2000dps. Must match the right wrist: the beacon's gyroMag is
// compared directly against the hub's torso-gate thresholds.
const float ACCEL_SCALE = 4096.0;
const float GYRO_SCALE = 16.4;

void writeRegister(uint8_t reg, uint8_t value)
{
    Wire.beginTransmission(MPU6500_ADDR);
    Wire.write(reg);
    Wire.write(value);
    Wire.endTransmission();
}

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

// ---------------- Gyro calibration ----------------
float gyroXoffset = 0, gyroYoffset = 0, gyroZoffset = 0;

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
    if (ok == 0)
        ok = 1;
    gyroXoffset = (sumX / (float)ok) / GYRO_SCALE;
    gyroYoffset = (sumY / (float)ok) / GYRO_SCALE;
    gyroZoffset = (sumZ / (float)ok) / GYRO_SCALE;
    Serial.println("Calibration done.");
}

// ---------------- Debug logging ----------------
void debugLog(String msg)
{
    if (!DEBUG_LOG)
        return;
    debugUdp.beginPacket(broadcastIP, DEBUG_PORT);
    debugUdp.print("[torso] " + msg);
    debugUdp.endPacket();
    Serial.println(msg);
}

// ---------------- Raw sample stream (RAW_STREAM builds only) ----------------
// Same block in the right wrist and torso sketches. Every sample goes out,
// RAW_BATCH at a time (10 packets/s), on RAW_PORT to the laptop only — no
// board listens on it, so it's not part of the shared wire protocol.
// Fixed-point so a batch stays small: accel in mg, gyro (offset-corrected)
// in 0.1 dps; +/-8g and +/-2000dps both fit in int16. seq counts batches, so
// the listener can report loss.
//   RawBatch 166 = magic(1) playerId(1) deviceId(1) seq(2) n(1) 10 x RawSample(16)
//   RawSample 16 = t(4) ax ay az gx gy gz (6 x int16)
#if RAW_STREAM
#define RAW_BATCH 10
const uint8_t RAW_MAGIC = 'R';

struct __attribute__((packed)) RawSample
{
    uint32_t t; // this device's local millis()
    int16_t ax, ay, az, gx, gy, gz;
};

struct __attribute__((packed)) RawBatch
{
    uint8_t magic; // RAW_MAGIC
    uint8_t playerId;
    uint8_t deviceId;
    uint16_t seq;
    uint8_t n; // samples used, always RAW_BATCH
    RawSample s[RAW_BATCH];
};
static_assert(sizeof(RawBatch) == 166, "RawBatch size changed");

RawBatch rawBatch;
uint16_t rawSeq = 0;

int16_t rawFixed(float v, float scale) { return (int16_t)constrain(lroundf(v * scale), -32767L, 32767L); }

void rawStreamPush(uint32_t t, float ax_g, float ay_g, float az_g, float gx_dps, float gy_dps, float gz_dps)
{
    RawSample &r = rawBatch.s[rawBatch.n++];
    r = {t, rawFixed(ax_g, 1000), rawFixed(ay_g, 1000), rawFixed(az_g, 1000),
         rawFixed(gx_dps, 10), rawFixed(gy_dps, 10), rawFixed(gz_dps, 10)};
    if (rawBatch.n < RAW_BATCH)
        return;
    rawBatch.magic = RAW_MAGIC;
    rawBatch.playerId = PLAYER_ID;
    rawBatch.deviceId = THIS_DEVICE_ID;
    rawBatch.seq = rawSeq++;
    debugUdp.beginPacket(broadcastIP, RAW_PORT);
    debugUdp.write((uint8_t *)&rawBatch, sizeof(rawBatch));
    debugUdp.endPacket();
    rawBatch.n = 0;
}
#endif

// ================= WEAPON-DETECT POSE =================

// ---------------- Pose-zero calibration ----------------
// Mounting orientation on the body isn't guaranteed to match the wrist
// mounts' axis convention, so raw pitch/roll from atan2() isn't directly
// comparable across devices. Capture a zero-reference here (player stands
// in a neutral reference stance) and subtract it from every later reading,
// the same way calibrateGyro() zeroes gyro drift.
float pitchZero = 0, rollZero = 0;

float computePitchDeg(float ay_g, float az_g)
{
    return atan2(ay_g, az_g) * 180.0 / PI;
}

float computeRollDeg(float ax_g, float az_g)
{
    return atan2(-ax_g, az_g) * 180.0 / PI;
}

// atan2() only returns [-180,180]; subtracting a zero-offset (or blending
// two readings in the EMA below) can land outside that range right at the
// wrap boundary, producing bogus ~360-degree jumps. Fold back into [-180,180].
float wrapAngle180(float deg)
{
    while (deg > 180.0)
        deg -= 360.0;
    while (deg < -180.0)
        deg += 360.0;
    return deg;
}

void calibratePoseZero()
{
    Serial.println("Calibrating pose zero... hold neutral reference stance!");
    double sumPitch = 0, sumRoll = 0;
    const int samples = 200;
    int16_t raw[7];
    int ok = 0;

    for (int i = 0; i < samples; i++)
    {
        if (readIMUBurst(raw))
        {
            float ax_g = raw[0] / ACCEL_SCALE;
            float ay_g = raw[1] / ACCEL_SCALE;
            float az_g = raw[2] / ACCEL_SCALE;
            sumPitch += computePitchDeg(ay_g, az_g);
            sumRoll += computeRollDeg(ax_g, az_g);
            ok++;
        }
        delay(3);
    }
    if (ok == 0)
        ok = 1;

    pitchZero = sumPitch / ok;
    rollZero = sumRoll / ok;
    Serial.println("Pose-zero calibration done.");
}

// ---------------- Smoothing + stillness gate ----------------
const float EMA_ALPHA = 0.2; // smoothing for pitch/roll display
// Only feeds TorsoPose.stable and the logs — the hub doesn't require torso
// stillness for the weapon lock (see runDetectionTick() there).
const float STILLNESS_GYRO_THRESHOLD_DPS = 15.0;
const unsigned long STILLNESS_HOLD_MS = 700;
const unsigned long POSE_BROADCAST_INTERVAL_MS = 100;

float pitchEMA = 0, rollEMA = 0;
float gyroMagEMA = 0; // smoothed before the stillness check, so one noisy
                      // 100Hz sample (hand tremor) doesn't reset the hold timer
bool emaInitialized = false;
unsigned long poseStillSinceMs = 0;
bool wasStill = false;
unsigned long lastPoseBroadcastMs = 0;
unsigned long lastPoseLogMs = 0;

// Pre-lock work for one IMU sample: update pose EMA + stillness, broadcast
// TorsoPose to the hub every POSE_BROADCAST_INTERVAL_MS.
void runPoseTick(float ax_g, float ay_g, float az_g, float gyroMag, unsigned long now)
{
    float pitch = wrapAngle180(computePitchDeg(ay_g, az_g) - pitchZero);
    float roll = wrapAngle180(computeRollDeg(ax_g, az_g) - rollZero);

    if (!emaInitialized)
    {
        pitchEMA = pitch;
        rollEMA = roll;
        gyroMagEMA = gyroMag;
        emaInitialized = true;
    }
    else
    {
        // Blend via the shortest angular distance, not raw subtraction, so
        // the EMA itself doesn't jump when pitch/roll cross the wrap point.
        pitchEMA = wrapAngle180(pitchEMA + EMA_ALPHA * wrapAngle180(pitch - pitchEMA));
        rollEMA = wrapAngle180(rollEMA + EMA_ALPHA * wrapAngle180(roll - rollEMA));
        gyroMagEMA = EMA_ALPHA * gyroMag + (1 - EMA_ALPHA) * gyroMagEMA;
    }

    if (gyroMagEMA > STILLNESS_GYRO_THRESHOLD_DPS)
    {
        poseStillSinceMs = now; // reset the hold timer, still moving
        wasStill = false;
    }
    bool stable = (now - poseStillSinceMs) >= STILLNESS_HOLD_MS;

    if (now - lastPoseBroadcastMs >= POSE_BROADCAST_INTERVAL_MS)
    {
        lastPoseBroadcastMs = now;

        TorsoPose pose;
        fillHeader(pose.hdr, PKT_TORSO_POSE);
        pose.ms = now;
        pose.pitch = pitchEMA;
        pose.roll = rollEMA;
        pose.stable = stable;

        poseUdp.beginPacket(broadcastIP, POSE_PORT);
        poseUdp.write((uint8_t *)&pose, sizeof(pose));
        poseUdp.endPacket();

        if (stable && !wasStill)
            debugLog("stable pitch=" + String(pitchEMA, 1) + " roll=" + String(rollEMA, 1));
        wasStill = stable;
    }

#if DEBUG_POSE_STREAM
    if (now - lastPoseLogMs >= POSE_LOG_INTERVAL_MS)
    {
        lastPoseLogMs = now;
        debugLog("raw pitch=" + String(pitchEMA, 1) + " roll=" + String(rollEMA, 1) +
                 " gyroMag=" + String(gyroMagEMA, 1) + " stable=" + String(stable ? 1 : 0));
    }
#endif
}

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
        return; // bad length, another player, or our own TorsoPose looping back
    if (hdr.type != PKT_HUB_STATUS || hdr.deviceId != RIGHT_WRIST_ID)
        return; // e.g. the left wrist's WristPose
    noteRx(hdr);

    HubStatus status;
    memcpy(&status, buf, sizeof(status));

    GamePhase newPhase = (GamePhase)status.phase;
    if (newPhase == PHASE_LOCKED && hubPhase != PHASE_LOCKED)
    {
        debugLog("Weapon locked by hub: weapon=" + String(status.weapon) + " — switching to beacon mode");
        setIdleWeaponColor((WeaponType)status.weapon);
        ledIdle();
    }
    hubSeen = true;
    hubPhase = newPhase;
    hubHolding = status.holding;
}

// Pre-lock LED mirrors the hub: blinking amber until the hub is heard from
// (or while it's still waiting for nodes), then green, blue while holding.
void updatePreLockLed()
{
    if (!hubSeen || hubPhase == PHASE_WAITING)
    {
        ledWaiting(); // called every pass so it can blink
    }
    else if (hubHolding)
    {
        if (ledState != LED_HOLDING)
            ledHolding();
    }
    else if (ledState != LED_SAMPLING)
        ledSampling();
}

// ---------------- Raw sample ring buffer ----------------
// Covers a few seconds so it comfortably contains any window the hub asks
// for. The longest is a whole spin: up to RAW_MAX_DURATION_MS (3.5s) on the
// hub, and the last SegmentRequest retry lands ~0.45s after the first.
#define RING_SIZE 450 // ~4.5s at 100Hz
Sample ring[RING_SIZE];
int ringHead = 0;
bool ringFilled = false;

void ringPush(const Sample &s)
{
    ring[ringHead] = s;
    ringHead = (ringHead + 1) % RING_SIZE;
    if (ringHead == 0)
        ringFilled = true;
}

// ---------------- Beacon broadcast ----------------
const unsigned long BEACON_INTERVAL_MS = 20; // ~50Hz
unsigned long lastBeaconMs = 0;
// Largest gyroMag since the last beacon. Sampling is 100Hz and beacons 50Hz,
// so sending only the current sample would drop every other one — and with
// it half the torso peaks the hub's torso gate looks for.
float beaconMaxGyroMag = 0;

void broadcastBeacon(float gyroMag)
{
    TorsoBeacon b;
    fillHeader(b.hdr, PKT_TORSO_BEACON);
    b.torsoMs = (uint32_t)millis();
    b.gyroMag = gyroMag;
    linkUdp.beginPacket(broadcastIP, LINK_PORT);
    linkUdp.write((uint8_t *)&b, sizeof(b));
    linkUdp.endPacket();
}

// ---------------- Segment request handling ----------------
// The hub retries a SegmentRequest until it sees our SegmentAck, so the same
// segId can arrive several times. Recent segIds are remembered so a retry is
// acked again but not processed twice.
#define SEEN_SEG_IDS 8
uint32_t seenSegIds[SEEN_SEG_IDS];
int seenSegCount = 0;
int seenSegHead = 0;

bool segIdSeen(uint32_t segId)
{
    for (int i = 0; i < seenSegCount; i++)
        if (seenSegIds[i] == segId)
            return true;
    return false;
}

void rememberSegId(uint32_t segId)
{
    seenSegIds[seenSegHead] = segId;
    seenSegHead = (seenSegHead + 1) % SEEN_SEG_IDS;
    if (seenSegCount < SEEN_SEG_IDS)
        seenSegCount++;
}

void sendSegmentAck(uint32_t segId)
{
    SegmentAck a;
    fillHeader(a.hdr, PKT_SEGMENT_ACK);
    a.segId = segId;
    linkUdp.beginPacket(broadcastIP, LINK_PORT);
    linkUdp.write((uint8_t *)&a, sizeof(a));
    linkUdp.endPacket();
}

// Finds the ring-buffer samples inside the requested window. The transmit to
// the Ultra96 isn't wired up yet, so this only logs how many would be sent —
// which still confirms the window lands on real samples (ring buffer deep
// enough, clock sync sane).
void sendMatchingSamplesToUltra96(uint32_t segId, uint32_t startMs, uint32_t endMs)
{
    int count = ringFilled ? RING_SIZE : ringHead;
    int oldestIdx = ringFilled ? ringHead : 0;

    int matchCount = 0;
    for (int i = 0; i < count; i++)
    {
        Sample &s = ring[(oldestIdx + i) % RING_SIZE];
        if (s.t >= startMs && s.t <= endMs)
            matchCount++;
    }

    if (matchCount == 0)
    {
        debugLog("SegmentRequest seg=" + String(segId) + " — no matching samples in ring buffer");
        return;
    }

    debugLog("[Ultra96 send skipped] would send " + String(matchCount) +
             " torso samples for seg=" + String(segId));
    ledFlashAccept();
}

void checkIncomingLink()
{
    uint8_t buf[32];
    int packetSize = linkUdp.parsePacket();
    if (packetSize <= 0)
        return;

    linkUdp.read(buf, sizeof(buf));
    // Our own TorsoBeacon looping back is dropped by acceptPacket().
    PacketHeader hdr;
    if (!acceptPacket(buf, packetSize, hdr))
        return;

    if (hdr.type == PKT_SEGMENT_REQUEST && hdr.deviceId == RIGHT_WRIST_ID)
    {
        noteRx(hdr);
        SegmentRequest req;
        memcpy(&req, buf, sizeof(req));
        // Ack every copy (our ack may have been the packet that got lost),
        // but only act on a segment once.
        sendSegmentAck(req.segId);
        if (segIdSeen(req.segId))
            return;
        rememberSegId(req.segId);
        debugLog("Got SegmentRequest seg=" + String(req.segId) +
                 " [" + String(req.startTorsoMs) + ", " + String(req.endTorsoMs) + "]");
        sendMatchingSamplesToUltra96(req.segId, req.startTorsoMs, req.endTorsoMs);
    }
#if LATENCY_TEST
    else if (hdr.type == PKT_PING && hdr.deviceId == RIGHT_WRIST_ID)
    {
        noteRx(hdr);
        // Echo t0Us back untouched; only the header changes.
        LatencyProbe p;
        memcpy(&p, buf, sizeof(p));
        fillHeader(p.hdr, PKT_PONG);
        linkUdp.beginPacket(broadcastIP, LINK_PORT);
        linkUdp.write((uint8_t *)&p, sizeof(p));
        linkUdp.endPacket();
    }
#endif
}

// ================= SETUP / LOOP =================
const unsigned long SAMPLE_PERIOD_US = 10000UL; // 100Hz
unsigned long lastSampleUs = 0;

// Soft local threshold just for the LED hint — NOT used for any gating logic.
const float LOCAL_ACTIVE_HINT_DPS = 120.0;

void setup()
{
    Serial.begin(115200);
    Wire.begin();
    Wire.setClock(400000);

    pinMode(LED_R_PIN, OUTPUT);
    pinMode(LED_G_PIN, OUTPUT);
    pinMode(LED_B_PIN, OUTPUT);
    ledBooting();

    motorSetup();

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
    linkUdp.begin(LINK_PORT);
    poseUdp.begin(POSE_PORT);

    writeRegister(REG_PWR_MGMT_1, 0x00);
    delay(100);
    writeRegister(REG_GYRO_CONFIG, 0x18);  // FS_SEL=11 -> +/-2000dps
    writeRegister(REG_ACCEL_CONFIG, 0x10); // AFS_SEL=10 -> +/-8g

    calibrateGyro();
    delay(100);
    calibratePoseZero();
    delay(100);

    ledWaiting();
    debugLog("Torso game node initialized at IP " + WiFi.localIP().toString());
    debugLog(String("[cfg] wifi_sleep=") + (WIFI_SLEEP_DISABLED ? "off" : "on") +
             " latency_test=" + String(LATENCY_TEST) + " raw_stream=" + String(RAW_STREAM) +
             " player=" + String(PLAYER_ID));
#ifdef CG_HARDWARE_GATEWAY
    gatewayBegin(PLAYER_ID, 1);
#endif
}

void loop()
{
    checkIncomingHubStatus();
    printLinkStats(millis());
    bool locked = (hubPhase == PHASE_LOCKED);

    // Polled in every phase so latency-test PINGs are answered before the
    // lock too (and don't queue up stale). SegmentRequests only arrive once
    // the hub is locked, so nothing else changes.
    checkIncomingLink();
    if (locked)
        updateLed();
    else
        updatePreLockLed();

    unsigned long nowUs = micros();
    if (nowUs - lastSampleUs < SAMPLE_PERIOD_US)
        return;
    lastSampleUs = nowUs;

    unsigned long now = millis();
    int16_t raw[7];
    if (!readIMUBurst(raw))
        return;

    float ax_g = raw[0] / ACCEL_SCALE;
    float ay_g = raw[1] / ACCEL_SCALE;
    float az_g = raw[2] / ACCEL_SCALE;
    float gx_dps = (raw[4] / GYRO_SCALE) - gyroXoffset;
    float gy_dps = (raw[5] / GYRO_SCALE) - gyroYoffset;
    float gz_dps = (raw[6] / GYRO_SCALE) - gyroZoffset;
#ifdef CG_HARDWARE_GATEWAY
    gatewaySample(ax_g, ay_g, az_g, gx_dps, gy_dps, gz_dps);
#endif

    Sample s = {(uint32_t)now, ax_g, ay_g, az_g, gx_dps, gy_dps, gz_dps};
    ringPush(s);
#if RAW_STREAM
    rawStreamPush(s.t, ax_g, ay_g, az_g, gx_dps, gy_dps, gz_dps);
#endif

    float gyroMag = sqrt(gx_dps * gx_dps + gy_dps * gy_dps + gz_dps * gz_dps);

    if (!locked)
    {
        runPoseTick(ax_g, ay_g, az_g, gyroMag, now);
        return;
    }

    // ---- PHASE_LOCKED: LED hint + beacons ----
    if (ledState != LED_FLASH_ACCEPT)
    {
        if (gyroMag > LOCAL_ACTIVE_HINT_DPS)
            ledLocalActive();
        else if (ledState == LED_ACTIVE)
            ledIdle();
    }

    if (gyroMag > beaconMaxGyroMag)
        beaconMaxGyroMag = gyroMag;
    if (now - lastBeaconMs >= BEACON_INTERVAL_MS)
    {
        lastBeaconMs = now;
        broadcastBeacon(beaconMaxGyroMag);
        beaconMaxGyroMag = 0;
    }
}
