#ifdef CG_HARDWARE_GATEWAY
#include "gateway_boundary.h"
#endif
// ============================================================================
// RIGHT WRIST NODE (hub) — weapon detection -> weapon lock -> valid-action
// detection.
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
//   PHASE_LOCKED    — weapon fixed until reboot. Segments the wrist's motion
//                     into candidate actions and accepts or rejects each one,
//                     using torso beacons on LINK_PORT.
//
// The hub broadcasts HubStatus on POSE_PORT every 100ms in every phase, so
// left wrist + torso follow the phase (and late joiners resync on their own).
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
// 4210-4214, player 2: 4220-4224), so the two sets of boards never see
// each other's broadcasts and the laptop can listen to each separately.
#define PLAYER_PORT_OFFSET ((PLAYER_ID - 1) * 10)

// 1 = latency instrumentation: PING the torso every 100ms and log
// "[rtt] us=<n>", and log TorsoBeacon inter-arrival times as
// "[beacon_iat] us=<n>,<n>,...". Must match the torso sketch, which answers
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

// 1 = stream every IMU sample to the laptop on RAW_PORT, in every phase, for
// offline waveform analysis (listener writes raw_<time>_<label>.csv). Gates
// and segmentation are unchanged. Must match the torso sketch to get both
// streams. 0 for normal play.
#ifndef RAW_STREAM
#define RAW_STREAM 1
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
#define THIS_DEVICE_ID RIGHT_WRIST_ID
#define WRIST_ID RIGHT_WRIST_ID // also seeds segId

#define MOTOR_PIN 4 // haptic ERM via 2N7000 low-side switch (PWM)

IPAddress broadcastIP;                             // subnet broadcast, computed in setup() once connected
const int LINK_PORT = 4210 + PLAYER_PORT_OFFSET;   // <-> torso (beacons, segment requests/acks), used once LOCKED
const int DEBUG_PORT = 4211 + PLAYER_PORT_OFFSET;  // -> laptop listener (debug lines)
const int POSE_PORT = 4212 + PLAYER_PORT_OFFSET;   // <- left wrist + torso poses, -> HubStatus broadcast
const int RESULT_PORT = 4213 + PLAYER_PORT_OFFSET; // -> laptop listener (milestone lines only)
const int RAW_PORT = 4214 + PLAYER_PORT_OFFSET;    // -> laptop listener (RawBatch, RAW_STREAM builds only)

// Same weapon must be classified continuously for this long before it locks.
const unsigned long WEAPON_LOCK_HOLD_MS = 3000;

// Dropout tolerance while holding: the candidate survives up to this long
// without being re-classified (a tremor spike clearing a stable flag, one
// late pose packet, a noisy tick across a threshold) before it's dropped and
// the lock timer restarts. Without it, a single bad 10ms tick anywhere in
// the hold throws the whole hold away.
const unsigned long WEAPON_DROPOUT_GRACE_MS = 500;

#define DEBUG_LOG true

// Live pose print during WAITING/DETECTING. Stops once the weapon is locked.
#define DEBUG_POSE_STREAM true
const unsigned long POSE_LOG_INTERVAL_MS = 300;

WiFiUDP debugUdp;
WiFiUDP linkUdp;
WiFiUDP poseUdp;

// ---------------- Local types ----------------
// Every type a function takes or returns is declared here, before any
// function: the Arduino IDE inserts auto-generated prototypes right after the
// includes, so a type declared further down isn't visible to them.
struct Sample
{
    uint32_t t; // this device's local millis()
    float ax, ay, az, gx, gy, gz;
};

// One coherent rotation inside a candidate — see "Strokes" below.
struct Stroke
{
    int a, b;          // inclusive sample range in buffer[]
    float ux, uy, uz;  // unit rotation axis at the (smoothed) peak
    float hx, hy, hz;  // unit push direction (see pushDirection()), all 0 = none
    float peakDps;     // max |omega| over the stroke
    float rotDeg;      // rotation about (ux, uy, uz) over the stroke
    float pathDeg;     // integral of |omega| over the stroke
    float torsoRotDeg; // torso rotation around the stroke (beacons)
    float torsoRatio;  // torsoRotDeg / pathDeg
    int torsoBeacons;  // beacons found for it (0 = no torso data)
};

// One action's strike template — see STRIKE_MODEL.
struct StrikeTemplate
{
    WeaponType weapon;  // WEAPON_UNKNOWN = every weapon
    const char *action; // logged as a hint only; the AI classifies
    float ux, uy, uz, axisSpreadDeg;
    float px, py, pz, pushSpreadDeg;
    float rotMeanDeg, rotSpreadDeg;
    float torsoRef, torsoSpread;
    bool bidirectional; // v and -v count as the same direction (spin)
};

// Nearest template for a stroke — see nearestTemplate().
struct TemplateMatch
{
    int idx; // into STRIKE_MODEL, -1 = none for this weapon
    float dist, dAxis, dPush, dRot, dTorso;
};

// One push of the hand (linear motion) inside a candidate — see "Push
// strokes" below. How a thrust is found: it barely rotates.
struct PushStroke
{
    int a, b, e;      // push a..b (accel along ux,uy,uz), hand stops at e
    float ux, uy, uz; // unit push direction
    float peakG;      // peak linear acceleration
    float reachM;     // distance the hand travels along the push, a..e
    float rotDeg;     // wrist rotation (integral of |omega|) over a..e
    int torsoBeacons; // beacons found around it (0 = no torso data)
};

// The thrust template — see THRUST_MODEL.
struct ThrustTemplate
{
    float ux, uy, uz, dirSpreadDeg;
    float reachMeanM, reachSpreadM;
    float rotRefDeg, rotSpreadDeg; // one-sided: only rotation above rotRefDeg counts
};

struct WeaponThrustTemplate
{
    WeaponType weapon;
    ThrustTemplate model;
};

struct WeaponActionLimits
{
    WeaponType weapon;
    float strikeMaxDist, thrustMaxDist, minThrustReachM;
};

// An accepted stroke of either kind, for the return / overlap / lead-in
// checks — see "Accepted strokes" below.
struct AcceptedStroke
{
    uint32_t t0, t1;
    float vx, vy, vz; // linear direction: a rotation stroke's push direction, a push's own
    bool spin;        // spins are exempt from return suppression
    bool rotation;    // rotation stroke (else push)
    uint32_t candId;
};

// A wrist's gravity direction as a unit vector — see "Pose (gravity
// direction)" below.
struct GravDir
{
    float x, y, z;
};

// A SegmentRequest waiting for the torso's ack — see "SegmentRequest
// delivery" below.
struct PendingSegReq
{
    bool active;
    uint32_t segId, startTorsoMs, endTorsoMs;
    uint8_t sends;
    unsigned long firstSentMs, lastSentMs;
};

// Feedback pattern player types — see "Feedback patterns" below.
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
// locally by the game logic (see LOCAL_FEEDBACK_TRIGGERS); a feedback
// message from the laptop can call onFeedbackEvent() with the same codes.
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
const unsigned long LED_REJECT_FLASH_MS = 200;

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
        idleR = 0, idleG = 40, idleB = 0; // dim green
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
    // CANDIDATE_COOLDOWN_MS, which would swap the red straight to blue.
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
void updateLed()
{
    if ((ledState == LED_FLASH_ACCEPT || ledState == LED_FLASH_REJECT) && !ledFlashing())
        ledIdle();
}

