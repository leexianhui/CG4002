import argparse
import csv
import os
import re
import socket
from datetime import datetime

# Prints every milestone line from the hub (port 4213) and also appends it to a
# CSV in the same format as listener.py: rx_time (laptop receive time, ISO
# 8601), source (the "[tag]" prefix, if any), line (raw text as received).
parser = argparse.ArgumentParser(description="Result listener for port 4213")
parser.add_argument("--label", default="", help="appended to the CSV name, e.g. sleep_off")
parser.add_argument("--out", default="", help="CSV path (default: logs/result_<time>[_<label>].csv)")
args = parser.parse_args()

csv_path = args.out
if not csv_path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{args.label}" if args.label else ""
    csv_path = os.path.join("logs", f"result_{stamp}{suffix}.csv")
os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)

TAG_RE = re.compile(r"^\[([^\]]+)\]")

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("", 4213))  # listen on all interfaces, port 4213

print("Listening for weapon-detection results on port 4213...")
print(f"Logging to {csv_path}")

with open(csv_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["rx_time", "source", "line"])
    f.flush()  # header on disk even if nothing ever arrives
    try:
        while True:
            data, addr = sock.recvfrom(1024)
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
