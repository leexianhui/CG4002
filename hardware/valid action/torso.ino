// ============================================================================
// TORSO NODE — supporting sensor for the wrist's validity gate.
//
// Role in the two-node design:
//   - Does NOT run its own attack state machine. The wrist decides what
//     counts as an action; torso just supplies the data it needs to decide.
//   - Broadcasts a small TorsoBeacon (~50Hz) with current rotation magnitude
//     — the wrist uses this to check torso stayed still during a candidate
//     swing (or rotated, for a likely Spin Attack) and also to keep its
//     clock in sync with this board.
//   - Continuously ring-buffers its own raw samples. When the wrist confirms
//     a valid segment, it sends a SegmentRequest with a time window; torso
//     slices that window out of its ring buffer and ships it to the Ultra96,
//     tagged with the same segId so the classifier can join both streams.
//
// Keep the shared struct definitions identical to right_wrist.ino — they're
// the wire protocol between the two boards.
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
const int DEBUG_PORT = 4211;
const int LINK_PORT = 4210;

#define DEBUG_LOG true

// Same toggles as right_wrist.ino — OFF by default, too noisy for normal
// testing. Flip to true only when debugging specific segments/thresholds.
#define DEBUG_RAW_STREAM false
#define RAW_LOG_INTERVAL_MS 100
#define DEBUG_SEGMENT_DUMP false // print matched samples when a SegmentRequest arrives

WiFiUDP debugUdp;
WiFiUDP linkUdp;
// Ultra96 send deliberately left out for now — see right_wrist.ino for the
// same note. Matching samples are found and logged, not transmitted yet.

// ---------------- Wire structs ----------------
struct Sample
{
    uint32_t t;
    float ax, ay, az, gx, gy, gz;
};

struct __attribute__((packed)) TorsoBeacon
{
    uint8_t deviceId;
    uint32_t torsoMs;
    float gyroMag;
};

struct __attribute__((packed)) SegmentRequest
{
    uint8_t deviceId;
    uint32_t segId;
    uint32_t startTorsoMs; // already in TORSO's own clock — no conversion needed here
    uint32_t endTorsoMs;
};

struct __attribute__((packed)) SegmentSampleMsg
{
    uint32_t segId;
    uint8_t deviceId;
    uint16_t index;
    uint16_t total;
    uint32_t t;
    float ax, ay, az, gx, gy, gz;
};

// ---------------- RGB LED ----------------
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

// ---------------- Haptic motor (kept for future hit-feedback use) ----------------
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
#define REG_ACCEL_XOUT_H 0x3B

// Must match right_wrist.ino — torso's beacon gyroMag is compared directly
// against the wrist's torso-gate thresholds. Widened from +/-2g/+/-250dps
// (the sensor's narrowest ranges, which clip on real motion) to +/-8g/
// +/-2000dps.
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

// ---------------- Raw sample ring buffer ----------------
// Covers a few seconds so it comfortably contains any window the wrist asks
// for (window <= ~1.5s per MAX_DURATION on the wrist, plus lookback/lookahead).
#define RING_SIZE 300 // ~3s at 100Hz
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

void broadcastBeacon(float gyroMag)
{
    TorsoBeacon b = {TORSO_ID, (uint32_t)millis(), gyroMag};
    linkUdp.beginPacket(broadcastIP, LINK_PORT);
    linkUdp.write((uint8_t *)&b, sizeof(b));
    linkUdp.endPacket();
}

