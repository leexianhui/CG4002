// ============================================================================
// RIGHT WRIST NODE — combined game workflow (weapon detection -> lock ->
// valid-action algorithm). Merges right_wrist_weapon.ino (weapon-detect hub)
// and right_wrist.ino (swing segmentation + validity gates) into one sketch.
//
// Phases (see GamePhase below):
//   PHASE_WAITING   — calibrated, waiting until fresh pose packets arrive from
//                     both left wrist and torso (they only broadcast after
//                     their own calibration, so "fresh" == "ready").
//   PHASE_DETECTING — classifies the held ready-stance weapon every tick. The
//                     same known weapon must be classified continuously for
//                     WEAPON_LOCK_HOLD_MS before it's locked; unknown/
//                     stale/unstable/different readings lasting longer
//                     than WEAPON_DROPOUT_GRACE_MS restart the timer.
//   PHASE_LOCKED    — weapon fixed until reboot. Runs the valid-action
//                     algorithm from right_wrist.ino unchanged, using torso
//                     beacons on LINK_PORT.
//
// The hub broadcasts HubStatus on POSE_PORT every 100ms in every phase, so
// left wrist + torso follow the phase (and late joiners resync on their own).
//
// Keep the shared struct definitions identical to torso_game.ino /
// left_wrist_game.ino — they're the wire protocol between the boards.
// ============================================================================

// ============================================================================
// BUILD CONFIG — edit here
// ============================================================================
// ---- WiFi credentials (laptop hotspot) — same on all three boards ----
const char *ssid = "xin hui's laptop";
const char *password = "12345678";

// 1 = WiFi.setSleep(false) once connected: modem sleep off, so broadcasts
//     aren't held until the next DTIM wake-up (lower, steadier latency, more
//     current). 0 = keep the ESP32 default (modem sleep on) — build this for
//     the "sleep on" half of the before/after comparison.
#ifndef WIFI_SLEEP_DISABLED
#define WIFI_SLEEP_DISABLED 1
#endif

// Which player this set of boards belongs to. Must be the same on all three
// boards of one player; packets carrying another PLAYER_ID are dropped.
#ifndef PLAYER_ID
#define PLAYER_ID 1
#endif

// 1 = latency instrumentation: PING the torso every 100ms and log
// "[rtt] us=<n>", and log TorsoBeacon inter-arrival times as
// "[beacon_iat] us=<n>,<n>,...". Must match torso_game.ino, which answers
// the PINGs. 0 for normal play.
#ifndef LATENCY_TEST
#define LATENCY_TEST 0
#endif

// 1 = the hub fires its own LED/haptic feedback (nodes ready, weapon locked,
// swing accepted). Set 0 once the laptop sends COMBAT_FEEDBACK and calls
// onFeedbackEvent() instead.
#ifndef LOCAL_FEEDBACK_TRIGGERS
#define LOCAL_FEEDBACK_TRIGGERS 1
#endif

#include <Wire.h>
#include <WiFi.h>
#include <WiFiUdp.h>

// ======================= SHARED WIRE PROTOCOL — BEGIN =======================
// MUST STAY IN SYNC: this whole block is copied verbatim into
// right_wrist_game.ino, torso_game.ino and left_wrist_game.ino. Change all
// three together. Declared before anything else so Arduino's auto-generated
// function prototypes can see these types.
//
// Every binary packet starts with a PacketHeader. Structs are packed and go
// on the wire as-is (little-endian). Receivers dispatch on hdr.type, reject
// a packet whose length isn't PACKET_SIZE[hdr.type], drop other players'
// packets (hdr.playerId) and drop their own broadcast echoes (hdr.deviceId).
//
// On-wire sizes (bytes):
//   PacketHeader     5 = type(1) playerId(1) deviceId(1) seq(2)
//   HubStatus        8 = hdr(5) phase(1) weapon(1) holding(1)          (was 4)
//   WristPose       18 = hdr(5) ms(4) gravEl(4) gravAz(4) stable(1)    (was 14)
//   TorsoPose       18 = hdr(5) ms(4) pitch(4) roll(4) stable(1)       (was 14)
//   TorsoBeacon     13 = hdr(5) torsoMs(4) gyroMag(4)                  (was 9)
//   SegmentRequest  17 = hdr(5) segId(4) startTorsoMs(4) endTorsoMs(4) (was 13)
//   LatencyProbe     9 = hdr(5) t0Us(4)    PING and PONG               (new)
//   SegmentAck       9 = hdr(5) segId(4)                               (new)

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
#define THIS_DEVICE_ID RIGHT_WRIST_ID
#define WRIST_ID RIGHT_WRIST_ID // also seeds segId

#define MOTOR_PIN 4 // haptic ERM via 2N7000 low-side switch (PWM)

IPAddress broadcastIP;        // subnet broadcast, computed in setup() once connected
const int DEBUG_PORT = 4211;  // -> laptop listener.py
const int LINK_PORT = 4210;   // <-> torso node (beacons + segment request), used once LOCKED
const int POSE_PORT = 4212;   // <- left wrist + torso poses, -> HubStatus broadcast
const int RESULT_PORT = 4213; // -> laptop result_listener.py (milestone lines only)

// Same weapon must be classified continuously for this long before it locks.
const unsigned long WEAPON_LOCK_HOLD_MS = 3000;

// Dropout tolerance while holding: the candidate survives up to this long
// without being re-classified (a tremor spike clearing a stable flag, one
// late pose packet, a noisy tick across a threshold) before it's dropped and
// the lock timer restarts. Without it, a single bad 10ms tick anywhere in
// the 5s hold threw the whole hold away.
const unsigned long WEAPON_DROPOUT_GRACE_MS = 500;

#define DEBUG_LOG true

// Live pose print during WAITING/DETECTING (the weapon-detect equivalent of
// DEBUG_RAW_STREAM below). Stops automatically once the weapon is locked.
#define DEBUG_POSE_STREAM true
const unsigned long POSE_LOG_INTERVAL_MS = 300;

// Raw sensor debug output — OFF by default, too noisy for normal testing.
// Flip to true only when you specifically need to inspect raw sample data
// (e.g. capturing a segment to send for threshold debugging).
#define DEBUG_RAW_STREAM false   // periodic live sample print, rate-limited
#define RAW_LOG_INTERVAL_MS 100  // ~10Hz when enabled
#define DEBUG_SEGMENT_DUMP false // full sample dump on every motion end

WiFiUDP debugUdp;
WiFiUDP linkUdp;
WiFiUDP poseUdp;
// Ultra96 send is deliberately left out for now — segments are validated
// and logged locally. Add a WiFiUDP/TCP client + the actual send call here
// once the Ultra96-side protocol is confirmed.

// ---------------- Wire structs (must be defined before Arduino's
// auto-generated prototypes) ----------------
struct Sample
{
    uint32_t t; // this device's local millis()
    float ax, ay, az, gx, gy, gz;
};

// Not sent yet (Ultra96 placeholder), so not part of the shared protocol.
struct __attribute__((packed)) SegmentSampleMsg
{
    uint32_t segId;
    uint8_t deviceId;
    uint16_t index; // sample index within this segment
    uint16_t total; // total samples in this segment
    uint32_t t;     // originating device's local ms
    float ax, ay, az, gx, gy, gz;
};

struct MotionStats
{
    float peakGyroMag;
    unsigned long duration;
    int sampleCount;
};

struct MotionShape
{
    float totalRotX, totalRotY, totalRotZ;
    float totalRotation;
    float peakLinearAccel;
    int dominantAxisCount;
};

// Diagnostic-only shape features (see computeShapeExtras() below for what
// these mean and why). Declared here alongside MotionStats/MotionShape —
// not next to computeShapeExtras() itself — because the Arduino IDE inserts
// auto-generated function prototypes right after the includes, before any
// type defined further down the file, and computeShapeExtras() returns this
// type.
struct ShapeExtras
{
    float sustainFraction; // fraction of the window spent near peak rotation
    int reversalCount;     // direction reversals on the dominant rotation axis
    unsigned long rampMs;  // time from ~half-peak rotation to the peak sample
};

// A wrist's gravity direction as a unit vector — see "Pose (gravity
// direction)" below. Declared here for the same reason as ShapeExtras.
struct GravDir
{
    float x, y, z;
};

// A SegmentRequest waiting for the torso's ack — see "SegmentRequest
// delivery" below. Declared here for the same reason as ShapeExtras.
struct PendingSegReq
{
    bool active;
    uint32_t segId, startTorsoMs, endTorsoMs;
    uint8_t sends;
    unsigned long firstSentMs, lastSentMs;
};

// Feedback pattern player types (see "Feedback patterns" below). Declared up
// here for the same reason as ShapeExtras: functions take them as params.
struct Pattern
{
    uint16_t onMs;
    uint16_t offMs;
    uint8_t repeats; // number of on/off cycles; 0 = repeat until stopped
};

enum PatternId
{
    PAT_BLINK,       // LED: 500ms on / 500ms off, forever
    PAT_BUZZ_SHORT,  // motor: one 100ms buzz
    PAT_BUZZ_DOUBLE, // motor: two 100ms buzzes, 150ms apart
    PAT_COUNT
};

struct PatternPlayer
{
    void (*output)(bool on); // drives the actual pin(s)
    const Pattern *pat;      // nullptr = idle
    uint8_t cyclesDone;
    bool isOn;
    unsigned long phaseStartMs;
};

