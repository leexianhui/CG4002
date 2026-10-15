#include "cg_frame.h"

#include <string.h>

// device_nonce is always sent as a 4-byte cleartext LE prefix for HELLO/HELLO_ACK
// (wire_protocol.md §5.1) — this firmware only ever originates HELLO, so the encode side only
// needs to handle that direction, but decode must also handle HELLO_ACK from the laptop.
#define CG_DEVICE_NONCE_SIZE 4

static void put_u16le(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
}

static uint16_t get_u16le(const uint8_t *p) {
    return (uint16_t)(p[0] | (p[1] << 8));
}

size_t cg_encode_frame(uint8_t packet_type, uint16_t seq, uint8_t player_id, const uint8_t *plaintext,
                        size_t plaintext_len, const uint8_t *cleartext_prefix, size_t cleartext_prefix_len,
                        const uint8_t psk[CG_PSK_SIZE], uint32_t nonce_session_or_device_nonce,
                        uint8_t direction, uint32_t ext_seq, uint8_t *out) {
    size_t payload_len = cleartext_prefix_len + plaintext_len + CG_GCM_TAG_SIZE;
    if (CG_HEADER_SIZE + payload_len + CG_CRC_SIZE > CG_MAX_FRAME_SIZE) {
        return 0;
    }

    uint8_t *header = out;
    header[0] = CG_MAGIC;
    header[1] = CG_PROTOCOL_VERSION;
    header[2] = packet_type;
    put_u16le(header + 3, seq);
    header[5] = player_id;
    put_u16le(header + 6, (uint16_t)payload_len);

    uint8_t aad[CG_HEADER_SIZE + CG_DEVICE_NONCE_SIZE];
    memcpy(aad, header, CG_HEADER_SIZE);
    size_t aad_len = CG_HEADER_SIZE;
    if (cleartext_prefix_len > 0) {
        memcpy(out + CG_HEADER_SIZE, cleartext_prefix, cleartext_prefix_len);
        memcpy(aad + CG_HEADER_SIZE, cleartext_prefix, cleartext_prefix_len);
        aad_len += cleartext_prefix_len;
    }

    uint8_t nonce[CG_NONCE_SIZE];
    cg_derive_nonce(nonce, nonce_session_or_device_nonce, player_id, direction, ext_seq);

    uint8_t *sealed_out = out + CG_HEADER_SIZE + cleartext_prefix_len;
    if (!cg_gcm_seal(psk, aad, aad_len, nonce, plaintext, plaintext_len, sealed_out)) {
        return 0;
    }

    size_t body_len = CG_HEADER_SIZE + payload_len;
    uint16_t crc = cg_crc16(out, (uint32_t)body_len);
    put_u16le(out + body_len, crc);
    return body_len + CG_CRC_SIZE;
}

bool cg_frame_precheck(const uint8_t *frame, size_t frame_len, uint16_t *seq_out) {
    if (frame_len < CG_HEADER_SIZE + CG_CRC_SIZE) {
        return false;
    }
    size_t body_len = frame_len - CG_CRC_SIZE;
    uint16_t expected_crc = get_u16le(frame + body_len);
    if (cg_crc16(frame, (uint32_t)body_len) != expected_crc) {
        return false;
    }
    if (frame[0] != CG_MAGIC || frame[1] != CG_PROTOCOL_VERSION) {
        return false;
    }
    uint8_t type_byte = frame[2];
    if (type_byte & CG_TYPE_MORE_FRAGMENTS) {
        return false;  // fragmentation not implemented — hard error, wire_protocol.md §4.6
    }
    uint16_t payload_len = get_u16le(frame + 6);
    if (CG_HEADER_SIZE + payload_len != body_len) {
        return false;
    }
    *seq_out = get_u16le(frame + 3);
    return true;
}

size_t cg_decode_frame(const uint8_t *frame, size_t frame_len, size_t cleartext_prefix_len,
                        const uint8_t psk[CG_PSK_SIZE], uint32_t nonce_session_or_device_nonce,
                        uint8_t direction, uint32_t ext_seq, uint8_t *packet_type_out, uint16_t *seq_out,
                        uint8_t *player_id_out, uint8_t *plaintext_out, size_t plaintext_cap) {
    uint16_t seq;
    if (!cg_frame_precheck(frame, frame_len, &seq)) {
        return 0;
    }
    size_t body_len = frame_len - CG_CRC_SIZE;
    uint8_t type_byte = frame[2];
    uint8_t player_id = frame[5];
    uint16_t payload_len = get_u16le(frame + 6);
    if (payload_len < cleartext_prefix_len + CG_GCM_TAG_SIZE) {
        return 0;
    }

    uint8_t aad[CG_HEADER_SIZE + CG_DEVICE_NONCE_SIZE];
    memcpy(aad, frame, CG_HEADER_SIZE);
    size_t aad_len = CG_HEADER_SIZE;
    if (cleartext_prefix_len > 0) {
        memcpy(aad + CG_HEADER_SIZE, frame + CG_HEADER_SIZE, cleartext_prefix_len);
        aad_len += cleartext_prefix_len;
    }

    const uint8_t *sealed = frame + CG_HEADER_SIZE + cleartext_prefix_len;
    size_t sealed_len = payload_len - cleartext_prefix_len;
    size_t plaintext_len = sealed_len - CG_GCM_TAG_SIZE;
    if (plaintext_len > plaintext_cap) {
        return 0;
    }

    uint8_t nonce[CG_NONCE_SIZE];
    cg_derive_nonce(nonce, nonce_session_or_device_nonce, player_id, direction, ext_seq);

    if (!cg_gcm_open(psk, aad, aad_len, nonce, sealed, sealed_len, plaintext_out)) {
        return 0;
    }

    *packet_type_out = type_byte & CG_TYPE_MASK;
    *seq_out = seq;
    *player_id_out = player_id;
    return plaintext_len;
}
