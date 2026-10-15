// Compile the actual teammate sketch. No synthetic/demo gameplay runs here.
#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <WiFiUdp.h>
// Arduino's .ino preprocessor normally supplies these forward declarations.
void debugLog(String msg);
void onSegmentAck(uint32_t segId, unsigned long now);
float vecMagnitude(float x, float y, float z);
#if CG_GATEWAY_PLAYER == 1
#if CG_GATEWAY_ROLE == 0
#include "../../2 players/p1_right_wrist/p1_right_wrist.ino"
#elif CG_GATEWAY_ROLE == 1
#include "../../2 players/p1_torso/p1_torso.ino"
#else
#include "../../2 players/p1_left_wrist/p1_left_wrist.ino"
#endif
#else
#if CG_GATEWAY_ROLE == 0
#include "../../2 players/p2_right_wrist/p2_right_wrist.ino"
#elif CG_GATEWAY_ROLE == 1
#include "../../2 players/p2_torso/p2_torso.ino"
#else
#include "../../2 players/p2_left_wrist/p2_left_wrist.ino"
#endif
#endif