// Everything that can make the hub buzz / change its status LED. Fired
// locally today; later a COMBAT_FEEDBACK message from the laptop can call
// onFeedbackEvent() with the same codes.
enum FeedbackEvent
{
    FB_WAITING_FOR_NODES, // LED blinks amber
    FB_ALL_NODES_READY,   // LED solid green
    FB_WEAPON_LOCKED,     // 2 short buzzes, LED -> weapon colour (arg = WeaponType)
    FB_SWING_ACCEPTED     // 1 short buzz
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

// ---------------- RGB LED (common cathode) ----------------
#define LED_R_PIN 25
#define LED_G_PIN 26
#define LED_B_PIN 27

enum LedState
{
    LED_BOOTING,
    LED_WAITING,  // weapon phase: waiting for other nodes
    LED_SAMPLING, // weapon phase: detecting, nothing held
    LED_HOLDING,  // weapon phase: known weapon held, counting toward lock
    LED_IDLE,
    LED_ACTIVE,
    LED_FLASH_ACCEPT,
    LED_FLASH_REJECT
};
LedState ledState = LED_BOOTING;
unsigned long ledFlashStart = 0;
const unsigned long LED_FLASH_DURATION_MS = 200;
// Longer than the accept flash: an accept also buzzes, a reject is only the
// LED, and 200ms of red was easy to miss.
const unsigned long LED_REJECT_FLASH_MS = 600;

// Colour shown while idle after the lock — set per weapon by FB_WEAPON_LOCKED.
// Same colours as the left wrist's locked LED.
uint8_t idleR = 0, idleG = 40, idleB = 0;

void ledWrite(int r, int g, int b)
{
    analogWrite(LED_R_PIN, r);
    analogWrite(LED_G_PIN, g);
    analogWrite(LED_B_PIN, b);
}

// ---------------- Feedback patterns (non-blocking, millis()-driven) ----------------
// Each pattern is on-time / off-time / repeat count. A PatternPlayer steps
// through one pattern on its output; patternUpdate() is polled from loop().
const Pattern PATTERNS[PAT_COUNT] = {
    {500, 500, 0}, // PAT_BLINK
    {100, 0, 1},   // PAT_BUZZ_SHORT
    {100, 150, 2}, // PAT_BUZZ_DOUBLE
};

const uint8_t HAPTIC_DUTY = 200; // PWM duty (0-255) while the motor is on
uint8_t blinkR = 0, blinkG = 0, blinkB = 0;

void motorOutput(bool on) { analogWrite(MOTOR_PIN, on ? HAPTIC_DUTY : 0); }
void ledBlinkOutput(bool on)
{
    if (on)
        ledWrite(blinkR, blinkG, blinkB);
    else
        ledWrite(0, 0, 0);
}

PatternPlayer motorPlayer = {motorOutput, nullptr, 0, false, 0};
PatternPlayer ledBlinkPlayer = {ledBlinkOutput, nullptr, 0, false, 0};

void patternStart(PatternPlayer &pl, PatternId id)
{
    pl.pat = &PATTERNS[id];
    pl.cyclesDone = 0;
    pl.isOn = true;
    pl.phaseStartMs = millis();
    pl.output(true);
}

void patternStop(PatternPlayer &pl)
{
    if (!pl.pat)
        return;
    pl.pat = nullptr;
    pl.output(false);
}

void patternUpdate(PatternPlayer &pl, unsigned long now)
{
    if (!pl.pat)
        return;
    unsigned long elapsed = now - pl.phaseStartMs;
    if (pl.isOn)
    {
        if (elapsed < pl.pat->onMs)
            return;
        pl.isOn = false;
        pl.phaseStartMs = now;
        pl.output(false);
        pl.cyclesDone++;
        if (pl.pat->repeats != 0 && pl.cyclesDone >= pl.pat->repeats)
            pl.pat = nullptr; // finished, output already off
    }
    else if (elapsed >= pl.pat->offMs)
    {
        pl.isOn = true;
        pl.phaseStartMs = now;
        pl.output(true);
    }
}

void updateFeedback(unsigned long now)
{
    patternUpdate(motorPlayer, now);
    patternUpdate(ledBlinkPlayer, now);
}

// Every solid-colour LED state goes through here, which also cancels any blink.
void ledSetColor(int r, int g, int b)
{
    patternStop(ledBlinkPlayer);
    ledWrite(r, g, b);
}
void ledBooting()
{
    ledState = LED_BOOTING;
    ledSetColor(255, 255, 255);
}
void ledWaiting()
{
    ledState = LED_WAITING;
    blinkR = 255; // amber
    blinkG = 80;
    blinkB = 0;
    patternStart(ledBlinkPlayer, PAT_BLINK);
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
void ledIdle()
{
    ledState = LED_IDLE;
    ledSetColor(idleR, idleG, idleB); // weapon colour once locked
}
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
        idleR = 0, idleG = 40, idleB = 0; // old dim-green idle
        break;
    }
}
bool ledFlashing()
{
    if (ledState == LED_FLASH_ACCEPT)
        return millis() - ledFlashStart <= LED_FLASH_DURATION_MS;
    if (ledState == LED_FLASH_REJECT)
        return millis() - ledFlashStart <= LED_REJECT_FLASH_MS;
    return false;
}
void ledMotionActive()
{
    // Don't cut an accept/reject flash short: the return-to-guard motion
    // right after a swing usually opens the next candidate within
    // CANDIDATE_COOLDOWN_MS, which used to swap the red straight to blue.
    if (ledFlashing())
        return;
    ledState = LED_ACTIVE;
    ledSetColor(0, 0, 255);
}
void ledFlashAccept()
{
    ledState = LED_FLASH_ACCEPT;
    ledFlashStart = millis();
    ledSetColor(0, 255, 0);
}
void ledFlashReject()
{
    ledState = LED_FLASH_REJECT;
    ledFlashStart = millis();
    ledSetColor(255, 0, 0);
}
void ledLowBatteryAmber() { ledSetColor(255, 80, 0); }

void updateLed()
{
    if ((ledState == LED_FLASH_ACCEPT || ledState == LED_FLASH_REJECT) && !ledFlashing())
        ledIdle();
}

// ---------------- Haptic motor (2N7000 low-side switch on GPIO4) ----------------
// Same caveat as before: no flyback diode yet — add a 1N4148/1N4001 across the
// motor terminals before repeated/long-duration testing.
// (MOTOR_PIN is defined up in the CONFIG block — the pattern player above
// needs it.) Buzzes are played by motorPlayer; see onFeedbackEvent().
void motorSetup()
{
    pinMode(MOTOR_PIN, OUTPUT);
    analogWrite(MOTOR_PIN, 0);
}

// ---------------- Feedback events ----------------
// The single entry point for hub LED + haptic feedback. Today it's called
// from the game logic via localFeedback(). To drive it from the laptop
// instead, set LOCAL_FEEDBACK_TRIGGERS to 0 and call onFeedbackEvent() from
// the COMBAT_FEEDBACK message handler.
void onFeedbackEvent(FeedbackEvent e, uint8_t arg)
{
    switch (e)
    {
    case FB_WAITING_FOR_NODES:
        ledWaiting(); // blinking amber
        break;
    case FB_ALL_NODES_READY:
        ledSampling(); // solid green
        break;
    case FB_WEAPON_LOCKED:
        setIdleWeaponColor((WeaponType)arg);
        ledIdle();
        patternStart(motorPlayer, PAT_BUZZ_DOUBLE);
        break;
    case FB_SWING_ACCEPTED:
        patternStart(motorPlayer, PAT_BUZZ_SHORT);
        break;
    }
}

void localFeedback(FeedbackEvent e, uint8_t arg)
{
#if LOCAL_FEEDBACK_TRIGGERS
    onFeedbackEvent(e, arg);
#endif
}

// ---------------- MPU6500 ----------------
#define MPU6500_ADDR 0x68
#define REG_PWR_MGMT_1 0x6B
#define REG_GYRO_CONFIG 0x1B
#define REG_ACCEL_CONFIG 0x1C
#define REG_ACCEL_XOUT_H 0x3B // accel(6) + temp(2) + gyro(6) = 14 contiguous bytes

// Was ±2g/±250dps (16384/131) — the sensor's narrowest ranges. Real swings
// were hard-clipping: peak gyro magnitude repeatedly hit exactly ~433
// (sqrt(3)*250, the 3-axis saturation ceiling) and peakAccel repeatedly hit
// ~2.46 (sqrt(3)*2g-1g) across every action type. Widened for headroom;
// resolution loss is negligible (16.4 LSB/dps, 4096 LSB/g — far finer than
// any threshold here needs).
const float ACCEL_SCALE = 4096.0;
const float GYRO_SCALE = 16.4;

bool readSuccess = true;

void writeRegister(uint8_t reg, uint8_t value)
{
    Wire.beginTransmission(MPU6500_ADDR);
    Wire.write(reg);
    Wire.write(value);
    Wire.endTransmission();
}

// Single burst read of accel+temp+gyro instead of 6 separate transactions.
// out14 layout: [ax,ay,az,temp,gx,gy,gz] as raw int16.
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
        ok = 1; // avoid divide-by-zero if the sensor never answered

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
    debugUdp.print("[wrist] " + msg);
    debugUdp.endPacket();
    Serial.println(msg);
}

// Same idea as debugLog(), but on its own port (RESULT_PORT) — so milestone
// lines (all_nodes_ready / weapon_candidate / weapon_locked /
// action_accepted) don't get buried in the debug stream. Reuses debugUdp; a
// WiFiUDP socket can send to any destination port.
void resultLog(String msg)
{
    debugUdp.beginPacket(broadcastIP, RESULT_PORT);
    debugUdp.print("[right_wrist] " + msg);
    debugUdp.endPacket();
    Serial.println(msg);
}

// ================= WEAPON DETECTION (from right_wrist_weapon.ino) =================

// ---------------- Pose (gravity direction) ----------------
// A wrist's pose is the direction gravity points in that board's own
// frame: the accel vector, normalised and EMA-smoothed as a vector. Logged
// and compared as elevation/azimuth (el = asin(z), az = atan2(y, x)), the
// same encoding the left wrist sends in WristPose. No boot-time zero, so
// it only depends on how the board is strapped on.
//
// Was pitch = atan2(ay, az) / roll = atan2(-ax, az). Both divide by az, so
// in stances where gravity lies near the board's x/y plane (the right arm
// in mace + sword&shield, the left arm in sword&shield) they swung tens of
// degrees for a ~1 degree real tilt, and a pitch/roll "distance" didn't
// match real angles at all. Same change in left_wrist_game.ino.
// (GravDir itself is declared up top next to ShapeExtras, same reason.)
GravDir gravFromElAz(float elDeg, float azDeg)
{
    float el = elDeg * PI / 180.0, az = azDeg * PI / 180.0;
    return {cosf(el) * cosf(az), cosf(el) * sinf(az), sinf(el)};
}

float gravElDeg(const GravDir &g) { return asin(constrain(g.z, -1.0f, 1.0f)) * 180.0 / PI; }
float gravAzDeg(const GravDir &g) { return atan2(g.y, g.x) * 180.0 / PI; }

// Real angle between two gravity directions, degrees.
float gravAngleDeg(const GravDir &a, const GravDir &b)
{
    float dot = a.x * b.x + a.y * b.y + a.z * b.z;
    return acos(constrain(dot, -1.0f, 1.0f)) * 180.0 / PI;
}

// ---------------- Smoothing + stillness gate (own pose) ----------------
const float EMA_ALPHA = 0.2;
// Was 15: a wrist holding a prop in a ready stance reads 20-60 (peaks ~75)
// dps from hand tremor alone (2026-09-29 logs), so it was never "stable".
// Moving between stances reads 150-2000. The pose angles themselves stay
// within a few degrees during a hold.
const float STILLNESS_GYRO_THRESHOLD_DPS = 100.0;
const unsigned long STILLNESS_HOLD_MS = 700; // TODO: tune against real logs

GravDir rightGravEMA = {0, 0, 1}; // not re-normalised; gravRight() does that
float gyroMagEMA = 0;             // smoothed before the stillness check, so one noisy
                                  // 100Hz sample (hand tremor) doesn't reset the hold timer
bool emaInitialized = false;
unsigned long poseStillSinceMs = 0;
unsigned long lastPoseLogMs = 0;

// This wrist's smoothed gravity direction as a unit vector.
GravDir gravRight()
{
    const GravDir &g = rightGravEMA;
    float n = sqrt(g.x * g.x + g.y * g.y + g.z * g.z);
    if (n < 1e-6f)
        return {0, 0, 1};
    return {g.x / n, g.y / n, g.z / n};
}

