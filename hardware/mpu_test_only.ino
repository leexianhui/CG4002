// Standalone MPU6500 check for the ESP32 boards in this project.
// Upload, then open Serial Monitor at 115200 baud. Tilt or rotate the board:
// acceleration (g) and angular velocity (degrees/second) should change.
// Uses the project's default I2C pins (Wire.begin()) and address 0x68.

#include <Wire.h>

const uint8_t MPU_ADDR = 0x68;
const uint8_t WHO_AM_I = 0x75;
const uint8_t PWR_MGMT_1 = 0x6B;
const uint8_t GYRO_CONFIG = 0x1B;
const uint8_t ACCEL_CONFIG = 0x1C;
const uint8_t ACCEL_XOUT_H = 0x3B;

// Match the main firmware: +/-8 g and +/-2000 degrees/second.
const float ACCEL_LSB_PER_G = 4096.0f;
const float GYRO_LSB_PER_DPS = 16.4f;

bool readRegister(uint8_t reg, uint8_t &value)
{
    Wire.beginTransmission(MPU_ADDR);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0 || Wire.requestFrom((int)MPU_ADDR, 1) != 1)
        return false;
    value = Wire.read();
    return true;
}

bool writeRegister(uint8_t reg, uint8_t value)
{
    Wire.beginTransmission(MPU_ADDR);
    Wire.write(reg);
    Wire.write(value);
    return Wire.endTransmission() == 0;
}

bool readMotion(int16_t data[7])
{
    Wire.beginTransmission(MPU_ADDR);
    Wire.write(ACCEL_XOUT_H);
    if (Wire.endTransmission(false) != 0 || Wire.requestFrom((int)MPU_ADDR, 14) != 14)
        return false;

    // Registers contain accel XYZ, temperature, then gyro XYZ.
    for (int i = 0; i < 7; ++i)
    {
        uint16_t high = Wire.read();
        uint16_t low = Wire.read();
        data[i] = (int16_t)((high << 8) | low);
    }
    return true;
}

void setup()
{
    Serial.begin(115200);
    delay(500);
    Wire.begin();
    Serial.println("MPU6500 sensor check (I2C address 0x68)");
}

void loop()
{
    static bool initialized = false;
    if (!initialized)
    {
        uint8_t id;
        if (!readRegister(WHO_AM_I, id))
        {
            Serial.println("No I2C response at 0x68. Check power, SDA, SCL, and ground.");
            delay(1000);
            return;
        }
        Serial.print("WHO_AM_I = 0x");
        Serial.println(id, HEX);
        if (id != 0x70)
        {
            Serial.println("Unexpected ID (MPU6500 should return 0x70).");
            delay(1000);
            return;
        }
        if (!writeRegister(PWR_MGMT_1, 0x00) ||
            !writeRegister(GYRO_CONFIG, 0x18) ||
            !writeRegister(ACCEL_CONFIG, 0x10))
        {
            Serial.println("Sensor found, but initialization failed. Retrying...");
            delay(1000);
            return;
        }
        delay(100);
        Serial.println("Sensor found. Move it to check that the values change.");
        initialized = true;
    }

    int16_t data[7];
    if (!readMotion(data))
    {
        Serial.println("Motion read failed. Retrying sensor initialization...");
        initialized = false;
        delay(500);
        return;
    }

    Serial.print("accel [g] x=");
    Serial.print(data[0] / ACCEL_LSB_PER_G, 2);
    Serial.print(" y=");
    Serial.print(data[1] / ACCEL_LSB_PER_G, 2);
    Serial.print(" z=");
    Serial.print(data[2] / ACCEL_LSB_PER_G, 2);
    Serial.print(" | gyro [dps] x=");
    Serial.print(data[4] / GYRO_LSB_PER_DPS, 1);
    Serial.print(" y=");
    Serial.print(data[5] / GYRO_LSB_PER_DPS, 1);
    Serial.print(" z=");
    Serial.println(data[6] / GYRO_LSB_PER_DPS, 1);
    delay(200);
}
