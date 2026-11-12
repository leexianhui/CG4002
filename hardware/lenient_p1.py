"""Build and audit the P1 action-pattern bank, and replay the hub's decisions.

Uses the Python standard library plus figpipe.handling. P1 holds the weapons
differently from the players the fitted STRIKE_MODEL came from, so this script
turns P1's own forward strokes into a fallback bank, P1_LENIENT_MODEL, used by
both hub sketches alongside P2_LENIENT_MODEL:

  * weapon-agnostic: a P1 pattern matches whatever weapon the hub locked;
  * wider: raw distance <= P1_LENIENT_MAX_DIST (1.0) on the P2 bank's scales;
  * it only fires for a candidate with no original accept, at most once.

Every pattern must pass the commitment gate (figpipe.handling:
rotation_commit_reject / push_commit_reject), and a pattern is pruned when it
would match any stroke in a fake/guard session, so the bank cannot reproduce
the flick, raise and back-swing accepts seen in the 2026-11-12 captures.

The replay re-runs the hub's per-stroke decision on the logged stroke
features: handlingReject, the commitment gate, guard suppression, return
history, the fitted distance limits, then the two banks. It estimates rather
than proves live behaviour (push context reasons and guard timing are taken
from the logs). Reports:
  * per action session: recall of forward-stroke attempts, logged vs replayed,
    leave-one-session-out, and split-half for single P1 horizontal sessions;
  * per fake/guard session: replayed accepts, with the bank pruned without
    that session (every accept there is false).
"""

import argparse
import csv
import re
import statistics
from datetime import datetime
from pathlib import Path

import bisect
import math

from figpipe.handling import (push_commit_reject, rotation_commit_reject,
                              rotation_reject, torso_quiet_cut)
from guard_replay import guard_windows
from lenient_p2 import WEAPONS, angle, dot, score

ROOT = Path(__file__).resolve().parent
LOGS_ROOT = ROOT / "2 players" / "logs"
# P1 action captures: the 2026-11-09 sessions and the 2026-11-12 horizontals.
P1_SOURCES = [LOGS_ROOT / "himeth_v2", LOGS_ROOT / "p1_v1", LOGS_ROOT / "p1_v3", LOGS_ROOT / "p1"]
P2_SOURCES = [LOGS_ROOT / "himeth_v1"]
NEGATIVE_SOURCES = [LOGS_ROOT / name for name in ("p1", "p1_v1", "p1_v2", "p1_v3", "p2", "p2_v1", "p2_v2")]
AUDIT_CSV = ROOT / "p1_attempts.csv"
SKETCHES = [ROOT / "2 players" / f"p{n}_right_wrist" / f"p{n}_right_wrist.ino" for n in (1, 2)]
P2_BEGIN = "// ===== P2_LENIENT_MODEL BEGIN ====="
P2_END = "// ===== P2_LENIENT_MODEL END ====="
BEGIN = "// ===== P1_LENIENT_MODEL BEGIN ====="
END = "// ===== P1_LENIENT_MODEL END ====="
ACTIONS = ("vertical", "horizontal", "diagonal", "uppercut", "thrust", "spin")
FULL_WEAPON = {"sword": "sword_shield", "mace": "mace", "spear": "spear"}
P2_MAX_DIST = 0.35
P1_MAX_DIST = 1.0  # keep equal to P1_LENIENT_MAX_DIST in the sketches
DEDUPE_DIST = 0.3
GUARD_EDGE_S = 0.6      # keep equal to GUARD_EDGE_MS in the sketches
CLOSE_LAG_S = 0.35      # stroke end -> its debug line (candidate settle hold)
# Fitted-path limits, mirroring ACTION_LIMITS / strikeDistanceLimit / onMotionEnd.
STRIKE_LIMIT = {"sword": 1.5, "spear": 2.0, "mace": 1.75}
VALUE = re.compile(r"([A-Za-z][A-Za-z0-9]*)=(-?[0-9.]+)")


def weapon_key(text):
    for key in ("sword", "mace", "spear"):
        if key in text:
            return key
    return None


def rx_seconds(text):
    return datetime.fromisoformat(text).timestamp()