// Updates rightGravEMA/gyroMagEMA from one IMU sample and returns whether
// this wrist has been held still for STILLNESS_HOLD_MS.
bool updateOwnPose(float ax_g, float ay_g, float az_g, float gyroMag, unsigned long now)
{
    float accelMag = sqrt(ax_g * ax_g + ay_g * ay_g + az_g * az_g);
    if (accelMag < 0.1f)
        accelMag = 0.1f; // free-fall / bad read: keep the division sane
    float ux = ax_g / accelMag, uy = ay_g / accelMag, uz = az_g / accelMag;

    if (!emaInitialized)
    {
        rightGravEMA = {ux, uy, uz};
        gyroMagEMA = gyroMag;
        emaInitialized = true;
    }
    else
    {
        rightGravEMA.x += EMA_ALPHA * (ux - rightGravEMA.x);
        rightGravEMA.y += EMA_ALPHA * (uy - rightGravEMA.y);
        rightGravEMA.z += EMA_ALPHA * (uz - rightGravEMA.z);
        gyroMagEMA = EMA_ALPHA * gyroMag + (1 - EMA_ALPHA) * gyroMagEMA;
    }

    if (gyroMagEMA > STILLNESS_GYRO_THRESHOLD_DPS)
        poseStillSinceMs = now;
    return (now - poseStillSinceMs) >= STILLNESS_HOLD_MS;
}

// ---------------- Incoming poses from left wrist + torso ----------------
const unsigned long POSE_FRESHNESS_MS = 400; // drop stale packets (missed beacons, disconnect)

float leftEl = 0, leftAz = 0; // left wrist gravity direction (WristPose)
bool leftStable = false;
bool leftSeen = false;
unsigned long lastLeftRxMs = 0;

float torsoPitch = 0, torsoRoll = 0;
bool torsoStable = false;
bool torsoSeen = false;
unsigned long lastTorsoRxMs = 0;

// "Seen" guard: before the first packet, lastRxMs=0 and millis() may still
// be < POSE_FRESHNESS_MS right after boot, which would read as fresh.
bool leftFresh(unsigned long now) { return leftSeen && (now - lastLeftRxMs) < POSE_FRESHNESS_MS; }
bool torsoFresh(unsigned long now) { return torsoSeen && (now - lastTorsoRxMs) < POSE_FRESHNESS_MS; }

void checkIncomingPose()
{
    uint8_t buf[32];
    int packetSize = poseUdp.parsePacket();
    if (packetSize <= 0)
        return;

    poseUdp.read(buf, sizeof(buf));
    // Dispatch on hdr.type. acceptPacket() drops wrong-length packets, other
    // players' boards, and our own HubStatus broadcast looping back.
    PacketHeader hdr;
    if (!acceptPacket(buf, packetSize, hdr))
        return;

    if (hdr.type == PKT_WRIST_POSE && hdr.deviceId == LEFT_WRIST_ID)
    {
        noteRx(hdr);
        WristPose p;
        memcpy(&p, buf, sizeof(p));
        leftEl = p.gravEl;
        leftAz = p.gravAz;
        leftStable = p.stable;
        leftSeen = true;
        lastLeftRxMs = millis();
    }
    else if (hdr.type == PKT_TORSO_POSE && hdr.deviceId == TORSO_ID)
    {
        noteRx(hdr);
        TorsoPose p;
        memcpy(&p, buf, sizeof(p));
        torsoPitch = p.pitch;
        torsoRoll = p.roll;
        torsoStable = p.stable;
        torsoSeen = true;
        lastTorsoRxMs = millis();
    }
}

// ---------------- Weapon classification ----------------
// Nearest-reference: each weapon's ready stance is a reference gravity
// direction for each wrist, given as (el, az) degrees — the same numbers
// the pose stream prints. The distance to a reference is
// sqrt(rightAngle^2 + leftAngle^2), where each angle is the real angle
// between the measured and reference gravity directions. A reading is the
// weapon of the closest reference if it's within WEAPON_MATCH_MAX_DEG AND
// at least WEAPON_MATCH_MARGIN_DEG closer than the closest reference of any
// other weapon (see those constants). A weapon can have more than one
// reference.
//
// Only tilt/twist is measurable: an accelerometer can't see heading, so
// "arm out to the left" vs "arm in front" only shows up through how it
// changes the forearm's tilt/twist. So each weapon's stance must differ in
// forearm tilt/twist, not just in where the arms point: with the left unit
// below the wrist, the first stances tried had the same left forearm angle
// for all three weapons and sword&shield-mace were only 18 degrees apart.
//
// The torso isn't used: it zeroes itself at boot, and its lean only varied
// ~10 degrees across the 2026-09-29 captures.
//
// References measured 2026-09-29 from the el/az pose stream with the left
// unit strapped BELOW the wrist (the shield sits above it), via
// weapon_refs.py over the hold_<weapon> sessions (18:04 sword&shield,
// 18:05 spear, 19:28 mace; stable samples only, rest poses dropped).
// Within-hold spread p95: 8.9 / 7.6 / 4.7 degrees.
// Separation: sword&shield-mace 53.5 (right 27.8, left 45.7),
// sword&shield-spear 79.2, spear-mace 112.4.
//
// To recapture (whenever a board's mounting or a stance changes): record
// listener sessions labelled hold_sword_shield / hold_spear / hold_mace,
// run `python weapon_refs.py`, and paste its table here. It checks that
// every pair is far enough apart for each weapon's own hold spread (see
// WEAPON_MATCH_MARGIN_DEG).
struct WeaponRef
{
    WeaponType weapon;
    float rEl, rAz, lEl, lAz;
};

const WeaponRef WEAPON_REFS[] = {
    {WEAPON_SWORD_SHIELD, -5.8, -66.1, -7.9, 173.8},
    {WEAPON_SPEAR, -20.5, 13.4, -20.7, 171.6},
    {WEAPON_MACE, 19.3, -78.2, 16.4, -147.1},
};
const int WEAPON_REF_COUNT = sizeof(WEAPON_REFS) / sizeof(WEAPON_REFS[0]);

// Match rule: nearest weapon, by margin rather than a fixed radius.
// Was a single 20 deg radius around every reference, capped at half the
// closest cross-weapon separation (sword&shield-mace 53.5) so zones couldn't
// overlap — which made every stance strict in every direction, even
// directions that lead nowhere near another weapon (spear is 79 and 112 deg
// from the other two).
//
// Now a pose is accepted if the nearest weapon is within
// WEAPON_MATCH_MAX_DEG and beats the runner-up weapon by at least
// WEAPON_MATCH_MARGIN_DEG. Zones can't overlap by construction (only one
// weapon can be MARGIN closer than all others). Straight toward another
// weapon a pose can drift (separation - MARGIN) / 2 — 19.3 deg for
// sword&shield-mace, about the old radius — and up to MAX in any other
// direction. A loose pose still has to be held continuously for
// WEAPON_LOCK_HOLD_MS, which is what stops passing poses from locking.
// TODO: both are starting values — check them against hold sessions that
// include deliberately sloppy holds (the pose stream logs dist and margin).
const float WEAPON_MATCH_MAX_DEG = 35.0;
const float WEAPON_MATCH_MARGIN_DEG = 15.0;

// Returns the matched weapon (WEAPON_UNKNOWN if the nearest is too far or
// too close to a second weapon). nearest/bestDist are the closest reference
// regardless of the rule; margin = runner-up weapon's distance - bestDist
// (999 with only one weapon in WEAPON_REFS). All three are for logs.
WeaponType classifyWeapon(const GravDir &right, const GravDir &left,
                          WeaponType &nearest, float &bestDist, float &margin)
{
    // Closest reference per weapon (a weapon can have several), indexed by
    // WeaponType.
    float perWeapon[WEAPON_MACE + 1];
    for (int w = 0; w <= WEAPON_MACE; w++)
        perWeapon[w] = 1e9;
    for (int i = 0; i < WEAPON_REF_COUNT; i++)
    {
        const WeaponRef &ref = WEAPON_REFS[i];
        float dR = gravAngleDeg(right, gravFromElAz(ref.rEl, ref.rAz));
        float dL = gravAngleDeg(left, gravFromElAz(ref.lEl, ref.lAz));
        float d = sqrtf(dR * dR + dL * dL);
        if (d < perWeapon[ref.weapon])
            perWeapon[ref.weapon] = d;
    }

    WeaponType best = WEAPON_UNKNOWN;
    bestDist = 1e9;
    float runnerUp = 1e9;
    for (int w = 0; w <= WEAPON_MACE; w++)
    {
        if (perWeapon[w] < bestDist)
        {
            runnerUp = bestDist;
            bestDist = perWeapon[w];
            best = (WeaponType)w;
        }
        else if (perWeapon[w] < runnerUp)
            runnerUp = perWeapon[w];
    }
    nearest = best;
    margin = (runnerUp >= 1e9) ? 999.0 : runnerUp - bestDist;
    bool ok = bestDist <= WEAPON_MATCH_MAX_DEG && margin >= WEAPON_MATCH_MARGIN_DEG;
    return ok ? best : WEAPON_UNKNOWN;
}

const char *weaponName(WeaponType w)
{
    switch (w)
    {
    case WEAPON_SWORD_SHIELD:
        return "sword_shield";
    case WEAPON_SPEAR:
        return "spear";
    case WEAPON_MACE:
        return "mace";
    default:
        return "unknown";
    }
}

// ---------------- Game phase + weapon lock ----------------
GamePhase phase = PHASE_WAITING;

// Current candidate while detecting (WEAPON_UNKNOWN = nothing held) and
// when it was first classified; see runDetectionTick().
WeaponType candidateWeapon = WEAPON_UNKNOWN;
unsigned long candidateSinceMs = 0;
unsigned long candidateLastSeenMs = 0; // last tick that classified as candidateWeapon
WeaponType lockedWeapon = WEAPON_UNKNOWN;

const unsigned long HUB_STATUS_INTERVAL_MS = 100;
unsigned long lastHubStatusMs = 0;

// Periodic (not edge-only) so a board that reboots or joins late still
// picks up the current phase within one interval.
void broadcastHubStatus(unsigned long now)
{
    if (now - lastHubStatusMs < HUB_STATUS_INTERVAL_MS)
        return;
    lastHubStatusMs = now;

    HubStatus status;
    fillHeader(status.hdr, PKT_HUB_STATUS);
    status.phase = (uint8_t)phase;
    status.weapon = (uint8_t)(phase == PHASE_LOCKED ? lockedWeapon : candidateWeapon);
    status.holding = (phase == PHASE_DETECTING && candidateWeapon != WEAPON_UNKNOWN);
    poseUdp.beginPacket(broadcastIP, POSE_PORT);
    poseUdp.write((uint8_t *)&status, sizeof(status));
    poseUdp.endPacket();
}

