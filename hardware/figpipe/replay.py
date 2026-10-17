"""Sample-by-sample replay of the hub's valid-action check (stroke + template
version) over RAW_STREAM sessions, so the firmware logic can be tuned and
checked offline before it's flashed.

Mirrors 2 players/p1_right_wrist.ino:
  segment()          updateMotionState(): candidate open / settle close,
                     pretrigger, buffer cap (unchanged from the old firmware)
  emulate_beacons()  the torso's 50 Hz TorsoBeacon (max gyroMag per 20 ms)
  candidate_strokes() onMotionEnd(): findStrokes() on the candidate buffer,
                     plus per-stroke features (rotation axis, angle, torso ratio)
  fit_templates()    the STRIKE_MODEL table (fit_strike_model.py prints it as C)
  template_distance() nearest-template score
  decide()           torso-data check, return suppression, distance limit

The replay is split in two: replay_session() does everything that doesn't
depend on the model (segmentation, strokes, features), decide() applies a
model, so a model can be refitted and re-scored cheaply.
"""

import re

import numpy as np
import pandas as pd

from . import raw
from .logs import REAL_ACTIONS

# Candidate segmentation — firmware constants.
START_THRESHOLD = 60.0
JERK_START_THRESHOLD = 0.3    # was 1.3: a thrust with no twist must open a candidate on its own
                              # (slow thrusts: |accel| - 1 g peaked at only 0.41-0.64)
SETTLE_FRACTION = 0.5
SETTLE_HOLD_MS = 350          # POST_PEAK_MS in the old firmware
CANDIDATE_COOLDOWN_MS = 150
RAW_MAX_DURATION_MS = 3500
MAX_SAMPLES = 500
PRETRIGGER_SAMPLES = 20

# Stroke check (new firmware constants).
MAX_STROKES = 8               # strokes per candidate (fixed array on the hub)
BEACON_INTERVAL_MS = 20
TORSO_PAD_FRAC = 0.5          # torso window = stroke +- this x its duration (min 100 ms)
RETURN_MIN_MS = 300           # return window = max(RETURN_MIN_MS, RETURN_REL x strike duration)
RETURN_REL = 1.0
RETURN_COS = -0.5             # axis . strike axis below this = opposite direction
THRUST_MIN_REACH_M = 0.35      # retain measured slow pushes while rejecting short returns
THRUST_REPEAT_GUARD_MS = 700  # a second push immediately after a thrust is its return
UPPERCUT_TORSO_GATE_PEAK_DPS = 250.0
UPPERCUT_MIN_TORSO_RATIO = 0.045
PUSH_GRAVITY_SAMPLES = 3      # samples before the stroke averaged as the starting gravity
STRIKE_MAX_DIST = 3.0         # default; tuned by tune_max_dist()

# Template fitting floors, so a tight training set doesn't make a template
# that only its own reps can hit. Picked with a small leave-one-speed-out
# grid on the 2026-10-04 sessions (rotation 8-40% x push 20-40 deg x axis
# 10/15 deg): narrower rotation floors failed unseen speeds (horizontal at
# normal speed turned 110 deg vs 126 slow/fast), wider ones let fast returns
# through.
AXIS_SPREAD_FLOOR_DEG = 10.0
PUSH_SPREAD_FLOOR_DEG = 30.0
ROT_SPREAD_FLOOR_DEG = 8.0
ROT_SPREAD_FLOOR_FRAC = 0.20
TORSO_SPREAD_FLOOR = 0.1

# Push strokes — linear motion, for an action that barely rotates (thrust).
# A thrust with no wrist twist never makes a rotation stroke, so it's found
# from linear acceleration instead: accel minus gravity, with gravity tracked
# by the gyro and pulled slowly toward the accel (complementary filter).
G0 = 9.81
GRAVITY_ALPHA = 0.02          # per-sample pull of the gravity estimate toward the accel
PUSH_MIN_PEAK_G = 0.3         # only absolute limit: above hand jitter
PUSH_FRAC = 0.2               # push = linear accel along its peak direction > this x peak
REACH_MAX_MS = 1500           # integrate the reach at most this long
THRUST_MAX_DIST = 1.0         # default; tuned by tune_thrust_dist()
THRUST_LIMIT_FLOOR = 1.5      # held-out fast thrusts need this tolerance
THRUST_DIR_SPREAD_FLOOR_DEG = 18.0
THRUST_REACH_SPREAD_FLOOR_M = 0.35
THRUST_ROT_REF_FLOOR_DEG = 55.0
# Weighting, from a leave-one-speed-out grid over both 2026-10-04 collections
# (reach spread x1/x2/ignored, rotation spread 25/50% of its reference):
# rotation separates a thrust from a swing's lead-in or handling best (AUC
# 0.89); reach drifts a little with speed (slow 0.57 m, fast 0.77 m), so it
# gets half weight. 37/47 thrusts, 3 false thrusts held out.
REACH_SPREAD_FLOOR_FRAC = 0.2
REACH_SPREAD_MULT = 2.0
THRUST_ROT_SPREAD_FRAC = 0.25
THRUST_ROT_SPREAD_FLOOR_DEG = 10.0
THRUST_ACTION = "thrust"


# ------------------------------------------------------------ segmentation

