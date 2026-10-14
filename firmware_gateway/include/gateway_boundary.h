#pragma once
#include <stdint.h>
#include "cg_protocol.h"
void gatewayBegin(uint8_t player, uint8_t role);
void gatewayPoll(uint8_t locked_weapon = 0);
void gatewaySample(float ax, float ay, float az, float gx, float gy, float gz);
bool gatewayTakeFeedback(cg_payload_combat_feedback_t &feedback);
void gatewayReadFrame(cg_payload_sensor_frame_t &frame);
uint8_t gatewayWeapon();
bool gatewayQueueFeedback(const uint8_t *payload);
// Main-thread haptic PWM; -1 means leave the hardware's local pattern player alone.
int gatewayFeedbackDuty();
