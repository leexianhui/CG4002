"""Build and audit the combined action-pattern bank from P2 recordings.

Uses only the Python standard library. A candidate is inferred to be an
attempt when it has a substantial forward stroke. Opposite-direction returns
are excluded using the direction of accepted strokes in that session. The
CSV audit is deliberately kept next to this script so the inference is
reviewable without changing the original recordings.
"""

import argparse
import bisect
import csv
import math
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "2 players" / "logs" / "himeth_v1"
RECENT_LOGS = ROOT / "2 players" / "logs" / "p2"
P1_LOGS = ROOT / "2 players" / "logs" / "p1_v1"
AUDIT_CSV = ROOT / "p2_attempts.csv"
SKETCHES = [ROOT / "2 players" / f"p{n}_right_wrist" / f"p{n}_right_wrist.ino" for n in (1, 2)]
WEAPONS = {"sword": "WEAPON_SWORD_SHIELD", "mace": "WEAPON_MACE", "spear": "WEAPON_SPEAR"}
FIELDS = ("peak", "rot", "reach", "ax", "ay", "az", "px", "py", "pz", "beacons", "accept", "t0", "t1", "dist")
VALUE = re.compile(r"([A-Za-z][A-Za-z0-9]*)=(-?[0-9.]+)")
BEGIN = "// ===== P2_LENIENT_MODEL BEGIN ====="
END = "// ===== P2_LENIENT_MODEL END ====="


def values(line):
    return {key: float(value) for key, value in VALUE.findall(line) if key in FIELDS}


def dot(a, b):
    return sum(a["a" + c] * b["a" + c] for c in "xyz")


def sessions(logs_dir=LOGS):
    for path in sorted(logs_dir.glob("debug_*.csv")):
        label = re.sub(r"^debug_\d{8}_\d{6}_", "", path.stem)
        if label.startswith("sword_shield_"):
            weapon, action = "sword", label[len("sword_shield_"):]
        else:
            weapon, action = label.split("_", 1)
        if weapon not in WEAPONS:
            continue
        blocks, strokes = [], []
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                line = row["line"]
                if "Stroke i=" in line:
                    stroke = values(line)
                    stroke["kind"] = "push" if "kind=push" in line else "rot"
                    stroke["reason"] = re.search(r"reason=(\w+)", line).group(1)
                    strokes.append(stroke)
                elif "Candidate ENDED" in line:
                    blocks.append((strokes, int(re.search(r"accepted=(\d+)", line).group(1))))
                    strokes = []
        yield path.name, weapon, action, blocks


def attempts():
    marked, examples = [], []
    for name, weapon, action, blocks in sessions():
        raw_path = LOGS / ("raw_" + name[6:])
        with raw_path.open(newline="", encoding="utf-8") as handle:
            raw_times = sorted(int(row["t_ms"]) for row in csv.DictReader(handle)
                               if row["node"] == "right_wrist")
        kind = "push" if action == "thrust" else "rot"
        accepted = [s for strokes, _ in blocks for s in strokes
                    if s["kind"] == kind and s["accept"]]
        seed = max(accepted, key=lambda s: s["peak"], default=None)
        if action == "horizontal":
            # Some horizontal strikes were previously accepted in the return
            # direction. Use the dominant direction in the complete session.
            first = [max((s for s in strokes if s["kind"] == "rot"),
                         key=lambda s: s["peak"] * min(s["rot"], 250), default=None)
                     for strokes, _ in blocks]
            first = [s for s in first if s is not None]
            if first:
                seed = max(first, key=lambda s: sum(dot(s, other) >= 0.25 for other in first))
        for index, (strokes, count) in enumerate(blocks):
            choices = [s for s in strokes if s["kind"] == kind and s["beacons"] > 0]
            if kind == "rot":
                choices = [s for s in choices if s["peak"] >= 100 and s["rot"] >= 35
                           and (seed is None or action == "spin" or dot(s, seed) >= 0.25)]
                key = lambda s: s["peak"] * min(s["rot"], 250)
            else:
                choices = [s for s in choices if s["peak"] >= 0.3 and s["reach"] >= 0.15
                           and (seed is None or dot(s, seed) >= 0.25)]
                key = lambda s: s["peak"] * min(s["reach"], 1.5)
            if not choices:
                continue
            chosen = max(choices, key=key)
            lo = bisect.bisect_left(raw_times, int(chosen["t0"]))
            hi = bisect.bisect_right(raw_times, int(chosen["t1"]))
            sample_times = raw_times[lo:hi]
            marked.append({"session": name, "candidate": index, "weapon": weapon,
                           "action": action, "kind": kind, "t0_ms": int(chosen["t0"]),
                           "t1_ms": int(chosen["t1"]), "baseline_accepts": count,
                           "raw_samples": len(sample_times),
                           "raw_gaps_gt25ms": sum(b - a > 25 for a, b in zip(sample_times, sample_times[1:])),
                           "inference": "forward_stroke"})
            if count == 0:
                examples.append((weapon, action, kind, chosen))
    return marked, examples


