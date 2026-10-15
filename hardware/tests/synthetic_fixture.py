"""TEST FIXTURE ONLY — fabricated log lines that exercise every figure's code
path. The numbers are made up; never point make_figures.py at this output
for real figures. Lines follow the firmware's exact log formats.
"""

import csv
import math
import os
from datetime import datetime, timedelta

import numpy as np

from figpipe.logs import ACTION_LABELS, REAL_ACTIONS


class _Writer:
    def __init__(self, path, t0):
        self.f = open(path, "w", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        self.w.writerow(["rx_time", "source", "line"])
        self.t = t0

    def line(self, text, dt_ms=5):
        self.t += timedelta(milliseconds=dt_ms)
        self.w.writerow([self.t.isoformat(timespec="microseconds"), text[1:text.index("]")], text])

    def close(self):
        self.f.close()


def _seg_dump(rng, kind, t_start):
    """Samples shaped like a swing / spin / thrust, plus firmware-style trim."""
    n = {"spin": 220, "thrust": 60}.get(kind, 90)
    t = t_start + 10 * np.arange(n)
    k = np.arange(n)
    if kind == "spin":
        g = 40 + 380 * np.clip(np.sin(np.pi * (k - 15) / (n - 45)), 0, None)
        j = 0.3 + 0.2 * rng.random(n)
    elif kind == "thrust":
        g = 20 + 60 * np.exp(-((k - 30) / 6.0) ** 2)
        j = 0.05 + 2.4 * np.exp(-((k - 31) / 3.0) ** 2)
    else:
        g = 20 + 420 * np.exp(-((k - 35) / 7.0) ** 2)
        j = 0.1 + 1.0 * np.exp(-((k - 36) / 5.0) ** 2)
    g = g + 5 * rng.random(n)
    score = np.maximum(g / 60.0, j / 1.3)
    pk = int(np.argmax(score))
    a = max(0, pk - 14)  # PRE_PEAK_MS 150 at 10 ms
    b = min(n - 1, pk + 34)  # POST_PEAK_MS 350
    rows = []
    for i in range(n):
        gx, gy, gz = g[i] * 0.8, g[i] * 0.5, g[i] * 0.33
        star = "*" if a <= i <= b else " "
        rows.append(f"[wrist] [seg] i={i}{star} t={t[i]} ax=0.100 ay=0.200 az={1 + j[i]:.3f} gx={gx:.1f} "
                    f"gy={gy:.1f} gz={gz:.1f} gyroMag={g[i]:.1f} jerk={j[i]:.2f}")
    return rows, n, a, b, float(g[a:b + 1].max()), int(t[-1] - t[0])


def _ended(weapon, raw, raw_dur, a, b, peak, total_rot, raw_rot, accel, sustain, rev, torso):
    return (f"[wrist] Motion ENDED. weapon={weapon} raw={raw} rawDur={raw_dur} trimmed=[{a},{b}] peak={peak:.1f} "
            f"dur={(b - a) * 10} totalRot={total_rot:.1f} rawTotalRot={raw_rot:.1f} peakAccel={accel:.2f} axes=2 "
            f"sustainFrac={sustain:.2f} reversals={rev} rampMs=120 magOK=1 shapeOK=1 spinLikely=0 "
            f"thrustLikely=0 swingShapeOK=1 spinReversalOK=1 torsoPeak={torso:.1f} torsoDataOK=1 "
            f"torsoCeilingOK=1 torsoEngagementOK=1 torsoOK=1 rawDurationOK=1")


def write(logs_dir, seed=0):
    rng = np.random.default_rng(seed)
    os.makedirs(logs_dir, exist_ok=True)
    day = datetime(2026, 10, 1, 10, 0, 0)

    # ---- actions (B, C, F, and A via seg dumps) ----
    for n_lab, lab in enumerate(ACTION_LABELS):
        t0 = day + timedelta(minutes=n_lab)
        w = _Writer(os.path.join(logs_dir, f"debug_{t0:%Y%m%d_%H%M%S}_{lab}_synthetic.csv"), t0)
        w.line("[wrist] [cfg] wifi_sleep=off latency_test=0 player=1")
        real = lab in REAL_ACTIONS
        for rep in range(8):
            ms = 100000 + rep * 5000
            w.line(f"[wrist] Motion STARTED at {ms + 190}", 2000)
            w.line("[wrist] [candidate] closing: settled", 600)
            kind = "spin" if lab == "spin" else ("thrust" if lab == "thrust" else "swing")
            rows, raw, a, b, peak, raw_dur = _seg_dump(rng, kind, ms)
            torso = rng.uniform(40, 250) if real else rng.uniform(2, 30)
            if lab == "fake_body_turn":
                torso = rng.uniform(250, 420)
            rot = {"spin": rng.uniform(120, 200), "thrust": rng.uniform(8, 30)}.get(lab, rng.uniform(55, 200))
            raw_rot = rng.uniform(320, 520) if lab in ("spin", "fake_body_turn") else rot * 1.4
            rev = 0 if lab == "spin" else (1 if real else int(rng.integers(0, 9)))
            sustain = rng.uniform(0.28, 0.6) if real else rng.uniform(0.05, 0.5)
            if lab in ("fake_walking", "fake_handling"):
                raw_dur = int(rng.uniform(2000, 4300))
            w.line(_ended("spear", raw, raw_dur, a, b, peak, rot, raw_rot, rng.uniform(0.5, 2.2), sustain, rev,
                          torso), 5)
            for r in rows:
                w.line(r, 1)
        w.close()

    # ---- stance holds and transitions (D, E3) + a lock attempt (E2) ----
    t0 = day + timedelta(hours=1)
    name = f"debug_{t0:%Y%m%d_%H%M%S}_holds_synthetic.csv"
    w = _Writer(os.path.join(logs_dir, name), t0)
    refs = {"spear": (-15, 5, 50, -15), "sword_shield": (-1, -70, -5, -1), "mace": (3, -67, -24, -52)}
    ranges = []
    for cycle in range(3):
        for weapon, (re_, ra, le, la) in refs.items():
            start = w.t
            for _ in range(30):
                w.line(f"[wrist] raw rEl={re_ + rng.normal(0, 2):.1f} rAz={ra + rng.normal(0, 3):.1f} "
                       f"rGyroMag={rng.uniform(15, 80):.1f} rStable=1 | left el={le + rng.normal(0, 2):.1f} "
                       f"az={la + rng.normal(0, 3):.1f} fresh=1 stable=1 | torso pitch=1.0 roll=0.5 fresh=1 "
                       f"stable=1 | nearest={weapon} dist={abs(rng.normal(4, 2)):.1f} candidate=unknown", 100)
                w.line(f"[left_wrist] raw el={le:.1f} az={la:.1f} gyroMag={rng.uniform(10, 70):.1f} stable=1 "
                       f"gx=1.0 gy=1.0 gz=1.0 imuFails=0", 50)
                w.line(f"[torso] raw pitch=1.0 roll=0.5 gyroMag={rng.uniform(2, 12):.1f} stable=1", 50)
            ranges.append((start, w.t, f"hold_{weapon}"))
            start = w.t
            for _ in range(10):
                w.line(f"[wrist] raw rEl={rng.uniform(-40, 40):.1f} rAz={rng.uniform(-90, 20):.1f} "
                       f"rGyroMag={rng.uniform(150, 900):.1f} rStable=0 | left el=0.0 az=0.0 fresh=1 stable=0 | "
                       f"torso pitch=1.0 roll=0.5 fresh=1 stable=0 | nearest=mace dist={rng.uniform(30, 90):.1f} "
                       f"candidate=unknown", 100)
                w.line(f"[left_wrist] raw el=0.0 az=0.0 gyroMag={rng.uniform(120, 700):.1f} stable=0 gx=1.0 "
                       f"gy=1.0 gz=1.0 imuFails=0", 50)
                w.line(f"[torso] raw pitch=1.0 roll=0.5 gyroMag={rng.uniform(20, 120):.1f} stable=0", 50)
            ranges.append((start, w.t, "transition"))
    # lock attempt after a >2 s gap
    w.t += timedelta(seconds=5)
    held = None
    for k in range(40):
        dist = 60 * math.exp(-k / 6) + abs(rng.normal(3, 1))
        cand = "spear" if dist < 20 else "unknown"
        held = (held + 300) if (cand != "unknown" and held is not None) else (0 if cand != "unknown" else None)
        tail = f" candidate={cand}" + (f" heldMs={held}" if held is not None else "")
        w.line(f"[wrist] raw rEl=-15.0 rAz=5.0 rGyroMag=20.0 rStable=1 | left el=50.0 az=-15.0 fresh=1 stable=1 | "
               f"torso pitch=1.0 roll=0.5 fresh=1 stable=1 | nearest=spear dist={dist:.1f}{tail}", 300)
        if held is not None and held >= 5000:
            break
    w.line("[torso] Weapon locked by hub: weapon=2 — switching to beacon mode", 50)
    w.close()
    with open(os.path.join(logs_dir, "labels.csv"), "w", newline="", encoding="utf-8") as f:
        cw = csv.writer(f)
        cw.writerow(["session", "start", "end", "label"])
        for s, e, lab in ranges:
            cw.writerow([name, s.isoformat(), e.isoformat(), lab])

    # ---- network (H) ----
    for i, sleep in enumerate(["off", "on"]):
        t0 = day + timedelta(hours=2, minutes=i * 10)
        w = _Writer(os.path.join(logs_dir, f"debug_{t0:%Y%m%d_%H%M%S}_sleep_{sleep}_synthetic.csv"), t0)
        for node in ("wrist", "torso", "left_wrist"):
            w.line(f"[{node}] [cfg] wifi_sleep={sleep} latency_test=1 player=1")
        base = 4 if sleep == "off" else 40
        for s in range(60):
            for _ in range(10):
                w.line(f"[wrist] [rtt] us={int(rng.gamma(2.0, base * 500))}", 100)
            iats = [int(rng.normal(20000, 1500 if sleep == 'off' else 20000)) for _ in range(50)]
            w.line("[wrist] [beacon_iat] us=" + ",".join(str(max(v, 500)) for v in iats), 1)
            w.line(f"[wrist] [stats] to=right_wrist from=torso type=BEACON rx=50 "
                   f"lost={int(rng.poisson(0.2 if sleep == 'off' else 2))}", 1)
            w.line(f"[torso] [stats] to=torso from=right_wrist type=HUB_STATUS rx=10 "
                   f"lost={int(rng.poisson(0.05 if sleep == 'off' else 0.6))}", 1)
            w.line(f"[left_wrist] [stats] to=left_wrist from=right_wrist type=HUB_STATUS rx=10 "
                   f"lost={int(rng.poisson(0.05 if sleep == 'off' else 0.5))}", 1)
        w.close()
