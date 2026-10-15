// AES-128-GCM seal/open + nonce derivation for the CG4002 wire protocol.
//
// Mirrors comms/src/cg_comms/protocol/crypto.py (derive_nonce/seal/open_sealed) using mbedtls,
// which the ESP32 Arduino core already bundles — no new crypto library needed. See
// shared/protocol/wire_protocol.md §5 for the normative nonce/AAD layout this implements.

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cg_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

// 12-byte nonce = sessionId(u32) | playerId(u8) | direction(u8) | extSeq(u32) | reserved(u16=0).
// For HELLO/HELLO_ACK, pass deviceNonce in place of sessionId (wire_protocol.md §5.1).
void cg_derive_nonce(uint8_t out[CG_NONCE_SIZE], uint32_t session_id_or_device_nonce, uint8_t player_id,
                      uint8_t direction, uint32_t ext_seq);

// Writes plaintext_len bytes of ciphertext followed by a 16-byte tag into `out`
// (caller provides plaintext_len + CG_GCM_TAG_SIZE bytes). Returns true on success.
bool cg_gcm_seal(const uint8_t psk[CG_PSK_SIZE], const uint8_t *aad, size_t aad_len,
                  const uint8_t nonce[CG_NONCE_SIZE], const uint8_t *plaintext, size_t plaintext_len,
                  uint8_t *out);

// `ciphertext_and_tag_len` is ciphertext length including the trailing 16-byte tag (matches wire
// LEN). Writes (ciphertext_and_tag_len - 16) bytes of plaintext into `out`. Returns false on
// auth failure (tampered frame or wrong key/nonce) — callers must treat that as a dropped packet.
bool cg_gcm_open(const uint8_t psk[CG_PSK_SIZE], const uint8_t *aad, size_t aad_len,
                  const uint8_t nonce[CG_NONCE_SIZE], const uint8_t *ciphertext_and_tag,
                  size_t ciphertext_and_tag_len, uint8_t *out);

#ifdef __cplusplus
}
#endif
