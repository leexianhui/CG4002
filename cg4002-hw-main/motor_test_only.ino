// Minimal motor-only test sketch.
// No WiFi, no IMU, no LED — just pulses the vibration motor on and off
// so we can confirm the motor/MOSFET/diode circuit works, independent
// of everything else in the full project.
//
// Flyback diode is installed across the motor terminals, so this is
// safe to run for longer/repeated cycles than the earlier no-diode test.

#define MOTOR_PIN 4

void setup()
{
    Serial.begin(115200);
    delay(500);
    Serial.println("Motor test starting...");

    pinMode(MOTOR_PIN, OUTPUT);
    analogWrite(MOTOR_PIN, 0); // ensure it starts off
}

void loop()
{
    Serial.println("Motor ON (full)");
    analogWrite(MOTOR_PIN, 255); // full duty, should be clearly noticeable
    delay(1000);

    Serial.println("Motor OFF");
    analogWrite(MOTOR_PIN, 0);
    delay(1500);

    Serial.println("Motor ON (half duty - weaker buzz)");
    analogWrite(MOTOR_PIN, 128);
    delay(1000);

    Serial.println("Motor OFF");
    analogWrite(MOTOR_PIN, 0);
    delay(1500);

    Serial.println("Motor - quick double pulse (like a hit-confirm pattern)");
    analogWrite(MOTOR_PIN, 255);
    delay(80);
    analogWrite(MOTOR_PIN, 0);
    delay(80);
    analogWrite(MOTOR_PIN, 255);
    delay(80);
    analogWrite(MOTOR_PIN, 0);
    delay(1500);
}
