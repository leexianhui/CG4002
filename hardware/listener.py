import argparse
import csv
import os
import re
import socket
from datetime import datetime

# Prints every debug line from the boards (port 4211) and also appends it to a
# CSV for analyse.py: rx_time (laptop receive time, ISO 8601), source (the
# board's "[tag]" prefix, e.g. wrist / torso / left_wrist), line (raw text).
parser = argparse.ArgumentParser(description="Debug listener for port 4211")
parser.add_argument("--label", default="", help="appended to the CSV name, e.g. sleep_off")
parser.add_argument("--out", default="", help="CSV path (default: logs/debug_<time>[_<label>].csv)")
args = parser.parse_args()

csv_path = args.out
if not csv_path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{args.label}" if args.label else ""
    csv_path = os.path.join("logs", f"debug_{stamp}{suffix}.csv")
os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)

TAG_RE = re.compile(r"^\[([^\]]+)\]")

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("", 4211))  # listen on all interfaces, port 4211

print("Listening for debug messages on port 4211...")
print(f"Logging to {csv_path}")

with open(csv_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["rx_time", "source", "line"])
    f.flush()  # header on disk even if nothing ever arrives
    try:
        while True:
            data, addr = sock.recvfrom(2048)
            now = datetime.now()
            # backslashreplace keeps any non-UTF-8 byte visible instead of dropping it
            line = data.decode(errors="backslashreplace")
            timestamp = now.strftime("%H:%M:%S.%f")[:-3]
            print(f"[{timestamp}] {addr[0]}: {line}")

            m = TAG_RE.match(line)
            writer.writerow([now.isoformat(timespec="microseconds"), m.group(1) if m else "", line])
            f.flush()  # keep the CSV complete even if the listener is killed
    except KeyboardInterrupt:
        print(f"\nStopped. CSV saved to {csv_path}")
