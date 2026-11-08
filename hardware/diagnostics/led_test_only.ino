// Minimal RGB LED test sketch.
// No WiFi, no IMU, no motor — just cycles the common-cathode RGB LED
// so we can confirm the wiring and each channel works, independent
// of everything else in the full project.
//
// Pins match combined/*_game.ino (R=25, G=26, B=27).
// If the colours come out inverted (off when they should be on), the LED
// is actually common anode — set COMMON_ANODE to true.

#define LED_R_PIN 25
#define LED_G_PIN 26
#define LED_B_PIN 27

#define COMMON_ANODE false

void ledSetColor(int r, int g, int b)
{
    if (COMMON_ANODE)
    {
        r = 255 - r;
        g = 255 - g;
        b = 255 - b;
    }
    analogWrite(LED_R_PIN, r);
    analogWrite(LED_G_PIN, g);
    analogWrite(LED_B_PIN, b);
}

void showColor(const char *name, int r, int g, int b, int ms)
{
    Serial.println(name);
    ledSetColor(r, g, b);
    delay(ms);
}

void setup()
{
    Serial.begin(115200);
    delay(500);
    Serial.println("RGB LED test starting...");

    pinMode(LED_R_PIN, OUTPUT);
    pinMode(LED_G_PIN, OUTPUT);
    pinMode(LED_B_PIN, OUTPUT);
    ledSetColor(0, 0, 0); // ensure it starts off
}

void loop()
{
    // 1. Each channel on its own — checks wiring of every leg
    showColor("RED only (pin 25)", 255, 0, 0, 1000);
    showColor("GREEN only (pin 26)", 0, 255, 0, 1000);
    showColor("BLUE only (pin 27)", 0, 0, 255, 1000);
    showColor("OFF", 0, 0, 0, 500);

    // 2. Mixed colours — same ones the game sketches use
    showColor("WHITE (booting)", 255, 255, 255, 1000);
    showColor("DIM GREEN (idle)", 0, 40, 0, 1000);
    showColor("AMBER (low battery)", 255, 80, 0, 1000);
    showColor("OFF", 0, 0, 0, 500);

    // 3. Brightness fade on each channel — checks PWM works
    Serial.println("Fading R, G, B");
    for (int ch = 0; ch < 3; ch++)
    {
        for (int v = 0; v <= 255; v += 5)
        {
            ledSetColor(ch == 0 ? v : 0, ch == 1 ? v : 0, ch == 2 ? v : 0);
            delay(10);
        }
        for (int v = 255; v >= 0; v -= 5)
        {
            ledSetColor(ch == 0 ? v : 0, ch == 1 ? v : 0, ch == 2 ? v : 0);
            delay(10);
        }
    }

    showColor("OFF", 0, 0, 0, 1500);
}
