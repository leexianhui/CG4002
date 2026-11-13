"""Hardware debug logs (hub/torso/left-wrist debugLog -> hub DEVICE_LOG pieces -> relay -> per-match
CSVs under hardware/game/). See relay/device_log.py and hardware/gateway/include/gateway_log.h."""

import csv
from datetime import datetime, timedelta

from test_segments import frame, session

from cg_comms.protocol.constants import DEVICE_LOG_MORE, DEVICE_LOG_TEXT_LEN, PacketType
from cg_comms.relay.device_log import DeviceLogWriter, line_kind


def pieces(line: str, source: int, line_seq: int, device_ms: int = 1000) -> list[dict]:
    """Splits a line the way gatewayLog() does."""
    raw = line.encode("ascii")
    chunks = [raw[i:i + DEVICE_LOG_TEXT_LEN] for i in range(0, len(raw), DEVICE_LOG_TEXT_LEN)] or [b""]
    return [{"device_timestamp_ms": device_ms, "source": source, "line_seq": line_seq,
             "part": i | (DEVICE_LOG_MORE if i + 1 < len(chunks) else 0),
             "text": chunk.ljust(DEVICE_LOG_TEXT_LEN, b"\0")} for i, chunk in enumerate(chunks)]


def clock(start=datetime(2026, 11, 13, 15, 30, 0)):
    t = [start]

    def now():
        return t[0]
    return t, now


def rows(path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


LONG = ("Stroke i=0 kind=rot t0=1000 t1=1400 peak=520.1 rot=140.2 ax=0.1 ay=0.2 az=0.9 "
        "nearest=vertical dist=0.42 reason=guarding accept=0")


def test_long_lines_are_reassembled_per_board_and_interleaving_is_fine(tmp_path):
    writer = DeviceLogWriter(tmp_path, player_id=1, now=clock()[1])
    hub = pieces(LONG, source=0, line_seq=5)
    left = pieces("guard_wait el=35.2 az=-80.1 dist=6.3 inside=1 armed=0 farAgo=-1", source=2, line_seq=9)
    assert len(hub) > 2 and len(left) == 2
    for p in [hub[0], left[0], hub[1], left[1], *hub[2:]]:
        writer.add_piece(p)
    writer.close()

    (path,) = tmp_path.glob("*.csv")
    assert path.name == "match_20261113_153000_p1.csv"
    got = rows(path)
    assert [r["board"] for r in got] == ["relay", "left_wrist", "hub"]
    assert got[0]["kind"] == "match_start" and got[0]["message"] == "relay start"
    assert got[1]["message"].startswith("guard_wait el=35.2") and got[1]["kind"] == "guard_wait"
    assert got[2]["message"] == LONG and got[2]["complete"] == "1" and got[2]["device_ms"] == "1000"


def test_a_lost_piece_leaves_a_marked_gap_instead_of_stalling(tmp_path):
    writer = DeviceLogWriter(tmp_path, player_id=2, now=clock()[1])
    first = pieces(LONG, source=1, line_seq=1)
    for p in [first[0], *first[2:]]:  # piece 1 lost
        writer.add_piece(p)
    second = pieces(LONG, source=1, line_seq=2)
    writer.add_piece(second[0])  # its tail lost; the next line from the board flushes it
    for p in pieces("[stats] to=torso from=hub type=HUB_STATUS rx=10 lost=0", source=1, line_seq=3):
        writer.add_piece(p)
    writer.close()

    got = rows(next(tmp_path.glob("*.csv")))[1:]
    assert [r["complete"] for r in got] == ["0", "0", "1"]
    assert "..." in got[0]["message"] and got[0]["message"].endswith(LONG[-20:])
    assert got[1]["message"].endswith("...")
    assert got[2]["kind"] == "stats"


def test_each_match_reset_starts_a_new_file_and_empty_matches_leave_none(tmp_path):
    t, now = clock()
    writer = DeviceLogWriter(tmp_path, player_id=1, now=now)
    for p in pieces("all_nodes_ready", 0, 1):
        writer.add_piece(p)
    t[0] += timedelta(minutes=1)
    writer.start_match("match reset")  # no lines in this one
    t[0] += timedelta(minutes=1)
    writer.start_match("match reset")
    for p in pieces("weapon_locked weapon=3", 0, 2):
        writer.add_piece(p)
    writer.close()

    files = sorted(p.name for p in tmp_path.glob("*.csv"))
    assert files == ["match_20261113_153000_p1.csv", "match_20261113_153200_p1.csv"]
    second = rows(tmp_path / files[1])
    assert second[0]["message"] == "match reset" and second[1]["message"] == "weapon_locked weapon=3"


def test_line_kind():
    assert line_kind("[stats] to=hub") == "stats"
    assert line_kind("guard_state=GUARDING weapon=2") == "guard_state"
    assert line_kind("Stroke i=0 kind=rot") == "Stroke"
    assert line_kind("  ") == ""


async def test_session_writes_device_log_packets(tmp_path):
    subject = session()
    subject.device_log = DeviceLogWriter(tmp_path, player_id=1, now=clock()[1])
    for seq, p in enumerate(pieces(LONG, source=0, line_seq=1), start=1):
        await subject._handle_frame(frame(p, seq, kind=PacketType.DEVICE_LOG))
    subject.device_log.close()
    got = rows(next(tmp_path.glob("*.csv")))
    assert got[-1]["message"] == LONG and got[-1]["board"] == "hub"
    subject._send_ack.assert_not_awaited()  # best-effort: never ACKed