// ---------------- Haptic motor (2N7000 low-side switch on MOTOR_PIN) ----------------
// Buzzes are played by motorPlayer; see onFeedbackEvent().
void motorSetup()
{
    pinMode(MOTOR_PIN, OUTPUT);
    analogWrite(MOTOR_PIN, 0);
}

// ---------------- Feedback events ----------------
// The single entry point for hub LED + haptic feedback. The game logic calls
// it via localFeedback(). To drive it from the laptop instead, set
// LOCAL_FEEDBACK_TRIGGERS to 0 and call onFeedbackEvent() from the laptop's
// feedback message handler.
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

// +/-8g, +/-2000dps. The narrowest ranges (+/-2g, +/-250dps) clip on real
// swings: peak gyro magnitude saturated at ~433 dps (sqrt(3)*250) and peak
// accel at ~2.46g. Resolution at these ranges (4096 LSB/g, 16.4 LSB/dps) is
// still far finer than any threshold here needs.
const float ACCEL_SCALE = 4096.0;
const float GYRO_SCALE = 16.4;

void writeRegister(uint8_t reg, uint8_t value)
{
    Wire.beginTransmission(MPU6500_ADDR);
    Wire.write(reg);
    Wire.write(value);
    Wire.endTransmission();
}

// Single burst read of accel+temp+gyro instead of 6 separate transactions.
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

// ================= WEAPON DETECTION =================

// ---------------- Pose (gravity direction) ----------------
// A wrist's pose is the direction gravity points in that board's own
// frame: the accel vector, normalised and EMA-smoothed as a vector. Logged
// and compared as elevation/azimuth (el = asin(z), az = atan2(y, x)), the
// same encoding the left wrist sends in WristPose. No boot-time zero, so
// it only depends on how the board is strapped on.
//
// Not pitch = atan2(ay, az) / roll = atan2(-ax, az): both divide by az, so
// in stances where gravity lies near the board's x/y plane (the right arm
// in mace + sword&shield, the left arm in sword&shield) they swing tens of
// degrees for a ~1 degree real tilt.
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
// A wrist holding a prop in a ready stance reads 20-60 (peaks ~75) dps from
// hand tremor alone; moving between stances reads 150-2000. The pose angles
// themselves stay within a few degrees during a hold.
const float STILLNESS_GYRO_THRESHOLD_DPS = 100.0;
const unsigned long STILLNESS_HOLD_MS = 700;

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
// The torso isn't used: it zeroes itself at boot, and its lean only varies
// ~10 degrees between the three stances.
//
// References measured from the el/az pose stream (stable samples of
// recorded holds of each stance) with the left unit strapped BELOW the wrist
// (the shield sits above it). Within-hold spread p95: 8.9 / 7.6 / 4.7
// degrees. Separation: sword&shield-mace 53.5 (right 27.8, left 45.7),
// sword&shield-spear 79.2, spear-mace 112.4. Re-measure whenever a board's
// mounting or a stance changes.
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
// A pose is accepted if the nearest weapon is within WEAPON_MATCH_MAX_DEG
// and beats the runner-up weapon by at least WEAPON_MATCH_MARGIN_DEG. Zones
// can't overlap by construction (only one weapon can be MARGIN closer than
// all others). Straight toward another weapon a pose can drift
// (separation - MARGIN) / 2 — 19.3 deg for sword&shield-mace — and up to
// MAX in any other direction, so a stance is only strict in the directions
// that lead toward another weapon. A loose pose still has to be held
// continuously for WEAPON_LOCK_HOLD_MS, which is what stops passing poses
// from locking.
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
// least-delayed one and the best estimate (an average would be biased low by
// the mean delay). Crystal drift between the boards is tens of ppm, far
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

// Torso BEACON_INTERVAL_MS — each beacon covers this much time.
const float TORSO_BEACON_INTERVAL_S = 0.020;

