#pragma once
#include <stdint.h>
#include "cg_protocol.h"

// Hardware debug lines to the relay, which writes them per match into hardware/game/ CSVs.
// A line travels as DEVICE_LOG pieces (cg_protocol.h, CG_DEVICE_LOG_TEXT_LEN bytes of text each):
//   torso / left wrist -> hub over the BLE node link (tag GATEWAY_LINK_LOG_TAG), then
//   hub -> relay as best-effort CG_PKT_DEVICE_LOG packets, rate-limited by the hub's BLE task.
// Best-effort end to end: a full queue or a busy link drops pieces, never blocks the caller.

// Any board, any task. Called from the sketches' debugLog()/resultLog(). Lines are cut at
// GATEWAY_LOG_MAX_PIECES pieces. A node with no hub connected drops the line.
void gatewayLog(const char *line);
// printf-style: prints to Serial as given (keep the trailing \n) and logs it without the newline.
void gatewayLogf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

// Hub: create the queue. Called from gatewayBegin(); lines logged before it are dropped.
void gatewayLogBegin();
// Hub, BLE task: the next piece to send, its own lines and its nodes' merged. False when empty.
bool gatewayLogTake(cg_payload_device_log_t &piece);
// Hub: pieces dropped for a full queue since the last call.
uint32_t gatewayLogTakeDropped();

#define GATEWAY_LOG_MAX_PIECES 16 // 768 characters
#define GATEWAY_LOG_QUEUE 128     // hub pieces waiting for the relay link (~7 KB)
