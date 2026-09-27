/*
 * CG4002 wire protocol — shared C header.
 *
 * Normative source: shared/protocol/wire_protocol.md (§4 of docs/COMMS_IMPLEMENTATION_PLAN.md).
 * If this header and wire_protocol.md ever disagree, that is a bug — fix both in the same PR.
 *
 * Included by ESP32 firmware (Arduino / ESP-IDF, C++) and mirrored by the Python codec
 * (comms/src/cg_comms/protocol/). No dependencies beyond <stdint.h>.
 *
 * All multi-byte integers are little-endian. Both ends of the link (ESP32 Xtensa/RISC-V, and any
 * laptop CPU this will run on) are little-endian natively, so fields are read/written directly with
 * no byte-swapping.
 */

#ifndef CG_PROTOCOL_H
#define CG_PROTOCOL_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------------------------------- */
/* Protocol version                                                                             */
/* ------------------------------------------------------------------------------------------- */

#define CG_PROTOCOL_VERSION 1

/* ------------------------------------------------------------------------------------------- */
/* Frame layout constants                                                                       */
/* ------------------------------------------------------------------------------------------- */

#define CG_MAGIC 0xC9

/* TYPE byte: bit 7 = MORE_FRAGMENTS, bits 6-0 = packet type. Packet type values are therefore
 * constrained to 0x00-0x7F. */
#define CG_TYPE_MORE_FRAGMENTS 0x80
#define CG_TYPE_MASK           0x7F

#define CG_HEADER_SIZE  8   /* MAGIC..LEN, before PAYLOAD                                      */
#define CG_CRC_SIZE     2   /* trailing CRC16                                                  */
#define CG_GCM_TAG_SIZE 16  /* trailing auth tag, inside PAYLOAD/LEN                            */
#define CG_NONCE_SIZE   12  /* derived AES-GCM nonce, never sent on the wire                    */
#define CG_PSK_SIZE     16  /* AES-128 key                                                      */

/* direction byte used in nonce derivation (see cg_nonce_t below) */
#define CG_DIR_DEVICE_TO_LAPTOP 0
#define CG_DIR_LAPTOP_TO_DEVICE 1

/* ------------------------------------------------------------------------------------------- */
/* Packet types                                                                                 */
/* ------------------------------------------------------------------------------------------- */

#define CG_PKT_HELLO           0x01 /* device -> laptop   reliable    */
#define CG_PKT_HELLO_ACK       0x02 /* laptop -> device   reliable    */
#define CG_PKT_TIME_SYNC_REQ   0x03 /* device -> laptop   reliable    */
#define CG_PKT_TIME_SYNC_RESP  0x04 /* laptop -> device   reliable    */
#define CG_PKT_SENSOR_FRAME    0x10 /* device -> laptop   best-effort */
#define CG_PKT_RFID_EVENT      0x11 /* device -> laptop   reliable    */
#define CG_PKT_HEARTBEAT       0x20 /* both               best-effort */
#define CG_PKT_ACK             0x21 /* both               never acked itself */
#define CG_PKT_COMBAT_FEEDBACK 0x30 /* laptop -> device   reliable    */

/* Assessment/demo extension range 0x40-0x4F. Used only by the comms-assessment demo features
 * (keyboard-driven echo pipeline, file transfer); the real game never sends these.
 *
 * Only the laptop -> device directions are reliable (ACK + retransmit). That is deliberate: the
 * firmware's CgReliableSend (lib/cg_protocol/cg_reliable.h) is a SINGLE SLOT already shared with
 * RFID_EVENT, so a reliable device -> laptop assessment type would contend with the working RFID
 * path. The device only has to ACK these; the _RESP direction is re-driven by the _REQ retransmit. */
#define CG_PKT_ASSESS_ECHO_REQ    0x40 /* laptop -> device   reliable    */
#define CG_PKT_ASSESS_ECHO_RESP   0x41 /* device -> laptop   best-effort */
#define CG_PKT_ASSESS_FILE_CHUNK  0x42 /* laptop -> device   reliable    */
#define CG_PKT_ASSESS_FILE_ACK    0x43 /* device -> laptop   best-effort */
#define CG_PKT_ASSESS_FILE_READ 0x44
#define CG_PKT_ASSESS_FILE_DATA 0x45
#define CG_PKT_ASSESS_WEAPON_REQ 0x46
#define CG_PKT_ASSESS_WEAPON_RESP 0x47

/* ------------------------------------------------------------------------------------------- */
/* SENSOR_FRAME.flags bits                                                                      */
/* ------------------------------------------------------------------------------------------- */

