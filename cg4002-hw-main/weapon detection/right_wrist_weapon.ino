// ============================================================================
// RIGHT WRIST NODE — weapon-detection feature (prototype, standalone from
// right_wrist.ino). Acts as the fusion hub for this feature: reads its own
// MPU6500 pose, listens for WristPose (left wrist) and TorsoPose (torso)
// broadcasts, and once all three readings are fresh + held still, classifies
// which of the three weapons the player is holding.
//
// Weapons: sword & shield / spear / mace. Detection is a held "ready stance"
// check (not continuous mid-swing), so classification only runs once a
// reading has been stationary for STILLNESS_HOLD_MS — see left_wrist_weapon
// .ino / torso_weapon.ino for how each node computes + gates its own pose.
//
// This file intentionally does NOT touch right_wrist.ino — it's a separate
// feature, prototyped standalone (same precedent as motor_test_only.ino
// being a standalone bench sketch).
//
// Keep the WristPose/TorsoPose structs identical to the copies in
// left_wrist_weapon.ino / torso_weapon.ino — they're the wire protocol.
// ============================================================================

#include <Wire.h>
#include <WiFi.h>
#include <WiFiUdp.h>

// Must come before ANY other statement in the file (not just "near the
// top") — the Arduino IDE's auto-generated function prototypes get
// inserted right before the first real C++ statement it finds (comments,
// blank lines and #define/#include are skipped, but e.g. the `ssid`
// assignment below counts). classifyWeapon()'s auto-generated prototype
// needs this type visible by that insertion point, wherever it ends up,
// so this has to be the very first real declaration in the file.
enum WeaponType
{
    WEAPON_UNKNOWN,
    WEAPON_SWORD_SHIELD,
    WEAPON_SPEAR,
    WEAPON_MACE
};

// ================= CONFIG =================
#define RIGHT_WRIST_ID 0
#define TORSO_ID 1
#define LEFT_WRIST_ID 2

const char *ssid = "xin hui's laptop";
const char *password = "12345678";

IPAddress broadcastIP(192, 168, 137, 255);
const int DEBUG_PORT = 4211;  // -> laptop listener.py (noisy raw-stream text, all 3 nodes)
const int POSE_PORT = 4212;   // <- left wrist + torso (binary pose packets)
const int RESULT_PORT = 4213; // -> laptop result_listener.py (weapon= lines only)

#define DEBUG_LOG true

WiFiUDP debugUdp;
WiFiUDP poseUdp;

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
void ledSampling() { ledSetColor(0, 255, 0); }        // green — no weapon classified yet
void ledDetected() { ledSetColor(0, 0, 255); }        // blue — a weapon has been classified

// ---------------- Wire structs (must match left_wrist_weapon.ino / torso_weapon.ino) ----------------
struct __attribute__((packed)) WristPose
{
    uint8_t deviceId; // LEFT_WRIST_ID or RIGHT_WRIST_ID
    uint32_t ms;
    float pitch, roll;
    bool stable;
};

struct __attribute__((packed)) TorsoPose
{
    uint8_t deviceId; // TORSO_ID
    uint32_t ms;
    float pitch, roll;
    bool stable;
};

// Broadcast out to left wrist + torso so their status LEDs can reflect the
// hub's actual detection result instead of just their own stillness. Only
// 2 bytes, so it can never collide with WristPose/TorsoPose (14 bytes) on
// the same port — size alone is enough to tell them apart on the receiver.
struct __attribute__((packed)) DetectionStatus
{
    uint8_t deviceId; // RIGHT_WRIST_ID (source)
    bool detected;
};

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
    Serial.println("Gyro calibration done.");
}

// ---------------- Pose-zero calibration ----------------
// Mounting orientation on the wrist isn't guaranteed to match the torso
// mount's axis convention, so raw pitch/roll from atan2() isn't directly
// comparable across devices. Capture a zero-reference here (player holds
// arm in a neutral reference stance, e.g. relaxed at side) and subtract it
// from every later reading, the same way calibrateGyro() zeroes gyro drift.
float pitchZero = 0, rollZero = 0;