// ---------------- Torso beacon ring buffer + clock sync ----------------
// The beacon's torsoMs also doubles as our time-sync source: every time one
// arrives we update a smoothed offset so we can translate OUR millis() into
// TORSO's clock domain when asking it for a matching sample window.
#define TORSO_BEACON_BUF 150 // ~3s of history at ~50Hz beacon rate
struct TorsoBeaconEntry
{
    uint32_t torsoMs;
    float gyroMag;
    bool valid;
};
TorsoBeaconEntry torsoBuf[TORSO_BEACON_BUF];
int torsoBufHead = 0;

// Each beacon gives one sample of (torsoMs - our millis() at receipt) =
// true offset - that beacon's network/polling delay. Delay is never
// negative, so every sample sits at or below the true offset: the largest
// sample over the last CLOCK_OFFSET_WINDOW beacons (~1s at 50Hz) is the
// least-delayed one and the best estimate. (The old 0.9/0.1 EMA averaged the
// delays in, biasing the offset low by the mean delay, and one slow beacon
// dragged it down.) Crystal drift between the boards is tens of ppm, far
// under 1ms per window. All int32: the uint32 subtraction wraps correctly
// across millis() rollover, and the cast gives the signed offset.
#define CLOCK_OFFSET_WINDOW 50
int32_t clockOffsetSamples[CLOCK_OFFSET_WINDOW];
int clockOffsetCount = 0; // filled entries, up to CLOCK_OFFSET_WINDOW
int clockOffsetHead = 0;
int32_t clockOffsetMs = 0; // torsoMs - wristMs, max over the window

void updateClockOffset(uint32_t torsoMs, uint32_t wristMsAtRx)
{
    clockOffsetSamples[clockOffsetHead] = (int32_t)(torsoMs - wristMsAtRx);
    clockOffsetHead = (clockOffsetHead + 1) % CLOCK_OFFSET_WINDOW;
    if (clockOffsetCount < CLOCK_OFFSET_WINDOW)
        clockOffsetCount++;

    int32_t best = clockOffsetSamples[0];
    for (int i = 1; i < clockOffsetCount; i++)
        if (clockOffsetSamples[i] > best)
            best = clockOffsetSamples[i];
    clockOffsetMs = best;
}

uint32_t toTorsoClock(uint32_t wristMs)
{
    return wristMs + (uint32_t)clockOffsetMs; // modular add, same as a signed add
}

void storeTorsoBeacon(uint32_t torsoMs, float gyroMag)
{
    torsoBuf[torsoBufHead] = {torsoMs, gyroMag, true};
    torsoBufHead = (torsoBufHead + 1) % TORSO_BEACON_BUF;
}

float torsoPeakInWindow(uint32_t startTorsoMs, uint32_t endTorsoMs)
{
    float peak = 0;
    for (int i = 0; i < TORSO_BEACON_BUF; i++)
    {
        if (torsoBuf[i].valid && torsoBuf[i].torsoMs >= startTorsoMs && torsoBuf[i].torsoMs <= endTorsoMs)
        {
            if (torsoBuf[i].gyroMag > peak)
                peak = torsoBuf[i].gyroMag;
        }
    }
    return peak;
}

bool torsoDataAvailable(uint32_t startTorsoMs, uint32_t endTorsoMs)
{
    for (int i = 0; i < TORSO_BEACON_BUF; i++)
        if (torsoBuf[i].valid && torsoBuf[i].torsoMs >= startTorsoMs && torsoBuf[i].torsoMs <= endTorsoMs)
            return true;
    return false;
}

void checkIncomingLink()
{
    uint8_t buf[32];
    int packetSize = linkUdp.parsePacket();
    if (packetSize <= 0)
        return;

    linkUdp.read(buf, sizeof(buf));
    // Our own SegmentRequest looping back is dropped by acceptPacket().
    PacketHeader hdr;
    if (!acceptPacket(buf, packetSize, hdr))
        return;

    if (hdr.type == PKT_TORSO_BEACON && hdr.deviceId == TORSO_ID)
    {
        noteRx(hdr);
#if LATENCY_TEST
        noteBeaconArrival();
#endif
        TorsoBeacon b;
        memcpy(&b, buf, sizeof(b));
        uint32_t rxMs = millis();
        storeTorsoBeacon(b.torsoMs, b.gyroMag);
        updateClockOffset(b.torsoMs, rxMs);
    }
    else if (hdr.type == PKT_SEGMENT_ACK && hdr.deviceId == TORSO_ID)
    {
        noteRx(hdr);
        SegmentAck a;
        memcpy(&a, buf, sizeof(a));
        onSegmentAck(a.segId, millis());
    }
#if LATENCY_TEST
    else if (hdr.type == PKT_PONG && hdr.deviceId == TORSO_ID)
    {
        uint32_t rxUs = micros();
        noteRx(hdr);
        LatencyProbe p;
        memcpy(&p, buf, sizeof(p));
        debugLog("[rtt] us=" + String(rxUs - p.t0Us));
    }
#endif
}

// ---------------- Latency test (LATENCY_TEST builds only) ----------------
// RTT rather than one-way delay: the hub and torso clocks aren't synced, but
// both ends of an RTT are read from the hub's own micros(). Both numbers
// include up to one loop() pass of polling delay on each board.
#if LATENCY_TEST
const unsigned long PING_INTERVAL_US = 100000UL; // 10Hz
unsigned long lastPingUs = 0;

void sendPingIfDue()
{
    uint32_t nowUs = micros();
    if (nowUs - lastPingUs < PING_INTERVAL_US)
        return;
    lastPingUs = nowUs;

    LatencyProbe p;
    fillHeader(p.hdr, PKT_PING);
    p.t0Us = micros();
    linkUdp.beginPacket(broadcastIP, LINK_PORT);
    linkUdp.write((uint8_t *)&p, sizeof(p));
    linkUdp.endPacket();
}

// TorsoBeacon inter-arrival times, batched into one debug line a second (or
// sooner if the batch fills) — one line per beacon would add 50 broadcasts/s
// to the very link being measured.
#define BEACON_IAT_BATCH 64
uint32_t beaconIatUs[BEACON_IAT_BATCH];
int beaconIatCount = 0;
uint32_t lastBeaconRxUs = 0;
bool beaconRxSeen = false;
unsigned long lastIatFlushMs = 0;

void flushBeaconIat()
{
    lastIatFlushMs = millis();
    if (beaconIatCount == 0)
        return;
    String line = "[beacon_iat] us=";
    line.reserve(20 + beaconIatCount * 7);
    for (int i = 0; i < beaconIatCount; i++)
    {
        if (i > 0)
            line += ',';
        line += String(beaconIatUs[i]);
    }
    beaconIatCount = 0;
    debugLog(line);
}

void noteBeaconArrival()
{
    uint32_t nowUs = micros();
    if (beaconRxSeen)
        beaconIatUs[beaconIatCount++] = nowUs - lastBeaconRxUs;
    beaconRxSeen = true;
    lastBeaconRxUs = nowUs;
    if (beaconIatCount >= BEACON_IAT_BATCH)
        flushBeaconIat();
}

void runLatencyTest(unsigned long now)
{
    sendPingIfDue();
    if (now - lastIatFlushMs >= 1000)
        flushBeaconIat();
}
#endif

// ---------------- SegmentRequest delivery (ack + retry) ----------------
// A lost SegmentRequest means the Ultra96 never gets torso data for that
// segment, so each request stays pending until the torso's SegmentAck comes
// back, and is resent every SEG_REQ_RETRY_MS up to SEG_REQ_MAX_SENDS times.
// The torso acks every copy but only acts on the first, so retries are safe.
// Timeout sits above a normal RTT (a few ms with WiFi sleep off, tens with it
// on); the last retry still lands inside the torso's ~4.5s ring buffer, even
// for a whole-spin window.
const unsigned long SEG_REQ_RETRY_MS = 150;
const uint8_t SEG_REQ_MAX_SENDS = 4;
#define SEG_REQ_PENDING_MAX 4                      // swings are >=~0.5s apart, so 4 slots is plenty
PendingSegReq pendingSegReqs[SEG_REQ_PENDING_MAX]; // struct declared up top

void transmitSegmentRequest(PendingSegReq &p, unsigned long now)
{
    SegmentRequest req;
    fillHeader(req.hdr, PKT_SEGMENT_REQUEST);
    req.segId = p.segId;
    req.startTorsoMs = p.startTorsoMs;
    req.endTorsoMs = p.endTorsoMs;
    linkUdp.beginPacket(broadcastIP, LINK_PORT);
    linkUdp.write((uint8_t *)&req, sizeof(req));
    linkUdp.endPacket();
    p.sends++;
    p.lastSentMs = now;
}

void sendSegmentRequestToTorso(uint32_t segId, uint32_t startTorsoMs, uint32_t endTorsoMs)
{
    unsigned long now = millis();
    // Free slot, or else evict the oldest pending request.
    int slot = 0;
    for (int i = 0; i < SEG_REQ_PENDING_MAX; i++)
    {
        if (!pendingSegReqs[i].active)
        {
            slot = i;
            break;
        }
        if (pendingSegReqs[i].firstSentMs < pendingSegReqs[slot].firstSentMs)
            slot = i;
    }
    PendingSegReq &p = pendingSegReqs[slot];
    if (p.active)
        debugLog("[segreq] dropped seg=" + String(p.segId) + " sends=" + String(p.sends) + " (pending table full)");
    p = {true, segId, startTorsoMs, endTorsoMs, 0, now, now};
    transmitSegmentRequest(p, now);
    debugLog("Sent SegmentRequest seg=" + String(segId));
}

// Call every loop(): resends overdue requests, gives up after the last try.
void serviceSegmentRequests(unsigned long now)
{
    for (int i = 0; i < SEG_REQ_PENDING_MAX; i++)
    {
        PendingSegReq &p = pendingSegReqs[i];
        if (!p.active || now - p.lastSentMs < SEG_REQ_RETRY_MS)
            continue;
        if (p.sends >= SEG_REQ_MAX_SENDS)
        {
            debugLog("[segreq] lost seg=" + String(p.segId) + " sends=" + String(p.sends) +
                     " (no ack - torso data for this segment is missing)");
            p.active = false;
            continue;
        }
        transmitSegmentRequest(p, now);
    }
}

void onSegmentAck(uint32_t segId, unsigned long now)
{
    for (int i = 0; i < SEG_REQ_PENDING_MAX; i++)
    {
        PendingSegReq &p = pendingSegReqs[i];
        if (p.active && p.segId == segId)
        {
            debugLog("[segreq] acked seg=" + String(segId) + " sends=" + String(p.sends) +
                     " ms=" + String(now - p.firstSentMs));
            p.active = false;
            return;
        }
    }
    // No match: a duplicate ack for a request already cleared. Nothing to do.
}

// ---------------- Motion segmentation ----------------
enum MotionState
{
    IDLE,
    ACTIVE
};
MotionState state = IDLE;

// Rotation-based threshold (dps) that opens a new candidate — tune against
// your own calibration log.
const float START_THRESHOLD = 60.0;

