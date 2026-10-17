// Wire-frame encode/decode: MAGIC VERSION TYPE SEQ PLAYER_ID LEN PAYLOAD CRC16.
// Mirrors comms/src/cg_comms/protocol/codec.py (encode_packet/decode_packet). See
// shared/protocol/wire_protocol.md §4-§5 for the normative layout this implements.

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cg_crypto.h"
#include "cg_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

// Max frame this firmware ever sends: header(8) + max plaintext payload(208, SEGMENT_CHUNK) +
// tag(16) + crc(2) = 234, rounded up.
#define CG_MAX_FRAME_SIZE 240
// Max frame this firmware ever receives (laptop -> device; the largest is HELLO_ACK at 46 bytes).
// Kept separate so the incoming queue and duplicate cache do not grow with the uplink.
#define CG_MAX_RX_FRAME_SIZE 96

// Builds one wire frame into `out` (caller-provided CG_MAX_FRAME_SIZE buffer) and returns its
// length, or 0 on failure (encryption error or oversized payload).
//
// `cleartext_prefix`/`cleartext_prefix_len` is the HELLO/HELLO_ACK device_nonce special case
// (wire_protocol.md §5.1) — pass cleartext_prefix_len=0 for every other packet type.
// `nonce_session_or_device_nonce` is session_id normally, or device_nonce for HELLO/HELLO_ACK.
size_t cg_encode_frame(uint8_t packet_type, uint16_t seq, uint8_t player_id, const uint8_t *plaintext,
                        size_t plaintext_len, const uint8_t *cleartext_prefix, size_t cleartext_prefix_len,
                        const uint8_t psk[CG_PSK_SIZE], uint32_t nonce_session_or_device_nonce,
                        uint8_t direction, uint32_t ext_seq, uint8_t *out);

// Validates a frame's length/CRC/magic/version/fragmentation-bit without touching the replay
// window or attempting decryption, and writes out the wire seq on success. Callers MUST run this
// (or cg_decode_frame, which calls it internally) before feeding a wire seq to
// CgSequenceTracker::accept() — otherwise a frame corrupted in transit (bad CRC) can consume a
// replay-window slot before it's known to be genuine, wrongly advancing or gapping the window for
// subsequent legitimate packets. Mirrors the check order in codec.py::decode_packet, which
// validates CRC/magic/version/fragmentation before ever touching its SequenceTracker.
bool cg_frame_precheck(const uint8_t *frame, size_t frame_len, uint16_t *seq_out);

// Decodes one wire frame already isolated by the caller (BLE notifications arrive one frame at a
// time, so no MAGIC-resync scanning is implemented here — see cg_frame.cpp for the rationale).
// `cleartext_prefix_len` must match what the sender used (4 for HELLO/HELLO_ACK, 0 otherwise).
// On success, writes packet_type/seq/player_id and up to `plaintext_cap` bytes of plaintext into
// the out-params, returns the plaintext length; returns 0 and leaves out-params untouched on any
// decode failure (bad magic/version, CRC mismatch, or GCM auth failure — all must be treated as a
// dropped packet, never surfaced as a partial result).
size_t cg_decode_frame(const uint8_t *frame, size_t frame_len, size_t cleartext_prefix_len,
                        const uint8_t psk[CG_PSK_SIZE], uint32_t nonce_session_or_device_nonce,
                        uint8_t direction, uint32_t ext_seq, uint8_t *packet_type_out, uint16_t *seq_out,
                        uint8_t *player_id_out, uint8_t *plaintext_out, size_t plaintext_cap);

#ifdef __cplusplus
}
#endif
