"""Wire protocol constants — mirrors shared/protocol/cg_protocol.h exactly.

If this module and cg_protocol.h ever disagree, that is a bug: fix both in the same PR
(see shared/protocol/wire_protocol.md).
"""

from __future__ import annotations

from enum import IntEnum

PROTOCOL_VERSION: int = 1

MAGIC: int = 0xC9

# TYPE byte: bit 7 = MORE_FRAGMENTS, bits 6-0 = packet type.
TYPE_MORE_FRAGMENTS: int = 0x80
TYPE_MASK: int = 0x7F

HEADER_SIZE: int = 8  # MAGIC..LEN, before PAYLOAD
CRC_SIZE: int = 2  # trailing CRC16
GCM_TAG_SIZE: int = 16  # trailing auth tag, inside PAYLOAD/LEN
NONCE_SIZE: int = 12  # derived AES-GCM nonce, never sent on the wire
PSK_SIZE: int = 16  # AES-128 key

# The largest real payload (SENSOR_FRAME, 42 B plaintext) is well under this. Used by framing.py
# to reject an implausible LEN field as a false-positive MAGIC match rather than stalling forever
# waiting for bytes that will never complete a bogus frame.
MAX_PAYLOAD_LEN: int = 512


class Direction(IntEnum):
    """Nonce derivation direction byte (wire_protocol.md §5)."""

    DEVICE_TO_LAPTOP = 0
    LAPTOP_TO_DEVICE = 1


class PacketType(IntEnum):
    HELLO = 0x01
    HELLO_ACK = 0x02
    TIME_SYNC_REQ = 0x03
    TIME_SYNC_RESP = 0x04
    SENSOR_FRAME = 0x10
    RFID_EVENT = 0x11
    HEARTBEAT = 0x20
    ACK = 0x21
    COMBAT_FEEDBACK = 0x30


# Packets that require ACK + retransmit (wire_protocol.md §2 / COMMS_IMPLEMENTATION_PLAN.md §6).
# SENSOR_FRAME and HEARTBEAT are best-effort. ACK is never itself acked.
RELIABLE_PACKET_TYPES: frozenset[PacketType] = frozenset(
    {
        PacketType.HELLO,
        PacketType.HELLO_ACK,
        PacketType.TIME_SYNC_REQ,
        PacketType.TIME_SYNC_RESP,
        PacketType.RFID_EVENT,
        PacketType.COMBAT_FEEDBACK,
    }
)

# SENSOR_FRAME.flags bits (wire_protocol.md §3.1).
FLAG_DOM_STALE: int = 1 << 0
FLAG_OFF_STALE: int = 1 << 1
FLAG_MOTION_ACTIVE: int = 1 << 2
FLAG_LOW_BATTERY: int = 1 << 3
FLAG_CALIBRATING: int = 1 << 4
# Bits 5-7: 3-bit trigger id (1-7) marking which action button started a triggered capture; 0/absent
# means "not a triggered capture". Previously reserved: must be 0 on send, ignored on receive.
FLAG_TRIGGER_ID_SHIFT: int = 5
FLAG_TRIGGER_ID_MASK: int = 0b111 << 5

# IMU fixed-point scaling (wire_protocol.md §3.2), matching MPU-6050 full-scale settings.
ACCEL_LSB_PER_G: float = 2048.0  # 1 LSB = 1/2048 g, full scale +-16 g
GYRO_LSB_PER_DPS: float = 16.4  # 1 LSB = 1/16.4 deg/s, full scale +-2000 deg/s
