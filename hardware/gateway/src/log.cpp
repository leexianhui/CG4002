// Hardware debug lines -> relay (gateway_log.h).
#include <Arduino.h>
#include <stdarg.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include "gateway_link.h"
#include "gateway_log.h"

static_assert(sizeof(cg_payload_device_log_t) == 56, "DEVICE_LOG payload size changed");
static_assert(sizeof(cg_payload_device_log_t) <= GATEWAY_LINK_MAX_PACKET,
              "a DEVICE_LOG piece must fit one node-link packet");

namespace {
// The hub logs from its hardware loop and its BLE task, so the line counter is shared.
portMUX_TYPE seqMux = portMUX_INITIALIZER_UNLOCKED;
uint16_t lineSeq;

#if CG_GATEWAY_ROLE == 0
QueueHandle_t queue;
portMUX_TYPE dropMux = portMUX_INITIALIZER_UNLOCKED;
uint32_t dropped;

void put(const cg_payload_device_log_t &piece) {
    if (queue && xQueueSend(queue, &piece, 0) == pdTRUE) return;
    portENTER_CRITICAL(&dropMux);
    dropped++;
    portEXIT_CRITICAL(&dropMux);
}
#endif
} // namespace

void gatewayLog(const char *line) {
    if (!line) return;
    size_t len = strnlen(line, (size_t)GATEWAY_LOG_MAX_PIECES * CG_DEVICE_LOG_TEXT_LEN);
    unsigned pieces = len ? (unsigned)((len + CG_DEVICE_LOG_TEXT_LEN - 1) / CG_DEVICE_LOG_TEXT_LEN) : 1;
    portENTER_CRITICAL(&seqMux);
    uint16_t seq = lineSeq++;
    portEXIT_CRITICAL(&seqMux);
    cg_payload_device_log_t piece;
    piece.device_timestamp_ms = millis();
    piece.source = CG_GATEWAY_ROLE;
    piece.line_seq = seq;
    for (unsigned i = 0; i < pieces; i++) {
        size_t off = (size_t)i * CG_DEVICE_LOG_TEXT_LEN;
        size_t n = len - off < CG_DEVICE_LOG_TEXT_LEN ? len - off : CG_DEVICE_LOG_TEXT_LEN;
        memset(piece.text, 0, sizeof(piece.text));
        memcpy(piece.text, line + off, n);
        piece.part = (uint8_t)(i | (i + 1 < pieces ? CG_DEVICE_LOG_MORE : 0));
#if CG_GATEWAY_ROLE == 0
        put(piece);
#else
        // No-op without a BLE node link or a subscribed hub.
        gatewayLinkSend(GATEWAY_LINK_LOG_TAG, (const uint8_t *)&piece, sizeof(piece));
#endif
    }
}

void gatewayLogf(const char *fmt, ...) {
    char buf[256];
    va_list args;
    va_start(args, fmt);
    vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);
    Serial.print(buf);
    size_t n = strlen(buf);
    while (n && (buf[n - 1] == '\n' || buf[n - 1] == '\r')) buf[--n] = '\0';
    gatewayLog(buf);
}

#if CG_GATEWAY_ROLE == 0
void gatewayLogBegin() {
    if (!queue) queue = xQueueCreate(GATEWAY_LOG_QUEUE, sizeof(cg_payload_device_log_t));
    gatewayLinkOpen(GATEWAY_LINK_LOG_TAG); // receive the nodes' pieces (no-op without a node link)
}

bool gatewayLogTake(cg_payload_device_log_t &piece) {
    if (!queue) return false;
    // Move the nodes' pieces out of the small link queue first, so a burst from a node waits here
    // instead of being overwritten there.
    uint8_t raw[GATEWAY_LINK_MAX_PACKET];
    size_t len;
    while (gatewayLinkTake(GATEWAY_LINK_LOG_TAG, raw, len)) {
        if (len != sizeof(cg_payload_device_log_t)) continue;
        cg_payload_device_log_t node;
        memcpy(&node, raw, sizeof(node));
        put(node);
    }
    return xQueueReceive(queue, &piece, 0) == pdTRUE;
}

uint32_t gatewayLogTakeDropped() {
    portENTER_CRITICAL(&dropMux);
    uint32_t n = dropped;
    dropped = 0;
    portEXIT_CRITICAL(&dropMux);
    return n;
}
#else
void gatewayLogBegin() {}
bool gatewayLogTake(cg_payload_device_log_t &) { return false; }
uint32_t gatewayLogTakeDropped() { return 0; }
#endif