// Linear-jerk threshold (g, deviation from resting 1g) — this is what lets
// a low-rotation Thrust still open a candidate.
const float JERK_START_THRESHOLD = 1.3;

// Was END_THRESHOLD=20/END_HOLD_MS=150 (close a candidate once gyroMag drops
// below 20dps and STAYS there for 150ms) + activityLow()/belowThresholdSince.
// That assumed the player returns to a genuinely still baseline between
// actions. In real gameplay they're usually still moving (repositioning,
// weaving) between attacks, so gyroMag/jerk may never drop that low, or not
// for long enough -- meaning onMotionEnd() could simply never fire (losing
// the action entirely), or one giant segment could swallow multiple real
// swings (findPeakActivityIndex() only ever returns the single highest-
// scoring sample in the whole buffer, so every swing but the biggest one in
// that stretch would be silently dropped).
//
// Replaced with peak-relative settling: track the highest score seen this
// candidate (score = max(gyroMag/START_THRESHOLD, jerk/JERK_START_THRESHOLD),
// the same normalization findPeakActivityIndex() already scores samples
// with) and the last time the score was at or above SETTLE_FRACTION of that
// peak. Once that was POST_PEAK_MS ago (below -- already used to size the
// post-peak trim window, one constant doing double duty as the settle
// period), the candidate is done, independent of whether ambient motion
// ever quiets down. A genuinely sustained action (e.g. Spin) keeps
// re-crossing back above its own half-peak and stays open for its whole
// real duration; a real strike decays well below its peak and stays there,
// so it closes promptly. See updateMotionState() for the actual tracking.
const float SETTLE_FRACTION = 0.5;

// Cooldown after firing a candidate before a new one can open -- stops a
// real strike's own settling tail (e.g. residual prop-wobble jerk right
// after impact) from being misread as the start of a second candidate.
// No existing data to calibrate this from (nothing like it existed before
// this redesign); starting value chosen shorter than any real inter-swing
// gap in a combo -- verify with a back-to-back-attacks test, same
// discipline as every other constant this session.
const unsigned long CANDIDATE_COOLDOWN_MS = 150;

// Torso gating — max torso rotation allowed DURING the window for the
// segment to be accepted as a real swing rather than whole-body movement.
// Spin Attack gets a much looser cap since torso rotation is expected there.
// Raised from data: real Diagonal/Horizontal/Thrust/Uppercut swings commonly
// showed torsoPeak of 80-260 (normal core engagement, not cheating) — the
// old 80 dps gate was rejecting nearly all of them. 300 clears every
// observed legitimate non-spin rep with some margin. Spin's own torsoPeak
// reached 381 — 450 leaves margin there too.
// CAVEAT: these are set from POSITIVE examples only (real swings). Still
// don't have data on what a deliberate "turn body without swinging" cheat
// actually looks like on torsoPeak — that's the real test of whether this
// gate means anything. Get that data before trusting these as final.
const float TORSO_GATE_MAX_NORMAL = 300.0;
const float TORSO_GATE_MAX_SPIN = 450.0;

// Minimum torso engagement -- the OTHER side of the torso gate. The MAX_*
// constants above only ever caught whole-body cheating (too much torso
// rotation); nothing previously required any torso rotation at all, which
// let wrist-only flicks/shakes through since they don't move the torso at
// all by construction. A quick jerky "flick" and a real Thrust look
// identical on rotation-shape features (reversals/sustainFraction/rampMs
// were all tried and all failed -- see plan doc), but not on this: real
// Thrust's lowest observed torsoPeak across two separate test sessions was
// 33.0; real swings' lowest (across all batches this session) was 23.7.
// Was 15.0 -- a follow-up batch showed real actions never dropping below
// 31.7 that round, while several flicks still slipped through right above
// the old floor (torsoPeak 16.1-19.8). Raised to 20.0: still 3.7 below the
// true observed real-action minimum (23.7), while now catching those
// near-miss flicks. One outlier flick at torsoPeak=31.2 (genuine incidental
// body movement during a wrist flick) remains a known, accepted gap -- not
// fixable by this gate alone.
// Lowered to 10.0 (2026-09-29, 21:37-21:46 batch, after the torso beacon
// switched to max-gyro-since-last-beacon): 6 of 25 real verticals fell at
// 11.9-19.6 and were rejected, while the fakes this gate alone stops (wrist
// flicks) all sat at <=9.2. Thin margin (9.2 vs 11.9) -- re-check on the
// next batch. Now that isValidAttack requires >=1.0g, most flicks are also
// caught there.
const float MIN_TORSO_ENGAGEMENT = 10.0;

// Raw-window sanity cap. MAX_DURATION (below, in isValidAttack) only ever
// checks the TRIMMED window, which is architecturally ~500ms almost
// regardless of what happened before/after the peak. That leaves a gap: a
// long stretch of sustained motion (walking around holding the device,
// casual handling) that happens to contain one brief isolated flick still
// produces a normal-looking trimmed segment and gets accepted. Caught this
// via real negative-example data: largest raw duration across every genuine
// swing (all 6 actions) was 285 samples (2.85s, Spin); the false-positive
// walking/handling reps hit 433 samples (4.33s). 3.5s leaves ~23% margin
// above the real max while sitting well below the false-positive value.
// Also now used live in updateMotionState() as the "tooLong" safety cap
// that force-closes a candidate that never settles (see SETTLE_FRACTION
// above) — same value, same purpose, just also checked before the fact
// rather than purely after.
const unsigned long RAW_MAX_DURATION_MS = 3500;

// Shape gating derived from a real-vs-fake calibration round (all 6 actions
// plus a batch of deliberate fakes: random wrist flicks, moving/handling the
// board without swinging). Findings:
//   - Spin: every real rep hit reversals==0, sustainFraction 0.45-1.00. Every
//     fake hit reversals>=3. Clean separation -> gate spin on reversals==0.
//   - Vertical/Horizontal/Diagonal/Uppercut ("swing" bucket): reversals
//     mostly 1 (one real outlier at 3), sustainFraction 0.29-0.49. Fakes
//     ranged reversals 3-13, sustainFraction 0.02-0.49 -> gate this bucket on
//     reversals/sustainFraction, with the cutoff placed to keep the one real
//     outlier accepted (a false reject on a real swing is worse than a false
//     accept on a fake) at the cost of letting the closest fake through too.
//   - Thrust: reversals 3-5, sustainFraction 0.14-0.20 -- this range sits
//     INSIDE the fakes' range, so a universal gate can't admit real Thrust
//     without also admitting most fakes. Deliberately exempted from the
//     swing-shape gate below; a thrust-shaped fake is a known, accepted gap
//     (see plan doc) rather than something this pass claims to fix.
const int SWING_MAX_REVERSALS = 3;
const float SWING_MIN_SUSTAIN_FRACTION = 0.25;

// Spin reversal cap. Was a hard reversals==0. Reversals are counted over the
// TRIMMED window (the final ~500ms swing), not the turn itself, so the
// closing swing can legitimately flip once: in the 2026-09-29 21:42 batch
// 8 of 17 real spins had exactly 1 reversal and were rejected, while the
// only spin-likely fake (a body turn) had 0 and was stopped by the
// magnitude floor anyway.
const int SPIN_MAX_REVERSALS = 1;

// "thrustLikely" isn't classifying WHICH action this is (that's still
// Ultra96's job) -- it's just picking which existing validity sub-rule
// applies, the same way spinLikely already branches the torso gate.
// Was 25.0 -- a later batch produced a real Thrust rep at totalRot=28.5,
// which got misclassified as NOT thrust-like and consequently rejected by
// the swing-shape gate (a false reject on a genuine attack). Real Thrust
// trimmed totalRotation observed across all batches: 6.3-28.5 deg. Real
// Vertical/Horizontal/Diagonal/Uppercut: 50-220 deg (lowest observed: 50).
// 35 keeps real margin above the new Thrust max while staying well below
// every swing-bucket rep.
const float THRUST_MAX_ROTATION_DEG = 35.0;

unsigned long motionStartTime = 0;

// Peak-settle tracking for the currently-open candidate — see
// SETTLE_FRACTION above. Reset in onMotionStart() for each new candidate.
float candidatePeakScore = 0;
unsigned long candidateSettleTimerResetAt = 0;

// No new candidate may open before this time — see CANDIDATE_COOLDOWN_MS.
unsigned long cooldownUntil = 0;

// Was 250 (2.5s at 100Hz) — real Spin Attack reps were consistently hitting
// this cap (raw=250 in the logs), meaning the tail of the spin was silently
// dropped rather than captured. 500 = 5s of headroom.
#define MAX_SAMPLES 500
Sample buffer[MAX_SAMPLES];
int sampleCount = 0;

// Always-on pre-trigger ring buffer so the ramp-up before crossing
// START_THRESHOLD isn't lost.
#define PRETRIGGER_SAMPLES 20 // ~200ms at 100Hz
Sample pretrigBuf[PRETRIGGER_SAMPLES];
int pretrigHead = 0;
bool pretrigFilled = false;

void storePretrigger(const Sample &s)
{
    pretrigBuf[pretrigHead] = s;
    pretrigHead = (pretrigHead + 1) % PRETRIGGER_SAMPLES;
    if (pretrigHead == 0)
        pretrigFilled = true;
}

void seedSegmentFromPretrigger()
{
    sampleCount = 0;
    int count = pretrigFilled ? PRETRIGGER_SAMPLES : pretrigHead;
    int start = pretrigFilled ? pretrigHead : 0;
    for (int i = 0; i < count && sampleCount < MAX_SAMPLES; i++)
        buffer[sampleCount++] = pretrigBuf[(start + i) % PRETRIGGER_SAMPLES];
}

// ---------------- Peak-centered trimming ----------------
// The raw [motionStartTime, now] window can include slow wind-up or
// return-to-guard motion swept in alongside the actual strike (anything
// energetic enough to cross the START threshold extends the segment). Rather
// than compute stats/shape over the whole raw window, find the timestamp of
// peak activity and use a fixed window around THAT — this reliably isolates
// the strike regardless of how the player moves before/after it.
const unsigned long PRE_PEAK_MS = 150;
const unsigned long POST_PEAK_MS = 350;

int findPeakActivityIndex()
{
    int peakIdx = 0;
    float peakScore = -1;
    for (int i = 0; i < sampleCount; i++)
    {
        float gyroMag = vecMagnitude(buffer[i].gx, buffer[i].gy, buffer[i].gz);
        float accelMag = vecMagnitude(buffer[i].ax, buffer[i].ay, buffer[i].az);
        float jerk = fabs(accelMag - 1.0);
        // Normalize against each signal's own start threshold so either a
        // rotation-heavy swing or a jerk-heavy thrust can be "the peak".
        float score = max(gyroMag / START_THRESHOLD, jerk / JERK_START_THRESHOLD);
        if (score > peakScore)
        {
            peakScore = score;
            peakIdx = i;
        }
    }
    return peakIdx;
}

