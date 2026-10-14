"""Supplied hardware layouts stay separate from BLE and from live combat inputs."""

import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from cg_comms.relay.hardware_probe import decode_hub_datagram, ports_for_player
from cg_comms.tools.gateway_probe import capture, replay

# Bytes following the sketches' packed C fields, including both counter wrap boundaries.
LOCKED_SWORD = bytes.fromhex("01 01 00 ff ff 02 01 01")
RAW = bytes.fromhex("52 01 00 ff ff 02 "
                    "ffffffff e803 18fc f401 0a00 ecff 2c01 "
                    "00000000 0000 0000 e803 0000 0000 0000")


def test_candidate_weapon_is_not_a_selection():
    candidate = decode_hub_datagram(bytes.fromhex("01 01 00 01 00 01 02 01"), "pose", 1)
    assert candidate["candidate_weapon"] == "SPEAR" and candidate["locked_weapon"] is None
    locked = decode_hub_datagram(LOCKED_SWORD, "pose", 1)
    assert locked["locked_weapon"] == "SWORD" and locked["sequence"] == 65535


def test_raw_preserves_role_units_and_wrapping_device_clock():
    decoded = decode_hub_datagram(RAW, "raw", 1)
    assert decoded["device_role"] == "right_wrist"
    assert decoded["samples"] == [
        dict(device_ms=4294967295, accel_g=[1, -1, .5], gyro_dps=[1, -2, 30]),
        dict(device_ms=0, accel_g=[0, 0, 1], gyro_dps=[0, 0, 0]),
    ]
    assert "offhand" not in decoded and "action" not in decoded


@pytest.mark.parametrize("packet,stream,error", [
    (b"", "pose", "truncated_header"),
    (bytes.fromhex("01 02 00 ff ff 02 01 01"), "pose", "wrong_player"),
    (bytes.fromhex("01 01 01 ff ff 02 01 01"), "pose", "not_right_wrist_hub"),
    (LOCKED_SWORD[:-1], "pose", "invalid_hub_status_layout"),
    (bytes.fromhex("01 01 00 ff ff 02 00 01"), "pose", "invalid_hub_status_value"),
    (RAW[:-1], "raw", "invalid_raw_length"),
    (RAW + b"extra", "raw", "invalid_raw_length"),
    (b"R\x01\x00\x00\x00\x00", "raw", "invalid_raw_length"),
    (LOCKED_SWORD, "raw", "invalid_raw_header"),
])
def test_mismatches_do_not_enter_the_normalized_boundary(packet, stream, error):
    with pytest.raises(ValueError, match=error):
        decode_hub_datagram(packet, stream, 1)


def test_player_two_port_and_header_identity():
    assert ports_for_player(2) == {"pose": 4222, "raw": 4224}
    packet = LOCKED_SWORD[:1] + b"\x02" + LOCKED_SWORD[2:]
    assert decode_hub_datagram(packet, "pose", 2)["player_id"] == 2
    with pytest.raises(ValueError, match="player"):
        ports_for_player(3)


def test_loopback_capture_replay_and_no_transmission(tmp_path):
    # Reserve two different ports, then let the receiver bind them. Repeated sends avoid
    # depending on thread startup timing; no fixed hardware ports or boards are used.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as a, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as b:
        a.bind(("127.0.0.1", 0))
        b.bind(("127.0.0.1", 0))
        ports = {"pose": a.getsockname()[1], "raw": b.getsockname()[1]}
    path = tmp_path / "capture.jsonl"
    with ThreadPoolExecutor(max_workers=1) as executor, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.bind(("127.0.0.1", 0))
        sender.settimeout(.05)
        worker = executor.submit(capture, path, 1, "127.0.0.1", bind="127.0.0.1",
                                 duration=.4, ports=ports)
        while not worker.done():
            sender.sendto(LOCKED_SWORD, ("127.0.0.1", ports["pose"]))
            sender.sendto(RAW, ("127.0.0.1", ports["raw"]))
            time.sleep(.02)
        counts = worker.result()
        with pytest.raises((TimeoutError, ConnectionResetError)):
            sender.recvfrom(1024)
    assert counts["hub_status"] > 0 and counts["raw_batch"] > 0
    assert replay(path) == counts
    records = [json.loads(line) for line in path.read_text().splitlines()]
    # Replay ignores decoded fields, so edits there can't turn recorded bytes into an action.
    records[1]["decoded"] = {"action": "HIT"}
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    assert replay(path) == counts
    with pytest.raises(FileExistsError):
        capture(path, 1, "127.0.0.1", bind="127.0.0.1", duration=.01, ports=ports)


def test_unselected_source_is_not_recorded(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as a, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as b:
        a.bind(("127.0.0.1", 0))
        b.bind(("127.0.0.1", 0))
        ports = {"pose": a.getsockname()[1], "raw": b.getsockname()[1]}
    path = tmp_path / "filtered.jsonl"
    with ThreadPoolExecutor(max_workers=1) as executor, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        worker = executor.submit(capture, path, 1, "127.0.0.2", bind="127.0.0.1",
                                 duration=.2, ports=ports)
        while not worker.done():
            sender.sendto(LOCKED_SWORD, ("127.0.0.1", ports["pose"]))
            time.sleep(.01)
        counts = worker.result()
    assert counts["other_source_ignored"] > 0 and not counts["hub_status"]
    assert len(path.read_text().splitlines()) == 1
