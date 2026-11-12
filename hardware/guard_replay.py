"""Replay the left-wrist guard detector on raw captures and tune it.

Mirrors updateGuardDetection() in both left-wrist sketches: gravity is an EMA
(EMA_ALPHA) of the unit accel vector, its elevation/azimuth are compared with
each guard reference of the locked weapon, and GUARDING is entered once the
pose is within that reference's enter radius while the gyro EMA has stayed at
or below GUARD_STILL_DPS for GUARD_HOLD_MS. The arm must also have arrived:
within the last GUARD_ARM_MS it was more than each reference's arm distance
from it, and the peak gyro EMA in that window reaches the entered
reference's arrival speed. It is left when the pose moves more than every
reference's exit radius away.

Scoring uses the raw_*.csv left_wrist rows (RAW_STREAM captures):
  * guard sessions (name starts with "guard_"): guard episodes found, and the
    fraction of still-at-guard-pose time spent GUARDING;
  * every other session: each GUARDING episode is false.

    python guard_replay.py            # current vs proposed settings, per session
    python guard_replay.py --sweep    # grid search, ranked
"""

import argparse
import csv
import itertools
import math
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS_ROOT = ROOT / "2 players" / "logs"
SOURCES = [LOGS_ROOT / n for n in ("p1", "p1_v1", "p1_v2", "p1_v3", "himeth_v2", "himeth_v1", "p2", "p2_v1", "p2_v2")]
EMA_ALPHA = 0.2

# (el, az, enter radius, exit radius, arm distance, arrival dps) per weapon.
# Keep in sync with GUARD_REFS in both left-wrist sketches.
# The pre-2026-11-12 detector: 700 ms of stillness (gyro EMA <= 100) then a
# 150 ms hold inside the radius, approximated here as one 850 ms hold.
OLD = {"refs": {"sword": [(-3, 94, 25, 35, 0, 0)], "spear": [(60, -76, 25, 35, 0, 0)],
                "mace": [(68, -68, 25, 35, 0, 0)]},
       "still_dps": 100.0, "hold_ms": 850, "arm_ms": 0}
# Chosen with --sweep on the November P1 and P2 captures. Sword guards must
# come from rest (> 80 deg away) at >= 350 dps: P1's fake guards start
# half-lowered (62-82 deg) and rise slowly (300-410 dps).
NEW = {"refs": {"sword": [(-3, 94, 10, 20, 80, 350)],
                "spear": [(60, -76, 15, 25, 60, 0), (28, -76, 15, 25, 60, 0)],
                "mace": [(68, -68, 25, 35, 60, 0), (40, -79, 20, 30, 60, 0)]},
       "still_dps": 100.0, "hold_ms": 400, "arm_ms": 1500}


def weapon_key(text):
    for key in ("sword", "mace", "spear"):
        if key in text:
            return key
    return None


def angle_diff(a, b):
    return abs((a - b + 540.0) % 360.0 - 180.0)


def load_left(raw_path):
    """[(t_ms, rx_seconds, ax, ay, az, gyroMag)] for the left wrist."""
    rows = []
    with raw_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["node"] != "left_wrist":
                continue
            g = math.sqrt(sum(float(row[k]) ** 2 for k in ("gx", "gy", "gz")))
            rows.append((int(row["t_ms"]), datetime.fromisoformat(row["rx_time"]).timestamp(),
                         float(row["ax"]), float(row["ay"]), float(row["az"]), g))
    rows.sort()
    return rows


def pose_trace(rows):
    """[(t_ms, rx, el, az, gyroEMA)] - the left wrist's gravity and gyro EMAs (setting-independent)."""
    gx = gy = gz = None
    gema = 0.0
    out = []
    for t, rx, ax, ay, az, gyro in rows:
        mag = max(math.sqrt(ax * ax + ay * ay + az * az), 0.1)
        ux, uy, uz = ax / mag, ay / mag, az / mag
        if gx is None:
            gx, gy, gz, gema = ux, uy, uz, gyro
        else:
            gx += EMA_ALPHA * (ux - gx)
            gy += EMA_ALPHA * (uy - gy)
            gz += EMA_ALPHA * (uz - gz)
            gema = EMA_ALPHA * gyro + (1 - EMA_ALPHA) * gema
        n = math.sqrt(gx * gx + gy * gy + gz * gz)
        out.append((t, rx, math.degrees(math.asin(max(-1.0, min(1.0, gz / n)))),
                    math.degrees(math.atan2(gy, gx)), gema))
    return out