def segment(t, gm, jerk):
    """Candidates as (first, last, reason): inclusive hub-sample indices of the
    buffer onMotionEnd() sees, and why it closed."""
    out = []
    active = False
    cooldown_until = -np.inf
    for i in range(len(t)):
        now = t[i]
        if not active:
            if now >= cooldown_until and (gm[i] > START_THRESHOLD or jerk[i] > JERK_START_THRESHOLD):
                active = True
                first = max(0, i - PRETRIGGER_SAMPLES + 1)  # pretrigger ring includes this sample
                opened, peak, reset_at = now, 0.0, now
            continue
        score = max(gm[i] / START_THRESHOLD, jerk[i] / JERK_START_THRESHOLD)
        peak = max(peak, score)
        if score >= SETTLE_FRACTION * peak:
            reset_at = now
        settled = now - reset_at >= SETTLE_HOLD_MS
        too_long = now - opened >= RAW_MAX_DURATION_MS
        full = i - first + 1 >= MAX_SAMPLES - 1
        if settled or too_long or full:
            out.append((first, i, "settled" if settled else ("timeout" if too_long else "bufferFull")))
            active = False
            cooldown_until = now + CANDIDATE_COOLDOWN_MS
    return out


def emulate_beacons(torso, torso_to_hub_ms):
    """(hub-clock time, gyroMag) per 20 ms torso beacon: the max over the
    samples since the previous beacon, stamped at the last of them."""
    if torso is None or not len(torso):
        return np.array([]), np.array([])
    tt = torso.t.to_numpy(float)
    g = torso.gyroMag.to_numpy()
    b = np.floor(tt / BEACON_INTERVAL_MS)
    df = pd.DataFrame({"b": b, "t": tt, "g": g}).groupby("b").agg(t=("t", "max"), g=("g", "max"))
    return df.t.to_numpy() + torso_to_hub_ms, df.g.to_numpy()


# ----------------------------------------------------------------- strokes

