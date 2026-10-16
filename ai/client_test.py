"""
Test client -- run on your LAPTOP (through the SSH tunnel) or on the board itself.

  # 1. open the tunnel in another terminal (see docs; keep it running):
  #    ssh -J <UNIXID>@stfjump.comp.nus.edu.sg -N -L localhost:8888:localhost:8888 xilinx@makerslab-fpga-53.ddns.comp.nus.edu.sg
  # 2. then:
  python3 board/client_test.py --n 500 --rate 10

Sends SENSOR_WINDOW messages at --rate per second and reports the round-trip time (network + tunnel +
classification).  Compare with the on-board latency to see how much comes from the network, and check the
result stays well below the 100 ms period at 10 Hz.
"""
import argparse
import json
import socket
import time

import numpy as np


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--rate", type=float, default=10.0)
    ap.add_argument("--weapon", default="SWORD")
    ap.add_argument("--samples", type=int, default=100)
    a = ap.parse_args()

    rng = np.random.RandomState(1)
    s = socket.create_connection((a.host, a.port), timeout=5)
    f = s.makefile("rwb")
    rtts, errors, last = [], 0, None
    nxt = time.perf_counter()
    for i in range(a.n):
        now = int(time.time() * 1000)
        req = {"type": "SENSOR_WINDOW", "version": 1, "playerId": 1, "requestId": i, "weapon": a.weapon,
               "timestamp": now, "windowStartTs": now - 20 * (a.samples - 1), "windowEndTs": now,
               "sampleRateHz": 50, "stalePct": 0,
               "dominant": (rng.randn(a.samples, 6) * np.array([0.3, 0.3, 0.3, 50, 50, 50])).round(4).tolist(),
               "offhand": (rng.randn(a.samples, 6) * np.array([0.3, 0.3, 0.3, 50, 50, 50])).round(4).tolist()}
        t0 = time.perf_counter()
        f.write((json.dumps(req) + "\n").encode())
        f.flush()
        resp = json.loads(f.readline().decode())
        rtts.append((time.perf_counter() - t0) * 1000)
        errors += resp.get("type") != "ACTION_RESULT" or resp.get("requestId") != i
        last = resp
        nxt += 1.0 / a.rate
        sl = nxt - time.perf_counter()
        if sl > 0:
            time.sleep(sl)
    r = np.array(rtts)
    print("last reply:", last)
    print("n=%d errors=%d | RTT ms: mean %.2f  p50 %.2f  p95 %.2f  p99 %.2f  max %.2f" %
          (a.n, errors, r.mean(), *np.percentile(r, [50, 95, 99]), r.max()))
    s.close()


if __name__ == "__main__":
    main()