def parse_session(path):
    """Candidates of one debug log: strokes, original accept count, locked weapon, guard windows."""
    label = re.sub(r"^debug_\d{8}_\d{6}_", "", path.stem)
    tokens = label.split("_")
    info = {"name": path.name, "dir": path.parent.name, "label": label, "weapon": weapon_key(label),
            "action": next((a for a in ACTIONS if a in tokens), None),
            "negative": "fake" in label or "guard" in label, "blocks": [], "guards": []}
    strokes, guard_on = [], None
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            line = row["line"]
            if "Stroke i=" in line:
                stroke = {k: float(v) for k, v in VALUE.findall(line)}
                stroke["kind"] = "push" if "kind=push" in line else "rot"
                stroke["reason"] = re.search(r"reason=(\w+)", line).group(1)
                nearest = re.search(r"nearest=(\w+)", line)
                stroke["nearest"] = nearest.group(1) if nearest else None
                stroke["rx"] = rx_seconds(row["rx_time"])
                strokes.append(stroke)
            elif "Candidate ENDED" in line:
                locked = re.search(r"weapon=(\w+)", line).group(1)
                info["blocks"].append({"strokes": strokes,
                                       "count": int(re.search(r"accepted=(\d+)", line).group(1)),
                                       "locked": weapon_key(locked) or info["weapon"]})
                strokes = []
            elif "[left_wrist] guard_state=" in line:
                t = rx_seconds(row["rx_time"])
                if "GUARDING" in line and guard_on is None:
                    guard_on = t
                elif "IDLE" in line and guard_on is not None:
                    info["guards"].append((guard_on, t))
                    guard_on = None
    if guard_on is not None:
        info["guards"].append((guard_on, float("inf")))
    add_push_gyro_peaks(info, path.with_name("raw_" + path.name[len("debug_"):]))
    # Guard suppression follows the guard detector being flashed, replayed on
    # the raw left-wrist stream; the logged windows came from older detectors.
    replayed = guard_windows(path.with_name("raw_" + path.name[len("debug_"):]))
    if replayed is not None:
        info["guards"] = replayed
    return info


def add_push_gyro_peaks(info, raw_path):
    """Peak wrist angular speed over each push (PushStroke.peakGyroDps).

    Newer logs carry it as gyroPk=; older ones are filled from the raw wrist
    stream, which holds the same gyro samples the hub buffered."""
    pushes = [s for b in info["blocks"] for s in b["strokes"] if s["kind"] == "push" and "gyroPk" not in s]
    if not pushes or not raw_path.exists():
        return
    samples = []
    with raw_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["node"] == "right_wrist":
                samples.append((int(row["t_ms"]),
                                math.sqrt(sum(float(row[k]) ** 2 for k in ("gx", "gy", "gz")))))
    samples.sort()
    times = [t for t, _ in samples]
    for s in pushes:
        lo, hi = bisect.bisect_left(times, s["t0"]), bisect.bisect_right(times, s["t1"])
        if hi - lo >= 5:
            s["gyroPk"] = max(g for _, g in samples[lo:hi])


def load(dirs, negative=None):
    out = []
    for d in dirs:
        if d.is_dir():
            out += [parse_session(p) for p in sorted(d.glob("debug_*.csv"))]
    if negative is not None:
        out = [s for s in out if s["negative"] == negative and (negative or s["action"])]
    return out


def strong(stroke, kind):
    if stroke["kind"] != kind or stroke.get("beacons", 0) <= 0:
        return False
    if kind == "rot":
        return stroke["peak"] >= 100 and stroke["rot"] >= 35
    return stroke["peak"] >= 0.3 and stroke.get("reach", 0) >= 0.15


def strength(stroke):
    if stroke["kind"] == "rot":
        return stroke["peak"] * min(stroke["rot"], 250)
    return stroke["peak"] * min(stroke["reach"], 1.5)


def duration(stroke):
    return stroke["t1"] - stroke["t0"]


def axis(stroke):
    return (stroke["ax"], stroke["ay"], stroke["az"])


def commit(stroke):
    """The commitment gate's reason for this stroke, or None."""
    if stroke["kind"] == "rot":
        return rotation_commit_reject(stroke["peak"], stroke["rot"], duration(stroke),
                                      axis(stroke), stroke.get("torsoRatio", 0.0))
    return push_commit_reject(stroke["peak"], stroke.get("reach", 0.0), stroke.get("gyroPk"))


def handling(stroke, weapon, action):
    if stroke["kind"] != "rot" or not action:
        return None
    return rotation_reject(FULL_WEAPON.get(weapon), action, axis(stroke),
                           (stroke["px"], stroke["py"], stroke["pz"]), stroke["peak"], duration(stroke))


