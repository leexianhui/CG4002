#pragma once
#include <stdint.h>
#include "cg_protocol.h"

// Event mode (default): the hub sends each action segment its own segmenter accepts, once, as
// SEGMENT_CHUNKs, and nodes stop the 50 Hz sample broadcast. 0 restores the continuous 50 Hz
// SENSOR_FRAME stream (all three wearables' samples). Must match on hub and nodes of one player.
#ifndef CG_GATEWAY_EVENT_MODE
#define CG_GATEWAY_EVENT_MODE 1
#endif

struct GatewaySegment;

void gatewayBegin(uint8_t player, uint8_t role);
void gatewayPoll(uint8_t locked_weapon = 0);
void gatewaySample(float ax, float ay, float az, float gx, float gy, float gz);
bool gatewayTakeFeedback(cg_payload_combat_feedback_t &feedback);
void gatewayReadFrame(cg_payload_sensor_frame_t &frame);
uint8_t gatewayWeapon();
bool gatewayQueueFeedback(const uint8_t *payload);
// Hub, hardware thread: the left wrist entered (true) or left (false) the guard pose. Keeps only the
// newest edge, stamped with the hub's clock now. BLE task: takes it once, for a GUARD_EVENT.
void gatewayGuardEvent(bool guarding);
// Hub, hardware thread: whether a weapon may be locked now. False from boot until the relay says the
// player's phone has calibrated (COMBAT_FEEDBACK code 5 allows, 6 blocks; neither buzzes).
bool gatewayWeaponSelectAllowed();
// Hub, hardware thread: true once per match reset (COMBAT_FEEDBACK code 7 WEAPON_UNLOCK): drop the
// locked weapon and detect again. Taking it clears it.
bool gatewayTakeWeaponUnlock();
// Hub, hardware thread: a weapon pose started counting toward the lock (weapon 1-3) or stopped (0).
// Keeps only the newest edge. BLE task: takes it once, for a reliable WEAPON_CANDIDATE.
void gatewayWeaponCandidate(uint8_t weapon, uint32_t event_ms, uint16_t lock_ms);
bool gatewayTakeWeaponCandidate(uint8_t &weapon, uint32_t &event_ms, uint16_t &lock_ms);
// Hub, hardware thread: the operator's SET_WEAPON (COMBAT_FEEDBACK codes 8-10): lock this weapon
// (1-3) now. Taking it clears it.
bool gatewayTakeForcedWeapon(uint8_t &weapon);
bool gatewayTakeGuardEvent(uint8_t &state, uint32_t &event_ms);
// Main-thread haptic PWM; -1 means leave the hardware's local pattern player alone.
int gatewayFeedbackDuty();

// Hub, event mode, hardware thread: hand one accepted segment to the BLE task. Call Begin, then
// Sample for each source sample in time order (g, deg/s, as the segmenter stores them), then End.
// End returns false when nothing was queued. No-ops in continuous builds.
void gatewaySegmentBegin(uint32_t segment_id, uint8_t weapon, uint16_t source_samples);
void gatewaySegmentSample(uint32_t t_ms, float ax, float ay, float az, float gx, float gy, float gz);
bool gatewaySegmentEnd();
// Hub, BLE task: oldest queued segment no older than max_age_ms (older ones are discarded), or
// nullptr. The segment stays valid until gatewaySegmentDone(slot).
const GatewaySegment *gatewaySegmentTake(uint32_t now, uint32_t max_age_ms, int &slot);
void gatewaySegmentDone(int slot);
