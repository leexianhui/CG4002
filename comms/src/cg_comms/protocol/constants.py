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
    # Assessment/demo extension range (0x40-0x4F). Reserved for the comms-assessment features
    # (keyboard-driven echo pipeline, file transfer); the real game never sends these.
    ASSESS_ECHO_REQ = 0x40
    ASSESS_ECHO_RESP = 0x41
    ASSESS_FILE_CHUNK = 0x42
    ASSESS_FILE_ACK = 0x43
    ASSESS_FILE_READ = 0x44
    ASSESS_FILE_DATA = 0x45
    ASSESS_WEAPON_REQ = 0x46
    ASSESS_WEAPON_RESP = 0x47


# Delivery-sensitive types. RFID/feedback use ACK + retransmit; handshake/time-sync use bounded
# request/response with whole-handshake recovery. SENSOR_FRAME/HEARTBEAT and ACK are best-effort.
RELIABLE_PACKET_TYPES: frozenset[PacketType] = frozenset(
    {
        PacketType.HELLO,
        PacketType.HELLO_ACK,
        PacketType.TIME_SYNC_REQ,
        PacketType.TIME_SYNC_RESP,
        PacketType.RFID_EVENT,
        PacketType.COMBAT_FEEDBACK,
        PacketType.ASSESS_ECHO_REQ,
        PacketType.ASSESS_WEAPON_REQ,
        PacketType.ASSESS_FILE_CHUNK,
        PacketType.ASSESS_FILE_READ,
    }
)

# The subset of RELIABLE_PACKET_TYPES that recovers via ACK + identical-ciphertext retransmit +
# duplicate suppression, as opposed to the handshake/time-sync types, which recover by redoing the
# whole handshake and must NOT be cached for retransmit.
#
# This set exists because codec.py used to hard-code `(RFID_EVENT, COMBAT_FEEDBACK)` inline in two
# separate places. Adding a type to RELIABLE_PACKET_TYPES alone therefore did nothing at all: the
# retransmit cache never held it and a duplicate was re-applied instead of re-ACKed. Any new
# retransmittable type must be listed HERE, not just above.
#
# Both assessment entries are laptop -> device on purpose. The firmware's CgReliableSend is a single
# slot shared with RFID_EVENT, so making a device -> laptop assessment type reliable would contend
# with the working RFID path. Keeping the retransmitting end in Python (relay/reliability.py) leaves
# that path untouched; the device only has to ACK, and the response direction is naturally re-driven
# by the request's own retransmit.
ACK_RETRANSMIT_PACKET_TYPES: frozenset[PacketType] = frozenset(
    {
        PacketType.RFID_EVENT,
        PacketType.COMBAT_FEEDBACK,
        PacketType.ASSESS_ECHO_REQ,
        PacketType.ASSESS_WEAPON_REQ,
        PacketType.ASSESS_FILE_CHUNK,
        PacketType.ASSESS_FILE_READ,
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