// Torso rotation (deg) over a window in the TORSO's clock: the sum of beacon
// gyroMag x the beacon interval. A beacon carries the max |omega| since the
// previous one, so this reads slightly high; figpipe/replay.py emulates the
// same thing. count = beacons found (0 = no torso data for the window).
float torsoRotationInWindow(uint32_t startTorsoMs, uint32_t endTorsoMs, int &count)
{
    float sum = 0;
    count = 0;
    for (int i = 0; i < TORSO_BEACON_BUF; i++)
    {
        if (torsoBuf[i].valid && torsoBuf[i].torsoMs >= startTorsoMs && torsoBuf[i].torsoMs <= endTorsoMs)
        {
            sum += torsoBuf[i].gyroMag;
            count++;
        }
    }
    return sum * TORSO_BEACON_INTERVAL_S;
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
// A lost SegmentRequest means the torso's data for that segment is never
// sent on, so each request stays pending until the torso's SegmentAck comes
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
// a thrust with no wrist twist still open a candidate. Low on purpose: a push
// at right angles to gravity barely changes |accel| (slow thrusts measured
// 0.41-0.64), and opening a candidate only buffers — the stroke templates
// decide. 1.3 let only thrusts that also twisted >60 dps through.
const float JERK_START_THRESHOLD = 0.3;

// Candidate end: peak-relative settling. Track the highest score seen this
// candidate (score = max(gyroMag/START_THRESHOLD, jerk/JERK_START_THRESHOLD),
// so either a rotation-heavy swing or a jerk-heavy thrust can be the peak)
// and the last time the score was at or above SETTLE_FRACTION of that peak.
// Once that was SETTLE_HOLD_MS ago, the candidate is done.
//
// A fixed "below X dps for Y ms" end condition assumes the player returns to
// a still baseline between actions. In real play they keep moving
// (repositioning, weaving), so a candidate could never close, or one segment
// could swallow several swings. With settling, a sustained action (e.g.
// Spin) keeps re-crossing its own half-peak and stays open for its whole
// duration, while a strike decays well below its peak and closes promptly.
// See updateMotionState().
const float SETTLE_FRACTION = 0.5;

// Cooldown after closing a candidate before a new one can open — stops a
// strike's own settling tail (e.g. residual prop wobble right after impact)
// from opening a second candidate. Shorter than any real inter-swing gap in
// a combo.
const unsigned long CANDIDATE_COOLDOWN_MS = 150;

// How long the score must stay below SETTLE_FRACTION of its peak before the
// candidate closes. The candidate buffer only has to contain each stroke
// whole; the offline replay found no stroke cut short at any speed with 350.
const unsigned long SETTLE_HOLD_MS = 350;

// Force-closes a candidate that never settles (sustained motion: walking,
// handling). The longest real action measured was a slow spin, ~2.1s.
const unsigned long RAW_MAX_DURATION_MS = 3500;

unsigned long motionStartTime = 0;

// Peak-settle tracking for the currently-open candidate — see
// SETTLE_FRACTION above. Reset in onMotionStart() for each new candidate.
float candidatePeakScore = 0;
unsigned long candidateSettleTimerResetAt = 0;

// No new candidate may open before this time — see CANDIDATE_COOLDOWN_MS.
unsigned long cooldownUntil = 0;

// 5s at 100Hz — room for a whole Spin Attack (2.5s wasn't enough).
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

// ---------------- Strokes ----------------
// A candidate can hold several movements — a strike and its return to guard,
// or a combo. Each one is found as a stroke: a coherent rotation, the run of
// samples around a gyro peak where rotation about that peak's own axis stays
// above STROKE_FRAC of the peak. The biggest unclaimed peak goes first and
// claims its samples; a smaller peak inside a claimed run is part of that
// stroke. Every limit is relative to the stroke's own peak, so a slow swing
// is found exactly like a fast one, and the stroke covers the whole movement
// rather than a fixed window around its peak.
//
// Must match figpipe/raw.py find_strokes_arrays(), which the offline replay
// (figpipe/replay.py) uses to fit STRIKE_MODEL and tune these numbers.
const int STROKE_SMOOTH = 5;                   // moving-average length, samples (odd)
const float STROKE_FRAC = 0.15;                // stroke = rotation about the peak axis > this x peak
const float STROKE_MIN_PEAK_DPS = 80.0;        // only absolute limit: above hand tremor (20-60, peaks ~75)
const float STROKE_MIN_ROT_DEG = 20.0;         // drop twitches
const unsigned long STROKE_MAX_HALF_MS = 3000; // grow at most this far either side of the peak
#define MAX_STROKES 8

float smoothG[MAX_SAMPLES][3]; // smoothed gyro of buffer[], for finding strokes only
bool sampleClaimed[MAX_SAMPLES];
Stroke strokes[MAX_STROKES];
int strokeCount = 0;

// STROKE_SMOOTH-point moving average of buffer[]'s gyro, counting samples
// beyond either end as 0 (np.convolve 'same' in the replay).
void smoothBufferGyro()
{
    const int h = STROKE_SMOOTH / 2;
    for (int i = 0; i < sampleCount; i++)
    {
        float s[3] = {0, 0, 0};
        for (int k = i - h; k <= i + h; k++)
        {
            if (k < 0 || k >= sampleCount)
                continue;
            s[0] += buffer[k].gx;
            s[1] += buffer[k].gy;
            s[2] += buffer[k].gz;
        }
        for (int c = 0; c < 3; c++)
            smoothG[i][c] = s[c] / STROKE_SMOOTH;
    }
}

float smoothMag(int i) { return vecMagnitude(smoothG[i][0], smoothG[i][1], smoothG[i][2]); }
float smoothAbout(int i, float ux, float uy, float uz) { return smoothG[i][0] * ux + smoothG[i][1] * uy + smoothG[i][2] * uz; }

// Fills strokes[0..strokeCount) from buffer[], sorted by start.
void findStrokes()
{
    smoothBufferGyro();
    for (int i = 0; i < sampleCount; i++)
        sampleClaimed[i] = false;
    strokeCount = 0;

    while (strokeCount < MAX_STROKES)
    {
        // Biggest unclaimed peak (first one on a tie).
        int ip = -1;
        float peak = -1;
        for (int i = 0; i < sampleCount; i++)
        {
            if (sampleClaimed[i])
                continue;
            float m = smoothMag(i);
            if (m > peak)
            {
                peak = m;
                ip = i;
            }
        }
        if (ip < 0 || peak < STROKE_MIN_PEAK_DPS)
            break;

        float ux = smoothG[ip][0] / peak, uy = smoothG[ip][1] / peak, uz = smoothG[ip][2] / peak;
        float lim = STROKE_FRAC * peak;
        int a = ip, b = ip;
        while (a > 0 && smoothAbout(a - 1, ux, uy, uz) > lim && buffer[ip].t - buffer[a - 1].t < STROKE_MAX_HALF_MS)
            a--;
        while (b < sampleCount - 1 && smoothAbout(b + 1, ux, uy, uz) > lim && buffer[b + 1].t - buffer[ip].t < STROKE_MAX_HALF_MS)
            b++;

        bool overlaps = false;
        for (int i = a; i <= b; i++)
        {
            overlaps |= sampleClaimed[i];
            sampleClaimed[i] = true;
        }
        if (overlaps)
            continue; // a smaller peak inside a bigger stroke is part of it

        float rot = 0;
        for (int i = a + 1; i <= b; i++)
            rot += 0.5f * (smoothAbout(i, ux, uy, uz) + smoothAbout(i - 1, ux, uy, uz)) *
                   (buffer[i].t - buffer[i - 1].t) / 1000.0f;
        if (rot < STROKE_MIN_ROT_DEG)
            continue;

        Stroke &s = strokes[strokeCount++];
        s = {};
        s.a = a;
        s.b = b;
        s.ux = ux;
        s.uy = uy;
        s.uz = uz;
    }

    // Insertion sort by start sample (at most MAX_STROKES).
    for (int i = 1; i < strokeCount; i++)
        for (int j = i; j > 0 && strokes[j].a < strokes[j - 1].a; j--)
        {
            Stroke tmp = strokes[j];
            strokes[j] = strokes[j - 1];
            strokes[j - 1] = tmp;
        }
}

// ---------------- Stroke features ----------------
// Gravity at the start of a stroke = mean accel over this many samples
// before it (pushDirection()).
const int PUSH_GRAVITY_SAMPLES = 3;
// Torso window = stroke +- this x its duration (at least 100 ms).
const float TORSO_PAD_FRAC = 0.5;

// Direction the hand is pushed in over the first half of the stroke, in the
// board's frame: linear acceleration (accel minus gravity) summed and
// normalised. Gravity starts as the accel just before the stroke and is
// rotated with the gyro sample by sample (dg/dt = -omega x g). A direction,
// not a size, so it doesn't change with speed — it's what tells a thrust
// (a push, little rotation) from handling the weapon.
void pushDirection(Stroke &s)
{
    int g0 = max(0, s.a - PUSH_GRAVITY_SAMPLES);
    float gx = 0, gy = 0, gz = 0;
    for (int i = g0; i <= s.a; i++)
    {
        gx += buffer[i].ax;
        gy += buffer[i].ay;
        gz += buffer[i].az;
    }
    int n = s.a - g0 + 1;
    gx /= n, gy /= n, gz /= n;

    int nHalf = max(1, (s.b - s.a + 1) / 2);
    float sx = 0, sy = 0, sz = 0;
    for (int i = s.a; i < s.a + nHalf; i++)
    {
        float dt = (i > 0) ? (buffer[i].t - buffer[i - 1].t) / 1000.0f : 0.01f;
        float wx = buffer[i].gx * DEG_TO_RAD, wy = buffer[i].gy * DEG_TO_RAD, wz = buffer[i].gz * DEG_TO_RAD;
        float cx = wy * gz - wz * gy, cy = wz * gx - wx * gz, cz = wx * gy - wy * gx; // omega x g
        gx -= cx * dt, gy -= cy * dt, gz -= cz * dt;
        sx += buffer[i].ax - gx;
        sy += buffer[i].ay - gy;
        sz += buffer[i].az - gz;
    }
    float m = vecMagnitude(sx, sy, sz);
    if (m > 1e-9f)
        s.hx = sx / m, s.hy = sy / m, s.hz = sz / m;
    else
        s.hx = s.hy = s.hz = 0;
}

// Rotation, peak, push direction and torso involvement for one stroke.
// closeMs = when the candidate closed: torso beacons after it haven't arrived.
void computeStrokeFeatures(Stroke &s, uint32_t closeMs)
{
    s.peakDps = 0;
    s.rotDeg = 0;
    s.pathDeg = 0;
    for (int i = s.a; i <= s.b; i++)
    {
        float m = vecMagnitude(buffer[i].gx, buffer[i].gy, buffer[i].gz);
        if (m > s.peakDps)
            s.peakDps = m;
        if (i == s.a)
            continue;
        float dt = (buffer[i].t - buffer[i - 1].t) / 1000.0f;
        float w1 = buffer[i].gx * s.ux + buffer[i].gy * s.uy + buffer[i].gz * s.uz;
        float w0 = buffer[i - 1].gx * s.ux + buffer[i - 1].gy * s.uy + buffer[i - 1].gz * s.uz;
        float m0 = vecMagnitude(buffer[i - 1].gx, buffer[i - 1].gy, buffer[i - 1].gz);
        s.rotDeg += 0.5f * (w0 + w1) * dt;
        s.pathDeg += 0.5f * (m0 + m) * dt;
    }
    pushDirection(s);

    float dur = buffer[s.b].t - buffer[s.a].t;
    float pad = TORSO_PAD_FRAC * max(dur, 100.0f);
    uint32_t from = buffer[s.a].t - (uint32_t)pad;
    uint32_t to = min(buffer[s.b].t + (uint32_t)pad, closeMs);
    s.torsoRotDeg = torsoRotationInWindow(toTorsoClock(from), toTorsoClock(to), s.torsoBeacons);
    s.torsoRatio = s.pathDeg > 0 ? s.torsoRotDeg / s.pathDeg : 0;
}

// ---------------- Strike templates ----------------
// Each action's strike is a template: which way the arm rotates (axis), which
// way the hand is pushed (push direction), how far it turns (rotation) and how
// much the torso turns with it. A stroke's distance to a template is
//   sqrt(dAxis^2 + dPush^2 + dRot^2 + dTorso^2)
// with each term scaled by that action's own spread in the recordings, and
// the stroke is valid if its nearest template is within the locked weapon's
// ACTION_LIMITS strike distance. One
// combined distance, so being a little off on one feature doesn't reject a
// stroke on its own — the same nearest-reference idea as classifyWeapon().
// None of it depends on speed: directions and angles are the same for a
// slow swing and a fast one.
//
// The table is fitted from RAW_STREAM recordings by fit_strike_model.py
// (python fit_strike_model.py --write) — refit, don't hand-edit. It's
// checked offline with speed_study.py before flashing.
// ===== STRIKE_MODEL — BEGIN (generated by fit_strike_model.py, don't hand-edit) =====
// Fitted 2026-10-13 from 76 verified hub debug sessions (5 attested physical-grip overrides).
const StrikeTemplate STRIKE_MODEL[] = {
    // weapon, action, axis ux,uy,uz, axisSpreadDeg, push px,py,pz, pushSpreadDeg,
    // rotMeanDeg, rotSpreadDeg, torsoRef, torsoSpread, bidirectional
    {WEAPON_SWORD_SHIELD, "vertical", 0.7698f, 0.0586f, -0.6356f, 18.9151f, -0.0032f, 1.0000f, 0.0052f, 46.7357f, 167.7970f, 33.5594f, 0.0872f, 0.1000f, false},
    {WEAPON_SWORD_SHIELD, "horizontal", 0.9438f, 0.3279f, -0.0421f, 19.8844f, 0.3286f, 0.9382f, 0.1091f, 30.0000f, 134.3595f, 26.8719f, 0.1169f, 0.1169f, false},
    {WEAPON_SWORD_SHIELD, "diagonal", 0.9527f, 0.1032f, -0.2858f, 31.4765f, 0.3239f, 0.9460f, 0.0156f, 30.0000f, 158.1065f, 31.6213f, 0.1260f, 0.1260f, false},
    {WEAPON_SWORD_SHIELD, "uppercut", -0.1126f, 0.0779f, 0.9906f, 10.0000f, 0.7462f, 0.6557f, 0.1151f, 30.0000f, 113.1250f, 22.6250f, 0.1398f, 0.1398f, false},
    {WEAPON_SWORD_SHIELD, "spin", 0.9332f, -0.3257f, 0.1517f, 21.8175f, 0.4224f, 0.6464f, -0.6354f, 30.0000f, 427.0889f, 85.4178f, 0.7942f, 0.7942f, true},
    {WEAPON_SPEAR, "vertical", 0.5807f, -0.3736f, -0.7233f, 15.7398f, 0.2131f, 0.9729f, -0.0895f, 30.0000f, 138.6840f, 27.7368f, 0.1231f, 0.1231f, false},
    {WEAPON_SPEAR, "horizontal", 0.9988f, 0.0413f, 0.0245f, 10.9966f, 0.2967f, 0.7774f, -0.5547f, 30.0000f, 128.7545f, 25.7509f, 0.3885f, 0.3885f, false},
    {WEAPON_SPEAR, "diagonal", 0.8518f, -0.3322f, -0.4050f, 19.5271f, 0.4537f, 0.7905f, -0.4114f, 45.0487f, 166.2523f, 33.2505f, 0.3168f, 0.3168f, false},
    {WEAPON_SPEAR, "uppercut", 0.2889f, 0.1889f, 0.9385f, 18.6788f, 0.8835f, -0.1386f, -0.4474f, 34.7252f, 105.7115f, 21.1423f, 0.1933f, 0.1933f, false},
    {WEAPON_SPEAR, "spin", 0.9852f, -0.0106f, 0.1708f, 14.8957f, 0.1354f, 0.3054f, -0.9425f, 30.0000f, 420.5593f, 84.1119f, 0.8253f, 0.8253f, true},
    {WEAPON_MACE, "vertical", 0.5207f, -0.2376f, -0.8201f, 14.7084f, -0.0493f, 0.9850f, -0.1656f, 30.0000f, 168.3650f, 33.6730f, 0.0912f, 0.1000f, false},
    {WEAPON_MACE, "horizontal", 0.9244f, 0.2134f, -0.3163f, 15.5854f, -0.0198f, 0.7093f, -0.7046f, 30.0000f, 136.1000f, 27.2200f, 0.3776f, 0.3776f, false},
    {WEAPON_MACE, "diagonal", 0.6187f, -0.2737f, -0.7364f, 19.8041f, 0.2486f, 0.9086f, -0.3357f, 30.0000f, 194.4475f, 38.8895f, 0.2527f, 0.2527f, false},
    {WEAPON_MACE, "uppercut", -0.1110f, 0.2893f, 0.9508f, 23.1616f, 0.7363f, 0.6159f, -0.2803f, 40.6459f, 92.3128f, 18.4626f, 0.2030f, 0.2030f, false},
    {WEAPON_MACE, "spin", 0.9089f, -0.4162f, 0.0272f, 27.3932f, 0.3762f, 0.5538f, -0.7428f, 30.0000f, 422.7714f, 84.5543f, 0.8267f, 0.8267f, true},
};
const int STRIKE_MODEL_COUNT = sizeof(STRIKE_MODEL) / sizeof(STRIKE_MODEL[0]);
const WeaponActionLimits ACTION_LIMITS[] = {
    {WEAPON_SWORD_SHIELD, 1.5000f, 1.0000f, 0.1500f},
    {WEAPON_SPEAR, 2.0000f, 1.5000f, 0.1500f},
    {WEAPON_MACE, 1.7500f, 0.7500f, 0.1500f},
};
const WeaponThrustTemplate THRUST_MODEL[] = {
    {WEAPON_SWORD_SHIELD, {-0.2290f, 0.9613f, -0.1529f, 20.7218f, 0.6780f, 0.4069f, 55.0000f, 13.7500f}},
    {WEAPON_SPEAR, {-0.1334f, 0.9824f, -0.1310f, 26.6635f, 0.4942f, 0.3500f, 55.0000f, 13.7500f}},
    {WEAPON_MACE, {-0.1738f, 0.9031f, -0.3927f, 35.4135f, 0.6533f, 0.3519f, 63.3200f, 15.8300f}},
};
// ===== STRIKE_MODEL — END =====

const WeaponActionLimits *activeActionLimits()
{
    for (unsigned int i = 0; i < sizeof(ACTION_LIMITS) / sizeof(ACTION_LIMITS[0]); i++)
        if (ACTION_LIMITS[i].weapon == lockedWeapon)
            return &ACTION_LIMITS[i];
    return nullptr;
}

const ThrustTemplate *activeThrustTemplate()
{
    for (unsigned int i = 0; i < sizeof(THRUST_MODEL) / sizeof(THRUST_MODEL[0]); i++)
        if (THRUST_MODEL[i].weapon == lockedWeapon)
            return &THRUST_MODEL[i].model;
    return nullptr;
}

const unsigned long THRUST_REPEAT_GUARD_MS = 700;
const float UPPERCUT_TORSO_GATE_PEAK_DPS = 250.0f;
const float UPPERCUT_MIN_TORSO_RATIO = 0.045f;

// A spin is a full-body rotation, not an ordinary ~110-200 degree swing
// whose axis happens to resemble the spin axis. Keep this outside the
// generated model block so fit_strike_model.py cannot overwrite it.
const float SPIN_MIN_ROT_DEG = 250.0;

float angleBetweenDeg(float ax, float ay, float az, float bx, float by, float bz, bool signFree)
{
    float c = ax * bx + ay * by + az * bz;
    if (signFree)
        c = fabs(c);
    return acos(constrain(c, -1.0f, 1.0f)) * RAD_TO_DEG;
}

// Nearest template for the locked weapon. A stroke with no push (all 0)
// scores the full 180 deg on the push term.
TemplateMatch nearestTemplate(const Stroke &s)
{
    TemplateMatch best = {-1, 1e9, 0, 0, 0, 0};
    bool noPush = s.hx == 0 && s.hy == 0 && s.hz == 0;
    for (int k = 0; k < STRIKE_MODEL_COUNT; k++)
    {
        const StrikeTemplate &m = STRIKE_MODEL[k];
        if (m.weapon != WEAPON_UNKNOWN && m.weapon != lockedWeapon)
            continue;
        if (strcmp(m.action, "spin") == 0 && fabs(s.rotDeg) < SPIN_MIN_ROT_DEG)
            continue;
        float dA = angleBetweenDeg(s.ux, s.uy, s.uz, m.ux, m.uy, m.uz, m.bidirectional) / m.axisSpreadDeg;
        float dP = (noPush ? 180.0f : angleBetweenDeg(s.hx, s.hy, s.hz, m.px, m.py, m.pz, m.bidirectional)) /
                   m.pushSpreadDeg;
        float dR = (s.rotDeg - m.rotMeanDeg) / m.rotSpreadDeg;
        float dT = max(0.0f, s.torsoRatio - m.torsoRef) / m.torsoSpread;
        float d = sqrt(dA * dA + dP * dP + dR * dR + dT * dT);
        if (d < best.dist)
            best = {k, d, dA, dP, dR, dT};
    }
    return best;
}

// ---------------- Push strokes (thrust) ----------------
// A thrust barely rotates, so with no wrist twist it never makes a rotation
// stroke. It's found from linear motion instead: accel minus gravity, with
// gravity tracked by the gyro and pulled GRAVITY_ALPHA of the way toward the
// measured accel each sample (so it can't drift off). A push is the run of
// samples around a linear-accel peak where accel along that peak's own
// direction stays above PUSH_FRAC of the peak — the same peak-relative
// claiming as findStrokes(). Its features don't depend on speed: the push
// direction, how far the hand travels before it stops (reach), and how much
// the wrist turns meanwhile. Must match figpipe/replay.py find_push_strokes().
const float GRAVITY_ALPHA = 0.02;
const float PUSH_MIN_PEAK_G = 0.3; // only absolute limit: above hand jitter
const float PUSH_FRAC = 0.2;
const unsigned long REACH_MAX_MS = 1500;
const float G_MS2 = 9.81;
#define MAX_PUSHES 8

float linRaw[MAX_SAMPLES][3]; // accel minus gravity, g
float linAcc[MAX_SAMPLES][3]; // ... smoothed like the gyro (STROKE_SMOOTH)
PushStroke pushes[MAX_PUSHES];
int pushCount = 0;

void computeLinearAccel()
{
    int n0 = min(5, sampleCount);
    float gx = 0, gy = 0, gz = 0;
    for (int i = 0; i < n0; i++)
    {
        gx += buffer[i].ax;
        gy += buffer[i].ay;
        gz += buffer[i].az;
    }
    gx /= n0, gy /= n0, gz /= n0;
    for (int i = 0; i < sampleCount; i++)
    {
        float dt = (i > 0) ? (buffer[i].t - buffer[i - 1].t) / 1000.0f : 0.01f;
        float wx = buffer[i].gx * DEG_TO_RAD, wy = buffer[i].gy * DEG_TO_RAD, wz = buffer[i].gz * DEG_TO_RAD;
        float cx = wy * gz - wz * gy, cy = wz * gx - wx * gz, cz = wx * gy - wy * gx; // omega x g
        gx -= cx * dt, gy -= cy * dt, gz -= cz * dt;
        gx += GRAVITY_ALPHA * (buffer[i].ax - gx);
        gy += GRAVITY_ALPHA * (buffer[i].ay - gy);
        gz += GRAVITY_ALPHA * (buffer[i].az - gz);
        linRaw[i][0] = buffer[i].ax - gx;
        linRaw[i][1] = buffer[i].ay - gy;
        linRaw[i][2] = buffer[i].az - gz;
    }
    const int h = STROKE_SMOOTH / 2;
    for (int i = 0; i < sampleCount; i++)
    {
        float s[3] = {0, 0, 0};
        for (int k = i - h; k <= i + h; k++)
        {
            if (k < 0 || k >= sampleCount)
                continue;
            for (int c = 0; c < 3; c++)
                s[c] += linRaw[k][c];
        }
        for (int c = 0; c < 3; c++)
            linAcc[i][c] = s[c] / STROKE_SMOOTH;
    }
}

float linMag(int i) { return vecMagnitude(linAcc[i][0], linAcc[i][1], linAcc[i][2]); }
float linAlong(int i, float ux, float uy, float uz) { return linAcc[i][0] * ux + linAcc[i][1] * uy + linAcc[i][2] * uz; }

// Fills pushes[0..pushCount) from buffer[], sorted by start. closeMs as in
// computeStrokeFeatures().
void findPushStrokes(uint32_t closeMs)
{
    computeLinearAccel();
    for (int i = 0; i < sampleCount; i++)
        sampleClaimed[i] = false;
    pushCount = 0;

    while (pushCount < MAX_PUSHES)
    {
        int ip = -1;
        float peak = -1;
        for (int i = 0; i < sampleCount; i++)
        {
            if (sampleClaimed[i])
                continue;
            float m = linMag(i);
            if (m > peak)
            {
                peak = m;
                ip = i;
            }
        }
        if (ip < 0 || peak < PUSH_MIN_PEAK_G)
            break;

        float ux = linAcc[ip][0] / peak, uy = linAcc[ip][1] / peak, uz = linAcc[ip][2] / peak;
        float lim = PUSH_FRAC * peak;
        int a = ip, b = ip;
        while (a > 0 && linAlong(a - 1, ux, uy, uz) > lim)
            a--;
        while (b < sampleCount - 1 && linAlong(b + 1, ux, uy, uz) > lim)
            b++;
        bool overlaps = false;
        for (int i = a; i <= b; i++)
        {
            overlaps |= sampleClaimed[i];
            sampleClaimed[i] = true;
        }
        if (overlaps)
            continue;

        // Reach: integrate along the push from a until the hand stops
        // (velocity back to 0 after b), at most REACH_MAX_MS.
        float v = 0, x = 0;
        int e = a;
        for (int i = a + 1; i < sampleCount; i++)
        {
            float dt = (buffer[i].t - buffer[i - 1].t) / 1000.0f;
            v += 0.5f * (linAlong(i, ux, uy, uz) + linAlong(i - 1, ux, uy, uz)) * G_MS2 * dt;
            e = i;
            if (v <= 0 && i > b)
                break;
            x += v * dt;
            if (buffer[i].t - buffer[a].t >= REACH_MAX_MS)
                break;
        }
        float rot = 0;
        for (int i = a + 1; i <= e; i++)
            rot += 0.5f * (vecMagnitude(buffer[i].gx, buffer[i].gy, buffer[i].gz) + vecMagnitude(buffer[i - 1].gx, buffer[i - 1].gy, buffer[i - 1].gz)) *
                   (buffer[i].t - buffer[i - 1].t) / 1000.0f;

        PushStroke &p = pushes[pushCount++];
        p = {a, b, e, ux, uy, uz, peak, x, rot, 0};
        float dur = buffer[e].t - buffer[a].t;
        float pad = TORSO_PAD_FRAC * max(dur, 100.0f);
        uint32_t from = buffer[a].t - (uint32_t)pad;
        uint32_t to = min(buffer[e].t + (uint32_t)pad, closeMs);
        torsoRotationInWindow(toTorsoClock(from), toTorsoClock(to), p.torsoBeacons);
    }

    for (int i = 1; i < pushCount; i++)
        for (int j = i; j > 0 && pushes[j].a < pushes[j - 1].a; j--)
        {
            PushStroke tmp = pushes[j];
            pushes[j] = pushes[j - 1];
            pushes[j - 1] = tmp;
        }
}

// Distance to the thrust template: sqrt(dDir^2 + dReach^2 + dRot^2), each
// scaled by the thrusts' own spread; rotation only counts above rotRefDeg.
float thrustDistance(const PushStroke &p, float &dDir, float &dReach, float &dRot)
{
    const ThrustTemplate *selected = activeThrustTemplate();
    if (!selected)
    {
        dDir = dReach = dRot = 1e9f;
        return 1e9f;
    }
    const ThrustTemplate &m = *selected;
    dDir = angleBetweenDeg(p.ux, p.uy, p.uz, m.ux, m.uy, m.uz, false) / m.dirSpreadDeg;
    dReach = (p.reachM - m.reachMeanM) / m.reachSpreadM;
    dRot = max(0.0f, p.rotDeg - m.rotRefDeg) / m.rotSpreadDeg;
    return sqrt(dDir * dDir + dReach * dReach + dRot * dRot);
}

// ---------------- Accepted strokes ----------------
// The last few accepted strokes (either kind), for:
//   return   a return to guard is the mirror of its strike: same rotation
//            angle, same shape, opposite direction — and bringing the weapon
//            back up after a vertical rotates the same way as an uppercut. So
//            a stroke that starts within max(RETURN_MIN_MS, RETURN_REL x the
//            strike's duration) after an accepted stroke and goes the
//            opposite way (cos < RETURN_COS) is its return: a rotation
//            stroke compared by rotation axis with the last accepted
//            rotation stroke, a push by linear direction with the last
//            accepted stroke of either kind. Scaled by the strike's own
//            duration, so a slow strike gets a slow return. Spins are exempt
//            (they're done in both directions).
//   overlap  a push overlapping an accepted stroke is the same movement.
//   lead_in  a push next to an accepted rotation strike of the same
//            candidate is that swing's lead-in, not a thrust.
const unsigned long RETURN_MIN_MS = 300;
const float RETURN_REL = 1.0;
const float RETURN_COS = -0.5;
#define ACCEPTED_HISTORY 8

AcceptedStroke acceptedHist[ACCEPTED_HISTORY];
int acceptedCount = 0; // filled entries, newest at (acceptedHead - 1)
int acceptedHead = 0;
uint32_t candidateId = 0;

bool lastStrikeValid = false; // last accepted ROTATION stroke
float lastStrikeUx, lastStrikeUy, lastStrikeUz;
uint32_t lastStrikeEndMs;
unsigned long lastStrikeDurMs;
bool lastStrikeSpin;

void rememberAccepted(uint32_t t0, uint32_t t1, float vx, float vy, float vz, bool spin, bool rotation)
{
    acceptedHist[acceptedHead] = {t0, t1, vx, vy, vz, spin, rotation, candidateId};
    acceptedHead = (acceptedHead + 1) % ACCEPTED_HISTORY;
    if (acceptedCount < ACCEPTED_HISTORY)
        acceptedCount++;
}

const AcceptedStroke &acceptedAt(int k) { return acceptedHist[(acceptedHead - 1 - k + ACCEPTED_HISTORY) % ACCEPTED_HISTORY]; }

bool returnOf(uint32_t prevEnd, unsigned long prevDur, bool prevSpin, uint32_t t0, float cosToPrev)
{
    if (prevSpin)
        return false;
    int32_t gap = (int32_t)(t0 - prevEnd);
    unsigned long window = max(RETURN_MIN_MS, (unsigned long)(RETURN_REL * prevDur));
    return gap <= (int32_t)window && cosToPrev < RETURN_COS;
}

bool isReturnOfLastStrike(const Stroke &s)
{
    if (!lastStrikeValid)
        return false;
    float c = s.ux * lastStrikeUx + s.uy * lastStrikeUy + s.uz * lastStrikeUz;
    return returnOf(lastStrikeEndMs, lastStrikeDurMs, lastStrikeSpin, buffer[s.a].t, c);
}

void rememberStrike(const Stroke &s, const TemplateMatch &m)
{
    bool spin = m.idx >= 0 && STRIKE_MODEL[m.idx].bidirectional;
    lastStrikeValid = true;
    lastStrikeUx = s.ux, lastStrikeUy = s.uy, lastStrikeUz = s.uz;
    lastStrikeEndMs = buffer[s.b].t;
    lastStrikeDurMs = buffer[s.b].t - buffer[s.a].t;
    lastStrikeSpin = spin;
    rememberAccepted(buffer[s.a].t, buffer[s.b].t, s.hx, s.hy, s.hz, spin, true);
}

// The push's reason to be rejected from context, or nullptr.
const char *pushContextReject(const PushStroke &p)
{
    uint32_t t0 = buffer[p.a].t, t1 = buffer[p.e].t;
    uint32_t w = max((uint32_t)RETURN_MIN_MS, t1 - t0);
    for (int k = 0; k < acceptedCount; k++)
    {
        const AcceptedStroke &a = acceptedAt(k);
        if ((int32_t)(a.t0 - t1) < 0 && (int32_t)(t0 - a.t1) < 0)
            return "overlap";
    }
    int newest = -1; // most recent accepted stroke that ended before this push
    for (int k = 0; k < acceptedCount; k++)
    {
        const AcceptedStroke &a = acceptedAt(k);
        if (a.rotation && a.candId == candidateId && (int32_t)(a.t0 - (t1 + w)) < 0 && (int32_t)((t0 - w) - a.t1) < 0)
            return "lead_in";
        // A return can create a new candidate and appear to push in the
        // same direction after gravity tracking. Suppress it by timing.
        if (!a.rotation && (int32_t)(t0 - a.t1) >= 0 && (uint32_t)(t0 - a.t1) <= THRUST_REPEAT_GUARD_MS)
            return "return";
        if ((int32_t)(a.t1 - t0) <= 0 && (newest < 0 || (int32_t)(a.t1 - acceptedAt(newest).t1) > 0))
            newest = k;
    }
    if (newest >= 0)
    {
        const AcceptedStroke &a = acceptedAt(newest);
        float c = p.ux * a.vx + p.uy * a.vy + p.uz * a.vz;
        if (returnOf(a.t1, a.t1 - a.t0, a.spin, t0, c))
            return "return";
    }
    return nullptr;
}

// ---------------- Segment -> Ultra96 ----------------
// Samples for this segment live in buffer[sendStart..sendEnd] — the accepted
// stroke plus SEND_PAD_FRAC of its duration either side (see acceptSegment()).
// Gateway builds (hardware/gateway, event mode) transmit it; others only log.
const float SEND_PAD_FRAC = 0.25;

void sendSegmentToUltra96(uint32_t segId, int sendStart, int sendEnd)
{
#if defined(CG_HARDWARE_GATEWAY) && CG_GATEWAY_EVENT_MODE
    // Gateway event mode: hand the segment to the BLE task, which sends it to the relay once as
    // SEGMENT_CHUNKs (decimated to the classifier's 50 Hz). Returns immediately; no radio work here.
    gatewaySegmentBegin(segId, (uint8_t)lockedWeapon, (uint16_t)(sendEnd - sendStart + 1));
    for (int i = sendStart; i <= sendEnd; i++)
    {
        const Sample &s = buffer[i];
        gatewaySegmentSample(s.t, s.ax, s.ay, s.az, s.gx, s.gy, s.gz);
    }
    bool queued = gatewaySegmentEnd();
    debugLog(String(queued ? "[Ultra96 send queued] " : "[Ultra96 send failed] ") +
             String(sendEnd - sendStart + 1) + " wrist samples for seg=" + String(segId));
#else
    debugLog("[Ultra96 send skipped] would send " + String(sendEnd - sendStart + 1) +
             " wrist samples for seg=" + String(segId));
#endif
}

// Sends buffer[a..b] plus a margin, so the AI sees the whole movement.
void acceptSegment(int a, int b, const char *hint, float dist)
{
    uint32_t pad = (uint32_t)(SEND_PAD_FRAC * (buffer[b].t - buffer[a].t));
    int sendStart = a, sendEnd = b;
    while (sendStart > 0 && buffer[a].t - buffer[sendStart - 1].t <= pad)
        sendStart--;
    while (sendEnd < sampleCount - 1 && buffer[sendEnd + 1].t - buffer[b].t <= pad)
        sendEnd++;
    uint32_t segId = ((uint32_t)WRIST_ID << 28) ^ (buffer[b].t & 0x0FFFFFFF);
    sendSegmentRequestToTorso(segId, toTorsoClock(buffer[sendStart].t), toTorsoClock(buffer[sendEnd].t));
    sendSegmentToUltra96(segId, sendStart, sendEnd);
    resultLog("action_accepted weapon=" + String(weaponName(lockedWeapon)) + " seg=" + String(segId) +
              " hint=" + String(hint) + " dist=" + String(dist, 2));
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

// Splits the closed candidate into rotation strokes and push strokes and
// accepts each one that is near a template and isn't explained by context
// (a return, or a swing's own lead-in). Rotation strokes are decided first,
// then pushes. Several can be accepted (a combo); each gets its own segment.
void onMotionEnd(const char *closeReason)
{
    candidateId++;
    uint32_t closeMs = buffer[sampleCount - 1].t;
    unsigned long rawDurationMs = (sampleCount > 1) ? (buffer[sampleCount - 1].t - buffer[0].t) : 0;
    int accepted = 0;

    findStrokes();
    for (int k = 0; k < strokeCount; k++)
    {
        Stroke &s = strokes[k];
        computeStrokeFeatures(s, closeMs);
        TemplateMatch m = nearestTemplate(s);

        const char *reason;
        if (s.torsoBeacons == 0)
            reason = "torso_data"; // check torso link/power
        else if (isReturnOfLastStrike(s))
            reason = "return";
        else if (m.idx >= 0 && strcmp(STRIKE_MODEL[m.idx].action, "uppercut") == 0 &&
                 s.peakDps >= UPPERCUT_TORSO_GATE_PEAK_DPS && s.torsoRatio < UPPERCUT_MIN_TORSO_RATIO)
            reason = "torso_quiet";
        else if (m.idx < 0 || !activeActionLimits() || m.dist > activeActionLimits()->strikeMaxDist)
            reason = "far";
        else
            reason = "ok";
        bool ok = strcmp(reason, "ok") == 0;

        debugLog("Stroke i=" + String(k) + " kind=rot t0=" + String(buffer[s.a].t) + " t1=" + String(buffer[s.b].t) +
                 " peak=" + String(s.peakDps, 1) + " rot=" + String(s.rotDeg, 1) +
                 " ax=" + String(s.ux, 3) + " ay=" + String(s.uy, 3) + " az=" + String(s.uz, 3) +
                 " px=" + String(s.hx, 3) + " py=" + String(s.hy, 3) + " pz=" + String(s.hz, 3) +
                 " torsoRot=" + String(s.torsoRotDeg, 1) + " torsoRatio=" + String(s.torsoRatio, 3) +
                 " beacons=" + String(s.torsoBeacons) +
                 " nearest=" + String(m.idx >= 0 ? STRIKE_MODEL[m.idx].action : "none") +
                 " dist=" + String(m.dist, 2) + " dAxis=" + String(m.dAxis, 2) + " dPush=" + String(m.dPush, 2) +
                 " dRot=" + String(m.dRot, 2) + " dTorso=" + String(m.dTorso, 2) +
                 " reason=" + reason + " accept=" + String(ok ? 1 : 0));
        if (!ok)
            continue;
        rememberStrike(s, m);
        accepted++;
        acceptSegment(s.a, s.b, STRIKE_MODEL[m.idx].action, m.dist);
    }

    findPushStrokes(closeMs);
    for (int k = 0; k < pushCount; k++)
    {
        PushStroke &p = pushes[k];
        float dDir, dReach, dRot;
        float d = thrustDistance(p, dDir, dReach, dRot);

        const char *reason = nullptr;
        if (p.torsoBeacons == 0)
            reason = "torso_data";
        if (!reason)
            reason = pushContextReject(p);
        if (!reason && (!activeActionLimits() || p.reachM < activeActionLimits()->minThrustReachM))
            reason = "short";
        if (!reason)
            reason = !activeActionLimits() || d > activeActionLimits()->thrustMaxDist ? "far" : "ok";
        bool ok = strcmp(reason, "ok") == 0;

        debugLog("Stroke i=" + String(k) + " kind=push t0=" + String(buffer[p.a].t) + " t1=" + String(buffer[p.e].t) +
                 " peak=" + String(p.peakG, 2) + " rot=" + String(p.rotDeg, 1) + " reach=" + String(p.reachM, 3) +
                 " ax=" + String(p.ux, 3) + " ay=" + String(p.uy, 3) + " az=" + String(p.uz, 3) +
                 " beacons=" + String(p.torsoBeacons) + " nearest=thrust dist=" + String(d, 2) +
                 " dAxis=" + String(dDir, 2) + " dReach=" + String(dReach, 2) + " dRot=" + String(dRot, 2) +
                 " reason=" + reason + " accept=" + String(ok ? 1 : 0));
        if (!ok)
            continue;
        rememberAccepted(buffer[p.a].t, buffer[p.e].t, p.ux, p.uy, p.uz, false, false);
        accepted++;
        acceptSegment(p.a, p.e, "thrust", d);
    }

    debugLog("Candidate ENDED weapon=" + String(weaponName(lockedWeapon)) + " raw=" + String(sampleCount) +
             " rawDur=" + String(rawDurationMs) + " strokes=" + String(strokeCount) + " pushes=" + String(pushCount) +
             " accepted=" + String(accepted) + " reason=" + closeReason);

    if (accepted > 0)
    {
        ledFlashAccept();
        localFeedback(FB_SWING_ACCEPTED, 0);
    }
    else
        ledFlashReject();
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
        // Peak-relative settle tracking — see SETTLE_FRACTION above. Each
        // signal is normalised by its own start threshold, so a
        // rotation-heavy swing and a jerk-heavy thrust both track correctly.
        float score = max(gyroMag / START_THRESHOLD, jerk / JERK_START_THRESHOLD);
        if (score > candidatePeakScore)
            candidatePeakScore = score;
        if (score >= SETTLE_FRACTION * candidatePeakScore)
            candidateSettleTimerResetAt = now;

        bool settled = (now - candidateSettleTimerResetAt) >= SETTLE_HOLD_MS;
        // Safety nets: don't let a candidate stay open forever on sustained
        // non-peaky motion, or overrun the buffer.
        bool tooLong = (now - motionStartTime) >= RAW_MAX_DURATION_MS;
        bool bufferFull = sampleCount >= MAX_SAMPLES - 1;

        if (settled || tooLong || bufferFull)
        {
            const char *reason = settled ? "settled" : (tooLong ? "timeout" : "bufferFull");
            debugLog("[candidate] closing: " + String(reason));
            state = IDLE;
            cooldownUntil = now + CANDIDATE_COOLDOWN_MS;
            onMotionEnd(reason);
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
    lastStrikeValid = false;
    acceptedCount = 0;
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

// PHASE_WAITING / PHASE_DETECTING: weapon classification plus the
// continuous-hold lock timer.
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
        // (15 dps) is far stricter than the wrists' (100). The torso still
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

// PHASE_LOCKED: the valid-action algorithm.
void runValidActionTick(const Sample &s, unsigned long now)
{
    storePretrigger(s);
    bufferSample(s);

    float gyroMag = vecMagnitude(s.gx, s.gy, s.gz);
    float accelMag = vecMagnitude(s.ax, s.ay, s.az);
    float jerk = fabs(accelMag - 1.0);
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
             " latency_test=" + String(LATENCY_TEST) + " raw_stream=" + String(RAW_STREAM) +
             " player=" + String(PLAYER_ID));
#ifdef CG_HARDWARE_GATEWAY
    gatewayBegin(PLAYER_ID, 0);
#endif
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
#ifdef CG_HARDWARE_GATEWAY
    gatewayPoll(phase == PHASE_LOCKED ? (uint8_t)lockedWeapon : 0);
    int relayDuty = gatewayFeedbackDuty();
    if (relayDuty >= 0) {
        patternStop(motorPlayer);
        analogWrite(MOTOR_PIN, relayDuty);
    }
#endif

    unsigned long nowUs = micros();
    if (nowUs - lastSampleUs < SAMPLE_PERIOD_US)
        return;
    lastSampleUs = nowUs;

    unsigned long now = millis();
    broadcastHubStatus(now);

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
#ifdef CG_HARDWARE_GATEWAY
    gatewaySample(ax_g, ay_g, az_g, gx_dps, gy_dps, gz_dps);
#endif
#if RAW_STREAM
    rawStreamPush((uint32_t)now, ax_g, ay_g, az_g, gx_dps, gy_dps, gz_dps);
#endif

    if (phase == PHASE_LOCKED)
    {
        static unsigned long lastWeaponLogMs = 0;
        if (now - lastWeaponLogMs >= 1000)
        {
            lastWeaponLogMs = now;
            debugLog("Active weapon=" + String(weaponName(lockedWeapon)));
        }
        Sample s = {(uint32_t)now, ax_g, ay_g, az_g, gx_dps, gy_dps, gz_dps};
        runValidActionTick(s, now);
    }
    else
    {
        runDetectionTick(ax_g, ay_g, az_g, vecMagnitude(gx_dps, gy_dps, gz_dps), now);
    }
}
