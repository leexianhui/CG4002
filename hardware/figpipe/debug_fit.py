"""Convert the hub's logged Stroke features into model-ready training rows."""

from dataclasses import dataclass
import os

import numpy as np
import pandas as pd

from . import raw
from .logs import FAKE_ACTIONS, REAL_ACTIONS, load, speed_from_suffix, label_class


@dataclass
class Capture:
    name: str
    path: str
    label: str
    speed: str
    cls: str
    weapon: str
    split: str
    locked_weapon: str = None


def load_captures(logs_dir):
    ds = load(logs_dir)
    captures = []
    for item in ds.sessions:
        if item["kind"] != "debug" or item["label"] not in REAL_ACTIONS + FAKE_ACTIONS:
            continue
        weapon, split = raw.recording_metadata(item["suffix"])
        if weapon:
            captures.append(Capture(
                name=os.path.basename(item["path"])[6:-4], path=item["path"],
                label=item["label"], speed=speed_from_suffix(item["suffix"]),
                cls=label_class(item["label"]), weapon=weapon, split=split))
    names = {"debug_" + c.name + ".csv" for c in captures}
    bad = [e for e in ds.parse_errors if e.split(":", 1)[0] in names]
    if bad:
        raise ValueError("unparseable training log lines:\n" + "\n".join(bad[:20]))
    return captures, ds.lines


def stroke_rows(captures, lines):
    """Logged features, grouped by the Candidate ENDED boundaries in each file."""
    rows, warnings = [], []
    for capture in captures:
        name = "debug_" + capture.name + ".csv"
        g = lines[(lines.session == name) & (lines.stream == "debug") &
                  (lines.node == "right_wrist")].sort_values("row")
        cand, seen, candidate_rows = 0, 0, []
        for _, r in g.iterrows():
            if r.kind == "motion_started" and seen:
                warnings.append(f"{capture.name} candidate {cand}: missing closing line")
                cand += 1
                seen, candidate_rows = 0, []
            if r.kind == "candidate_ended":
                expected = int(r.strokes + r.pushes)
                if expected != seen or len(candidate_rows) != seen:
                    warnings.append(f"{capture.name} candidate {cand}: {len(candidate_rows)} complete of {seen} "
                                    f"logged stroke lines; hub reported {expected}")
                else:
                    for i in candidate_rows:
                        rows[i]["candidate_complete"] = True
                cand += 1
                seen, candidate_rows = 0, []
                continue
            if r.kind != "stroke":
                continue
            seen += 1
            is_push = r.stroke_kind == "push"
            required = ("ax", "ay", "az", "peak", "rot", "t0", "t1", "beacons")
            required += ("reach",) if is_push else ("px", "py", "pz", "torsoRatio")
            if any(pd.isna(r[k]) for k in required):
                warnings.append(f"{capture.name} row {int(r.row)}: incomplete {r.stroke_kind} features")
                continue
            candidate_rows.append(len(rows))
            rows.append({
                "session": capture.name, "weapon": capture.weapon,
                "locked_weapon": capture.locked_weapon or capture.weapon,
                "split": capture.split,
                "label": capture.label, "speed": capture.speed, "cls": capture.cls,
                "kind": r.stroke_kind, "cand": cand, "t0": float(r.t0), "t1": float(r.t1),
                "dur": float(r.t1 - r.t0), "peak": float(r.peak), "rot": float(r.rot),
                "ux": float(r.ax), "uy": float(r.ay), "uz": float(r.az),
                "hx": np.nan if is_push else float(r.px),
                "hy": np.nan if is_push else float(r.py),
                "hz": np.nan if is_push else float(r.pz),
                "torso_ratio": np.nan if is_push else float(r.torsoRatio),
                "torso_beacons": int(r.beacons),
                "reach": float(r.reach) if is_push else np.nan,
                "logged_nearest": r.nearest, "logged_dist": float(r.dist),
                "logged_reason": r.reason, "logged_accept": bool(r.accept),
                "candidate_complete": False,
            })
        if seen:
            warnings.append(f"{capture.name}: {seen} stroke lines without a closing candidate")
    if not rows:
        raise ValueError("no usable hub Stroke lines found")
    rep = pd.DataFrame(rows)
    # Rotation features are individually complete even if a nearby debug line
    # was lost. Push selection depends on candidate boundaries and counts.
    rep["has_gap"] = (rep.torso_beacons <= 0) | ((rep.kind == "push") & ~rep.candidate_complete)
    return rep, warnings


def label_roles(rep):
    """Use the fast action's dominant direction to separate strikes and returns."""
    rep = rep.copy()
    rep["role"] = np.where(rep.cls == "fake", "fake", "other")
    for weapon in raw.WEAPONS:
        for action in REAL_ACTIONS:
            if action == "thrust":
                continue
            mask = (rep.weapon == weapon) & (rep.label == action) & (rep.kind == "rot")
            group = rep[mask]
            if action == "spin":
                group = group[group.rot.abs() >= 250.0]
            fast = group[group.speed == "fast"]
            seed_group = fast if len(fast) else group
            if seed_group.empty:
                continue
            seed = seed_group.loc[seed_group.peak.idxmax(), ["ux", "uy", "uz"]].to_numpy(float)
            u = group[["ux", "uy", "uz"]].to_numpy(float)
            cos = u @ seed
            if action == "spin":
                aligned = u * np.where(cos >= 0, 1, -1)[:, None]
                axis = aligned.mean(axis=0)
                axis /= np.linalg.norm(axis)
                cos = np.abs(u @ axis)
            else:
                axis = u[cos > raw.ROLE_COS].mean(axis=0)
                axis /= np.linalg.norm(axis)
                cos = u @ axis
            rep.loc[group.index[cos > raw.ROLE_COS], "role"] = "strike"
            if action != "spin":
                rep.loc[group.index[cos < -raw.ROLE_COS], "role"] = "return"
        th = rep[(rep.weapon == weapon) & (rep.label == "thrust") &
                 (rep.kind == "push") & ~rep.has_gap]
        if th.empty:
            continue
        from .replay import thrust_forward
        forward = thrust_forward(th)
        cos = th[["ux", "uy", "uz"]].to_numpy(float) @ forward
        rep.loc[th.index[cos < -0.7], "role"] = "return"
        positives = th[cos > 0.7]
        if len(positives):
            from .replay import THRUST_REPEAT_GUARD_MS
            chosen = positives.loc[positives.groupby(["session", "cand"]).peak.idxmax()]
            for _, session_pushes in chosen.groupby("session"):
                last_end = -np.inf
                for i, push in session_pushes.sort_values("t0").iterrows():
                    if 0 <= push.t0 - last_end <= THRUST_REPEAT_GUARD_MS:
                        rep.loc[i, "role"] = "return"
                    else:
                        rep.loc[i, "role"] = "strike"
                        last_end = push.t1
    return rep
