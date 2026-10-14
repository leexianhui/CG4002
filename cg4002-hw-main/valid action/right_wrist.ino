// ============================================================================
// RIGHT WRIST NODE — primary swing detector.
//
// Role in the two-node design:
//   - Runs the actual start/end segmentation on its own gyro + accel.
//   - Listens to lightweight rotation "beacons" broadcast by the torso node
//     (see torso.ino) and uses torso's peak rotation DURING the candidate
//     window as a validity gate — high torso rotation alongside a high wrist
//     rotation usually means the player turned/moved their whole body, not a
//     clean swing (except Spin Attack, where torso rotation is expected).
//   - On a valid segment, tells the torso node which time window to ship
//     (SegmentRequest), and streams its own buffered samples to the Ultra96.
//
// Keep the shared struct definitions (TorsoBeacon, SegmentRequest,
// SegmentSampleMsg) identical to the copies in torso.ino — they're the wire
// protocol between the two boards.
// ============================================================================

#include <Wire.h>
#include <WiFi.h>
#include <WiFiUdp.h>

// ================= CONFIG =================
#define WRIST_ID 0
#define TORSO_ID 1

const char *ssid = "xin hui's laptop";
const char *password = "12345678";

IPAddress broadcastIP(192, 168, 137, 255);
const int DEBUG_PORT = 4211; // -> laptop listener.py
const int LINK_PORT = 4210;  // <-> torso node (beacons + segment request)

#define DEBUG_LOG true

// Raw sensor debug output — OFF by default, too noisy for normal testing.
// Flip to true only when you specifically need to inspect raw sample data
// (e.g. capturing a segment to send for threshold debugging).
#define DEBUG_RAW_STREAM false   // periodic live sample print, rate-limited
#define RAW_LOG_INTERVAL_MS 100  // ~10Hz when enabled
#define DEBUG_SEGMENT_DUMP false // full sample dump on every motion end

WiFiUDP debugUdp;
WiFiUDP linkUdp;
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

struct __attribute__((packed)) TorsoBeacon
{
    uint8_t deviceId; // TORSO_ID
    uint32_t torsoMs;
    float gyroMag; // instantaneous |omega|, dps
};

struct __attribute__((packed)) SegmentRequest
{
    uint8_t deviceId; // WRIST_ID
    uint32_t segId;
    uint32_t startTorsoMs; // window converted into TORSO's clock domain
    uint32_t endTorsoMs;
};

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

// ---------------- RGB LED (common cathode) ----------------
#define LED_R_PIN 25
#define LED_G_PIN 26
#define LED_B_PIN 27