def guarding(stroke, guards):
    end = stroke["rx"] - CLOSE_LAG_S
    start = end - duration(stroke) / 1000.0
    # Only the guard edges (raising/lowering the shield); swings while the
    # guard is held count, as in guardSuppressed().
    return any(start < edge + GUARD_EDGE_S and edge - GUARD_EDGE_S < end
               for on, off in guards for edge in (on, off) if edge != float("inf"))


def forward_strokes(session):
    """(candidate index, stroke) for each candidate whose strongest stroke is a forward stroke."""
    kind = "push" if session["action"] == "thrust" else "rot"
    tops = []
    for index, block in enumerate(session["blocks"]):
        choices = [s for s in block["strokes"] if strong(s, kind)]
        if choices:
            tops.append((index, max(choices, key=strength)))
    if not tops:
        return []
    lead = max((s for _, s in tops), key=strength)
    groups = ([t for t in tops if dot(t[1], lead) >= 0], [t for t in tops if dot(t[1], lead) < 0])
    return max((g for g in groups if g), key=lambda g: statistics.median(strength(s) for _, s in g))


def rotation_limit(weapon, stroke):
    action = stroke["nearest"]
    if action == "spin":
        return max(STRIKE_LIMIT[weapon], 2.25)
    if weapon == "sword":
        if action in ("horizontal", "diagonal"):
            return 1.75
        if (action == "uppercut" and stroke["peak"] >= 250 and stroke["rot"] >= 60 and
                stroke.get("torsoRatio", 0) >= 0.070):
            return 2.25
    return STRIKE_LIMIT[weapon]


def push_limit(weapon, stroke):
    if weapon == "sword":
        return 1.5 if stroke["peak"] >= 1.5 else 1.1
    return {"spear": 1.5, "mace": 0.95}[weapon]


def is_return(stroke, last):
    if not last or last["nearest"] == "spin":
        return False
    gap = stroke["t0"] - last["t1"]
    return gap <= max(300, duration(last)) and dot(stroke, last) < -0.5


def replay(session, p2_bank, p1_bank, gates=True):
    """Re-run the hub's decision. Returns {candidate: source} for accepted candidates."""
    accepted, last, lenient = {}, None, None
    for index, block in enumerate(session["blocks"]):
        weapon = block["locked"]
        source = None
        for s in block["strokes"]:
            if s["kind"] != "rot":
                continue
            if s.get("beacons", 0) <= 0 or handling(s, weapon, s["nearest"]):
                continue
            if gates and (commit(s) or guarding(s, session["guards"])):
                continue
            if s["nearest"] != "spin" and is_return(s, last):
                continue
            if (s["nearest"] == "uppercut" and s["peak"] >= 250 and s.get("torsoRatio", 0) < 0.045):
                continue
            if torso_quiet_cut(s["nearest"], s.get("torsoRatio", 0), s["rot"], s["peak"]):
                continue
            # The hub's own distance verdict wins: its relaxed sword-uppercut
            # distance differs from the logged nearest-template one.
            if s["reason"] == "far" or s.get("dist", 99) > rotation_limit(weapon, s):
                continue
            last, source = s, "fit"
        for s in block["strokes"]:
            if s["kind"] != "push" or s.get("beacons", 0) <= 0:
                continue
            if s["reason"] in ("handling", "overlap", "lead_in", "return"):
                continue
            if gates and (commit(s) or guarding(s, session["guards"])):
                continue
            if s.get("reach", 0) < 0.15 or (weapon == "sword" and s["rot"] > 55 and s["reach"] < 0.65):
                continue
            if s.get("dist", 99) > push_limit(weapon, s):
                continue
            source = "fit"
        if source is None:
            best = None
            for s in block["strokes"]:
                kind = s["kind"]
                if not strong(s, kind) or s["reason"] in ("torso_data", "lead_in", "overlap", "handling"):
                    continue
                if gates and (commit(s) or guarding(s, session["guards"])):
                    continue
                if lenient:
                    prev, prev_hint = lenient
                    if s["t0"] - prev["t1"] <= max(300, duration(prev)) and prev_hint != "spin":
                        if kind == "rot" and prev["kind"] == "rot" and dot(s, prev) < -0.5:
                            continue
                        if kind == "push" and angle(s, prev, "a", "a" if prev["kind"] == "push" else "p") > 120:
                            continue
                p1_return = (is_return(s, last) if kind == "rot" else s["reason"] == "return")
                for tag, bank in (("p2", p2_bank), ("p1", p1_bank)):
                    if tag == "p1" and p1_return:
                        continue
                    scale = 1.0 if tag == "p2" else P2_MAX_DIST / P1_MAX_DIST
                    for pattern in bank:
                        if pattern["kind"] != kind or (tag == "p2" and pattern["weapon"] != weapon):
                            continue
                        if handling(s, weapon, pattern["hint"]):
                            continue
                        if kind == "rot" and torso_quiet_cut(pattern["hint"], s.get("torsoRatio", 0), s["rot"], s["peak"]):
                            continue
                        d = score(s, pattern, kind, pattern["hint"] == "spin") * scale
                        if d < P2_MAX_DIST and (best is None or d < best[0]):
                            best = (d, s, pattern["hint"], tag)
            if best:
                lenient = (best[1], best[2])
                source = best[3]
        if source:
            accepted[index] = source
    return accepted


