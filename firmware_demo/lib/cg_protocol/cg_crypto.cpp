#include "cg_crypto.h"

#include <string.h>

#ifndef CG_NATIVE_TEST
#include <mbedtls/gcm.h>
#endif

void cg_derive_nonce(uint8_t out[CG_NONCE_SIZE], uint32_t session_id_or_device_nonce, uint8_t player_id,
                      uint8_t direction, uint32_t ext_seq) {
    // Matches cg_nonce_t field order exactly (little-endian, packed, 12 bytes):
    // session_id(4) | player_id(1) | direction(1) | ext_seq(4) | reserved(2)=0.
    out[0] = (uint8_t)(session_id_or_device_nonce & 0xFF);
    out[1] = (uint8_t)((session_id_or_device_nonce >> 8) & 0xFF);
    out[2] = (uint8_t)((session_id_or_device_nonce >> 16) & 0xFF);
    out[3] = (uint8_t)((session_id_or_device_nonce >> 24) & 0xFF);
    out[4] = player_id;
    out[5] = direction;
    out[6] = (uint8_t)(ext_seq & 0xFF);
    out[7] = (uint8_t)((ext_seq >> 8) & 0xFF);
    out[8] = (uint8_t)((ext_seq >> 16) & 0xFF);
    out[9] = (uint8_t)((ext_seq >> 24) & 0xFF);
    out[10] = 0;
    out[11] = 0;
}

#ifndef CG_NATIVE_TEST // Native tests cover nonce/CRC/reliability, not the ESP32 mbedTLS implementation.
bool cg_gcm_seal(const uint8_t psk[CG_PSK_SIZE], const uint8_t *aad, size_t aad_len,
                  const uint8_t nonce[CG_NONCE_SIZE], const uint8_t *plaintext, size_t plaintext_len,
                  uint8_t *out) {
    mbedtls_gcm_context ctx;
    mbedtls_gcm_init(&ctx);
    bool ok = mbedtls_gcm_setkey(&ctx, MBEDTLS_CIPHER_ID_AES, psk, CG_PSK_SIZE * 8) == 0;
    if (ok) {
        uint8_t *tag = out + plaintext_len;
        ok = mbedtls_gcm_crypt_and_tag(&ctx, MBEDTLS_GCM_ENCRYPT, plaintext_len, nonce, CG_NONCE_SIZE, aad,
                                        aad_len, plaintext, out, CG_GCM_TAG_SIZE, tag) == 0;
    }
    mbedtls_gcm_free(&ctx);
    return ok;
}

bool cg_gcm_open(const uint8_t psk[CG_PSK_SIZE], const uint8_t *aad, size_t aad_len,
                  const uint8_t nonce[CG_NONCE_SIZE], const uint8_t *ciphertext_and_tag,
                  size_t ciphertext_and_tag_len, uint8_t *out) {
    if (ciphertext_and_tag_len < CG_GCM_TAG_SIZE) {
        return false;
    }
    size_t plaintext_len = ciphertext_and_tag_len - CG_GCM_TAG_SIZE;
    const uint8_t *tag = ciphertext_and_tag + plaintext_len;

    mbedtls_gcm_context ctx;
    mbedtls_gcm_init(&ctx);
    bool ok = mbedtls_gcm_setkey(&ctx, MBEDTLS_CIPHER_ID_AES, psk, CG_PSK_SIZE * 8) == 0;
    if (ok) {
        ok = mbedtls_gcm_auth_decrypt(&ctx, plaintext_len, nonce, CG_NONCE_SIZE, aad, aad_len, tag,
                                       CG_GCM_TAG_SIZE, ciphertext_and_tag, out) == 0;
    }
    mbedtls_gcm_free(&ctx);
    return ok;
}
#endif // !CG_NATIVE_TEST