def recent_mace_targets():
    """Two forward thrusts attested from Lin's new P2 capture."""
    target_times = (195021, 202774)
    capture = RECENT_LOGS / "debug_20261106_231654_mace_thrust.csv"
    if not capture.exists():
        # The raw/debug captures are ignored by git. Preserve the reviewed
        # window metadata and exemplar when the local recording is archived.
        with AUDIT_CSV.open(newline="", encoding="utf-8") as handle:
            marked = [row for row in csv.DictReader(handle)
                      if row["session"] == capture.name and int(row["t0_ms"]) in target_times]
        if {int(row["t0_ms"]) for row in marked} != set(target_times):
            raise ValueError("saved mace thrust target windows are missing")
        extra = [("mace", "thrust", "push",
                  {"ax": -0.088, "ay": 0.986, "az": -0.142,
                   "rot": 124.4, "reach": 1.270})]
        return marked, extra
    for name, weapon, action, blocks in sessions(RECENT_LOGS):
        if name != "debug_20261106_231654_mace_thrust.csv":
            continue
        raw_path = RECENT_LOGS / ("raw_" + name[6:])
        with raw_path.open(newline="", encoding="utf-8") as handle:
            raw_times = sorted(int(row["t_ms"]) for row in csv.DictReader(handle)
                               if row["node"] == "right_wrist")
        marked, extra = [], []
        for index, (strokes, count) in enumerate(blocks):
            for stroke in strokes:
                if stroke["kind"] != "push" or int(stroke["t0"]) not in target_times:
                    continue
                lo = bisect.bisect_left(raw_times, int(stroke["t0"]))
                hi = bisect.bisect_right(raw_times, int(stroke["t1"]))
                times = raw_times[lo:hi]
                marked.append({"session": name, "candidate": index, "weapon": weapon,
                               "action": action, "kind": "push", "t0_ms": int(stroke["t0"]),
                               "t1_ms": int(stroke["t1"]), "baseline_accepts": count,
                               "raw_samples": len(times),
                               "raw_gaps_gt25ms": sum(b - a > 25 for a, b in zip(times, times[1:])),
                               "inference": "forward_mace_thrust"})
                if int(stroke["t0"]) == 202774:
                    extra.append((weapon, action, "push", stroke))
        if {r["t0_ms"] for r in marked} != set(target_times):
            raise ValueError("Lin's mace thrust targets are missing from the new log")
        return marked, extra
    raise ValueError("Lin's mace thrust capture is missing")


def block_for(examples):
    lines = [BEGIN, "// Combined-model variants from forward strokes in the P2 recordings.",
             "// Regenerate with: python lenient_p2.py --write",
             "const LenientTemplate P2_LENIENT_MODEL[] = {"]
    for weapon, action, kind, stroke in examples:
        def f(key):
            return f'{stroke.get(key, 0.0):.3f}f'
        lines.append("    {" + ", ".join(
            [WEAPONS[weapon], f'"{action}"', "true" if kind == "push" else "false"] +
            [f(k) for k in ("ax", "ay", "az", "px", "py", "pz", "rot", "reach")]) + "},")
    lines += ["};", "const int P2_LENIENT_COUNT = sizeof(P2_LENIENT_MODEL) / sizeof(P2_LENIENT_MODEL[0]);", END]
    return "\n".join(lines)


def angle(a, b, prefix_a="a", prefix_b="a", sign_free=False):
    cosine = sum(a[prefix_a + c] * b[prefix_b + c] for c in "xyz")
    if sign_free:
        cosine = abs(cosine)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def score(stroke, example, kind, spin):
    da = angle(stroke, example, sign_free=spin) / 25
    if kind == "rot":
        dp = angle(stroke, example, "p", "p", sign_free=spin) / 45
        dr = (stroke["rot"] - example["rot"]) / 50
        return math.sqrt(da * da + dp * dp + dr * dr)
    reach = (stroke["reach"] - example["reach"]) / 0.40
    dr = (stroke["rot"] - example["rot"]) / 45
    return math.sqrt(da * da + reach * reach + dr * dr)