float computePitchDeg(float ay_g, float az_g)
{
    return atan2(ay_g, az_g) * 180.0 / PI;
}

float computeRollDeg(float ax_g, float az_g)
{
    return atan2(-ax_g, az_g) * 180.0 / PI;
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

// atan2() only returns [-180,180]; subtracting a zero-offset (or blending
// two readings in the EMA below) can land outside that range right at the
// wrap boundary (e.g. true angle -178 read as +179 one sample later),
// producing bogus ~360-degree jumps. Fold back into [-180,180].
float wrapAngle180(float deg)
{
    while (deg > 180.0)
        deg -= 360.0;
    while (deg < -180.0)
        deg += 360.0;
    return deg;
}

// ---------------- Debug logging ----------------
void debugLog(String msg)
{
    if (!DEBUG_LOG)
        return;
    debugUdp.beginPacket(broadcastIP, DEBUG_PORT);
    debugUdp.print("[right_wrist] " + msg);
    debugUdp.endPacket();
}

// Same idea as debugLog(), but on its own port (RESULT_PORT) — so the
// occasional weapon= line doesn't get buried in the ~3x/sec raw-stream
// debug output from all three nodes on DEBUG_PORT. Reuses debugUdp; a
// WiFiUDP socket can send to any destination port regardless of which
// port it's locally bound to for receiving.
void resultLog(String msg)
{
    debugUdp.beginPacket(broadcastIP, RESULT_PORT);
    debugUdp.print("[right_wrist] " + msg);
    debugUdp.endPacket();
}

// ---------------- Smoothing + stillness gate (own pose) ----------------
const float EMA_ALPHA = 0.2;
const float STILLNESS_GYRO_THRESHOLD_DPS = 15.0; // TODO: tune against real logs
const unsigned long STILLNESS_HOLD_MS = 700;     // TODO: tune against real logs

// Continuous debug print (independent of the stable/unstable edge, and
// independent of classification) so you can watch live pitch/roll for all
// three nodes while tuning thresholds. Leave on during calibration; flip
// off once thresholds are locked in.
#define DEBUG_RAW_STREAM true
const unsigned long RAW_LOG_INTERVAL_MS = 300;
unsigned long lastRawLogMs = 0;

float pitchEMA = 0, rollEMA = 0;
float gyroMagEMA = 0; // smoothed before the stillness check, so one noisy
                       // 100Hz sample (hand tremor) doesn't reset the hold timer
bool emaInitialized = false;
unsigned long stillSinceMs = 0;
bool ledDetectedState = false;

const unsigned long DETECT_BROADCAST_INTERVAL_MS = 100;
unsigned long lastDetectBroadcastMs = 0;

const unsigned long SAMPLE_PERIOD_US = 10000UL; // 100Hz
unsigned long lastSampleUs = 0;

// ---------------- Incoming poses from left wrist + torso ----------------
const unsigned long POSE_FRESHNESS_MS = 400; // drop stale packets (missed beacons, disconnect)

float leftPitch = 0, leftRoll = 0;
bool leftStable = false;
unsigned long lastLeftRxMs = 0;

float torsoPitch = 0, torsoRoll = 0;
bool torsoStable = false;
unsigned long lastTorsoRxMs = 0;

void checkIncomingPose()
{
    uint8_t buf[32];
    int packetSize = poseUdp.parsePacket();
    if (packetSize <= 0)
        return;

    int len = poseUdp.read(buf, sizeof(buf));
    // WristPose and TorsoPose share the exact same wire layout, so their
    // sizes are identical — size alone can't tell them apart. Parse once
    // and dispatch on deviceId instead.
    if (len != sizeof(WristPose))
        return;

    WristPose p;
    memcpy(&p, buf, sizeof(p));

    if (p.deviceId == LEFT_WRIST_ID)
    {
        leftPitch = p.pitch;
        leftRoll = p.roll;
        leftStable = p.stable;
        lastLeftRxMs = millis();
    }
    else if (p.deviceId == TORSO_ID)
    {
        torsoPitch = p.pitch;
        torsoRoll = p.roll;
        torsoStable = p.stable;
        lastTorsoRxMs = millis();
    }
}

// ---------------- Weapon classification ----------------
// Calibrated from real torso-corrected captures (2026-09-18, xin hui):
//   mace           rPitch=-68 rRoll=+33  lPitch=+1 lRoll=+59
//   sword & shield rPitch=-75 rRoll=-12  lPitch=+2 lRoll=+59
//   spear          rPitch=+29 rRoll=-23  lPitch=+3 lRoll=+52
//
// lPitch/lRoll are NOT used below — in this capture the left arm read
// almost identically across all three poses (didn't actually move to the
// shield/forward/stomach positions the design called for), so there's no
// real per-weapon signal there yet. Once re-captured with the left arm
// genuinely repositioned per weapon, lPitch/lRoll should be reintroduced
// to disambiguate cases the right wrist alone can't (there are none in
// this data, but a 4th weapon or edge case might need it later).
//
// Right wrist alone separates all three with wide margins: spear's pitch
// (+29) is ~100 degrees from mace/sword&shield's (-68/-75), and mace vs
// sword&shield split cleanly on roll (+33 vs -12). Thresholds below sit
// roughly halfway between each pair, with a buffer gap either side that
// falls through to WEAPON_UNKNOWN instead of guessing.
const float SPEAR_RIGHT_PITCH_MIN = 0.0;        // spear (+29) vs mace/sword (-68/-75)
const float MACE_SWORD_RIGHT_PITCH_MAX = -30.0; // anything above this (but below SPEAR_RIGHT_PITCH_MIN) is ambiguous
const float MACE_RIGHT_ROLL_MIN = 10.0;         // mace (+33) vs sword&shield (-12)
const float SWORD_RIGHT_ROLL_MAX = -5.0;        // sword&shield (-12) vs mace (+33)

WeaponType classifyWeapon(float rPitch, float rRoll, float lPitch, float lRoll)
{
    if (rPitch >= SPEAR_RIGHT_PITCH_MIN)
        return WEAPON_SPEAR;

    if (rPitch <= MACE_SWORD_RIGHT_PITCH_MAX)
    {
        if (rRoll >= MACE_RIGHT_ROLL_MIN)
            return WEAPON_MACE;
        if (rRoll <= SWORD_RIGHT_ROLL_MAX)
            return WEAPON_SWORD_SHIELD;
    }

    return WEAPON_UNKNOWN;
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

WeaponType lastReported = WEAPON_UNKNOWN;

void setup()
{
    Serial.begin(115200);
    Wire.begin();
    Wire.setClock(400000);

    pinMode(LED_R_PIN, OUTPUT);
    pinMode(LED_G_PIN, OUTPUT);
    pinMode(LED_B_PIN, OUTPUT);
    ledCalibrating();

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
    poseUdp.begin(POSE_PORT);

    writeRegister(REG_PWR_MGMT_1, 0x00);
    delay(100);
    writeRegister(REG_GYRO_CONFIG, 0x18);  // +/-2000dps
    writeRegister(REG_ACCEL_CONFIG, 0x10); // +/-8g

    calibrateGyro();
    delay(100);
    calibratePoseZero();
    delay(100);

    ledSampling();
    debugLog("Right-wrist weapon-detect hub initialized at IP " + WiFi.localIP().toString());
}

void loop()
{
    checkIncomingPose();

    unsigned long nowUs = micros();
    if (nowUs - lastSampleUs < SAMPLE_PERIOD_US)
        return;
    lastSampleUs = nowUs;

    int16_t raw[7];
    if (!readIMUBurst(raw))
        return;

    float ax_g = raw[0] / ACCEL_SCALE;
    float ay_g = raw[1] / ACCEL_SCALE;
    float az_g = raw[2] / ACCEL_SCALE;
    float gx_dps = (raw[4] / GYRO_SCALE) - gyroXoffset;
    float gy_dps = (raw[5] / GYRO_SCALE) - gyroYoffset;
    float gz_dps = (raw[6] / GYRO_SCALE) - gyroZoffset;

    float gyroMag = sqrt(gx_dps * gx_dps + gy_dps * gy_dps + gz_dps * gz_dps);

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

    unsigned long now = millis();
    if (gyroMagEMA > STILLNESS_GYRO_THRESHOLD_DPS)
        stillSinceMs = now;
    bool rightStable = (now - stillSinceMs) >= STILLNESS_HOLD_MS;

    bool leftFresh = (now - lastLeftRxMs) < POSE_FRESHNESS_MS;
    bool torsoFresh = (now - lastTorsoRxMs) < POSE_FRESHNESS_MS;

    bool nowDetected = false;
    if (rightStable && leftFresh && leftStable && torsoFresh && torsoStable)
    {
        // Torso-relative correction: cancel out stance lean before comparing
        // wrist angles against the (upright-stance) thresholds above.
        float rPitchCorrected = wrapAngle180(pitchEMA - torsoPitch);
        float rRollCorrected = wrapAngle180(rollEMA - torsoRoll);
        float lPitchCorrected = wrapAngle180(leftPitch - torsoPitch);
        float lRollCorrected = wrapAngle180(leftRoll - torsoRoll);

        WeaponType w = classifyWeapon(rPitchCorrected, rRollCorrected, lPitchCorrected, lRollCorrected);
        nowDetected = (w != WEAPON_UNKNOWN);

        if (w != lastReported)
        {
            resultLog("weapon=" + String(weaponName(w)) +
                      " rPitch=" + String(rPitchCorrected, 1) + " rRoll=" + String(rRollCorrected, 1) +
                      " lPitch=" + String(lPitchCorrected, 1) + " lRoll=" + String(lRollCorrected, 1));
            lastReported = w;
        }
    }

    // Blue only while a weapon is actively classified right now — drops back
    // to green the instant any input goes stale/unstable or the pose no
    // longer matches a known weapon, not just when the reported label changes.
    if (nowDetected != ledDetectedState)
    {
        nowDetected ? ledDetected() : ledSampling();
        ledDetectedState = nowDetected;
    }

    // Broadcast the same detection result so left wrist/torso can mirror it
    // on their own LEDs — periodic (not edge-only) so a board that reboots
    // or joins late still picks up the current state within one interval.
    if (now - lastDetectBroadcastMs >= DETECT_BROADCAST_INTERVAL_MS)
    {
        lastDetectBroadcastMs = now;
        DetectionStatus status;
        status.deviceId = RIGHT_WRIST_ID;
        status.detected = nowDetected;
        poseUdp.beginPacket(broadcastIP, POSE_PORT);
        poseUdp.write((uint8_t *)&status, sizeof(status));
        poseUdp.endPacket();
    }

#if DEBUG_RAW_STREAM
    if (now - lastRawLogMs >= RAW_LOG_INTERVAL_MS)
    {
        lastRawLogMs = now;
        debugLog("raw rPitch=" + String(pitchEMA, 1) + " rRoll=" + String(rollEMA, 1) +
                  " rGyroMag=" + String(gyroMagEMA, 1) + " rStable=" + String(rightStable ? 1 : 0) +
                  " | left pitch=" + String(leftPitch, 1) + " roll=" + String(leftRoll, 1) +
                  " fresh=" + String(leftFresh ? 1 : 0) + " stable=" + String(leftStable ? 1 : 0) +
                  " | torso pitch=" + String(torsoPitch, 1) + " roll=" + String(torsoRoll, 1) +
                  " fresh=" + String(torsoFresh ? 1 : 0) + " stable=" + String(torsoStable ? 1 : 0));
    }
#endif
}