// ---------------- Segment request handling ----------------
void sendMatchingSamplesToUltra96(uint32_t segId, uint32_t startMs, uint32_t endMs)
{
    // Still does the real ring-buffer lookup, just doesn't transmit yet —
    // this lets you confirm the window actually lands on real samples
    // (i.e. the ring buffer is deep enough and clock sync is sane) before
    // wiring up the send. When ready, build SegmentSampleMsg per matched
    // sample and send it the same way right_wrist.ino's placeholder notes.
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

    if (DEBUG_SEGMENT_DUMP)
    {
        int idx = 0;
        for (int i = 0; i < count; i++)
        {
            Sample &s = ring[(oldestIdx + i) % RING_SIZE];
            if (s.t < startMs || s.t > endMs)
                continue;
            float gyroMag = sqrt(s.gx * s.gx + s.gy * s.gy + s.gz * s.gz);
            float accelMag = sqrt(s.ax * s.ax + s.ay * s.ay + s.az * s.az);
            float jerk = fabs(accelMag - 1.0);
            debugLog("[seg] i=" + String(idx++) +
                     " t=" + String(s.t) +
                     " ax=" + String(s.ax, 3) + " ay=" + String(s.ay, 3) + " az=" + String(s.az, 3) +
                     " gx=" + String(s.gx, 1) + " gy=" + String(s.gy, 1) + " gz=" + String(s.gz, 1) +
                     " gyroMag=" + String(gyroMag, 1) + " jerk=" + String(jerk, 2));
        }
    }

    ledFlashAccept();
}

void checkIncomingLink()
{
    int packetSize = linkUdp.parsePacket();
    if (packetSize == sizeof(SegmentRequest))
    {
        SegmentRequest req;
        linkUdp.read((uint8_t *)&req, sizeof(req));
        if (req.deviceId == WRIST_ID)
        {
            debugLog("Got SegmentRequest seg=" + String(req.segId) +
                     " [" + String(req.startTorsoMs) + ", " + String(req.endTorsoMs) + "]");
            sendMatchingSamplesToUltra96(req.segId, req.startTorsoMs, req.endTorsoMs);
        }
    }
    // (TorsoBeacon-sized packets showing up here would be our own broadcast
    // looping back on some networks — harmless to ignore.)
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
    debugLog("Torso node initialized at IP " + WiFi.localIP().toString());
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
    int16_t raw[7];
    if (!readIMUBurst(raw))
        return;

    float ax_g = raw[0] / ACCEL_SCALE;
    float ay_g = raw[1] / ACCEL_SCALE;
    float az_g = raw[2] / ACCEL_SCALE;
    float gx_dps = (raw[4] / GYRO_SCALE) - gyroXoffset;
    float gy_dps = (raw[5] / GYRO_SCALE) - gyroYoffset;
    float gz_dps = (raw[6] / GYRO_SCALE) - gyroZoffset;

    Sample s = {(uint32_t)now, ax_g, ay_g, az_g, gx_dps, gy_dps, gz_dps};
    ringPush(s);

    float gyroMag = sqrt(gx_dps * gx_dps + gy_dps * gy_dps + gz_dps * gz_dps);

    if (DEBUG_RAW_STREAM)
    {
        static unsigned long lastRawLogMs = 0;
        if (now - lastRawLogMs >= RAW_LOG_INTERVAL_MS)
        {
            lastRawLogMs = now;
            float accelMag = sqrt(ax_g * ax_g + ay_g * ay_g + az_g * az_g);
            float jerk = fabs(accelMag - 1.0);
            debugLog("[raw] t=" + String(now) +
                     " ax=" + String(ax_g, 3) + " ay=" + String(ay_g, 3) + " az=" + String(az_g, 3) +
                     " gx=" + String(gx_dps, 1) + " gy=" + String(gy_dps, 1) + " gz=" + String(gz_dps, 1) +
                     " gyroMag=" + String(gyroMag, 1) + " jerk=" + String(jerk, 2));
        }
    }

    if (ledState != LED_FLASH_ACCEPT && ledState != LED_FLASH_REJECT)
    {
        if (gyroMag > LOCAL_ACTIVE_HINT_DPS)
            ledLocalActive();
        else if (ledState == LED_ACTIVE)
            ledIdle();
    }

    if (now - lastBeaconMs >= BEACON_INTERVAL_MS)
    {
        lastBeaconMs = now;
        broadcastBeacon(gyroMag);
    }
}
