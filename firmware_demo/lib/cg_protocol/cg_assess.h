// Device-side handling of the two laptop -> device assessment packets (ASSESS_ECHO_REQ,
// ASSESS_FILE_CHUNK). Kept free of BLE/Arduino so the decision logic can be compiled and reasoned
// about on its own; main.cpp owns the sending, ACKing and replay-state commits.
//
// The Python mirror of this file is comms/src/cg_comms/mock/assess_device.py (used by
// mock/firebeetle_sim.py), which is what the loopback tests exercise. Keep the two identical.
//
// File-transfer contract (one transfer at a time, stop-and-wait, driven by relay/assess_file.py):
//   - chunk_index 0 always starts a NEW transfer, discarding any earlier (complete or abandoned) one.
//     That is how the laptop restarts after an aborted transfer; there is no resume.
//   - Chunks must then arrive in order 1, 2, ... total_chunks-1. Retransmits never reach this code:
//     an identical retransmit is caught earlier by main.cpp's exact-frame cache and only re-ACKed.
//   - Every accepted chunk is answered with FILE_ACK{chunk_index, running_crc, received_bytes}.
//     An out-of-sequence chunk is still answered, with the state left UNCHANGED, so the laptop sees
//     received_bytes disagree with what it sent and aborts with a precise reason instead of
//     retransmitting into a wall until the reliable sender gives up.
//   - A malformed chunk (reserved != 0, bad chunk_len, non-zero padding, index >= total) is a
//     laptop bug, not a link fault: it is neither ACKed nor answered.
#pragma once
#include <stdint.h>
#include <string.h>

#include "cg_assess_transform.h"
#include "cg_protocol.h"

#define CG_ASSESS_FILE_CHUNK_DATA_LEN 32

// CRC-16/CCITT-FALSE, resumable: cg_crc16(d, n) == cg_crc16_update(0xFFFF, d, n), and feeding a
// buffer in pieces gives the same result as feeding it whole. That is what lets the device report a
// running CRC per chunk that the laptop can compare against crc16(file[:received_bytes]).
static inline uint16_t cg_crc16_update(uint16_t crc, const uint8_t *data, uint32_t length) {
    for (uint32_t i = 0; i < length; i++) {
        crc ^= (uint16_t)((uint16_t)data[i] << 8);
        for (int bit = 0; bit < 8; bit++) {
            crc = (crc & 0x8000u) ? (uint16_t)((crc << 1) ^ 0x1021u) : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

// Builds the ASSESS_ECHO_RESP for one request: identity fields echoed unchanged, values modified by
// the keycode-selected transform (cg_assess_transform.h). Values are copied to locals first because
// taking the address of a member of a packed struct is unaligned-pointer territory.
static inline void cg_assess_build_echo_resp(const cg_payload_assess_echo_req_t *req, uint32_t now_ms,
                                             cg_payload_assess_echo_resp_t *resp) {
    int16_t a = req->value_a, b = req->value_b, c = req->value_c;
    uint8_t transform_id = cg_assess_transform_for(req->keycode);
    cg_assess_apply_transform(transform_id, req->keycode, &a, &b, &c);
    memset(resp, 0, sizeof(*resp));  // reserved = 0
    resp->trace_id = req->trace_id;
    resp->device_timestamp_ms = now_ms;
    resp->keycode = req->keycode;
    resp->payload_kind = req->payload_kind;
    resp->transform_id = transform_id;
    resp->value_a = a;
    resp->value_b = b;
    resp->value_c = c;
}

enum CgFileVerdict {
    CG_FILE_MALFORMED = 0,    // do not ACK, do not answer, do not touch replay state
    CG_FILE_APPENDED = 1,     // state advanced; answer + ACK
    CG_FILE_OUT_OF_SEQUENCE = 2,  // state unchanged; answer (so the laptop can see why) + ACK
};

struct CgAssessFileRx {
    bool active = false;       // a transfer has started (chunk 0 seen) this session
    uint16_t total_chunks = 0;
    uint16_t next_index = 0;   // == total_chunks once complete
    uint16_t running_crc = 0xFFFF;
    uint32_t received_bytes = 0;

    void reset() { *this = CgAssessFileRx(); }
    bool complete() const { return active && next_index == total_chunks; }

    // PURE: writes the would-be state to *next and returns the verdict, without modifying this.
    // main.cpp commits *next only once the FILE_ACK was accepted by the BLE stack, so a FILE_ACK
    // dropped on-chip leaves the chunk unACKed and the laptop's identical retransmit is processed
    // as if for the first time -- rather than appending the same bytes twice.
    CgFileVerdict evaluate(const cg_payload_assess_file_chunk_t *chunk, CgAssessFileRx *next,
                           const char **reason) const {
        *next = *this;
        const uint16_t index = chunk->chunk_index;
        const uint16_t total = chunk->total_chunks;
        const uint8_t len = chunk->chunk_len;
        if (chunk->reserved != 0) { *reason = "reserved!=0"; return CG_FILE_MALFORMED; }
        if (len == 0 || len > CG_ASSESS_FILE_CHUNK_DATA_LEN) { *reason = "chunk_len"; return CG_FILE_MALFORMED; }
        if (total == 0 || index >= total) { *reason = "index>=total"; return CG_FILE_MALFORMED; }
        // Only the final chunk may be short; anything else means the sender's chunking is broken.
        if (index + 1u < total && len != CG_ASSESS_FILE_CHUNK_DATA_LEN) { *reason = "short_non_final"; return CG_FILE_MALFORMED; }
        for (unsigned i = len; i < CG_ASSESS_FILE_CHUNK_DATA_LEN; i++) {
            if (chunk->data[i] != 0) { *reason = "padding!=0"; return CG_FILE_MALFORMED; }
        }

        if (index == 0) {
            next->reset();
            next->active = true;
            next->total_chunks = total;
        } else if (!active || total != total_chunks || index != next_index) {
            *reason = "out_of_sequence";
            return CG_FILE_OUT_OF_SEQUENCE;
        }
        next->running_crc = cg_crc16_update(next->running_crc, chunk->data, len);
        next->received_bytes += len;
        next->next_index = (uint16_t)(index + 1u);
        *reason = "ok";
        return CG_FILE_APPENDED;
    }
};