// Fills trimStart/trimEnd with inclusive indices into buffer[] spanning
// PRE_PEAK_MS before and POST_PEAK_MS after the peak-activity sample,
// clipped to the buffer's actual bounds.
void computeTrimmedRange(int &trimStart, int &trimEnd)
{
    int peakIdx = findPeakActivityIndex();
    uint32_t peakT = buffer[peakIdx].t;

    trimStart = peakIdx;
    while (trimStart > 0 && (peakT - buffer[trimStart - 1].t) < PRE_PEAK_MS)
        trimStart--;

    trimEnd = peakIdx;
    while (trimEnd < sampleCount - 1 && (buffer[trimEnd + 1].t - peakT) < POST_PEAK_MS)
        trimEnd++;
}

// Dumps every raw sample in the current segment, marking which ones fall
// inside the peak-trimmed range with a '*' — lets you see exactly what got
// trimmed vs. kept, and the actual raw numbers behind a given accept/reject.
void dumpSegmentSamples(int trimStart, int trimEnd)
{
    if (!DEBUG_SEGMENT_DUMP)
        return;
    for (int i = 0; i < sampleCount; i++)
    {
        float gyroMag = vecMagnitude(buffer[i].gx, buffer[i].gy, buffer[i].gz);
        float accelMag = vecMagnitude(buffer[i].ax, buffer[i].ay, buffer[i].az);
        float jerk = fabs(accelMag - 1.0);
        bool inTrim = (i >= trimStart && i <= trimEnd);
        debugLog("[seg] i=" + String(i) + (inTrim ? "*" : " ") +
                 " t=" + String(buffer[i].t) +
                 " ax=" + String(buffer[i].ax, 3) + " ay=" + String(buffer[i].ay, 3) + " az=" + String(buffer[i].az, 3) +
                 " gx=" + String(buffer[i].gx, 1) + " gy=" + String(buffer[i].gy, 1) + " gz=" + String(buffer[i].gz, 1) +
                 " gyroMag=" + String(gyroMag, 1) + " jerk=" + String(jerk, 2));
    }
}

// ---------------- Stats + shape (validity checks) ----------------
// Both now operate on an explicit [startIdx, endIdx] range (inclusive) into
// buffer[] rather than always the full raw segment — call with the
// peak-trimmed range from computeTrimmedRange().
MotionStats computeStats(int startIdx, int endIdx)
{
    MotionStats s = {0, 0, endIdx - startIdx + 1};
    for (int i = startIdx; i <= endIdx; i++)
    {
        float mag = vecMagnitude(buffer[i].gx, buffer[i].gy, buffer[i].gz);
        if (mag > s.peakGyroMag)
            s.peakGyroMag = mag;
    }
    s.duration = endIdx > startIdx ? (buffer[endIdx].t - buffer[startIdx].t) : 0;
    return s;
}

MotionShape computeShape(int startIdx, int endIdx)
{
    MotionShape s = {0, 0, 0, 0, 0, 0};
    if (endIdx <= startIdx)
        return s;
    float prevT = buffer[startIdx].t;

    for (int i = startIdx; i <= endIdx; i++)
    {
        float dt = (buffer[i].t - prevT) / 1000.0;
        prevT = buffer[i].t;

        s.totalRotX += buffer[i].gx * dt;
        s.totalRotY += buffer[i].gy * dt;
        s.totalRotZ += buffer[i].gz * dt;

        float accelMag = vecMagnitude(buffer[i].ax, buffer[i].ay, buffer[i].az);
        float linearAccel = fabs(accelMag - 1.0);
        if (linearAccel > s.peakLinearAccel)
            s.peakLinearAccel = linearAccel;
    }

    s.totalRotation = sqrt(s.totalRotX * s.totalRotX + s.totalRotY * s.totalRotY + s.totalRotZ * s.totalRotZ);

    float maxAxis = max(fabs(s.totalRotX), max(fabs(s.totalRotY), fabs(s.totalRotZ)));
    s.dominantAxisCount = 0;
    if (fabs(s.totalRotX) > 0.25 * maxAxis)
        s.dominantAxisCount++;
    if (fabs(s.totalRotY) > 0.25 * maxAxis)
        s.dominantAxisCount++;
    if (fabs(s.totalRotZ) > 0.25 * maxAxis)
        s.dominantAxisCount++;

    return s;
}

// ---------------- Shape instrumentation (diagnostic only, not gated yet) ----------------
// Peak magnitude/duration/rotation-amount can't separate a real committed
// swing from a fast wrist shake/flick — both produce a similar-sized burst
// (confirmed against real negative-example data: shake/flick peaks of
// 327-434 dps were indistinguishable from real swing peaks). These two
// candidate features are logged so a real-vs-fake batch can be compared
// before either becomes an actual gate — same discipline used to set the
// torso gate and spin threshold from real data instead of a guess.
// (ShapeExtras struct itself is declared up top near MotionStats/MotionShape
// — see the comment there for why.)
//
// Hypotheses being tested:
//   sustainFraction — a real swing (whole arm through an arc) should keep
//     rotation elevated over more of the window; a wrist-only shake/flick is
//     a narrower spike that decays fast.
//   reversalCount — a real swing is one coherent arc (0-1 reversals); a
//     shake oscillates back and forth (multiple reversals). Deadband avoids
//     counting sensor noise near zero as a reversal.
// See computeRampMs() below for the third feature (rampMs) — added later,
// after reversals/sustainFraction turned out not to separate real swings
// from slow/broad "move the arm around" fakes (both can have low reversals
// and a high sustainFraction; neither captures how FAST the motion ramped
// up to its peak).
ShapeExtras computeShapeExtras(int startIdx, int endIdx, float peakGyroMag)
{
    ShapeExtras e = {0, 0, 0};
    if (endIdx <= startIdx || peakGyroMag <= 0)
        return e;

    const float REVERSAL_DEADBAND_DPS = 20.0;

    // Dominant axis = whichever axis reaches the largest single-sample
    // magnitude anywhere in the window; reversals are counted on that one
    // axis for the whole window.
    float maxAbs = -1;
    int dominantAxis = 0; // 0=x, 1=y, 2=z
    for (int i = startIdx; i <= endIdx; i++)
    {
        float axVal[3] = {buffer[i].gx, buffer[i].gy, buffer[i].gz};
        for (int a = 0; a < 3; a++)
        {
            if (fabs(axVal[a]) > maxAbs)
            {
                maxAbs = fabs(axVal[a]);
                dominantAxis = a;
            }
        }
    }

    int sustainedCount = 0;
    int lastSign = 0; // -1, 0, +1
    for (int i = startIdx; i <= endIdx; i++)
    {
        float gyroMag = vecMagnitude(buffer[i].gx, buffer[i].gy, buffer[i].gz);
        if (gyroMag > 0.5 * peakGyroMag)
            sustainedCount++;

        float g = (dominantAxis == 0) ? buffer[i].gx : (dominantAxis == 1 ? buffer[i].gy : buffer[i].gz);
        int sign = 0;
        if (g > REVERSAL_DEADBAND_DPS)
            sign = 1;
        else if (g < -REVERSAL_DEADBAND_DPS)
            sign = -1;

        if (sign != 0)
        {
            if (lastSign != 0 && sign != lastSign)
                e.reversalCount++;
            lastSign = sign;
        }
    }

    e.sustainFraction = (float)sustainedCount / (endIdx - startIdx + 1);
    return e;
}

// rampMs — diagnostic only, logged alongside sustainFraction/reversalCount,
// not gated yet. Hypothesis: a real committed strike ramps up to peak
// rotation fast (the player commits explosively); a slow, broad "move the
// arm/board around" motion can reach a similar peak magnitude but climbs to
// it much more gradually — a different axis than sustainFraction (how much
// of the window sits near peak) or reversalCount (how many times direction
// flips).
//
// Scans the FULL RAW buffer (not the trimmed window — same reasoning as
// spinLikely/rawShape: the fixed 150ms pre-peak trim would otherwise cap
// what's measurable) from the segment start up to the peak-activity sample,
// finds the first point that already reached half the eventual peak, and
// returns the elapsed time from there to the peak. peakIdx is the index
// returned by findPeakActivityIndex() — pass it in rather than recomputing,
// since onMotionEnd() already calls that once for computeTrimmedRange().
unsigned long computeRampMs(int peakIdx, float peakGyroMag)
{
    if (peakIdx <= 0 || peakGyroMag <= 0)
        return 0;

    float halfPeak = 0.5 * peakGyroMag;
    for (int i = 0; i <= peakIdx; i++)
    {
        float gyroMag = vecMagnitude(buffer[i].gx, buffer[i].gy, buffer[i].gz);
        if (gyroMag >= halfPeak)
            return buffer[peakIdx].t - buffer[i].t;
    }
    // Never reached half-peak before the peak sample itself (shouldn't
    // normally happen since the peak sample trivially satisfies the
    // condition) — fall back to the full pre-peak span.
    return buffer[peakIdx].t - buffer[0].t;
}

bool isValidAttack(MotionStats s, MotionShape shape)
{
    // Intensity floor — this is what actually separates a real strike from a
    // controlled/deliberate motion like raising the sword to guard. A guard
    // raise is purposeful (passes the rotation-floor check in
    // matchesKnownPattern) but not violent — real accepted swings in testing
    // consistently peaked at 350-440 dps / 1.3-2.5g, well above this.
    // Both floors are now required (was: reject only if BOTH were low). In the
    // 2026-09-29 batch (~10 reps per action + fakes) every fake that got
    // through peaked >=188 dps, so the gyro floor alone never stopped one;
    // peakLinearAccel is what separates them: every accepted real rep hit
    // >=1.28g, while only 2 of 30 accepted fakes (walking/handling/body turn/
    // flick) reached 1.0g. 1.0 sits between, thin margin on the real side —
    // re-check on the next batch.
    const float MIN_PEAK_GYRO_DPS = 180.0;
    const float MIN_PEAK_JERK_G = 1.0;
    const unsigned long MIN_DURATION = 80;
    const unsigned long MAX_DURATION = 1500;
    const int MIN_SAMPLES = 5;

    if (s.duration < MIN_DURATION || s.duration > MAX_DURATION)
        return false;
    if (s.sampleCount < MIN_SAMPLES)
        return false;
    if (s.peakGyroMag < MIN_PEAK_GYRO_DPS || shape.peakLinearAccel < MIN_PEAK_JERK_G)
        return false; // not violent enough — e.g. raising to guard, walking, handling
    return true;
}

// This used to try to bucket motion into thrust/swing/spin shapes. Dropped
// that — real human wrist/arm motion couples across axes mechanically, so
// axis-dominance rejected plenty of genuine hard swings (see debug logs:
// peak=425+, clearly deliberate, axes=3, rejected). Classifying WHICH action
// this is belongs to the Ultra96 model, which sees the full waveform, not a
// single scalar. This just filters out idle jitter/noise from real intent.
bool matchesKnownPattern(MotionShape s)
{
    const float MIN_MEANINGFUL_ROTATION = 15.0; // deg, floor to reject idle drift
    const float MIN_MEANINGFUL_JERK = 0.4;      // g, catches low-rotation Thrust
    return (s.totalRotation > MIN_MEANINGFUL_ROTATION) || (s.peakLinearAccel > MIN_MEANINGFUL_JERK);
}

