// Firmware-side golden-vector test, per shared/protocol/wire_protocol.md §8 ("comms/tests/
// test_golden_vectors.py and firmware/test/test_golden_vectors.cpp both assert against the same
// file"). Runs natively (env:native_test) — no ESP32 needed.
//
// Vectors are transcribed verbatim from shared/protocol/golden_vectors.json (not re-derived from
// this firmware's own code, per that file's independence guarantee — see
// comms/tests/test_golden_vectors.py's docstring). Only checks the parts this firmware can verify
// without mbedtls linked natively: CRC16 over the full frame, and nonce derivation. GCM
// seal/open correctness for real hardware is instead proven the first time this firmware
// handshakes with a real cg-relay (comms/firmware/README.md "Verification").

#include <unity.h>

#include <stdint.h>
#include <string.h>

#include "cg_crypto.h"
#include "cg_protocol.h"

namespace {

size_t hex_decode(const char *hex, uint8_t *out, size_t out_cap) {
    size_t len = strlen(hex) / 2;
    TEST_ASSERT_TRUE(len <= out_cap);
    for (size_t i = 0; i < len; i++) {
        auto nibble = [](char c) -> uint8_t {
            if (c >= '0' && c <= '9') return (uint8_t)(c - '0');
            return (uint8_t)((c | 0x20) - 'a' + 10);
        };
        out[i] = (uint8_t)((nibble(hex[i * 2]) << 4) | nibble(hex[i * 2 + 1]));
    }
    return len;
}

struct Vector {
    const char *name;
    uint32_t session_id_or_device_nonce;
    uint8_t player_id;
    uint8_t direction;
    uint32_t ext_seq;
    const char *nonce_hex;
    const char *wire_frame_hex;  // full frame incl. trailing CRC16
};

// Transcribed from shared/protocol/golden_vectors.json — see file header note above.
const Vector kVectors[] = {
    {"HELLO", 3735928559u, 1, 0, 0, "efbeadde0100000000000000",
     "c901010000012400efbeadde521892b2c2d6084583822ba59b81fb5a4c8578d07410f2ee1fc7550774bc55e0f2e6"},
    {"HELLO_ACK", 3735928559u, 1, 1, 0, "efbeadde0101000000000000",
     "c901020000012400efbeadde9265d4f68e120891bdb1eb821ef372494af5c3a8916665cedd7cfafd000e1ca200e2"},
    {"TIME_SYNC_REQ", 3405691582u, 1, 0, 1, "bebafeca0100010000000000",
     "c901030100011400c20cf3085e5809cfaafbaed01146cafed0b18dad1700"},
    {"TIME_SYNC_RESP", 3405691582u, 1, 1, 1, "bebafeca0101010000000000",
     "c901040100011c001181746b50e16a349b3244dc732ac04db8730b39c8f134f55ddd5bfc189e"},
    {"SENSOR_FRAME", 3405691582u, 1, 0, 42, "bebafeca01002a0000000000",
     "c901102a00013a0026a123e3b35b195f10f313cb279b497d987c35bf04165688d6209a132e0472a08cde3b02affd4"
     "8b295454a102978fa9a5ab74e533766e6de55db03bc"},
    {"RFID_EVENT", 3405691582u, 1, 0, 7, "bebafeca0100070000000000",
     "c901110700011c00f8720a04580c9334e632e24002a802e32184d60be029bf3432c334cab069"},
    {"HEARTBEAT", 3405691582u, 1, 0, 3, "bebafeca0100030000000000",
     "c901200300011c001e9a0feee4d06f4beb52fd7c503f10e16006880ed1536b3b02315e60cec6"},
    {"ACK", 3405691582u, 1, 1, 9, "bebafeca0101090000000000",
     "c9012109000112004c923058bcbfa801fa6a89988897ee252491f0aa"},
    {"COMBAT_FEEDBACK", 3405691582u, 1, 1, 2, "bebafeca0101020000000000",
     "c9013002000116007274d402158a82e0fcb301204de276b9441affaf1dc3efd9"},
};

void test_crc16_matches_every_vector() {
    for (const Vector &v : kVectors) {
        uint8_t frame[128];
        size_t frame_len = hex_decode(v.wire_frame_hex, frame, sizeof(frame));
        uint16_t expected_crc = (uint16_t)(frame[frame_len - 2] | (frame[frame_len - 1] << 8));
        uint16_t actual_crc = cg_crc16(frame, (uint32_t)(frame_len - CG_CRC_SIZE));
        TEST_ASSERT_EQUAL_MESSAGE(expected_crc, actual_crc, v.name);
    }
}

void test_nonce_derivation_matches_every_vector() {
    for (const Vector &v : kVectors) {
        uint8_t expected[CG_NONCE_SIZE];
        hex_decode(v.nonce_hex, expected, sizeof(expected));

        uint8_t actual[CG_NONCE_SIZE];
        cg_derive_nonce(actual, v.session_id_or_device_nonce, v.player_id, v.direction, v.ext_seq);

        TEST_ASSERT_EQUAL_MEMORY_MESSAGE(expected, actual, CG_NONCE_SIZE, v.name);
    }
}

}  // namespace

void setup() {
    UNITY_BEGIN();
    RUN_TEST(test_crc16_matches_every_vector);
    RUN_TEST(test_nonce_derivation_matches_every_vector);
    UNITY_END();
}

void loop() {}

#ifndef ARDUINO
int main() {
    setup();
    return 0;
}
#endif