def push_direction(tb, Gb, Ab, a, b):
    """Unit direction of the hand's linear acceleration over the first half of
    the stroke (the push), in the board frame. Gravity starts as the mean
    accel over the PUSH_GRAVITY_SAMPLES before the stroke and is rotated with
    the gyro sample by sample (dg/dt = -w x g), then subtracted. A direction,
    not a size, so it doesn't change with speed."""
    g = Ab[max(0, a - PUSH_GRAVITY_SAMPLES):a + 1].mean(0)
    n_half = max(1, (b - a + 1) // 2)
    acc = np.zeros(3)
    for i in range(a, a + n_half):
        dt = (tb[i] - tb[i - 1]) / 1000.0 if i > 0 else raw.SAMPLE_MS / 1000.0
        g = g - np.cross(np.deg2rad(Gb[i]), g) * dt
        acc += Ab[i] - g
    n = np.linalg.norm(acc)
    return acc / n if n > 1e-9 else np.array([0.0, 0.0, 0.0])


def stroke_features(tb, Gb, Ab, a, b, u, beacon_t, beacon_g, close_t):
    h = push_direction(tb, Gb, Ab, a, b)
    ts = tb[a:b + 1] / 1000.0
    G = Gb[a:b + 1]
    gm = np.linalg.norm(G, axis=1)
    rot = float(np.trapezoid(G @ u, ts))
    path = float(np.trapezoid(gm, ts))
    dur = float(tb[b] - tb[a])
    pad = TORSO_PAD_FRAC * max(dur, 100.0)
    # only beacons that have arrived by the time the candidate closes
    m = (beacon_t >= tb[a] - pad) & (beacon_t <= min(tb[b] + pad, close_t))
    torso_rot = float(beacon_g[m].sum() * BEACON_INTERVAL_MS / 1000.0)
    return {"t0": float(tb[a]), "t1": float(tb[b]), "dur": dur, "peak": float(gm.max()), "rot": rot,
            "ux": u[0], "uy": u[1], "uz": u[2], "hx": h[0], "hy": h[1], "hz": h[2],
            "path": path, "torso_rot": torso_rot,
            "torso_beacons": int(m.sum()), "torso_ratio": torso_rot / path if path > 0 else np.nan}


def linear_accel(tb, Gb, Ab):
    """Accel minus gravity (g, board frame) over a candidate buffer. Gravity
    starts as the mean of the first PRETRIGGER-ish samples, is rotated with
    the gyro each sample (dg/dt = -w x g) and pulled GRAVITY_ALPHA of the way
    toward the measured accel, so it can't drift off."""
    g = Ab[:5].mean(0)
    lin = np.zeros_like(Ab)
    for i in range(len(Ab)):
        dt = (tb[i] - tb[i - 1]) / 1000.0 if i > 0 else raw.SAMPLE_MS / 1000.0
        g = g - np.cross(np.deg2rad(Gb[i]), g) * dt
        g = (1 - GRAVITY_ALPHA) * g + GRAVITY_ALPHA * Ab[i]
        lin[i] = Ab[i] - g
    return lin


def find_push_strokes(tb, Gb, Ab, max_strokes=MAX_STROKES):
    """Push strokes in one candidate buffer: [(a, b, e, u, peak_g, reach_m,
    rot_deg)]. Same claiming scheme as find_strokes_arrays(), on the smoothed
    linear acceleration: a..b is where accel along the peak's own direction u
    stays above PUSH_FRAC of the peak. reach = distance the hand travels along
    u from a until it stops (velocity back to 0 after b) at sample e; rot =
    the wrist's rotation (|omega| integral) over a..e. Both are the same at
    any speed: a faster thrust covers the same distance, sooner."""
    lin = raw.smooth_gyro(linear_accel(tb, Gb, Ab))  # same moving average as the gyro
    m = np.linalg.norm(lin, axis=1)
    used = np.zeros(len(m), bool)
    ts = tb / 1000.0
    out = []
    while len(out) < max_strokes:
        free = np.where(used, -1.0, m)
        ip = int(np.argmax(free)) if len(free) else 0
        if not len(free) or free[ip] < PUSH_MIN_PEAK_G:
            break
        u = lin[ip] / m[ip]
        p = lin @ u
        a = b = ip
        while a > 0 and p[a - 1] > PUSH_FRAC * m[ip]:
            a -= 1
        while b < len(p) - 1 and p[b + 1] > PUSH_FRAC * m[ip]:
            b += 1
        overlaps = used[a:b + 1].any()
        used[a:b + 1] = True
        if overlaps:
            continue
        v = x = 0.0
        e = a
        for i in range(a + 1, len(p)):
            dt = ts[i] - ts[i - 1]
            v += 0.5 * (p[i] + p[i - 1]) * G0 * dt
            e = i
            if v <= 0 and i > b:
                break
            x += v * dt
            if tb[i] - tb[a] >= REACH_MAX_MS:
                break
        wn = np.linalg.norm(Gb[a:e + 1], axis=1)
        rot = float(np.trapezoid(wn, ts[a:e + 1])) if e > a else 0.0
        out.append((a, b, e, u, float(m[ip]), float(x), rot))
    out.sort(key=lambda s: s[0])
    return out


def replay_session(sess):
    """Model-independent part of the replay: one row per stroke the hub would
    find, in time order. kind = 'rot' (rotation stroke) or 'push' (push
    stroke, for thrust)."""
    hub = sess.hub
    t = hub.t.to_numpy(float)
    G = hub[["gx", "gy", "gz"]].to_numpy()
    A = hub[["ax", "ay", "az"]].to_numpy()
    toff = (raw.rx_offset_ms(sess.torso) - raw.rx_offset_ms(hub)) if len(sess.torso) else 0.0
    bt, bg = emulate_beacons(sess.torso, toff)
    meta = {"session": sess.name, "label": sess.label, "speed": sess.speed, "cls": sess.cls,
            "weapon": sess.weapon}
    rows = []
    for c, (first, last, reason) in enumerate(segment(t, hub.gyroMag.to_numpy(), hub.jerk.to_numpy())):
        tb, Gb, Ab = t[first:last + 1], G[first:last + 1], A[first:last + 1]
        cand = {**meta, "cand": c, "cand_t0": tb[0], "cand_t1": tb[-1], "close": reason}
        for k, (a, b, ip, u) in enumerate(raw.find_strokes_arrays(tb, Gb, MAX_STROKES)):
            rows.append({**cand, "kind": "rot", "k": k, **stroke_features(tb, Gb, Ab, a, b, u, bt, bg, tb[-1])})
        for k, (a, b, e, u, peak, reach, rot) in enumerate(find_push_strokes(tb, Gb, Ab)):
            dur = float(tb[e] - tb[a])
            pad = TORSO_PAD_FRAC * max(dur, 100.0)
            nb = int(((bt >= tb[a] - pad) & (bt <= min(tb[e] + pad, tb[-1]))).sum())
            rows.append({**cand, "kind": "push", "k": k, "t0": float(tb[a]), "t1": float(tb[e]), "dur": dur,
                         "peak": peak, "rot": rot, "reach": reach, "ux": u[0], "uy": u[1], "uz": u[2],
                         "torso_beacons": nb, "push_has_gap": bool((np.diff(tb[a:e + 1]) > raw.GAP_MS).any())})
    return pd.DataFrame(rows)


def label_strokes(rep, table, forward_by_weapon=None):
    """Attach labels. Rotation strokes take the Phase-2 stroke table's role
    and has_gap (the table stroke of the same session they overlap most);
    push strokes are labelled by label_pushes()."""
    rep = rep.copy()
    for col, val in (("role", "none"), ("has_gap", False), ("tbl_t0", np.nan), ("tbl_t1", np.nan)):
        rep[col] = val
    rot = rep[rep.kind == "rot"]
    for s, g in rot.groupby("session"):
        tb = table[table.session == s]
        if not len(tb):
            continue
        a0, a1 = tb.t0.to_numpy(float), tb.t1.to_numpy(float)
        for i, r in g.iterrows():
            ov = np.minimum(a1, r.t1) - np.maximum(a0, r.t0)
            k = int(np.argmax(ov))
            m = tb.iloc[k]
            rep.loc[i, ["has_gap", "tbl_t0", "tbl_t1"]] = [bool(m.has_gap), m.t0, m.t1]
            rep.loc[i, "role"] = m.role if ov[k] > 0 else "none"
    return label_pushes(rep, forward_by_weapon=forward_by_weapon)


def thrust_forward(rep):
    """Forward direction of a thrust: mean direction of the biggest push in
    each candidate of the thrust sessions (sign-aligned to their majority)."""
    p = rep[(rep.kind == "push") & (rep.label == THRUST_ACTION)]
    top = p.loc[p.groupby(["session", "cand"]).peak.idxmax()]
    V = top[["ux", "uy", "uz"]].to_numpy(float)
    c, _ = _mean_dir(V, True)
    return c if (V @ c > 0).sum() >= (V @ c < 0).sum() else -c


def label_pushes(rep, forward_by_weapon=None):
    """Push-stroke roles. In thrust sessions the biggest forward push of each
    candidate is the thrust ('strike'), backward pushes are 'return', the rest
    'other'. Pushes in fake sessions are 'fake'; elsewhere 'other' (a swing's
    own linear motion)."""
    rep = rep.copy()
    push = rep.kind == "push"
    if not push.any():
        return rep
    rep.loc[push, "has_gap"] = rep.loc[push, "push_has_gap"].astype(bool)
    rep.loc[push, "role"] = np.where(rep.loc[push, "cls"] == "fake", "fake", "other")
    for _, group in rep.groupby("weapon", dropna=False):
        th = group[(group.kind == "push") & (group.label == THRUST_ACTION)]
        if th.empty:
            continue
        fwd = (forward_by_weapon[group.weapon.iloc[0]] if forward_by_weapon is not None
               else thrust_forward(group))
        cos = th[["ux", "uy", "uz"]].to_numpy(float) @ fwd
        rep.loc[th.index[cos < -0.7], "role"] = "return"
        fw = th[cos > 0.7]
        if len(fw):
            rep.loc[fw.loc[fw.groupby(["session", "cand"]).peak.idxmax()].index, "role"] = "strike"
    return rep


# --------------------------------------------------------------- templates

def _unit(v):
    return v / np.linalg.norm(v)


def _mean_dir(V, sign_free):
    """Mean unit direction of unit vectors V and each one's angle to it (deg).
    sign_free: v and -v count as the same direction (bidirectional actions)."""
    V = np.asarray(V, float)
    c = _unit(V[0])
    for _ in range(3 if sign_free else 0):
        V = V * np.where(V @ c >= 0, 1.0, -1.0)[:, None]
        c = _unit(V.mean(0))
    c = _unit(V.mean(0))
    return c, np.degrees(np.arccos(np.clip(V @ c, -1, 1)))


def fit_templates(strikes):
    """STRIKE_MODEL rows from strike strokes (columns label, ux/uy/uz, hx/hy/hz,
    rot, torso_ratio). Spin is bidirectional: its directions are sign-free."""
    rows = []
    for act in REAL_ACTIONS:
        s = strikes[strikes.label == act]
        if len(s) < 3:
            continue
        bid = act in raw.BIDIRECTIONAL
        c, ang = _mean_dir(s[["ux", "uy", "uz"]], bid)
        p, pang = _mean_dir(s[["hx", "hy", "hz"]], bid)
        rot = s.rot.to_numpy(float)
        tr = s.torso_ratio.dropna().to_numpy(float)
        torso_ref = float(np.percentile(tr, 95)) if len(tr) else 1.0
        rows.append({"action": act, "ux": c[0], "uy": c[1], "uz": c[2],
                     "axis_spread": max(float(np.percentile(ang, 95)), AXIS_SPREAD_FLOOR_DEG),
                     "px": p[0], "py": p[1], "pz": p[2],
                     "push_spread": max(float(np.percentile(pang, 95)), PUSH_SPREAD_FLOOR_DEG),
                     "rot_mean": float(rot.mean()),
                     "rot_spread": max(float(rot.std()), ROT_SPREAD_FLOOR_FRAC * float(rot.mean()),
                                       ROT_SPREAD_FLOOR_DEG),
                     "torso_ref": torso_ref, "torso_spread": max(torso_ref, TORSO_SPREAD_FLOOR),
                     "bidirectional": bid, "n": len(s)})
    return pd.DataFrame(rows)


def _angle_deg(a, b, sign_free):
    cos = float(np.dot(a, b))
    return float(np.degrees(np.arccos(np.clip(abs(cos) if sign_free else cos, -1, 1))))


def template_distance(u, h, rot, torso_ratio, model):
    """(nearest action, d, dAxis, dPush, dRot, dTorso); d = sqrt of the summed
    squares. A zero push vector (no linear motion) scores the full push term
    of 180 deg."""
    best = (None, np.inf, np.nan, np.nan, np.nan, np.nan)
    for _, m in model.iterrows():
        if m.action == "spin" and abs(rot) < 250.0:
            continue
        bid = bool(m.bidirectional)
        d_axis = _angle_deg(u, (m.ux, m.uy, m.uz), bid) / m.axis_spread
        d_push = (_angle_deg(h, (m.px, m.py, m.pz), bid) if np.any(h) else 180.0) / m.push_spread
        d_rot = (rot - m.rot_mean) / m.rot_spread
        d_torso = max(0.0, torso_ratio - m.torso_ref) / m.torso_spread if np.isfinite(torso_ratio) else 0.0
        d = float(np.sqrt(d_axis ** 2 + d_push ** 2 + d_rot ** 2 + d_torso ** 2))
        if d < best[1]:
            best = (m.action, d, d_axis, d_push, d_rot, d_torso)
    return best


def distances(rep, model):
    """Vectorised template_distance() for every row of rep: DataFrame with
    nearest, dist, d_axis, d_push, d_rot, d_torso (index = rep's)."""
    U = rep[["ux", "uy", "uz"]].to_numpy(float)
    H = rep[["hx", "hy", "hz"]].to_numpy(float)
    rot = rep.rot.to_numpy(float)
    tr = rep.torso_ratio.to_numpy(float)
    no_push = ~np.any(H != 0, axis=1)
    parts = []
    for _, m in model.iterrows():
        cu, cp = U @ np.array([m.ux, m.uy, m.uz]), H @ np.array([m.px, m.py, m.pz])
        if m.bidirectional:
            cu, cp = np.abs(cu), np.abs(cp)
        da = np.degrees(np.arccos(np.clip(cu, -1, 1))) / m.axis_spread
        dp = np.where(no_push, 180.0, np.degrees(np.arccos(np.clip(cp, -1, 1)))) / m.push_spread
        dr = (rot - m.rot_mean) / m.rot_spread
        dt = np.where(np.isfinite(tr), np.maximum(0.0, tr - m.torso_ref) / m.torso_spread, 0.0)
        d = np.sqrt(da ** 2 + dp ** 2 + dr ** 2 + dt ** 2)
        if m.action == "spin":
            d = np.where(np.abs(rot) < 250.0, np.inf, d)
        parts.append(np.stack([d, da, dp, dr, dt]))
    P = np.stack(parts)                      # template x {d, da, dp, dr, dt} x stroke
    k = np.argmin(P[:, 0, :], axis=0)
    pick = P[k, :, np.arange(len(rep))]      # stroke x {d, ...}
    nearest = model.action.to_numpy()[k].astype(object)
    nearest[np.isinf(pick[:, 0])] = None
    return pd.DataFrame({"nearest": nearest, "dist": pick[:, 0], "d_axis": pick[:, 1],
                         "d_push": pick[:, 2], "d_rot": pick[:, 3], "d_torso": pick[:, 4]}, index=rep.index)


def fit_thrust(pushes):
    """THRUST_MODEL from thrust push strokes (columns ux/uy/uz, reach, rot):
    push direction + spread, reach mean + spread, and the wrist rotation a
    thrust stays under (one-sided: only extra rotation counts against it)."""
    if len(pushes) < 3:
        return None
    c, ang = _mean_dir(pushes[["ux", "uy", "uz"]], False)
    reach = pushes.reach.to_numpy(float)
    # Fast thrusts rotate the wrist more than slow ones in held-out sessions.
    # These floors keep a model fitted without fast examples usable at speed.
    rot_ref = max(float(np.percentile(pushes.rot.to_numpy(float), 90)), THRUST_ROT_REF_FLOOR_DEG)
    return {"action": THRUST_ACTION, "ux": c[0], "uy": c[1], "uz": c[2],
            "dir_spread": max(float(np.percentile(ang, 95)), THRUST_DIR_SPREAD_FLOOR_DEG),
            "reach_mean": float(reach.mean()),
            "reach_spread": max(THRUST_REACH_SPREAD_FLOOR_M, REACH_SPREAD_MULT *
                                max(float(reach.std()), REACH_SPREAD_FLOOR_FRAC * float(reach.mean()))),
            "rot_ref": rot_ref, "rot_spread": max(THRUST_ROT_SPREAD_FRAC * rot_ref, THRUST_ROT_SPREAD_FLOOR_DEG),
            "n": len(pushes)}


def thrust_distances(push, thr):
    """Distance of each push stroke to the thrust template:
    sqrt(dDir^2 + dReach^2 + dRot^2)."""
    U = push[["ux", "uy", "uz"]].to_numpy(float)
    d_dir = np.degrees(np.arccos(np.clip(U @ np.array([thr["ux"], thr["uy"], thr["uz"]]), -1, 1))) / thr["dir_spread"]
    d_reach = (push.reach.to_numpy(float) - thr["reach_mean"]) / thr["reach_spread"]
    d_rot = np.maximum(0.0, push.rot.to_numpy(float) - thr["rot_ref"]) / thr["rot_spread"]
    return pd.DataFrame({"nearest": THRUST_ACTION, "dist": np.sqrt(d_dir ** 2 + d_reach ** 2 + d_rot ** 2),
                         "d_axis": d_dir, "d_reach": d_reach, "d_rot": d_rot}, index=push.index)


def decide(rep, model, max_dist=STRIKE_MAX_DIST, score=None, dist=None, thrust=None,
           thrust_max=THRUST_MAX_DIST, thrust_min_reach=THRUST_MIN_REACH_M):
    """Applies the models in time order, per session.

    Rotation strokes are scored against the rotation templates (model,
    max_dist), push strokes against the thrust template (thrust, thrust_max;
    None = never accepted). Then each candidate is decided like the hub does
    when it closes: its rotation strokes first, in time order, then its push
    strokes, in time order:
      torso_data  no torso beacons around it
      overlap     overlaps an already-accepted stroke (the same movement seen
                  by the other detector, e.g. a swing's own linear motion)
      lead_in     (push) an accepted rotation strike of the same candidate is
                  within max(RETURN_MIN_MS, its duration) — the swing's lead-in
      return      opposite direction straight after the last accepted stroke
                  (see RETURN_*)
      far         too far from its nearest template
      ok          accepted
    score(row) -> (ok, nearest, d) overrides the rotation score (separate-
    cutoffs comparison). dist: precomputed distances() of the rotation rows."""
    rep = rep.copy()
    if "kind" not in rep:
        rep["kind"] = "rot"
    if "cand" not in rep:
        rep["cand"] = np.arange(len(rep))
    rep["_order"] = (rep.kind == "push").astype(int)
    rep = rep.sort_values(["session", "cand", "_order", "t0"]).drop(columns="_order")
    rot = rep.kind == "rot"
    for c in ("nearest", "dist", "d_axis", "d_push", "d_rot", "d_torso", "d_reach"):
        rep[c] = np.nan if c != "nearest" else None
    ok_all = np.zeros(len(rep), bool)
    if rot.any():
        if score is None:
            dd = (dist if dist is not None else distances(rep[rot], model)).loc[rep.index[rot]]
            for c in dd.columns:
                rep.loc[rot, c] = dd[c].to_numpy()
            ok_all[rot.to_numpy()] = (dd.dist <= max_dist).to_numpy()
        else:
            res = [score(r) for _, r in rep[rot].iterrows()]
            ok_all[rot.to_numpy()] = [r[0] for r in res]
            rep.loc[rot, "nearest"] = [r[1] for r in res]
            rep.loc[rot, "dist"] = [r[2] for r in res]
    push = ~rot
    if push.any() and thrust is not None:
        dd = thrust_distances(rep[push], thrust)
        for c in dd.columns:
            rep.loc[push, c] = dd[c].to_numpy()
        ok_all[push.to_numpy()] = (dd.dist <= thrust_max).to_numpy()

    sess = rep.session.to_numpy()
    kind = rep.kind.to_numpy()
    U = rep[["ux", "uy", "uz"]].to_numpy(float)
    # Linear direction of each stroke: a rotation stroke's push direction, a
    # push stroke's own direction. Used to spot a push that is the return of
    # the stroke before it (a swing's return moves the hand back).
    H = (rep[["hx", "hy", "hz"]].to_numpy(float) if "hx" in rep else np.full((len(rep), 3), np.nan))
    lin_dir = np.where((kind == "push")[:, None], U, np.nan_to_num(H))
    t0, t1, dur = rep.t0.to_numpy(float), rep.t1.to_numpy(float), rep.dur.to_numpy(float)
    beacons, near, cand = rep.torso_beacons.to_numpy(), rep.nearest.to_numpy(), rep.cand.to_numpy()
    reasons = []
    last_rot = {}   # session -> last accepted rotation stroke
    accepted = {}   # session -> accepted strokes so far: dicts with t0, t1, u, v, dur, spin, kind, cand

    def is_return(prev, v):
        return (prev is not None and not prev["spin"]
                and t0[i] - prev["t1"] <= max(RETURN_MIN_MS, RETURN_REL * prev["dur"])
                and float(v @ prev["v"]) < RETURN_COS)

    for i in range(len(rep)):
        acc = accepted.get(sess[i], [])
        if beacons[i] == 0:
            reason = "torso_data"
        elif any(a["t0"] < t1[i] and t0[i] < a["t1"] for a in acc):
            reason = "overlap"
        elif kind[i] == "push" and any(
                a["kind"] == "rot" and a["cand"] == cand[i]
                and a["t0"] < t1[i] + max(RETURN_MIN_MS, dur[i]) and t0[i] - max(RETURN_MIN_MS, dur[i]) < a["t1"]
                for a in acc):
            reason = "lead_in"
        elif kind[i] == "rot" and is_return(last_rot.get(sess[i]), U[i]):
            reason = "return"
        elif (kind[i] == "rot" and near[i] == "uppercut"
              and rep.peak.iat[i] >= UPPERCUT_TORSO_GATE_PEAK_DPS
              and rep.torso_ratio.iat[i] < UPPERCUT_MIN_TORSO_RATIO):
            reason = "torso_quiet"
        elif kind[i] == "push" and any(
                a["kind"] == "push" and 0 <= t0[i] - a["t1"] <= THRUST_REPEAT_GUARD_MS for a in acc):
            reason = "return"
        elif kind[i] == "push" and is_return(max((a for a in acc if a["t1"] <= t0[i]), default=None,
                                                 key=lambda a: a["t1"]), lin_dir[i]):
            reason = "return"
        elif kind[i] == "push" and rep.reach.iat[i] < thrust_min_reach:
            reason = "short"
        elif not ok_all[i]:
            reason = "far"
        else:
            reason = "ok"
            rec = {"t0": t0[i], "t1": t1[i], "dur": dur[i], "kind": kind[i], "cand": cand[i],
                   "spin": near[i] in raw.BIDIRECTIONAL, "v": lin_dir[i]}
            if kind[i] == "rot":
                last_rot[sess[i]] = {**rec, "v": U[i]}
            accepted.setdefault(sess[i], []).append(rec)
        reasons.append(reason)
    rep["reason"] = reasons
    rep["accept"] = rep.reason == "ok"
    return rep


# -------------------------------------------------------------- evaluation

def score_table(dec, table):
    """Phase-2 table strokes marked accepted if an accepted replay stroke
    overlaps them. Also returns accepted replay strokes that overlap nothing."""
    tbl = table.copy()
    hit_all = np.zeros(len(tbl), bool)
    extra = 0
    sess = tbl.session.to_numpy()
    for s, g in dec[dec.accept].groupby("session"):
        m = sess == s
        a0, a1 = tbl.t0.to_numpy(float)[m], tbl.t1.to_numpy(float)[m]
        ov = (np.minimum(a1[:, None], g.t1.to_numpy(float)[None, :])
              - np.maximum(a0[:, None], g.t0.to_numpy(float)[None, :])) > 0
        extra += int((~ov.any(0)).sum())
        hit_all[m] = ov.any(1)
    tbl["new_accept"] = hit_all
    return tbl, extra


def rates(tbl, col):
    ok = ~tbl.has_gap
    st = tbl[ok & (tbl.role == "strike")]
    fake = tbl[ok & (tbl.cls == "fake")]
    between = tbl[ok & (tbl.cls == "real") & (tbl.role != "strike")]
    return {"strike": float(st[col].mean()) if len(st) else np.nan,
            "fake": float(fake[col].mean()) if len(fake) else np.nan,
            "between": float(between[col].mean()) if len(between) else np.nan}


def tune_max_dist(rep_train, table_train, model, grid=np.arange(1.0, 8.01, 0.25)):
    """Limit maximising strike TPR - FPR (fakes + between-rep strokes) on the
    training sessions. Ties go to the larger limit (more forgiving)."""
    best, best_v = grid[0], -np.inf
    rep_train = rep_train[rep_train.kind == "rot"] if "kind" in rep_train else rep_train
    table_train = table_train[table_train.label != THRUST_ACTION]  # thrust is scored by its push
    dist = distances(rep_train, model)
    for d in grid:
        tbl, _ = score_table(decide(rep_train, model, d, dist=dist), table_train)
        r = rates(tbl, "new_accept")
        neg = tbl[~tbl.has_gap & ((tbl.cls == "fake") | ((tbl.cls == "real") & (tbl.role != "strike")))]
        v = r["strike"] - float(neg.new_accept.mean())
        if v >= best_v - 1e-9:
            best, best_v = d, v
    return float(best)


def strikes_for_fit(rep):
    """Rotation strikes for fit_templates(). Thrust is matched by its push
    (fit_thrust), so it has no rotation template."""
    ok = (rep.role == "strike") & ~rep.has_gap.astype(bool) & (rep.label != THRUST_ACTION)
    return rep[ok & (rep.kind == "rot")] if "kind" in rep else rep[ok]


def thrusts_for_fit(rep, min_reach=THRUST_MIN_REACH_M):
    # Candidate boundaries can split the return into its own candidate; the
    # biggest forward-looking push in it may then be labelled a strike.
    return rep[(rep.kind == "push") & (rep.role == "strike") & ~rep.has_gap.astype(bool)
               & (rep.reach >= min_reach)]


def tune_thrust_dist(rep_train, model, max_dist, thrust, grid=np.arange(1.0, 5.01, 0.25),
                     min_reach=THRUST_MIN_REACH_M):
    """Thrust limit minimising missed thrusts + false thrusts (counts, not
    rates: the hundreds of pushes that are nowhere near a thrust would
    otherwise hide the few that are). Run through decide(), so the overlap,
    lead-in and return rules apply as on the hub. False thrusts: accepted
    pushes that aren't a thrust strike. Ties go to the smaller limit, with a
    floor checked against held-out fast thrusts."""
    best, best_err = grid[0], np.inf
    for g in grid:
        d = decide(rep_train, model, max_dist, thrust=thrust, thrust_max=g,
                   thrust_min_reach=min_reach)
        p = d[(d.kind == "push") & ~d.has_gap.astype(bool)]
        thrusts = p.role == "strike"
        err = int((thrusts & ~p.accept).sum()) + int((~thrusts & p.accept).sum())
        if err < best_err:
            best, best_err = g, err
    return max(float(best), THRUST_LIMIT_FLOOR)


def mark_hits(dec):
    """Each stroke row also marked 'hit' if any accepted stroke of the same
    session overlaps it — what counts for scoring (the rotation and push
    detectors can see the same movement)."""
    dec = dec.copy()
    dec["hit"] = False
    for s, g in dec.groupby("session"):
        acc = g[g.accept]
        if not len(acc):
            continue
        ov = ((np.minimum(g.t1.to_numpy(float)[:, None], acc.t1.to_numpy(float)[None, :])
               - np.maximum(g.t0.to_numpy(float)[:, None], acc.t0.to_numpy(float)[None, :])) > 0).any(1)
        dec.loc[g.index, "hit"] = ov
    return dec


def outcome(dec):
    """Accept rates from mark_hits(): real strikes (rotation strikes + thrust
    pushes) per label x speed, and accepted strokes per fake session."""
    d = mark_hits(dec)
    ok = ~d.has_gap.astype(bool)
    strikes = d[ok & (d.role == "strike") & (((d.kind == "rot") & (d.label != THRUST_ACTION))
                                              | ((d.kind == "push") & (d.label == THRUST_ACTION)))]
    by_action = strikes.groupby(["label", "speed"], dropna=False).hit.agg(["size", "sum", "mean"])
    fakes = d[d.cls == "fake"]
    fake_acc = fakes.groupby("session").accept.agg(["size", "sum"]).rename(columns={"size": "strokes",
                                                                                     "sum": "accepted"})
    return by_action, fake_acc, d


# ------------------------------------------------- the model a sketch carries

_NUM = r"(-?\d+(?:\.\d+)?)f?"
_ROW_RE = re.compile(r'\{\s*WEAPON_(\w+)\s*,\s*"(\w+)"\s*,\s*' + r"\s*,\s*".join([_NUM] * 12)
                     + r"\s*,\s*(true|false)\s*\}")
_LIMIT_RE = re.compile(r"const float STRIKE_MAX_DIST = " + _NUM)
_THRUST_RE = re.compile(r"const ThrustTemplate THRUST_MODEL = \{" + r"\s*,\s*".join([r"\s*" + _NUM] * 8) + r"\s*\}")
_THRUST_LIMIT_RE = re.compile(r"const float THRUST_MAX_DIST = " + _NUM)
_WEAPON_LIMIT_RE = re.compile(r"\{\s*WEAPON_(SWORD_SHIELD|SPEAR|MACE)\s*,\s*" + _NUM +
                              r"\s*,\s*" + _NUM + r"(?:\s*,\s*" + _NUM + r")?\s*\}")
_WEAPON_THRUST_RE = re.compile(
    r"\{\s*WEAPON_(SWORD_SHIELD|SPEAR|MACE)\s*,\s*\{\s*" +
    r"\s*,\s*".join([_NUM] * 8) + r"\s*\}\s*\}")
MODEL_COLS = ["ux", "uy", "uz", "axis_spread", "px", "py", "pz", "push_spread", "rot_mean", "rot_spread",
              "torso_ref", "torso_spread"]
THRUST_COLS = ["ux", "uy", "uz", "dir_spread", "reach_mean", "reach_spread", "rot_ref", "rot_spread"]


def model_from_sketch(path, weapon="sword_shield"):
    """(model, limit, thrust, thrust_limit) as flashed, parsed out of a hub
    sketch: the STRIKE_MODEL rows (fit_templates() layout) and THRUST_MODEL
    (fit_thrust() layout; None if the sketch has none)."""
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    rows = []
    for m in _ROW_RE.finditer(src):
        vals = [float(v) for v in m.groups()[2:14]]
        rows.append({"weapon": m.group(1).lower(), "action": m.group(2), **dict(zip(MODEL_COLS, vals)),
                     "bidirectional": m.group(15) == "true"})
    rows = [r for r in rows if r["weapon"] in (weapon, "unknown")]
    limits = {m.group(1).lower(): (float(m.group(2)), float(m.group(3)))
              for m in _WEAPON_LIMIT_RE.finditer(src)}
    lim = _LIMIT_RE.search(src)
    if not rows or (weapon not in limits and not lim):
        raise ValueError(f"{path}: no action model for {weapon}")
    tm, tl = _THRUST_RE.search(src), _THRUST_LIMIT_RE.search(src)
    thrusts = {m.group(1).lower(): {"action": THRUST_ACTION,
               **dict(zip(THRUST_COLS, (float(v) for v in m.groups()[1:])))}
               for m in _WEAPON_THRUST_RE.finditer(src)}
    thrust = thrusts.get(weapon) or ({"action": THRUST_ACTION,
              **dict(zip(THRUST_COLS, (float(v) for v in tm.groups())))} if tm else None)
    strike_limit, thrust_limit = limits.get(weapon, (float(lim.group(1)) if lim else STRIKE_MAX_DIST,
                                                float(tl.group(1)) if tl else THRUST_MAX_DIST))
    return pd.DataFrame(rows), strike_limit, thrust, thrust_limit


def reach_limit_from_sketch(path, weapon):
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    for m in _WEAPON_LIMIT_RE.finditer(src):
        if m.group(1).lower() == weapon:
            return float(m.group(4)) if m.group(4) else THRUST_MIN_REACH_M
    return THRUST_MIN_REACH_M


def compare_live(dec, fw_strokes):
    """Replay decisions vs the firmware's own Stroke lines for the same raw
    stream: one row per replay stroke with the overlapping firmware stroke."""
    rows = []
    for s, g in dec.groupby("session"):
        fw = fw_strokes.get(s)
        if fw is None:
            continue
        for _, r in g.iterrows():
            same = fw[fw.kind == r.kind] if "kind" in fw and "kind" in r else fw
            f0, f1 = same.t0.to_numpy(float), same.t1.to_numpy(float)
            ov = np.minimum(f1, r.t1) - np.maximum(f0, r.t0)
            k = int(np.argmax(ov)) if len(ov) else -1
            hit = k >= 0 and ov[k] > 0
            rows.append({"session": s, "t0": r.t0, "t1": r.t1, "replay_accept": bool(r.accept),
                         "replay_reason": r.reason, "replay_dist": r.dist,
                         "fw_found": hit, "fw_accept": bool(same.accept.iat[k]) if hit else False,
                         "fw_reason": same.reason.iat[k] if hit else None,
                         "fw_dist": float(same.dist.iat[k]) if hit else np.nan})
    return pd.DataFrame(rows)