// ---------------- Segment -> Ultra96 (placeholder) ----------------
// Samples for this segment live in buffer[sendStart..sendEnd] — the trimmed
// window, or the whole raw buffer for a spin (see onMotionEnd()). Not
// wiring up the actual transmit yet — just logging so you can confirm
// accept/reject behaviour first. When ready, loop over that range and send
// SegmentSampleMsg packets (see CHANGES.md), same pattern as torso's
// sendMatchingSamplesToUltra96().
void sendSegmentToUltra96(uint32_t segId, int sendStart, int sendEnd)
{
    debugLog("[Ultra96 send skipped] would send " + String(sendEnd - sendStart + 1) +
             " wrist samples for seg=" + String(segId));
}

// ---------------- Motion event handlers ----------------
void onMotionStart(unsigned long now)
{
    seedSegmentFromPretrigger();
    motionStartTime = now;
    candidatePeakScore = 0;
    candidateSettleTimerResetAt = now;
    ledMotionActive();
    debugLog("Motion STARTED at " + String(now));
}

void onMotionEnd(unsigned long now)
{
    int trimStart, trimEnd;
    computeTrimmedRange(trimStart, trimEnd);

    // Recomputed rather than threaded out of computeTrimmedRange() — cheap
    // (single pass over <=500 samples) and keeps that function's signature
    // unchanged. Needed by computeRampMs() below.
    int peakIdx = findPeakActivityIndex();

    MotionStats stats = computeStats(trimStart, trimEnd);
    MotionShape shape = computeShape(trimStart, trimEnd);

    bool magnitudeOK = isValidAttack(stats, shape);
    bool shapeOK = matchesKnownPattern(shape);

    // Spin detection uses the FULL raw window, not the trimmed one — a real
    // spin takes much longer to complete than the fixed post-peak trim
    // (350ms), so trimmed totalRotation was never reaching the spin
    // threshold even for genuine spins (observed: real spin reps topped out
    // around totalRot=150 trimmed, despite obviously being spins). This also
    // depends on MAX_SAMPLES actually being big enough to hold the whole
    // spin — bumped from 250 to 500 for the same reason.
    MotionShape rawShape = computeShape(0, sampleCount - 1);
    bool spinLikely = rawShape.totalRotation > 300.0;

    // Torso gate is checked against the TRIMMED window, so wind-up/
    // return-to-guard torso movement doesn't spuriously fail the gate.
    uint32_t torsoStart = toTorsoClock(buffer[trimStart].t);
    uint32_t torsoEnd = toTorsoClock(buffer[trimEnd].t);
    bool haveTorsoData = torsoDataAvailable(torsoStart, torsoEnd);
    float torsoPeak = torsoPeakInWindow(torsoStart, torsoEnd);

    // What gets sent to the Ultra96 (wrist samples + the torso window we ask
    // for). A spin is sent whole — the full raw buffer, turn included — since
    // the 500ms trim only holds the final swing and would look just like a
    // horizontal. Everything else sends the trimmed window. The gates above
    // and below still use the trimmed window they were tuned on.
    int sendStart = spinLikely ? 0 : trimStart;
    int sendEnd = spinLikely ? sampleCount - 1 : trimEnd;
    uint32_t sendTorsoStart = toTorsoClock(buffer[sendStart].t);
    uint32_t sendTorsoEnd = toTorsoClock(buffer[sendEnd].t);

    float torsoGateMax = spinLikely ? TORSO_GATE_MAX_SPIN : TORSO_GATE_MAX_NORMAL;
    bool torsoCeilingOK = torsoPeak < torsoGateMax;
    // See MIN_TORSO_ENGAGEMENT above — the floor side of the torso gate.
    // Exempt for Spin, which already requires high torso rotation via its
    // own (much higher) gate.
    bool torsoEngagementOK = spinLikely || (torsoPeak >= MIN_TORSO_ENGAGEMENT);
    bool torsoOK = haveTorsoData && torsoCeilingOK && torsoEngagementOK;

    // Raw-window sanity cap — see RAW_MAX_DURATION_MS above. Computed over
    // the FULL raw buffer (not the trimmed range), since the whole point is
    // to catch sustained motion that the trim would otherwise hide.
    unsigned long rawDurationMs = (sampleCount > 1) ? (buffer[sampleCount - 1].t - buffer[0].t) : 0;
    bool rawDurationOK = rawDurationMs <= RAW_MAX_DURATION_MS;

    ShapeExtras extras = computeShapeExtras(trimStart, trimEnd, stats.peakGyroMag);
    // Diagnostic only for now — see computeRampMs() above.
    extras.rampMs = computeRampMs(peakIdx, stats.peakGyroMag);

    // Which existing validity sub-rule applies — see the const comments
    // above. Not action classification: just choosing between the
    // spin/thrust/swing shape gates, same pattern spinLikely already set.
    bool thrustLikely = shape.totalRotation < THRUST_MAX_ROTATION_DEG;

    // Swing-shape gate only applies to the "neither spin nor thrust" bucket.
    // Forced true (bypassed) for spinLikely/thrustLikely candidates.
    bool swingShapeOK = spinLikely || thrustLikely ||
                        (extras.reversalCount <= SWING_MAX_REVERSALS &&
                         extras.sustainFraction >= SWING_MIN_SUSTAIN_FRACTION);

    // Only meaningful when spinLikely; always true otherwise.
    bool spinReversalOK = !spinLikely || (extras.reversalCount <= SPIN_MAX_REVERSALS);

    debugLog("Motion ENDED. weapon=" + String(weaponName(lockedWeapon)) +
             " raw=" + String(sampleCount) +
             " rawDur=" + String(rawDurationMs) +
             " trimmed=[" + String(trimStart) + "," + String(trimEnd) + "]" +
             " sent=[" + String(sendStart) + "," + String(sendEnd) + "]" +
             " peak=" + String(stats.peakGyroMag, 1) +
             " dur=" + String(stats.duration) +
             " totalRot=" + String(shape.totalRotation, 1) +
             " rawTotalRot=" + String(rawShape.totalRotation, 1) +
             " peakAccel=" + String(shape.peakLinearAccel, 2) +
             " axes=" + String(shape.dominantAxisCount) +
             " sustainFrac=" + String(extras.sustainFraction, 2) +
             " reversals=" + String(extras.reversalCount) +
             " rampMs=" + String(extras.rampMs) +
             " magOK=" + String(magnitudeOK) +
             " shapeOK=" + String(shapeOK) +
             " spinLikely=" + String(spinLikely) +
             " thrustLikely=" + String(thrustLikely) +
             " swingShapeOK=" + String(swingShapeOK) +
             " spinReversalOK=" + String(spinReversalOK) +
             " torsoPeak=" + String(torsoPeak, 1) +
             " torsoDataOK=" + String(haveTorsoData) +
             " torsoCeilingOK=" + String(torsoCeilingOK) +
             " torsoEngagementOK=" + String(torsoEngagementOK) +
             " torsoOK=" + String(torsoOK) +
             " rawDurationOK=" + String(rawDurationOK));

    dumpSegmentSamples(trimStart, trimEnd);

    if (magnitudeOK && shapeOK && torsoOK && rawDurationOK && swingShapeOK && spinReversalOK)
    {
        uint32_t segId = ((uint32_t)WRIST_ID << 28) ^ (now & 0x0FFFFFFF);
        sendSegmentRequestToTorso(segId, sendTorsoStart, sendTorsoEnd);
        sendSegmentToUltra96(segId, sendStart, sendEnd);
        resultLog("action_accepted weapon=" + String(weaponName(lockedWeapon)) + " seg=" + String(segId));
        ledFlashAccept();
        // Local placeholder — later driven by COMBAT_FEEDBACK from the laptop
        // (see onFeedbackEvent()).
        localFeedback(FB_SWING_ACCEPTED, 0);
    }
    else
    {
        if (!haveTorsoData)
            debugLog("REJECTED — no torso beacon data in window (check torso link/power)");
        else if (!torsoEngagementOK)
            debugLog("REJECTED — torsoPeak=" + String(torsoPeak, 1) + " below " + String(MIN_TORSO_ENGAGEMENT, 1) + " min (wrist-only motion, no body engagement — likely a flick)");
        else if (!rawDurationOK)
            debugLog("REJECTED — raw duration " + String(rawDurationMs) + "ms exceeds " + String(RAW_MAX_DURATION_MS) + "ms cap (sustained motion, not a clean strike)");
        else if (!swingShapeOK)
            debugLog("REJECTED — reversals=" + String(extras.reversalCount) + "/sustainFrac=" + String(extras.sustainFraction, 2) + " doesn't look like a single coherent swing");
        else if (!spinReversalOK)
            debugLog("REJECTED — spinLikely but reversals=" + String(extras.reversalCount) + " (max " + String(SPIN_MAX_REVERSALS) + " for a real spin)");
        else
            debugLog("REJECTED");
        ledFlashReject();
    }
}

void bufferSample(const Sample &s)
{
    if (state == ACTIVE && sampleCount < MAX_SAMPLES)
        buffer[sampleCount++] = s;
}

float vecMagnitude(float x, float y, float z) { return sqrt(x * x + y * y + z * z); }

bool activityHigh(float gyroMag, float jerk)
{
    return (gyroMag > START_THRESHOLD) || (jerk > JERK_START_THRESHOLD);
}

void updateMotionState(float gyroMag, float jerk, unsigned long now)
{
    if (state == IDLE)
    {
        if (now >= cooldownUntil && activityHigh(gyroMag, jerk))
        {
            state = ACTIVE;
            onMotionStart(now);
        }
    }
    else
    {
        // Peak-relative settle tracking — see SETTLE_FRACTION above.
        // Normalized the same way findPeakActivityIndex() scores samples,
        // so a rotation-heavy swing and a jerk-heavy thrust both track
        // correctly.
        float score = max(gyroMag / START_THRESHOLD, jerk / JERK_START_THRESHOLD);
        if (score > candidatePeakScore)
            candidatePeakScore = score;
        if (score >= SETTLE_FRACTION * candidatePeakScore)
            candidateSettleTimerResetAt = now;

        bool settled = (now - candidateSettleTimerResetAt) >= POST_PEAK_MS;
        // Safety nets, same spirit as before: don't let a candidate stay
        // open forever on sustained non-peaky motion (RAW_MAX_DURATION_MS,
        // same value the rawDurationOK gate already checks against) or
        // overrun the buffer.
        bool tooLong = (now - motionStartTime) >= RAW_MAX_DURATION_MS;
        bool bufferFull = sampleCount >= MAX_SAMPLES - 1;

        if (settled || tooLong || bufferFull)
        {
            debugLog("[candidate] closing: " + String(settled ? "settled" : (tooLong ? "timeout" : "bufferFull")));
            state = IDLE;
            cooldownUntil = now + CANDIDATE_COOLDOWN_MS;
            onMotionEnd(now);
        }
    }
}