#define CG_FLAG_DOM_STALE     (1u << 0) /* dominant wrist ESP-NOW frame missed this interval    */
#define CG_FLAG_OFF_STALE     (1u << 1) /* off-hand wrist ESP-NOW frame missed this interval    */
/* This frame is part of a triggered capture (comms-demo trigger mode): the belt has flagged a
 * bounded run of SENSOR_FRAMEs as one capture attempt following a button press. Bits 5-7 carry
 * which button (trigger id) started the capture. Continuous/real-game receivers may ignore both. */
#define CG_FLAG_MOTION_ACTIVE (1u << 2)
#define CG_FLAG_LOW_BATTERY   (1u << 3)
#define CG_FLAG_CALIBRATING   (1u << 4)
/* bits 5-7: 3-bit trigger id (1-7) identifying which action button started the capture marked by
 * CG_FLAG_MOTION_ACTIVE; 0 means "not a triggered capture". Previously reserved: must be 0 on send,
 * ignored on receive, by any receiver built before this change. */
#define CG_FLAG_TRIGGER_ID_SHIFT 5
#define CG_FLAG_TRIGGER_ID_MASK  (0x7u << CG_FLAG_TRIGGER_ID_SHIFT)

/* ------------------------------------------------------------------------------------------- */
/* Wire structs — packed, no padding, field order is the wire order                             */
/* ------------------------------------------------------------------------------------------- */

#if defined(__GNUC__) || defined(__clang__)
#define CG_PACKED __attribute__((packed))
#else
#error "cg_protocol.h requires a GCC/Clang-compatible compiler for __attribute__((packed))"
#endif

/* Bytes [0, 8): authenticated as AAD, immediately followed by PAYLOAD (ciphertext || 16-byte tag),
 * then the trailing 2-byte CRC16 (not part of this struct). */
typedef struct CG_PACKED {
    uint8_t  magic;      /* CG_MAGIC */
    uint8_t  version;    /* CG_PROTOCOL_VERSION */
    uint8_t  type;       /* CG_TYPE_MORE_FRAGMENTS | packet type */
    uint16_t seq;        /* per-device, per-direction, wraps at 65535 */
    uint8_t  player_id;  /* 1 or 2; 0 = unassigned, only valid in HELLO */
    uint16_t len;        /* byte length of PAYLOAD (ciphertext + 16-byte tag) */
} cg_header_t;

/* AES-128-GCM nonce, derived per-packet, never transmitted. See wire_protocol.md §5.
 *
 * HANDSHAKE EXCEPTION (§5.1): for HELLO and HELLO_ACK there is no session yet, so `session_id` below
 * holds the random 4-byte device_nonce instead — the same value that is sent in the clear as the
 * first 4 bytes of PAYLOAD, and that is appended to the header to form the AAD. Using 0 here would
 * repeat the nonce across devices and across reboots, which breaks GCM. */
typedef struct CG_PACKED {
    uint32_t session_id;  /* or device_nonce, for HELLO / HELLO_ACK — see note above */
    uint8_t  player_id;
    uint8_t  direction;   /* CG_DIR_DEVICE_TO_LAPTOP | CG_DIR_LAPTOP_TO_DEVICE */
    uint32_t ext_seq;
    uint16_t reserved;    /* must be 0x0000 */
} cg_nonce_t;

/* --- Payload structs (plaintext, before AES-128-GCM sealing) --------------------------------- */

/* device -> laptop, no session yet. This is the 16-byte PLAINTEXT; per wire_protocol.md §5.1 the
 * wire payload is 36 bytes (4-byte cleartext device_nonce prefix || this struct encrypted || 16-byte tag). */
typedef struct CG_PACKED {
    uint8_t  device_mac[6];
    uint8_t  player_id;
    uint8_t  protocol_version;
    uint16_t firmware_version;
    uint32_t device_nonce;
    uint16_t reserved;
} cg_payload_hello_t;

/* laptop -> device, no session yet. This is the 16-byte PLAINTEXT; per wire_protocol.md §5.1 the
 * wire payload is 36 bytes (4-byte cleartext device_nonce prefix || this struct encrypted || 16-byte tag). */
typedef struct CG_PACKED {
    uint32_t session_id;             /* 0 = handshake refused (version mismatch) */
    uint32_t server_epoch_unix_ms_lo;
    uint32_t server_epoch_unix_ms_hi;
    uint32_t device_nonce;           /* echoed from HELLO — proves freshness */
} cg_payload_hello_ack_t;

/* device -> laptop. 4 bytes. */
typedef struct CG_PACKED {
    uint32_t t0; /* device clock, at send */
} cg_payload_time_sync_req_t;

/* laptop -> device. 12 bytes. */
typedef struct CG_PACKED {
    uint32_t t0; /* echoed from TIME_SYNC_REQ */
    uint32_t t1; /* laptop clock, at receive */
    uint32_t t2; /* laptop clock, at send */
} cg_payload_time_sync_resp_t;

