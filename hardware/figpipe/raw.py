"""Raw IMU stream (RAW_STREAM builds) -> strokes -> speed-invariance features.

Reads logs/pN/raw_<time>_<label>.csv written by "2 players/listener_2p.py"
(one row per sample, hub + torso) next to the debug_<time>_<label>.csv of
the same session (the firmware's own Motion ENDED decisions).

A stroke is one coherent rotation: the contiguous run of samples around a
gyro peak where rotation about that peak's own axis stays above
STROKE_FRAC of the peak. Every threshold here is relative to the stroke's
own peak, so finding strokes doesn't depend on speed or on the firmware
segmenter under study. A real rep is a strike stroke followed by a return
stroke about roughly the opposite axis. The strike axis of each action is
taken from its fast session (mounting is fixed, so it holds at every speed).

Pipeline:
  load_sessions()   raw hub + torso per session; label/speed/class from the name
  find_strokes()    strokes on the hub
  strike_axes()     per action, the strike rotation axis (from the fast session)
  match_firmware()  what the firmware decided for each stroke (old Motion ENDED
                    lines; match_firmware_strokes() for the new Stroke lines)
  stroke_features() magnitude features the firmware gates on today, plus
                    speed-invariant candidates
  build_table()     all of the above, one row per stroke
"""

import glob
import os
import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .logs import label_class, label_from_suffix, load, speed_from_suffix

RAW_RE = re.compile(r"^raw_(\d{8}_\d{6})(?:_(.+))?\.csv$")
# Typos in session labels -> the intended speed tag.
SPEED_ALIASES = {"nowmal": "normal"}

SAMPLE_MS = 10
GAP_MS = 25  # a sample step longer than this = a lost batch inside the stroke

# Stroke detection. Smoothing is for finding boundaries only; features use
# the raw samples.
SMOOTH_SAMPLES = 5
STROKE_FRAC = 0.15        # stroke = rotation about the peak axis > this x peak
STROKE_MIN_PEAK_DPS = 80  # absolute floor: above hand tremor (20-60, peaks ~75)
STROKE_MIN_ROT_DEG = 20   # drop tiny twitches
STROKE_MAX_HALF_MS = 3000
ROLE_COS = 0.5            # |cos(stroke axis, strike axis)| above this = strike / return
# Spins were done in both directions (~400 deg each way, too much for a
# return to guard), so both directions are strikes.
BIDIRECTIONAL = {"spin"}

REVERSAL_FRAC = 0.10      # reversal deadband, relative to own peak
CONTEXT_FRAC = 0.5        # windows for wind-up / torso: stroke +- this x its duration
PROFILE_POINTS = 50

# Current firmware gates (2 players/p1_right_wrist.ino), for the bias table.
FW_MIN_PEAK_GYRO_DPS = 180.0
FW_MIN_PEAK_JERK_G = 1.0
FW_PRE_PEAK_MS = 150
FW_POST_PEAK_MS = 350


@dataclass
class Session:
    name: str
    label: str
    speed: str
    cls: str
    hub: pd.DataFrame
    torso: pd.DataFrame
    lost_batches: dict
    weapon: str = None
    split: str = None
    path: str = None


def _speed(suffix):
    if not suffix:
        return None
    return speed_from_suffix("_".join(SPEED_ALIASES.get(p, p) for p in suffix.split("_")))


def _node_frame(df, node):
    g = df[df.node == node].drop_duplicates("t_ms").sort_values("t_ms").reset_index(drop=True)
    g = g.rename(columns={"t_ms": "t"})
    g["gyroMag"] = np.sqrt(g.gx ** 2 + g.gy ** 2 + g.gz ** 2)
    g["jerk"] = (np.sqrt(g.ax ** 2 + g.ay ** 2 + g.az ** 2) - 1.0).abs()
    g["rx"] = pd.to_datetime(g.rx_time)
    return g


def _lost(df, node):
    seq = df.loc[df.node == node, "seq"].drop_duplicates().to_numpy()
    gaps = (np.diff(seq) & 0xFFFF) - 1
    return int(gaps[gaps < 1000].sum())


