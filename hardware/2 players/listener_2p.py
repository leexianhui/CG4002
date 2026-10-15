"""Laptop-side listener for both players' boards.

    python listener_2p.py [--label <label>] [--players 1,2] [--results-only]

Each player's boards use their own block of UDP ports (PLAYER_PORT_OFFSET in
the sketches), so the player is known from the port:
  player 1: debug 4211, result 4213, raw 4214
  player 2: debug 4221, result 4223, raw 4224
Every line is printed tagged P1/P2 (a * marks result lines) and written to
per-player CSVs (rx_time, source, line):
  logs/p1/debug_<time>[_<label>].csv   logs/p1/result_<time>[_<label>].csv
  logs/p2/debug_<time>[_<label>].csv   logs/p2/result_<time>[_<label>].csv
Boards built with RAW_STREAM 1 also send every IMU sample in binary batches;
those are decoded into one row per sample (not printed):
  logs/pN/raw_<time>[_<label>].csv     rx_time, node, seq, t_ms, ax..az (g), gx..gz (dps)
"""

import argparse
import csv
import os
import re
import selectors
import socket
import struct
from datetime import datetime

parser = argparse.ArgumentParser(description="Debug + result listener for both players")
parser.add_argument("--label", default="", help="appended to every CSV name, e.g. sleep_off")
parser.add_argument("--logs", default="logs", help="log root (default: logs)")
parser.add_argument("--players", default="1,2", help="which players to listen to (default: 1,2)")
parser.add_argument("--results-only", action="store_true", help="print only result lines (debug is still logged)")
args = parser.parse_args()

PORT_STRIDE = 10  # must match PLAYER_PORT_OFFSET in the sketches
STREAMS = {"debug": 4211, "result": 4213, "raw": 4214}  # player 1 ports

# RawBatch / RawSample in the right wrist and torso sketches (RAW_STREAM).
RAW_HDR = struct.Struct("<BBBHB")  # magic, playerId, deviceId, seq, n
RAW_SAMPLE = struct.Struct("<I6h")  # t, ax ay az (mg), gx gy gz (0.1 dps)
RAW_MAGIC = ord("R")
DEVICE_NAME = {0: "right_wrist", 1: "torso", 2: "left_wrist"}

players = [int(p) for p in args.players.split(",") if p.strip()]
stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
suffix = f"_{args.label}" if args.label else ""

TAG_RE = re.compile(r"^\[([^\]]+)\]")

# Per (player, node): last batch seq, batches received, batches missing.
raw_stats = {}


def write_raw(player, data, now, writer):
    """Decodes one RawBatch into per-sample rows. Returns False if malformed."""
    if len(data) < RAW_HDR.size:
        return False
    magic, _pid, dev, seq, n = RAW_HDR.unpack_from(data)
    if magic != RAW_MAGIC or len(data) != RAW_HDR.size + n * RAW_SAMPLE.size:
        return False
    node = DEVICE_NAME.get(dev, str(dev))
    st = raw_stats.setdefault((player, node), {"last": None, "rx": 0, "lost": 0})
    if st["last"] is not None:
        gap = (seq - st["last"]) & 0xFFFF
        if gap == 0:
            return True  # duplicate
        if gap < 1000:  # larger = the board rebooted; resync, don't count
            st["lost"] += gap - 1
    st["last"] = seq
    st["rx"] += 1
    rx = now.isoformat(timespec="microseconds")
    for i in range(n):
        t, ax, ay, az, gx, gy, gz = RAW_SAMPLE.unpack_from(data, RAW_HDR.size + i * RAW_SAMPLE.size)
        writer.writerow([rx, node, seq, t, ax / 1000, ay / 1000, az / 1000, gx / 10, gy / 10, gz / 10])
    return True


sel = selectors.DefaultSelector()
files = []
for player in players:
    folder = os.path.join(args.logs, f"p{player}")
    os.makedirs(folder, exist_ok=True)
    for stream, base_port in STREAMS.items():
        port = base_port + (player - 1) * PORT_STRIDE
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("", port))  # all interfaces
        sock.setblocking(False)

        path = os.path.join(folder, f"{stream}_{stamp}{suffix}.csv")
        f = open(path, "w", newline="", encoding="utf-8")
        writer = csv.writer(f)
        if stream == "raw":
            writer.writerow(["rx_time", "node", "seq", "t_ms", "ax", "ay", "az", "gx", "gy", "gz"])
        else:
            writer.writerow(["rx_time", "source", "line"])
        f.flush()  # header on disk even if nothing ever arrives
        files.append((f, path))

        sel.register(sock, selectors.EVENT_READ, (player, stream, writer, f))
        print(f"P{player} {stream:<6} port {port} -> {path}")

print("Listening... Ctrl+C to stop.")

try:
    while True:
        for key, _ in sel.select(timeout=0.5):
            player, stream, writer, f = key.data
            while True:
                try:
                    data, addr = key.fileobj.recvfrom(2048)
                except BlockingIOError:
                    break
                now = datetime.now()
                if stream == "raw":
                    if not write_raw(player, data, now, writer):
                        print(f"P{player} raw: dropped malformed packet ({len(data)} bytes) from {addr[0]}")
                    f.flush()
                    continue
                # backslashreplace keeps any non-UTF-8 byte visible instead of dropping it
                line = data.decode(errors="backslashreplace")
                if stream == "result" or not args.results_only:
                    timestamp = now.strftime("%H:%M:%S.%f")[:-3]
                    marker = "*" if stream == "result" else " "
                    print(f"[{timestamp}] P{player}{marker} {addr[0]}: {line}")

                m = TAG_RE.match(line)
                writer.writerow([now.isoformat(timespec="microseconds"), m.group(1) if m else "", line])
                f.flush()  # keep the CSV complete even if the listener is killed
except KeyboardInterrupt:
    print("\nStopped. CSVs saved:")
    for f, path in files:
        f.close()
        print(f"  {path}")
    for (player, node), st in sorted(raw_stats.items()):
        total = st["rx"] + st["lost"]
        pct = 100 * st["lost"] / total if total else 0
        print(f"  P{player} raw {node}: {st['rx']} batches, {st['lost']} lost ({pct:.1f}%)")
