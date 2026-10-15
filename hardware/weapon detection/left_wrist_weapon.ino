// ============================================================================
// LEFT WRIST NODE — weapon-detection feature (prototype, standalone).
//
// No left_wrist.ino existed in this repo at the time this was written — this
// file follows the same conventions as right_wrist.ino/torso.ino (same
// Sample-style struct shape, same WiFi/UDP broadcast pattern) so it's a
// drop-in match for the existing two-node design. If the real physical
// left-wrist firmware differs, adjust this file to match its actual wire
// format instead.
//
// Role:
//   - Reads its own MPU6500, computes gravity-relative pitch/roll (static
//     tilt only — no gyro integration, so no drift; this is a held-pose
//     reading, not a continuous-motion one).
//   - Gates readings on "stillness" (gyro magnitude below threshold for a
//     sustained window) so a reading is only trusted once the player is
//     holding the ready stance still.
//   - Broadcasts a lightweight WristPose UDP packet. The right-wrist hub
//     (see right_wrist_weapon.ino) fuses this with its own pose + the
//     torso's pose to classify the weapon.
//
// Keep the WristPose struct identical to the copy in right_wrist_weapon.ino
// — it's the wire protocol between the two.
// ============================================================================

#include <Wire.h>
#include <WiFi.h>
#include <WiFiUdp.h>

// ================= CONFIG =================
#define LEFT_WRIST_ID 2
#define RIGHT_WRIST_ID 0

const char *ssid = "xin hui's laptop";
const char *password = "12345678";

IPAddress broadcastIP(192, 168, 137, 255);
const int DEBUG_PORT = 4211; // -> laptop listener.py (human-readable text)
const int POSE_PORT = 4212;  // <-> right-wrist hub (binary WristPose out, DetectionStatus in)

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
void ledSampling() { ledSetColor(0, 255, 0); }        // green — hub hasn't detected a weapon
void ledDetected() { ledSetColor(0, 0, 255); }        // blue — hub says a weapon is detected

// ---------------- Wire structs (must match right_wrist_weapon.ino) ----------------
struct __attribute__((packed)) WristPose
{
    uint8_t deviceId; // LEFT_WRIST_ID (or RIGHT_WRIST_ID, unused here)
    uint32_t ms;
    float pitch, roll;
    bool stable;
};

// Mirrors right_wrist_weapon.ino's DetectionStatus — only 2 bytes, so it
// can never collide in size with WristPose (14 bytes) on the same port.
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
    debugUdp.print("[left_wrist] " + msg);
    debugUdp.endPacket();
}

// ---------------- Smoothing + stillness gate ----------------
const float EMA_ALPHA = 0.2; // smoothing for pitch/roll display
const float STILLNESS_GYRO_THRESHOLD_DPS = 15.0; // TODO: tune against real logs
const unsigned long STILLNESS_HOLD_MS = 700;     // TODO: tune against real logs
const unsigned long POSE_BROADCAST_INTERVAL_MS = 100;

// Continuous debug print (independent of the stable/unstable edge) so you
// can watch live pitch/roll while tuning thresholds. Leave on during
// calibration; flip off once thresholds are locked in.
#define DEBUG_RAW_STREAM true
const unsigned long RAW_LOG_INTERVAL_MS = 300;
unsigned long lastRawLogMs = 0;

float pitchEMA = 0, rollEMA = 0;
float gyroMagEMA = 0; // smoothed before the stillness check, so one noisy
                       // 100Hz sample (hand tremor) doesn't reset the hold timer
bool emaInitialized = false;
unsigned long stillSinceMs = 0;
bool wasStill = false;
unsigned long lastPoseBroadcastMs = 0;

// ---------------- Detection status from right-wrist hub ----------------
bool weaponDetected = false;
bool ledDetectedState = false;

void checkIncomingDetection()
{
    uint8_t buf[32];
    int packetSize = poseUdp.parsePacket();
    if (packetSize <= 0)
        return;

    int len = poseUdp.read(buf, sizeof(buf));
    if (len != sizeof(DetectionStatus))
        return; // some other packet on this port (e.g. our own broadcast looping back)

    DetectionStatus status;
    memcpy(&status, buf, sizeof(status));
    if (status.deviceId == RIGHT_WRIST_ID)
        weaponDetected = status.detected;
}

const unsigned long SAMPLE_PERIOD_US = 10000UL; // 100Hz
unsigned long lastSampleUs = 0;

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
    debugLog("Left-wrist weapon-pose node initialized at IP " + WiFi.localIP().toString());
}

void loop()
{
    checkIncomingDetection();

    if (weaponDetected != ledDetectedState)
    {
        weaponDetected ? ledDetected() : ledSampling();
        ledDetectedState = weaponDetected;
    }

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
    {
        stillSinceMs = now; // reset the hold timer, still moving
        wasStill = false;
    }
    bool stable = (now - stillSinceMs) >= STILLNESS_HOLD_MS;

    if (now - lastPoseBroadcastMs >= POSE_BROADCAST_INTERVAL_MS)
    {
        lastPoseBroadcastMs = now;

        WristPose pose;
        pose.deviceId = LEFT_WRIST_ID;
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

#if DEBUG_RAW_STREAM
    if (now - lastRawLogMs >= RAW_LOG_INTERVAL_MS)
    {
        lastRawLogMs = now;
        debugLog("raw pitch=" + String(pitchEMA, 1) + " roll=" + String(rollEMA, 1) +
                  " gyroMag=" + String(gyroMagEMA, 1) + " stable=" + String(stable ? 1 : 0));
    }
#endif
}