def run(trace, weapon, params):
    """GUARDING episodes [(on_rx, off_rx)] and a per-sample guarding flag.

    Entry: inside a reference's enter radius, gyro EMA <= still_dps for
    hold_ms, armed (within the last arm_ms the pose was beyond every
    reference's arm distance; each arrival allows one entry) and the windowed
    peak gyro EMA >= that reference's arrival dps. Arming is off when every
    arm distance is 0. Exit: beyond every exit radius.
    """
    refs = params["refs"][weapon]
    arm_ms = params["arm_ms"]
    arming = any(r[4] for r in refs)
    still_since = None
    far_at = None
    peak, peak_at = 0.0, None   # windowed max of the gyro EMA (GUARD_ARM_MS)
    guarding, episodes, flags = False, [], []
    on_rx = None
    for t, rx, el, az, gema in trace:
        if gema > params["still_dps"]:
            still_since = None
        elif still_since is None:
            still_since = t
        if peak_at is None or gema >= peak or t - peak_at > arm_ms:
            peak, peak_at = gema, t
        dists = [math.hypot(el - r[0], angle_diff(az, r[1])) for r in refs]
        if arming and all(d > r[4] for d, r in zip(dists, refs)):
            far_at = t
        if not guarding:
            armed = not arming or (far_at is not None and t - far_at <= arm_ms)
            held = still_since is not None and t - still_since >= params["hold_ms"]
            inside = any(d <= r[2] and peak >= r[5] for d, r in zip(dists, refs))
            if inside and armed and held:
                guarding, on_rx, far_at = True, rx, None
        elif all(d > r[3] for d, r in zip(dists, refs)):
            guarding = False
            episodes.append((on_rx, rx))
        flags.append(guarding)
    if guarding:
        episodes.append((on_rx, trace[-1][1]))
    return episodes, flags


def session_weapon(raw_path):
    debug = raw_path.with_name("debug_" + raw_path.name[4:])
    if debug.exists():
        with debug.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if "Candidate ENDED weapon=" in row["line"]:
                    return weapon_key(row["line"].split("weapon=")[1].split()[0])
    return weapon_key(raw_path.name)


def guard_windows(raw_path, params=NEW):
    """Replayed GUARDING windows in laptop time (for lenient_p1.py), or None."""
    if not raw_path.exists():
        return None
    weapon = session_weapon(raw_path)
    rows = load_left(raw_path)
    if not weapon or not rows:
        return None
    return run(pose_trace(rows), weapon, params)[0]


def sessions():
    for d in SOURCES:
        for raw in sorted(d.glob("raw_*.csv")):
            label = raw.stem[20:]
            yield d.name, label, raw


def evaluate(params, cache, verbose=False, only=None):
    found = coverage_num = coverage_den = false = 0
    lines = []
    for (folder, label, raw), (weapon, trace) in cache.items():
        if not weapon or not trace or (only and weapon != only):
            continue
        episodes, flags = run(trace, weapon, params)
        is_guard = label.startswith("guard_")
        if is_guard:
            # Time still (gyro EMA < 40) within 20 deg of the session's own guard pose.
            target = guard_pose(trace)
            held = [g for (_, _, el, az, ge), g in zip(trace, flags)
                    if ge < 40 and target and math.hypot(el - target[0], angle_diff(az, target[1])) < 20]
            coverage_num += sum(held)
            coverage_den += len(held)
            found += len(episodes)
        else:
            false += len(episodes)
        if verbose:
            kind = "GUARD" if is_guard else "other"
            extra = f" guarded {sum(held)}/{len(held)} still-at-pose samples" if is_guard else ""
            lines.append(f"  {folder:9} {label:34} {weapon:5} {kind}  episodes={len(episodes):3}{extra}")
    return found, (coverage_num / coverage_den if coverage_den else 0.0), false, lines


def guard_pose(trace):
    """Most common still pose (10-deg bins) that isn't arm-down rest, for a guard session."""
    bins = {}
    for _, _, el, az, ge in trace:
        if ge < 40:
            key = (round(el / 10) * 10, round(az / 10) * 10)
            bins[key] = bins.get(key, 0) + 1
    ranked = sorted(bins.items(), key=lambda kv: -kv[1])
    # Rest poses in the captures sit at az ~ +/-180 (sword) or el < -30.
    for (el, az), _ in ranked:
        if angle_diff(az, 180) > 30 and el > -30:
            return el, az
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sweep", action="store_true", help="grid-search the guard settings")
    args = parser.parse_args()
    cache = {}
    for key in sessions():
        rows = load_left(key[2])
        cache[key] = (session_weapon(key[2]), pose_trace(rows) if rows else [])
    if args.sweep:
        grids = {
            "sword": [[(-3, 94, enter, enter + 10, arm, arrival)]
                      for enter in (10, 12) for arm in (60, 70, 80, 85) for arrival in (0, 300, 350, 400)],
            "spear": [[(60, -76, 15, 25, 60, 0), (28, -76, enter, enter + 10, 60, 0)]
                      for enter in (10, 12, 15, 18, 20, 25)],
        }
        for weapon, options in grids.items():
            results = []
            for refs in options:
                params = dict(NEW, refs={weapon: refs})
                found, cover, false, _ = evaluate(params, cache, only=weapon)
                results.append((false, -round(cover, 2), -found, refs))
            print(f"\n{weapon}: false  coverage  episodes  refs")
            for false, cover, found, refs in sorted(results)[:10]:
                print(f"{'':7}{false:5}  {-cover:8.2f}  {-found:8}  {refs}")
        return
    for name, params in (("current", OLD), ("proposed", NEW)):
        found, cover, false, lines = evaluate(params, cache, verbose=True)
        print(f"\n{name}: guard episodes={found} coverage={cover:.2f} false episodes={false}")
        print("\n".join(lines))


if __name__ == "__main__":
    main()