def candidate_patterns(sessions):
    """Forward strokes that pass the gate: (session, candidate, pattern)."""
    out = []
    for session in sessions:
        kind = "push" if session["action"] == "thrust" else "rot"
        for index, stroke in forward_strokes(session):
            if commit(stroke):
                continue
            pattern = dict(stroke)
            pattern.update(weapon=session["weapon"], hint=session["action"], kind=kind)
            out.append((session["name"], index, pattern))
    return out


def firing_sessions(patterns, negatives):
    """For each pattern, the negative sessions containing a stroke it would accept."""
    reachable = []
    for session in negatives:
        for block in session["blocks"]:
            for s in block["strokes"]:
                if strong(s, s["kind"]) and not commit(s) and \
                        s["reason"] not in ("torso_data", "lead_in", "overlap", "handling"):
                    reachable.append((session["name"], block["locked"], s))
    fires = []
    for _, _, pattern in patterns:
        hit = set()
        for name, weapon, s in reachable:
            if s["kind"] == pattern["kind"] and not handling(s, weapon, pattern["hint"]) and \
                    not (s["kind"] == "rot" and torso_quiet_cut(pattern["hint"], s.get("torsoRatio", 0), s["rot"], s["peak"])) and \
                    score(s, pattern, pattern["kind"], pattern["hint"] == "spin") <= P1_MAX_DIST:
                hit.add(name)
        fires.append(hit)
    return fires


def make_bank(patterns, fires, exclude=lambda name, index: False, allowed_negative=None):
    """De-duplicated bank, without excluded exemplars, pruned by negative sessions."""
    bank = []
    for (name, index, pattern), hit in zip(patterns, fires):
        if exclude(name, index) or (hit - {allowed_negative}):
            continue
        if any(b["kind"] == pattern["kind"] and score(pattern, b, b["kind"], False) < DEDUPE_DIST for b in bank):
            continue
        bank.append(pattern)
    return bank


def p2_bank_from_sketch():
    """The P2 bank exactly as compiled into the sketches."""
    source = SKETCHES[0].read_text(encoding="utf-8")
    block = source.split(P2_BEGIN, 1)[1].split(P2_END, 1)[0]
    names = {v: k for k, v in WEAPONS.items()}
    bank = []
    for m in re.finditer(r'\{(WEAPON_\w+), "(\w+)", (true|false), ([^}]*)\}', block):
        nums = [float(x.strip().rstrip("f")) for x in m.group(4).split(",")]
        pattern = dict(zip(("ax", "ay", "az", "px", "py", "pz", "rot", "reach"), nums))
        pattern.update(weapon=names[m.group(1)], hint=m.group(2), kind="push" if m.group(3) == "true" else "rot")
        bank.append(pattern)
    return bank


def block_for(bank):
    lines = [BEGIN, "// Forward strokes from the P1 recordings (logs/himeth_v2, logs/p1) that pass the",
             "// commitment gate and match no fake/guard session. Matched for any locked weapon;",
             "// the weapon column records the session it came from.",
             "// Regenerate with: python lenient_p1.py --write",
             "const LenientTemplate P1_LENIENT_MODEL[] = {"]
    for p in bank:
        lines.append("    {" + ", ".join(
            [WEAPONS[p["weapon"]], f'"{p["hint"]}"', "true" if p["kind"] == "push" else "false"] +
            [f'{p.get(k, 0.0):.3f}f' for k in ("ax", "ay", "az", "px", "py", "pz", "rot", "reach")]) + "},")
    lines += ["};", "const int P1_LENIENT_COUNT = sizeof(P1_LENIENT_MODEL) / sizeof(P1_LENIENT_MODEL[0]);", END]
    return "\n".join(lines)


def recall_row(session, accepted, attempts):
    hit = sum(1 for i in attempts if i in accepted)
    extra = sum(1 for i in accepted if i not in attempts)
    return hit, extra