/* device -> laptop, 50 Hz, best-effort. 42 bytes. */
typedef struct CG_PACKED {
    uint32_t device_timestamp_ms;
    uint8_t  flags;      /* CG_FLAG_* */
    uint8_t  weapon_id;
    int16_t  dominant[6];
    int16_t  offhand[6];
    int16_t  torso[6];
} cg_payload_sensor_frame_t;

/* device -> laptop, on RFID tag change. 12 bytes. */
typedef struct CG_PACKED {
    uint32_t device_timestamp_ms;
    uint8_t  weapon_id;
    uint8_t  reserved;
    uint8_t  tag_uid[6];
} cg_payload_rfid_event_t;

/* both directions, 1 Hz, best-effort. 12 bytes. */
typedef struct CG_PACKED {
    uint32_t device_timestamp_ms;
    uint16_t vbat_belt_mv;
    uint16_t vbat_dom_mv;
    uint16_t vbat_off_mv;
    uint16_t status_flags;
} cg_payload_heartbeat_t;

/* both directions. 2 bytes. */
typedef struct CG_PACKED {
    uint16_t acked_seq;
} cg_payload_ack_t;

/* laptop -> device, on combat event. 6 bytes. */
typedef struct CG_PACKED {
    uint8_t  feedback_code;
    uint8_t  intensity_pct;
    uint16_t duration_ms;
    uint16_t repeat_count;
} cg_payload_combat_feedback_t;

/* --- Assessment/demo payloads (0x40-0x4F) ---------------------------------------------------- */

/* Assessment revision 2. Paired firmware/relay deployment required.
 * Echo bodies contain the exact 42-byte sensor layout after 6/8 bytes of metadata.
 * The split dominant fields preserve the transform's simple three-axis interface. */
typedef struct CG_PACKED {
    uint32_t trace_id;
    uint8_t keycode, payload_kind;
    uint32_t sent_ms;
    uint8_t flags, weapon_id;
    int16_t value_a, value_b, value_c;
    int16_t dominant_gyro[3], offhand[6], torso[6];
} cg_payload_assess_echo_req_t; /* 48 bytes, wire 74 */
typedef struct CG_PACKED {
    uint32_t trace_id;
    uint8_t keycode, payload_kind, transform_id, reserved;
    uint32_t device_timestamp_ms;
    uint8_t flags, weapon_id;
    int16_t value_a, value_b, value_c;
    int16_t dominant_gyro[3], offhand[6], torso[6];
} cg_payload_assess_echo_resp_t; /* 50 bytes, wire 76 */
typedef struct CG_PACKED {
    uint32_t transfer_id;
    uint16_t chunk_index, total_chunks;
    uint8_t chunk_len, reserved;
    uint8_t data[32];
} cg_payload_assess_file_chunk_t; /* 42 bytes */
typedef struct CG_PACKED {
    uint32_t transfer_id;
    uint16_t chunk_index, running_crc;
    uint32_t received_bytes;
} cg_payload_assess_file_ack_t; /* 12 bytes */
typedef struct CG_PACKED {
    uint32_t transfer_id;
    uint16_t chunk_index;
} cg_payload_assess_file_read_t;
typedef struct CG_PACKED {
    uint32_t transfer_id;
    uint16_t chunk_index, total_bytes;
    uint8_t chunk_len, reserved;
    uint8_t data[32];
} cg_payload_assess_file_data_t; /* 42 bytes */

typedef struct CG_PACKED {
    uint32_t trace_id;
} cg_payload_assess_weapon_req_t;
typedef struct CG_PACKED {
    uint32_t trace_id;
    uint32_t device_timestamp_ms;
    uint8_t weapon_id;
} cg_payload_assess_weapon_resp_t;

/* ------------------------------------------------------------------------------------------- */
/* CRC-16/CCITT-FALSE                                                                            */
/*   poly=0x1021 init=0xFFFF, no input/output reflection, no final XOR.                          */
/*   Computed over bytes [0, 8+LEN) — header plus PAYLOAD (ciphertext || tag), NOT the CRC field. */
/* ------------------------------------------------------------------------------------------- */

static inline uint16_t cg_crc16(const uint8_t *data, uint32_t length) {
    uint16_t crc = 0xFFFFu;
    uint32_t i;
    for (i = 0; i < length; i++) {
        crc ^= (uint16_t)((uint16_t)data[i] << 8);
        for (int bit = 0; bit < 8; bit++) {
            if (crc & 0x8000u) {
                crc = (uint16_t)((crc << 1) ^ 0x1021u);
            } else {
                crc = (uint16_t)(crc << 1);
            }
        }
    }
    return crc;
}

#ifdef __cplusplus
}
#endif

#endif /* CG_PROTOCOL_H */