// ================= PHASE TICKS =================

// Switches to the valid-action algorithm with a clean slate. Stays locked
// until reboot.
void lockWeapon(WeaponType w, unsigned long now)
{
    lockedWeapon = w;
    phase = PHASE_LOCKED;
    resultLog("weapon_locked=" + String(weaponName(w)));

    // Nothing was buffered for segmentation before now, but reset anyway so
    // the first candidate can't pick up stale samples.
    state = IDLE;
    sampleCount = 0;
    pretrigHead = 0;
    pretrigFilled = false;
    // The player drops out of the ready stance right after the lock — don't
    // let that first arm movement open a candidate.
    cooldownUntil = now + 500;

    localFeedback(FB_WEAPON_LOCKED, (uint8_t)w); // 2 buzzes + weapon colour
}

// Which gate stopped this tick from classifying as the current candidate.
// Lists every failing gate, e.g. "r_unstable,l_stale".
String detectionMissReason(WeaponType w, bool rightStable, bool lFresh, bool tFresh, float matchDist)
{
    if (w != WEAPON_UNKNOWN)
        return String("other_weapon=") + weaponName(w);
    String r;
    if (!rightStable)
        r += "r_unstable,";
    if (!lFresh)
        r += "l_stale,";
    else if (!leftStable)
        r += "l_unstable,";
    if (!tFresh)
        r += "t_stale,";
    if (r.length() == 0)
        return matchDist > WEAPON_MATCH_MAX_DEG ? "too_far" : "ambiguous"; // see classifyWeapon()
    r.remove(r.length() - 1);
    return r;
}

// Both wrists' gravity directions in the same (el, az) form as WEAPON_REFS,
// for the weapon milestone lines.
String poseString(const GravDir &right)
{
    return " rEl=" + String(gravElDeg(right), 1) + " rAz=" + String(gravAzDeg(right), 1) +
           " lEl=" + String(leftEl, 1) + " lAz=" + String(leftAz, 1);
}

// PHASE_WAITING / PHASE_DETECTING: same classification as
// right_wrist_weapon.ino, plus the continuous-hold lock timer.
void runDetectionTick(float ax_g, float ay_g, float az_g, float gyroMag, unsigned long now)
{
    bool rightStable = updateOwnPose(ax_g, ay_g, az_g, gyroMag, now);
    bool lFresh = leftFresh(now);
    bool tFresh = torsoFresh(now);

    // Classified every tick (for the pose stream's nearest/dist/margin); only
    // counts toward the lock when both wrists are fresh and stable and the
    // torso is fresh.
    GravDir right = gravRight();
    WeaponType nearest;
    float matchDist, matchMargin;
    WeaponType match = classifyWeapon(right, gravFromElAz(leftEl, leftAz), nearest, matchDist, matchMargin);

    if (phase == PHASE_WAITING)
    {
        if (lFresh && tFresh)
        {
            phase = PHASE_DETECTING;
            resultLog("all_nodes_ready");
            localFeedback(FB_ALL_NODES_READY, 0);
        }
    }
    else
    {
        // torsoStable is deliberately not required: classification only uses
        // the two wrists (see WEAPON_REFS), and the torso's stillness gate
        // (15 dps) was far stricter than the wrists' (100). The torso still
        // has to be fresh — the LOCKED phase needs its beacons.
        WeaponType w = WEAPON_UNKNOWN;
        if (rightStable && lFresh && leftStable && tFresh)
            w = match;

        // Lock timer: the same weapon must hold for WEAPON_LOCK_HOLD_MS, but
        // unknown/stale/unstable/different readings shorter than
        // WEAPON_DROPOUT_GRACE_MS are ridden through instead of resetting it.
        if (candidateWeapon != WEAPON_UNKNOWN)
        {
            if (w == candidateWeapon)
            {
                candidateLastSeenMs = now;
                if (now - candidateSinceMs >= WEAPON_LOCK_HOLD_MS)
                {
                    lockWeapon(w, now);
                    return;
                }
            }
            else
            {
                if (now - candidateLastSeenMs > WEAPON_DROPOUT_GRACE_MS)
                {
                    resultLog("weapon_candidate_lost (was " + String(weaponName(candidateWeapon)) +
                              " heldMs=" + String(candidateLastSeenMs - candidateSinceMs) +
                              " reason=" + detectionMissReason(w, rightStable, lFresh, tFresh, matchDist) +
                              " nearest=" + weaponName(nearest) + " dist=" + String(matchDist, 1) +
                              " margin=" + String(matchMargin, 1) + poseString(right) + ")");
                    candidateWeapon = WEAPON_UNKNOWN;
                }
            }
        }
        if (candidateWeapon == WEAPON_UNKNOWN && w != WEAPON_UNKNOWN)
        {
            candidateWeapon = w;
            candidateSinceMs = now;
            candidateLastSeenMs = now;
            resultLog("weapon_candidate=" + String(weaponName(w)) + " dist=" + String(matchDist, 1) +
                      " margin=" + String(matchMargin, 1) + poseString(right));
        }

        bool holding = (candidateWeapon != WEAPON_UNKNOWN);
        if (holding && ledState != LED_HOLDING)
            ledHolding();
        else if (!holding && ledState != LED_SAMPLING)
            ledSampling();
    }

#if DEBUG_POSE_STREAM
    if (now - lastPoseLogMs >= POSE_LOG_INTERVAL_MS)
    {
        lastPoseLogMs = now;
        debugLog("raw rEl=" + String(gravElDeg(right), 1) + " rAz=" + String(gravAzDeg(right), 1) +
                 " rGyroMag=" + String(gyroMagEMA, 1) + " rStable=" + String(rightStable ? 1 : 0) +
                 " | left el=" + String(leftEl, 1) + " az=" + String(leftAz, 1) +
                 " fresh=" + String(lFresh ? 1 : 0) + " stable=" + String(leftStable ? 1 : 0) +
                 " | torso pitch=" + String(torsoPitch, 1) + " roll=" + String(torsoRoll, 1) +
                 " fresh=" + String(tFresh ? 1 : 0) + " stable=" + String(torsoStable ? 1 : 0) +
                 " | nearest=" + String(weaponName(nearest)) +
                 (nearest != WEAPON_UNKNOWN ? " dist=" + String(matchDist, 1) + " margin=" + String(matchMargin, 1)
                                            : String("")) +
                 " candidate=" + String(weaponName(candidateWeapon)) +
                 (candidateWeapon != WEAPON_UNKNOWN ? " heldMs=" + String(now - candidateSinceMs) : String("")));
    }
#endif
}

// PHASE_LOCKED: the valid-action algorithm, unchanged from right_wrist.ino.
void runValidActionTick(const Sample &s, unsigned long now)
{
    storePretrigger(s);
    bufferSample(s);

    float gyroMag = vecMagnitude(s.gx, s.gy, s.gz);
    float accelMag = vecMagnitude(s.ax, s.ay, s.az);
    float jerk = fabs(accelMag - 1.0);

    if (DEBUG_RAW_STREAM)
    {
        static unsigned long lastRawLogMs = 0;
        if (now - lastRawLogMs >= RAW_LOG_INTERVAL_MS)
        {
            lastRawLogMs = now;
            debugLog("[raw] t=" + String(now) +
                     " ax=" + String(s.ax, 3) + " ay=" + String(s.ay, 3) + " az=" + String(s.az, 3) +
                     " gx=" + String(s.gx, 1) + " gy=" + String(s.gy, 1) + " gz=" + String(s.gz, 1) +
                     " gyroMag=" + String(gyroMag, 1) + " jerk=" + String(jerk, 2) +
                     " state=" + String(state == ACTIVE ? "ACTIVE" : "IDLE"));
        }
    }

    updateMotionState(gyroMag, jerk, now);
}

// ================= SETUP / LOOP =================
const unsigned long SAMPLE_PERIOD_US = 10000UL; // 100Hz, fixed-period via micros()
unsigned long lastSampleUs = 0;

void setup()
{
    Serial.begin(115200);
    Wire.begin();
    Wire.setClock(400000); // MPU6500 supports 400kHz fast-mode I2C

    pinMode(LED_R_PIN, OUTPUT);
    pinMode(LED_G_PIN, OUTPUT);
    pinMode(LED_B_PIN, OUTPUT);
    ledBooting();

    motorSetup();

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

    debugUdp.begin(DEBUG_PORT);
    linkUdp.begin(LINK_PORT);
    poseUdp.begin(POSE_PORT);

    writeRegister(REG_PWR_MGMT_1, 0x00);
    delay(100);
    writeRegister(REG_GYRO_CONFIG, 0x18);  // FS_SEL=11 -> +/-2000dps
    writeRegister(REG_ACCEL_CONFIG, 0x10); // AFS_SEL=10 -> +/-8g

    calibrateGyro();
    delay(100);

    phase = PHASE_WAITING;
    localFeedback(FB_WAITING_FOR_NODES, 0);
    debugLog("Right-wrist game hub initialized at IP " + WiFi.localIP().toString());
    debugLog(String("[cfg] wifi_sleep=") + (WIFI_SLEEP_DISABLED ? "off" : "on") +
             " latency_test=" + String(LATENCY_TEST) + " player=" + String(PLAYER_ID));
}

void loop()
{
    checkIncomingPose();
    checkIncomingLink();
    serviceSegmentRequests(millis());
    printLinkStats(millis());
#if LATENCY_TEST
    runLatencyTest(millis());
#endif
    updateLed();
    updateFeedback(millis());

    unsigned long nowUs = micros();
    if (nowUs - lastSampleUs < SAMPLE_PERIOD_US)
        return;
    lastSampleUs = nowUs;

    unsigned long now = millis();
    broadcastHubStatus(now);

    readSuccess = true;
    int16_t raw[7];
    if (!readIMUBurst(raw))
    {
        // Skip this tick. A failed read also counts as "not still", so the
        // weapon lock needs STILLNESS_HOLD_MS of unbroken good reads (same
        // guard as the left wrist).
        poseStillSinceMs = now;
        return;
    }

    float ax_g = raw[0] / ACCEL_SCALE;
    float ay_g = raw[1] / ACCEL_SCALE;
    float az_g = raw[2] / ACCEL_SCALE;
    float gx_dps = (raw[4] / GYRO_SCALE) - gyroXoffset;
    float gy_dps = (raw[5] / GYRO_SCALE) - gyroYoffset;
    float gz_dps = (raw[6] / GYRO_SCALE) - gyroZoffset;

    if (phase == PHASE_LOCKED)
    {
        Sample s = {(uint32_t)now, ax_g, ay_g, az_g, gx_dps, gy_dps, gz_dps};
        runValidActionTick(s, now);
    }
    else
    {
        runDetectionTick(ax_g, ay_g, az_g, vecMagnitude(gx_dps, gy_dps, gz_dps), now);
    }
}