def load_sessions(logs_dir):
    out = []
    for path in sorted(glob.glob(os.path.join(logs_dir, "**", "raw_*.csv"), recursive=True)):
        m = RAW_RE.match(os.path.basename(path))
        if not m:
            continue
        label = label_from_suffix(m.group(2))
        weapon, split = recording_metadata(m.group(2))
        df = pd.read_csv(path)
        out.append(Session(name=os.path.basename(path)[4:-4], label=label, speed=_speed(m.group(2)),
                           cls=label_class(label), hub=_node_frame(df, "right_wrist"),
                           torso=_node_frame(df, "torso"),
                           lost_batches={n: _lost(df, n) for n in ("right_wrist", "torso")},
                           weapon=weapon, split=split, path=path))
    return out


WEAPONS = ("sword_shield", "spear", "mace")
SPLITS = ("train", "val")


def recording_metadata(suffix):
    """Read weapon and optional split; weapon recordings default to training."""
    parts = f"_{suffix or ''}_"
    weapons = [w for w in WEAPONS if f"_{w}_" in parts]
    splits = [v for v in SPLITS if f"_{v}_" in parts]
    if len(weapons) > 1 or len(splits) > 1:
        raise ValueError(f"ambiguous recording label: {suffix}")
    weapon = weapons[0] if weapons else None
    return weapon, (splits[0] if splits else "train" if weapon else None)