enum LedState
{
    LED_BOOTING,
    LED_IDLE,
    LED_ACTIVE,
    LED_FLASH_ACCEPT,
    LED_FLASH_REJECT
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
void ledIdle()
{
    ledState = LED_IDLE;
    ledSetColor(0, 40, 0);
}
void ledMotionActive()
{
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
    if (ledState == LED_FLASH_ACCEPT || ledState == LED_FLASH_REJECT)
    {
        if (millis() - ledFlashStart > LED_FLASH_DURATION_MS)
            ledIdle();
    }
}

// ---------------- Haptic motor (2N7000 low-side switch on GPIO4) ----------------
// Same caveat as before: no flyback diode yet — add a 1N4148/1N4001 across the
// motor terminals before repeated/long-duration testing.
#define MOTOR_PIN 4
bool motorPulsing = false;
unsigned long motorPulseEnd = 0;

void motorSetup()
{
    pinMode(MOTOR_PIN, OUTPUT);
    analogWrite(MOTOR_PIN, 0);
}
void triggerHapticPulse(uint8_t intensity, unsigned long durationMs)
{
    analogWrite(MOTOR_PIN, intensity);
    motorPulsing = true;
    motorPulseEnd = millis() + durationMs;
}
void motorOff()
{
    analogWrite(MOTOR_PIN, 0);
    motorPulsing = false;
}
void updateMotor()
{
    if (motorPulsing && millis() >= motorPulseEnd)
        motorOff();
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

float clockOffsetMs = 0; // torsoMs - wristMs, exponentially smoothed
bool clockOffsetInit = false;

void updateClockOffset(uint32_t torsoMs, uint32_t wristMsAtRx)
{
    float sample = (float)torsoMs - (float)wristMsAtRx;
    if (!clockOffsetInit)
    {
        clockOffsetMs = sample;
        clockOffsetInit = true;
    }
    else
    {
        clockOffsetMs = 0.9f * clockOffsetMs + 0.1f * sample;
    }
}

uint32_t toTorsoClock(uint32_t wristMs)
{
    return (uint32_t)((float)wristMs + clockOffsetMs);
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
    int packetSize = linkUdp.parsePacket();
    if (packetSize == sizeof(TorsoBeacon))
    {
        TorsoBeacon b;
        linkUdp.read((uint8_t *)&b, sizeof(b));
        if (b.deviceId == TORSO_ID)
        {
            uint32_t rxMs = millis();
            storeTorsoBeacon(b.torsoMs, b.gyroMag);
            updateClockOffset(b.torsoMs, rxMs);
        }
    }
    else if (packetSize > 0)
    {
        linkUdp.flush();
    }
}

void sendSegmentRequestToTorso(uint32_t segId, uint32_t startTorsoMs, uint32_t endTorsoMs)
{
    SegmentRequest req = {WRIST_ID, segId, startTorsoMs, endTorsoMs};
    linkUdp.beginPacket(broadcastIP, LINK_PORT);
    linkUdp.write((uint8_t *)&req, sizeof(req));
    linkUdp.endPacket();
    debugLog("Sent SegmentRequest seg=" + String(segId));
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
const float MIN_TORSO_ENGAGEMENT = 20.0;

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
    // TODO: these two numbers are a starting guess based on limited data.
    // Calibrate properly: log peakGyroMag/peakLinearAccel for ~10 deliberate
    // "raise to guard" reps vs ~10 real swings and set this between the two
    // distributions rather than trusting this guess long-term.
    const float MIN_PEAK_GYRO_DPS = 180.0;
    const float MIN_PEAK_JERK_G = 0.9;
    const unsigned long MIN_DURATION = 80;
    const unsigned long MAX_DURATION = 1500;
    const int MIN_SAMPLES = 5;

    if (s.duration < MIN_DURATION || s.duration > MAX_DURATION)
        return false;
    if (s.sampleCount < MIN_SAMPLES)
        return false;
    if (s.peakGyroMag < MIN_PEAK_GYRO_DPS && shape.peakLinearAccel < MIN_PEAK_JERK_G)
        return false; // deliberate but not violent enough — e.g. raising to guard
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
// Trimmed samples for this segment live in buffer[trimStart..trimEnd]. Not
// wiring up the actual transmit yet — just logging so you can confirm
// accept/reject behaviour first. When ready, loop over that range and send
// SegmentSampleMsg packets (see CHANGES.md), same pattern as torso's
// sendMatchingSamplesToUltra96().
void sendSegmentToUltra96(uint32_t segId, int trimStart, int trimEnd)
{
    debugLog("[Ultra96 send skipped] would send " + String(trimEnd - trimStart + 1) +
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
    bool spinReversalOK = !spinLikely || (extras.reversalCount == 0);

    debugLog("Motion ENDED. raw=" + String(sampleCount) +
             " rawDur=" + String(rawDurationMs) +
             " trimmed=[" + String(trimStart) + "," + String(trimEnd) + "]" +
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
        sendSegmentRequestToTorso(segId, torsoStart, torsoEnd);
        sendSegmentToUltra96(segId, trimStart, trimEnd);
        ledFlashAccept();
        // Placeholder feedback pulse — later this should be driven by event
        // codes from the Central Game Engine (HIT_TAKEN, BLOCK_SUCCESS etc.)
        // rather than fired directly off local segmentation.
        triggerHapticPulse(180, 100);
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
            debugLog("REJECTED — spinLikely but reversals=" + String(extras.reversalCount) + " (expected 0 for a real spin)");
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

    debugUdp.begin(DEBUG_PORT);
    linkUdp.begin(LINK_PORT);

    writeRegister(REG_PWR_MGMT_1, 0x00);
    delay(100);
    writeRegister(REG_GYRO_CONFIG, 0x18);  // FS_SEL=11 -> +/-2000dps
    writeRegister(REG_ACCEL_CONFIG, 0x10); // AFS_SEL=10 -> +/-8g

    calibrateGyro();
    delay(100);

    ledIdle();
    debugLog("Wrist node initialized at IP " + WiFi.localIP().toString());
}

void loop()
{
    checkIncomingLink();
    updateLed();
    updateMotor();

    unsigned long nowUs = micros();
    if (nowUs - lastSampleUs < SAMPLE_PERIOD_US)
        return;
    lastSampleUs = nowUs;

    unsigned long now = millis();
    readSuccess = true;
    int16_t raw[7];
    if (!readIMUBurst(raw))
        return; // skip this tick, try again next loop

    float ax_g = raw[0] / ACCEL_SCALE;
    float ay_g = raw[1] / ACCEL_SCALE;
    float az_g = raw[2] / ACCEL_SCALE;
    float gx_dps = (raw[4] / GYRO_SCALE) - gyroXoffset;
    float gy_dps = (raw[5] / GYRO_SCALE) - gyroYoffset;
    float gz_dps = (raw[6] / GYRO_SCALE) - gyroZoffset;

    Sample s = {(uint32_t)now, ax_g, ay_g, az_g, gx_dps, gy_dps, gz_dps};
    storePretrigger(s);
    bufferSample(s);

    float gyroMag = vecMagnitude(gx_dps, gy_dps, gz_dps);
    float accelMag = vecMagnitude(ax_g, ay_g, az_g);
    float jerk = fabs(accelMag - 1.0);

    if (DEBUG_RAW_STREAM)
    {
        static unsigned long lastRawLogMs = 0;
        if (now - lastRawLogMs >= RAW_LOG_INTERVAL_MS)
        {
            lastRawLogMs = now;
            debugLog("[raw] t=" + String(now) +
                     " ax=" + String(ax_g, 3) + " ay=" + String(ay_g, 3) + " az=" + String(az_g, 3) +
                     " gx=" + String(gx_dps, 1) + " gy=" + String(gy_dps, 1) + " gz=" + String(gz_dps, 1) +
                     " gyroMag=" + String(gyroMag, 1) + " jerk=" + String(jerk, 2) +
                     " state=" + String(state == ACTIVE ? "ACTIVE" : "IDLE"));
        }
    }

    updateMotionState(gyroMag, jerk, now);
}
