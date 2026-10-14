"""Receive-only decoder for the supplied two-player hub's UDP diagnostic output.

This is NOT the BLE v1 codec or an agreed final gateway protocol. Source layouts:
cg4002-hw-main/2 players/p{1,2}_right_wrist/*.ino.
"""

from __future__ import annotations

import struct

HUB_STATUS = struct.Struct("<BBBHBBB")
RAW_HEADER = struct.Struct("<BBBHB")
RAW_SAMPLE = struct.Struct("<I6h")
WEAPONS = (None, "SWORD", "SPEAR", "MACE")
PHASES = ("WAITING", "DETECTING", "LOCKED")


def ports_for_player(player: int) -> dict[str, int]:
    if player not in (1, 2):
        raise ValueError("player must be 1 or 2")
    return {"pose": 4212 + (player - 1) * 10, "raw": 4214 + (player - 1) * 10}


def decode_hub_datagram(data: bytes, stream: str, player: int) -> dict[str, object]:
    """Validate lengths/identity; preserve the device clock and physical sensor role.

    No sequence ordering, boot detection, action inference or torso/offhand substitution.
    """
    ports_for_player(player)
    if len(data) < 3:
        raise ValueError("truncated_header")
    if data[1] != player:
        raise ValueError("wrong_player")
    if data[2] != 0:
        raise ValueError("not_right_wrist_hub")
    if stream == "pose":
        if len(data) != HUB_STATUS.size or data[0] != 1:
            raise ValueError("invalid_hub_status_layout")
        _, _, _, seq, phase, weapon, holding = HUB_STATUS.unpack(data)
        if phase > 2 or weapon > 3 or holding > 1 or (phase == 2 and weapon == 0):
            raise ValueError("invalid_hub_status_value")
        return dict(kind="hub_status", player_id=player, device_role="right_wrist", sequence=seq,
                    phase=PHASES[phase], candidate_weapon=WEAPONS[weapon], holding=bool(holding),
                    locked_weapon=WEAPONS[weapon] if phase == 2 else None)
    if stream == "raw":
        if len(data) < RAW_HEADER.size or data[0] != ord("R"):
            raise ValueError("invalid_raw_header")
        _, _, _, seq, count = RAW_HEADER.unpack_from(data)
        if not 1 <= count <= 10 or len(data) != RAW_HEADER.size + count * RAW_SAMPLE.size:
            raise ValueError("invalid_raw_length")
        samples = []
        for i in range(count):
            timestamp, *axes = RAW_SAMPLE.unpack_from(data, RAW_HEADER.size + i * RAW_SAMPLE.size)
            samples.append(dict(device_ms=timestamp, accel_g=[v / 1000 for v in axes[:3]],
                                gyro_dps=[v / 10 for v in axes[3:]]))
        return dict(kind="raw_batch", player_id=player, device_role="right_wrist", sequence=seq,
                    clock="hub_millis_uint32", samples=samples)
    raise ValueError("unknown_stream")