def audit(marked, examples, logs_dir=LOGS):
    """Score both pattern sets per stroke, then commit old accepts first."""
    selected = {(r["session"], r["candidate"]) for r in marked}
    missing, extras, baseline, added = [], [], 0, 0
    for name, weapon, action, blocks in sessions(logs_dir):
        extra_last = None
        bank = [(a, k, s) for w, a, k, s in examples if w == weapon]
        for cand, (strokes, count) in enumerate(blocks):
            baseline += count
            wider_mace = weapon == "mace" and any(
                s["kind"] == "push" and s["reason"] == "far" and
                0.75 < s["dist"] <= 0.95 and s["reach"] >= 0.15
                for s in strokes)
            best = None
            for stroke in strokes:
                kind = stroke["kind"]
                if stroke["beacons"] <= 0 or stroke["reason"] in ("torso_data", "lead_in", "overlap"):
                    continue
                if kind == "rot" and (stroke["peak"] < 100 or stroke["rot"] < 35):
                    continue
                if kind == "push" and (stroke["peak"] < 0.3 or stroke["reach"] < 0.15):
                    continue
                if extra_last:
                    prev, prev_action = extra_last
                    gap = stroke["t0"] - prev["t1"]
                    if gap <= max(300, prev["t1"] - prev["t0"]) and prev_action != "spin":
                        if kind == "rot" and prev["kind"] == "rot" and dot(stroke, prev) < -0.5:
                            continue
                        if kind == "push" and angle(stroke, prev, "a", "a" if prev["kind"] == "push" else "p") > 120:
                            continue
                for hint, example_kind, example in bank:
                    if kind != example_kind:
                        continue
                    distance = score(stroke, example, kind, hint == "spin")
                    if distance < 0.35 and (best is None or distance < best[0]):
                        best = (distance, stroke, hint)
            if count:
                continue
            if wider_mace:
                added += 1
                if (name, cand) not in selected:
                    extras.append((name, cand))
                continue
            if best:
                added += 1
                extra_last = (best[1], best[2])
                if (name, cand) not in selected:
                    extras.append((name, cand))
            elif (name, cand) in selected:
                missing.append((name, cand))
    print(f"{logs_dir.name} logged-feature replay: {baseline} old accepts retained; {added} added; "
          f"{len(missing)} marked windows missed; {len(extras)} unmarked candidates added.")
    if missing:
        print("Marked misses:", missing)
    if extras:
        print("Unmarked additions:", extras)
    return not missing


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="update both hub sketches and the attempt audit")
    args = parser.parse_args()
    marked, examples = attempts()
    recent, extra = recent_mace_targets()
    examples.extend(extra)
    print(f"Inferred {len(marked)} forward-stroke attempts; {len(examples)} missed candidates supply fallback examples.")
    by_session = {}
    for row in marked:
        by_session.setdefault(row["session"], [0, 0])
        by_session[row["session"]][0] += 1
        by_session[row["session"]][1] += row["baseline_accepts"] == 0
    for name, (total, missed) in by_session.items():
        print(f"{name}: {total} inferred attempts, {missed} missed candidates")
    covered = audit(marked, examples)
    if (RECENT_LOGS / "debug_20261106_231654_mace_thrust.csv").exists():
        covered = audit(recent, examples, RECENT_LOGS) and covered
    else:
        print("p2 recent capture absent; using saved targets and skipping its replay")
    if P1_LOGS.is_dir():
        audit([], examples, P1_LOGS)
    if args.write:
        audit_path = AUDIT_CSV
        with audit_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=marked[0].keys())
            writer.writeheader()
            writer.writerows(marked + recent)
        block = block_for(examples)
        pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
        for path in SKETCHES:
            source = path.read_text(encoding="utf-8")
            if len(pattern.findall(source)) != 1:
                raise ValueError(f"{path}: missing or duplicate fallback block")
            path.write_text(pattern.sub(block, source, count=1), encoding="utf-8", newline="")
        print(f"Wrote {audit_path} and both hub sketches")
    if not covered:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