def rx_offset_ms(node):
    """Median (rx epoch ms - node millis()). Each batch is sent right after its
    last sample, so node_t ~= rx_ms - this (+- WiFi latency)."""
    last = node.groupby("seq").agg(t=("t", "max"), rx=("rx", "first"))
    return float(np.median((last.rx.astype("int64") // 10 ** 6) - last.t))


# ---------------------------------------------------------------- strokes

def smooth_gyro(G):
    """SMOOTH_SAMPLES-point moving average per axis, zero beyond the ends
    (np.convolve 'same'). findStrokes() in the hub sketch does the same."""
    k = np.ones(SMOOTH_SAMPLES) / SMOOTH_SAMPLES
    return np.column_stack([np.convolve(G[:, i], k, mode="same") for i in range(3)])


def _smoothed_gyro(hub):
    return smooth_gyro(hub[["gx", "gy", "gz"]].to_numpy())


def find_strokes_arrays(t, G, max_strokes=None):
    """Strokes in one sample window: [(a, b, ip, axis)], sorted by start.
    a..b is the inclusive sample range, ip the (smoothed) peak index, axis the
    unit rotation axis there. The biggest unclaimed peak goes first and claims
    its samples; a smaller peak inside a claimed range is part of that stroke.
    max_strokes caps how many peaks are processed (the hub's fixed array)."""
    t = np.asarray(t, float)
    Gs = smooth_gyro(np.asarray(G, float))
    gm = np.linalg.norm(Gs, axis=1)
    used = np.zeros(len(gm), bool)
    out = []
    while max_strokes is None or len(out) < max_strokes:
        free = np.where(used, -1.0, gm)
        ip = int(np.argmax(free)) if len(free) else 0
        if not len(free) or free[ip] < STROKE_MIN_PEAK_DPS:
            break
        u = Gs[ip] / gm[ip]
        w = Gs @ u
        a = b = ip
        lim = STROKE_FRAC * gm[ip]
        while a > 0 and w[a - 1] > lim and t[ip] - t[a - 1] < STROKE_MAX_HALF_MS:
            a -= 1
        while b < len(w) - 1 and w[b + 1] > lim and t[b + 1] - t[ip] < STROKE_MAX_HALF_MS:
            b += 1
        overlaps = used[a:b + 1].any()
        used[a:b + 1] = True
        if overlaps:
            continue
        rot = np.trapezoid(w[a:b + 1], t[a:b + 1] / 1000)
        if rot >= STROKE_MIN_ROT_DEG:
            out.append((a, b, ip, u))
    out.sort(key=lambda s: s[0])
    return out


def find_strokes(hub):
    return find_strokes_arrays(hub.t.to_numpy(float), hub[["gx", "gy", "gz"]].to_numpy())


def strike_axes(sessions, strokes_by_session):
    """Per real action: mean axis of the strokes aligned with the session's
    biggest stroke, from the fast session (else any session of the action)."""
    axes = {}
    for s in sorted(sessions, key=lambda s: s.speed != "fast"):
        key = (s.weapon, s.label)
        if s.cls != "real" or key in axes:
            continue
        st = strokes_by_session[s.name]
        if not st:
            continue
        gm = np.linalg.norm(_smoothed_gyro(s.hub), axis=1)
        seed = st[int(np.argmax([gm[ip] for _, _, ip, _ in st]))][3]
        U = np.array([u for *_, u in st])
        c = U[U @ seed > ROLE_COS].mean(0)
        axes[key] = c / np.linalg.norm(c)
    return axes


def stroke_role(sess, u, axes):
    key = (sess.weapon, sess.label)
    if sess.cls != "real" or key not in axes:
        return "fake" if sess.cls == "fake" else "other"
    c = float(u @ axes[key])
    if sess.label in BIDIRECTIONAL:
        c = abs(c)
    return "strike" if c > ROLE_COS else ("return" if c < -ROLE_COS else "other")


# ------------------------------------------------------- firmware decisions

def firmware_events(logs_dir):
    """Motion ENDED records with accept + every failing gate, by session name."""
    ev = load(logs_dir).events
    if ev is None or ev.empty:
        return {}
    ev = ev.copy()
    ev["accept"] = (ev.magOK.astype(bool) & ev.shapeOK.astype(bool) & ev.torsoOK.astype(bool)
                    & ev.rawDurationOK.astype(bool) & ev.swingShapeOK.astype(bool) & ev.spinReversalOK.astype(bool))
    ev["fails"] = ev.apply(_fails, axis=1)
    ev["session"] = ev.session.str.replace(r"^debug_", "", regex=True).str.replace(r"\.csv$", "", regex=True)
    return {s: g.reset_index(drop=True) for s, g in ev.groupby("session")}


def firmware_strokes(logs_dir):
    """Stroke lines from the stroke + template firmware, by session name.
    t0/t1 are hub millis(), the same clock as the raw stream."""
    lines = load(logs_dir).lines
    if lines is None or lines.empty or "kind" not in lines or not (lines.kind == "stroke").any():
        return {}
    st = lines[lines.kind == "stroke"].copy()
    st["kind"] = st.stroke_kind  # rot / push
    st["kind_logged"] = st.kind_logged.astype(bool)
    st["session"] = st.session.str.replace(r"^debug_", "", regex=True).str.replace(r"\.csv$", "", regex=True)
    st["accept"] = st.accept.astype(int).astype(bool)
    return {s: g.reset_index(drop=True) for s, g in st.groupby("session")}


def match_firmware_strokes(sess, strokes, fw):
    """Same output as match_firmware(), from Stroke lines: a table stroke is
    accepted if an accepted firmware stroke overlaps it."""
    t = sess.hub.t.to_numpy(float)
    out = []
    f0, f1 = fw.t0.to_numpy(float), fw.t1.to_numpy(float)
    for a, b, *_ in strokes:
        ov = (np.minimum(f1, t[b]) - np.maximum(f0, t[a])) > 0
        hit = fw[ov]
        acc = bool(hit.accept.any())
        out.append({"fw_candidate": bool(ov.any()), "fw_accept": acc,
                    "fw_fails": [] if acc else (sorted(set(hit.reason)) if len(hit) else ["no_candidate"]),
                    "fw_totalRot": float(hit.rot.max()) if len(hit) else np.nan, "fw_peakAccel": np.nan,
                    "fw_thrust": None})
    return out


def _fails(e):
    f = []
    if e["peak"] < FW_MIN_PEAK_GYRO_DPS:
        f.append("gyro<180")
    if e["peakAccel"] < FW_MIN_PEAK_JERK_G:
        f.append("jerk<1.0")
    if not e["magOK"] and not f:
        f.append("duration")
    if not e["shapeOK"]:
        f.append("pattern")
    if not e["torsoDataOK"]:
        f.append("torso_data")
    elif not e["torsoEngagementOK"]:
        f.append("torso<10")
    elif not e["torsoCeilingOK"]:
        f.append("torso_ceiling")
    if not e["rawDurationOK"]:
        f.append("raw_duration")
    if not e["swingShapeOK"]:
        f.append("swing_shape")
    if not e["spinReversalOK"]:
        f.append("spin_reversal")
    return f


def match_firmware(sess, strokes, events):
    """Per stroke: the firmware candidate whose TRIMMED window overlaps it most
    (that window is what the gates scored). Candidate close time comes from the
    Motion ENDED line's rx_time mapped onto the hub clock."""
    blank = {"fw_candidate": False, "fw_accept": False, "fw_fails": ["no_candidate"],
             "fw_totalRot": np.nan, "fw_peakAccel": np.nan, "fw_thrust": None}
    out = [dict(blank) for _ in strokes]
    if events is None or not len(events) or not strokes:
        return out
    t = sess.hub.t.to_numpy()
    off = rx_offset_ms(sess.hub)
    a_t = np.array([t[a] for a, *_ in strokes], float)
    b_t = np.array([t[b] for _, b, *_ in strokes], float)
    best = {}
    for _, e in events.iterrows():
        close = e["rx_time"].value // 10 ** 6 - off
        raw_start = close - e["rawDur"]
        lo = raw_start + e["trimStart"] * SAMPLE_MS
        hi = raw_start + e["trimEnd"] * SAMPLE_MS
        ov = np.minimum(b_t, hi) - np.maximum(a_t, lo)
        k = int(np.argmax(ov))
        if ov[k] > 0 and ov[k] > best.get(k, (0, None))[0]:
            best[k] = (ov[k], e)
    for k, (_, e) in best.items():
        out[k] = {"fw_candidate": True, "fw_accept": bool(e["accept"]), "fw_fails": list(e["fails"]),
                  "fw_totalRot": e["totalRot"], "fw_peakAccel": e["peakAccel"], "fw_thrust": bool(e["thrustLikely"])}
    return out


# ---------------------------------------------------------------- features

def _resample(x, t, n=PROFILE_POINTS):
    u = (t - t[0]) / max(t[-1] - t[0], 1e-9)
    return np.interp(np.linspace(0, 1, n), u, x)


def stroke_features(sess, a, b, ip_s, u, torso_off_ms):
    hub = sess.hub
    t_all = hub.t.to_numpy(float)
    G_all = hub[["gx", "gy", "gz"]].to_numpy()
    h = slice(a, b + 1)
    t = t_all[h]
    ts = t / 1000.0
    G = G_all[h]
    gm = hub.gyroMag.to_numpy()[h]
    jerk = hub.jerk.to_numpy()[h]
    ip = int(np.argmax(gm))
    peak = gm[ip]
    w = G @ u                      # rotation about the stroke's axis (raw samples)
    dur = t[-1] - t[0]

    rot = np.trapezoid(w, ts)
    vec = np.array([np.trapezoid(G[:, k], ts) for k in range(3)])
    path = np.trapezoid(gm, ts)

    # context window around the stroke for wind-up and reversals
    pad = CONTEXT_FRAC * max(dur, 100)
    ctx = (t_all >= t[0] - pad) & (t_all <= t[-1] + pad)
    wc = G_all[ctx] @ u
    sign = np.where(wc > REVERSAL_FRAC * peak, 1, np.where(wc < -REVERSAL_FRAC * peak, -1, 0))
    nz = sign[sign != 0]
    reversals = int(np.sum(nz[1:] != nz[:-1])) if len(nz) > 1 else 0
    pre = G_all[(t_all >= t[0] - pad) & (t_all < t[0])] @ u
    windup = float(max(0.0, -pre.min()) / peak) if len(pre) else 0.0

    sm = np.convolve(gm, np.ones(3) / 3, mode="same") / peak
    n_peaks = int(((sm[1:-1] > sm[:-2]) & (sm[1:-1] >= sm[2:]) & (sm[1:-1] > 0.5)).sum()) if len(sm) > 2 else 1

    # half-peak width of the stroke as a share of its duration
    above = np.flatnonzero(gm > 0.5 * peak)
    sustain = (t[above[-1]] - t[above[0]]) / dur if dur > 0 and len(above) else np.nan

    # centripetal accel = w^2 r, so jerk / w^2 is an effective radius that
    # shouldn't change with speed: arm-length for a swing, small for a flick
    omega = np.deg2rad(peak)
    r_eff = jerk.max() * 9.81 / max(omega ** 2, 1e-9)

    # firmware's fixed trim centred on the peak: share of stroke rotation it keeps
    tw = (t >= t[ip] - FW_PRE_PEAK_MS) & (t <= t[ip] + FW_POST_PEAK_MS)
    trim_cov = float(np.trapezoid(np.where(tw, w, 0.0), ts) / rot) if rot > 0 else np.nan

    tor = sess.torso
    if tor is not None and len(tor) > 2:
        tt = tor.t.to_numpy(float) + torso_off_ms
        m = (tt >= t[0] - pad) & (tt <= t[-1] + pad)
        if m.sum() > 2:
            tgm = tor.gyroMag.to_numpy()[m]
            torso_path = np.trapezoid(tgm, tt[m] / 1000)
            torso_peak = tgm.max()
            torso_lag = (tt[m][int(np.argmax(tgm))] - t[ip]) / max(dur, 1)
        else:
            torso_path = torso_peak = torso_lag = np.nan
    else:
        torso_path = torso_peak = torso_lag = np.nan

    return {
        # what the current gates measure (absolute magnitudes)
        "peak_gyro": peak, "peak_jerk": float(jerk.max()), "stroke_ms": dur, "torso_peak": torso_peak,
        # speed-invariant candidates
        "rot_deg": rot,
        "straightness": float(np.linalg.norm(vec) / path) if path > 0 else np.nan,
        "peak_pos": (t[ip] - t[0]) / dur if dur > 0 else np.nan,
        "sustain": sustain,
        "reversals_rel": reversals,
        "n_peaks": n_peaks,
        "windup": windup,
        "r_eff": r_eff,
        "jerk_per_rot": float(np.trapezoid(jerk, ts) / max(path, 1e-9) * 100),
        "torso_rot": torso_path,
        "torso_ratio": torso_path / path if path > 0 else np.nan,
        "torso_lag": torso_lag,
        "trim_cov": trim_cov,
        "axis_x": u[0], "axis_y": u[1], "axis_z": u[2],
        "has_gap": bool((np.diff(t) > GAP_MS).any()),
        "_profile": _resample(w / peak, ts),
        "_jerk_profile": _resample(jerk / max(jerk.max(), 1e-9), ts),
    }


def build_table(logs_dir, sessions=None, axes_sessions=None):
    """One row per stroke across all raw sessions."""
    sessions = load_sessions(logs_dir) if sessions is None else sessions
    strokes = {s.name: find_strokes(s.hub) for s in sessions}
    axes = strike_axes(sessions if axes_sessions is None else axes_sessions, strokes)
    fw = firmware_events(logs_dir)       # old firmware: Motion ENDED lines
    fws = firmware_strokes(logs_dir)     # stroke + template firmware: Stroke lines
    rows = []
    for s in sessions:
        st = strokes[s.name]
        dec = (match_firmware_strokes(s, st, fws[s.name]) if s.name in fws
               else match_firmware(s, st, fw.get(s.name)))
        toff = (rx_offset_ms(s.torso) - rx_offset_ms(s.hub)) if len(s.torso) else 0.0
        for k, ((a, b, ip, u), d) in enumerate(zip(st, dec)):
            rows.append({"session": s.name, "label": s.label, "speed": s.speed, "cls": s.cls,
                         "weapon": s.weapon,
                         "role": stroke_role(s, u, axes), "stroke": k,
                         "t0": int(s.hub.t.iloc[a]), "t1": int(s.hub.t.iloc[b]),
                         **d, **stroke_features(s, a, b, ip, u, toff)})
    return pd.DataFrame(rows), sessions, axes