def report(p1_sessions, p2_sessions, negatives, p2_bank, patterns, fires, bank):
    print(f"\n{'action session':44} {'tries':>5} {'logged':>6} {'replay':>6} {'held-out':>8} {'extra':>5}")
    totals = {}
    for group, sessions in (("P1", p1_sessions), ("P2", p2_sessions)):
        tot = [0] * 5
        for session in sessions:
            attempts = {i for i, _ in forward_strokes(session)}
            logged = sum(1 for i in attempts if session["blocks"][i]["count"])
            now, extra = recall_row(session, replay(session, p2_bank, bank), attempts)
            name = session["name"]
            held_bank = make_bank(patterns, fires, exclude=lambda n, i: n == name)
            held, _ = recall_row(session, replay(session, p2_bank, held_bank), attempts)
            label = f"{session['dir']}/{session['label']}"
            if session["dir"] in ("p1", "p1_v1") and session["action"] == "horizontal":
                # Single session for this player: train on the first half, test on the second.
                half = len(session["blocks"]) // 2
                split_bank = make_bank(patterns, fires, exclude=lambda n, i: n == name and i >= half)
                second = {i for i in attempts if i >= half}
                split, _ = recall_row(session, replay(session, p2_bank, split_bank), second)
                label += f" (2nd half {split}/{len(second)})"
            row = (len(attempts), logged, now, held, extra)
            tot = [a + b for a, b in zip(tot, row)]
            print(f"{label:44} {row[0]:5} {row[1]:6} {row[2]:6} {row[3]:8} {row[4]:5}")
        print(f"{group + ' TOTAL':44} {tot[0]:5} {tot[1]:6} {tot[2]:6} {tot[3]:8} {tot[4]:5}")
        totals[group] = tot
    print(f"\n{'fake/guard session (all accepts false)':44} {'cand':>5} {'logged':>6} {'replay':>6}")
    neg_tot = [0, 0, 0]
    for session in negatives:
        logged = sum(1 for b in session["blocks"] if b["count"])
        honest = make_bank(patterns, fires, allowed_negative=session["name"])
        now = replay(session, p2_bank, honest)
        row = (len(session["blocks"]), logged, len(now))
        neg_tot = [a + b for a, b in zip(neg_tot, row)]
        print(f"{session['dir'] + '/' + session['label']:44} {row[0]:5} {row[1]:6} {row[2]:6}  "
              + " ".join(sorted(set(now.values()))))
    print(f"{'NEGATIVE TOTAL':44} {neg_tot[0]:5} {neg_tot[1]:6} {neg_tot[2]:6}")
    return totals, neg_tot


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="update both hub sketches and the attempt audit")
    args = parser.parse_args()
    p1_sessions = load(P1_SOURCES, negative=False)
    p2_sessions = load(P2_SOURCES, negative=False)
    negatives = load(NEGATIVE_SOURCES, negative=True)
    if not p1_sessions:
        raise SystemExit("no P1 action captures found")
    p2_bank = p2_bank_from_sketch()
    patterns = candidate_patterns(p1_sessions)
    fires = firing_sessions(patterns, negatives)
    bank = make_bank(patterns, fires)
    pruned = sum(1 for f in fires if f)
    print(f"{len(patterns)} gated P1 forward strokes; {pruned} pruned (fire on a fake/guard session); "
          f"bank = {len(bank)} patterns ({sum(p['kind'] == 'push' for p in bank)} thrust). "
          f"P2 bank = {len(p2_bank)}.")
    report(p1_sessions, p2_sessions, negatives, p2_bank, patterns, fires, bank)
    if args.write:
        with AUDIT_CSV.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["session", "candidate", "weapon", "action", "kind", "t0_ms", "t1_ms", "in_bank"])
            for (name, index, p), hit in zip(patterns, fires):
                writer.writerow([name, index, p["weapon"], p["hint"], p["kind"], int(p["t0"]), int(p["t1"]),
                                 any(b is p for b in bank)])
        block = block_for(bank)
        pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
        for path in SKETCHES:
            source = path.read_text(encoding="utf-8")
            if len(pattern.findall(source)) != 1:
                raise ValueError(f"{path}: missing or duplicate P1 fallback block")
            path.write_text(pattern.sub(lambda _: block, source, count=1), encoding="utf-8", newline="")
        print(f"Wrote {AUDIT_CSV} and both hub sketches")


if __name__ == "__main__":
    main()
