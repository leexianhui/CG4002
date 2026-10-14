"""Figures A-H. Each fig_* takes a Context and returns a FigResult; when the
data it needs isn't in the logs it returns status='skipped' and draws nothing.
Diagrams (I) live in diagrams.py.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from . import style as S
from .firmware import (el_az_from_accel, grav_angle_deg, grav_from_el_az, pitch_roll_from_accel,
                       weapon_distance)
from .gates import GATES
from .logs import ACTION_LABELS, FAKE_ACTIONS, HOLD_LABELS, REAL_ACTIONS, label_class

# The gyro range before commit 1623916's successors: GYRO_SCALE = 131.0 LSB/dps
# (+/-250 dps). Historical, not tunable, so it's pinned here with its source.
OLD_GYRO_SCALE_LSB_PER_DPS = 131.0
OLD_GYRO_TAG = "gyro250"  # listener --label containing this marks a +/-250 dps capture


@dataclass
class FigResult:
    fid: str
    stem: str
    title: str
    status: str = "skipped"          # made | skipped
    files: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    takeaway: str = ""
    caveats: list = field(default_factory=list)
    why_skipped: str = ""


@dataclass
class Context:
    fw: object
    ds: object
    ev: pd.DataFrame
    out: str


def _sources(df):
    return sorted(df["session"].unique()) if df is not None and len(df) else []


def _labelled_actions(ev):
    if ev is None or ev.empty:
        return ev
    return ev[ev["label"].isin(ACTION_LABELS)]


def _order(labels):
    return [lab for lab in ACTION_LABELS if lab in set(labels)]


def _fw_src(ctx, *nodes):
    return [ctx.fw.paths[n].replace("\\", "/") for n in nodes]


# ============================ A. Segmentation ============================

A_PANELS = [
    ("01a_segmentation_swing", "swing", ["vertical", "horizontal", "diagonal", "uppercut"]),
    ("01b_segmentation_spin", "spin", ["spin"]),
    ("01c_segmentation_thrust", "thrust", ["thrust"]),
]


def _dump_complete(e):
    """Every sample 0..raw-1 arrived and the '*' range matches trimmed=[a,b]."""
    s = e["seg"]
    idx = sorted(s["i"].astype(int))
    if idx != list(range(int(e["raw"]))):
        return False
    trim = sorted(s.loc[s["inTrim"].astype(bool), "i"].astype(int))
    return trim == list(range(int(e["trimStart"]), int(e["trimEnd"]) + 1))


def _pick_rep(cands):
    """Deterministic: among accepted reps (else all), the one with the median peak."""
    pool = cands[cands["accept"]] if cands["accept"].any() else cands
    pool = pool.sort_values("peak").reset_index(drop=True)
    return pool.iloc[(len(pool) - 1) // 2]


def fig_segmentation(ctx):
    fw, res = ctx.fw, []
    start, jerk_start = fw.rw("START_THRESHOLD"), fw.rw("JERK_START_THRESHOLD")
    settle, pre, post = fw.rw("SETTLE_FRACTION"), fw.rw("PRE_PEAK_MS"), fw.rw("POST_PEAK_MS")
    npre = fw.rw("PRETRIGGER_SAMPLES")
    ev = ctx.ev
    for stem, name, labels in A_PANELS:
        r = FigResult("A", stem, f"Segmentation walkthrough — {name}")
        res.append(r)
        if ev is None or ev.empty or "seg" not in ev:
            r.why_skipped = "no [seg] dumps (DEBUG_SEGMENT_DUMP off) in any labelled session"
            continue
        has = ev[ev["label"].isin(labels) & ev["seg"].map(lambda s: s is not None)]
        cands = has[has.apply(_dump_complete, axis=1)] if len(has) else has
        if cands.empty:
            r.why_skipped = (f"{len(has)} [seg] dump(s) for {'/'.join(labels)} but none complete (UDP lines lost)"
                             if len(has) else f"no [seg] dump for a rep labelled {'/'.join(labels)}")
            continue
        e = _pick_rep(cands)
        seg = e["seg"].sort_values("i")
        t = seg["t"].to_numpy(dtype=float)
        g = seg["gyroMag"].to_numpy() / start
        j = seg["jerk"].to_numpy() / jerk_start
        score = np.maximum(g, j)
        n_pre = min(npre, len(seg))
        t_open = float(e["startedMs"]) if pd.notna(e.get("startedMs")) else t[n_pre - 1]
        t0 = t_open
        x = t - t0
        ipk = int(np.argmax(score))
        trim = seg["inTrim"].to_numpy(dtype=bool)

        fig, ax = S.new_fig()
        fig.subplots_adjust(left=0.07, right=0.97, top=0.78, bottom=0.14)
        ax.axvspan(x[0], 0, color=S.NEUTRAL_FILL, zorder=0,
                   label=f"pretrigger ({n_pre} samples ≈ {-x[0]:.0f} ms)")
        if trim.any():
            ax.axvspan(x[trim][0], x[trim][-1], color="#cde2fb", alpha=0.6, zorder=0,
                       label=f"trimmed window (−{pre} / +{post} ms around peak)")
        ax.plot(x, g, color=S.BLUE, label=f"gyroMag ÷ START_THRESHOLD ({start:g} dps)")
        ax.plot(x, j, color=S.ORANGE, label=f"jerk ÷ JERK_START_THRESHOLD ({jerk_start:g} g)")
        S.hthreshold(ax, 1.0, "trigger = 1× start threshold", side="left")
        S.hthreshold(ax, settle * score[ipk], f"settle line = {settle:g} × peak", side="right")
        ax.plot([x[ipk]], [score[ipk]], "o", ms=13, color=S.INK, zorder=5)
        ax.annotate(f"peak {score[ipk]:.1f}×", (x[ipk], score[ipk]), xytext=(12, 4),
                    textcoords="offset points", fontsize=18, color=S.INK)
        S.vthreshold(ax, 0, "candidate opens", y=0.80)
        S.vthreshold(ax, x[-1], f"closes ({e.get('closeReason') or 'n/a'})", y=0.80, ha="right")
        ax.set_xlabel("Time relative to candidate opening (ms)")
        ax.set_ylabel("Activity (× its start threshold)")
        ax.set_ylim(0, max(score.max() * 1.18, 1.4))
        h, lab = ax.get_legend_handles_labels()
        order = [2, 3, 0, 1][:len(h)] if len(h) == 4 else range(len(h))
        ax.legend([h[i] for i in order], [lab[i] for i in order], loc="lower left", bbox_to_anchor=(0, 1.01),
                  ncol=2, fontsize=16)
        verdict = "accepted" if e["accept"] else "rejected"
        fig.suptitle(f"How one {e['label'].replace('_', ' ')} rep is segmented ({verdict})", x=0.012, y=0.975,
                     ha="left", fontsize=28, fontweight="bold", color=S.INK)
        S.footnote(fig, "Both signals are divided by their own start threshold, so one axis serves both. "
                        f"Source: {e['session']}")
        r.files = S.save(fig, ctx.out, stem)
        r.status = "made"
        r.sources = [e["session"]] + _fw_src(ctx, "right_wrist")
        r.counts = {f"{lab} reps with dump": int((cands["label"] == lab).sum()) for lab in labels
                    if (cands["label"] == lab).any()}
        r.takeaway = (f"The candidate stayed open {x[-1]:.0f} ms; the trimmed {x[trim][-1] - x[trim][0]:.0f} ms "
                      f"window around the peak is what the gates score.") if trim.any() else ""
        r.caveats = ["One representative rep (median peak among accepted reps), not an average.",
                     "Signals are the hub's logged [seg] values (gyroMag 1 dp, jerk 2 dp)."]
    return res


# ============================ B. Gate evidence ============================

def _strip(ax, df, col, order, *, log=False):
    rng = np.random.default_rng(0)  # fixed jitter so reruns are identical
    for i, lab in enumerate(order):
        vals = df.loc[df["label"] == lab, col].dropna().to_numpy(dtype=float)
        if not len(vals):
            continue
        color = S.CLASS_COLOR[label_class(lab)]
        ax.boxplot([vals], positions=[i], widths=0.55, showfliers=False, patch_artist=True,
                   boxprops=dict(facecolor="none", edgecolor=S.AXIS, linewidth=1.6),
                   medianprops=dict(color=S.INK_2, linewidth=2), whiskerprops=dict(color=S.AXIS, linewidth=1.6),
                   capprops=dict(color=S.AXIS, linewidth=1.6))
        ax.scatter(i + rng.uniform(-0.18, 0.18, len(vals)), vals, s=70, color=color, alpha=0.85,
                   edgecolors=S.SURFACE, linewidths=1.2, zorder=3)
    ax.set_xticks(range(len(order)))
    ax.set_xticklabels([f"{lab.replace('_', chr(10))}\n(n={int((df['label'] == lab).sum())})" for lab in order],
                       fontsize=15)
    ax.set_xlim(-0.6, len(order) - 0.4)
    if log:
        ax.set_yscale("log")
    # Legend sits above the plot, right-aligned on the title row, clear of the threshold lines.
    ax.legend(handles=[Patch(color=S.BLUE, label="real action"), Patch(color=S.ORANGE, label="fake / guard")],
              loc="lower right", bbox_to_anchor=(1, 1.0), ncol=2)


def _gate_fig(ctx, stem, title, df, col, ylabel, lines, *, log=False, note=""):
    r = FigResult("B", stem, title)
    if df is None or df.empty:
        r.why_skipped = "no labelled Motion ENDED events"
        return r
    order = _order(df["label"])
    fig, ax = S.new_fig()
    fig.subplots_adjust(left=0.08, right=0.97, top=0.86, bottom=0.22)
    _strip(ax, df, col, order, log=log)
    for y, lab, side in lines:
        S.hthreshold(ax, y, lab, side=side)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    if note:
        S.footnote(fig, note)
    r.files = S.save(fig, ctx.out, stem)
    r.status = "made"
    r.sources = _sources(df) + _fw_src(ctx, "right_wrist")
    r.counts = {lab: int((df["label"] == lab).sum()) for lab in order}
    classes = {label_class(lab) for lab in order}
    if classes != {"real", "fake"}:
        r.caveats.append(f"Only {'/'.join(sorted(classes))} reps so far — no real-vs-fake contrast yet.")
    return r


def _pct(mask):
    return 100.0 * mask.mean() if len(mask) else float("nan")


def fig_gates(ctx):
    fw, ev = ctx.fw, _labelled_actions(ctx.ev)
    res = []
    lo, hi_n, hi_s = fw.rw("MIN_TORSO_ENGAGEMENT"), fw.rw("TORSO_GATE_MAX_NORMAL"), fw.rw("TORSO_GATE_MAX_SPIN")
    r = _gate_fig(ctx, "02a_gate_torso_peak", "Torso gate: real strikes engage the body, flicks don't",
                  ev, "torsoPeak", "Torso peak rotation in trimmed window (dps)",
                  [(lo, f"MIN_TORSO_ENGAGEMENT {lo:g}", "right"),
                   (hi_n, f"TORSO_GATE_MAX_NORMAL {hi_n:g}", "right"),
                   (hi_s, f"TORSO_GATE_MAX_SPIN {hi_s:g}", "right")],
                  note="Floor is waived for spin-likely reps; spin-likely reps use the SPIN ceiling.")
    if r.status == "made":
        real, fake = ev[ev.label.isin(REAL_ACTIONS)], ev[ev.label.isin(FAKE_ACTIONS)]
        r.takeaway = (f"{_pct(real.torsoPeak >= lo):.0f}% of real reps clear the {lo:g} dps floor vs "
                      f"{_pct(fake.torsoPeak >= lo):.0f}% of fakes.") if len(real) and len(fake) else ""
    res.append(r)

    th = fw.rw("THRUST_MAX_ROTATION_DEG")
    r = _gate_fig(ctx, "02b_gate_total_rotation", "Trimmed rotation picks the shape rule (thrust vs swing)",
                  ev, "totalRot", "Integrated rotation in trimmed window (°)",
                  [(th, f"THRUST_MAX_ROTATION_DEG {th:g}", "right")], log=True,
                  note="Below the line a rep is thrust-likely and skips the swing-shape gate. Log scale.")
    if r.status == "made":
        thr, sw = ev[ev.label == "thrust"], ev[ev.label.isin(["vertical", "horizontal", "diagonal", "uppercut"])]
        parts = []
        if len(thr):
            parts.append(f"real thrusts max {thr.totalRot.max():.1f}°")
        if len(sw):
            parts.append(f"swings min {sw.totalRot.min():.1f}°")
        r.takeaway = (", ".join(parts) + f" (threshold {th:g}°).") if parts else ""
    res.append(r)

    cap = fw.rw("RAW_MAX_DURATION_MS")
    r = _gate_fig(ctx, "02c_gate_raw_duration", "Raw duration cap catches sustained handling",
                  ev, "rawDur", "Raw candidate duration (ms)",
                  [(cap, f"RAW_MAX_DURATION_MS {cap:g}", "right")])
    if r.status == "made":
        real, fake = ev[ev.label.isin(REAL_ACTIONS)], ev[ev.label.isin(FAKE_ACTIONS)]
        r.takeaway = (f"Longest real rep {real.rawDur.max():.0f} ms; {_pct(fake.rawDur > cap):.0f}% of fakes "
                      f"exceed the {cap:g} ms cap.") if len(real) and len(fake) else ""
        r.caveats.append("The firmware also force-closes a candidate at this same duration, so values pile up at "
                         "the cap instead of running past it.")
    res.append(r)

    res.append(_fig_swing_scatter(ctx, ev))
    res.append(_fig_spin_reversals(ctx, ev))
    return res


def _fig_swing_scatter(ctx, ev):
    fw = ctx.fw
    r = FigResult("B", "02d_gate_swing_shape", "Swing-shape gate: one coherent arc vs back-and-forth")
    df = ev[ev["bucket"] == "swing"] if ev is not None and len(ev) else ev
    if df is None or df.empty:
        r.why_skipped = "no labelled swing-bucket events (not spin-likely, not thrust-likely)"
        return r
    rmax, smin = fw.rw("SWING_MAX_REVERSALS"), fw.rw("SWING_MIN_SUSTAIN_FRACTION")
    fig, ax = S.new_fig()
    fig.subplots_adjust(left=0.08, right=0.97, top=0.88, bottom=0.12)
    xmax = max(df.reversals.max() + 1, rmax + 2)
    ax.fill_between([-0.5, rmax + 0.5], smin, 1.02, color="#cde2fb", alpha=0.6, zorder=0, linewidth=0)
    ax.text(-0.4, 1.0, "accept region", va="top", color=S.INK_2, fontsize=17)
    rng = np.random.default_rng(0)
    for cls, color in S.CLASS_COLOR.items():
        d = df[df.label.map(label_class) == cls]
        ax.scatter(d.reversals + rng.uniform(-0.2, 0.2, len(d)), d.sustainFrac, s=90, color=color, alpha=0.85,
                   edgecolors=S.SURFACE, linewidths=1.2, zorder=3,
                   label=f"{'real action' if cls == 'real' else 'fake / guard'} (n={len(d)})")
    S.vthreshold(ax, rmax + 0.5, f"SWING_MAX_REVERSALS {rmax}", y=0.6)
    S.hthreshold(ax, smin, f"SWING_MIN_SUSTAIN_FRACTION {smin:g}", side="right", va="top")
    ax.set_xlim(-0.5, xmax)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("Direction reversals on dominant gyro axis (jittered)")
    ax.set_ylabel("Sustain fraction (samples > ½ peak)")
    ax.set_title(r.title)
    ax.legend(loc="upper right")
    S.footnote(fig, "Only swing-bucket reps shown: spin-likely and thrust-likely reps bypass this gate.")
    r.files = S.save(fig, ctx.out, r.stem)
    r.status = "made"
    r.sources = _sources(df) + _fw_src(ctx, "right_wrist")
    r.counts = {lab: int((df.label == lab).sum()) for lab in _order(df.label)}
    real, fake = df[df.label.isin(REAL_ACTIONS)], df[df.label.isin(FAKE_ACTIONS)]
    inside = lambda d: (d.reversals <= rmax) & (d.sustainFrac >= smin)  # noqa: E731
    if len(real) and len(fake):
        r.takeaway = (f"{_pct(inside(real)):.0f}% of real swing-bucket reps fall inside the accept region vs "
                      f"{_pct(inside(fake)):.0f}% of fakes.")
    return r


def _fig_spin_reversals(ctx, ev):
    r = FigResult("B", "02e_gate_spin_reversals", "Spin gate: the closing swing of a spin barely reverses")
    df = ev[ev["spin_bucket"]] if ev is not None and len(ev) else ev
    if df is None or df.empty:
        r.why_skipped = "no labelled spin-likely events (rawTotalRot above the spin threshold)"
        return r
    spin_deg = ctx.fw.rw("SPIN_RAW_ROTATION_DEG")
    rmax = ctx.fw.rw("SPIN_MAX_REVERSALS")
    r = _gate_fig(ctx, r.stem, r.title, df, "reversals", "Direction reversals (trimmed window)",
                  [(rmax + 0.5, f"SPIN_MAX_REVERSALS {rmax}", "right")],
                  note=f"Spin-likely = raw rotation > {spin_deg:g}° (literal in onMotionEnd()).")
    if r.status == "made":
        real, fake = df[df.label == "spin"], df[df.label.isin(FAKE_ACTIONS)]
        if len(real):
            r.takeaway = f"{_pct(real.reversals <= rmax):.0f}% of real spins have ≤ {rmax} reversal(s)"
            r.takeaway += f" vs {_pct(fake.reversals <= rmax):.0f}% of spin-likely fakes." if len(fake) else "."
        r.caveats.append("Reversals here are counted over the trimmed window, as logged.")
    return r


# ============================ C. Outcome table ============================

def fig_outcomes(ctx):
    r = FigResult("C", "03_outcomes_by_label", "Which gate stops which action")
    ev = _labelled_actions(ctx.ev)
    if ev is None or ev.empty:
        r.why_skipped = "no labelled Motion ENDED events"
        return r
    order = _order(ev.label)
    gate_keys = [k for k, _ in GATES]
    fail = np.array([[100.0 * (~ev.loc[ev.label == lab, k].astype(bool)).mean() for k in gate_keys]
                     for lab in order])
    acc = [100.0 * ev.loc[ev.label == lab, "accept"].mean() for lab in order]
    ns = [int((ev.label == lab).sum()) for lab in order]

    fig, (axb, axh) = S.new_fig(1, 2, gridspec_kw={"width_ratios": [1, 3.2]})
    fig.subplots_adjust(left=0.165, right=0.97, top=0.86, bottom=0.2, wspace=0.05)
    y = np.arange(len(order))
    axb.barh(y, acc, color=[S.CLASS_COLOR[label_class(lab)] for lab in order], height=0.62)
    for yi, a in zip(y, acc):
        axb.text(min(a + 3, 97), yi, f"{a:.0f}%", va="center", ha="left" if a < 80 else "right",
                 color=S.INK if a < 80 else S.SURFACE, fontsize=16)
    axb.set_yticks(y)
    axb.set_yticklabels([f"{lab.replace('_', ' ')} (n={n})" for lab, n in zip(order, ns)], fontsize=16)
    axb.set_xlim(0, 100)
    axb.invert_yaxis()
    axb.set_xlabel("Accepted (%)")
    axb.grid(axis="y", visible=False)
    axb.set_title("Accept rate", fontsize=22)

    cmap = LinearSegmentedColormap.from_list("seq", [S.SURFACE] + S.SEQ_BLUE[2:])
    axh.imshow(fail, cmap=cmap, vmin=0, vmax=100, aspect="auto")
    for i in range(len(order)):
        for k in range(len(gate_keys)):
            v = fail[i, k]
            axh.text(k, i, f"{v:.0f}", ha="center", va="center", fontsize=15,
                     color=S.SURFACE if v > 55 else (S.INK if v > 0 else S.MUTED))
    axh.set_xticks(range(len(gate_keys)))
    axh.set_xticklabels([lab.replace(" (", "\n(") for _, lab in GATES], rotation=30, ha="right", fontsize=14)
    axh.set_yticks([])
    axh.grid(False)
    for s in axh.spines.values():
        s.set_visible(False)
    axh.set_title("Reps failing each gate (%)", fontsize=22)
    fig.suptitle(r.title, x=0.012, ha="left", fontsize=28, fontweight="bold", color=S.INK)
    S.footnote(fig, "Every gate re-evaluated from the logged features with the CURRENT firmware constants "
                    "(a rep can fail several). Blue bars = real actions, orange = fakes.")
    r.files = S.save(fig, ctx.out, r.stem)
    r.status = "made"
    r.sources = _sources(ev) + _fw_src(ctx, "right_wrist")
    r.counts = dict(zip(order, ns))
    real, fake = ev[ev.label.isin(REAL_ACTIONS)], ev[ev.label.isin(FAKE_ACTIONS)]
    if len(real) and len(fake):
        r.takeaway = (f"Overall {_pct(real.accept):.0f}% of real reps accepted and {_pct(fake.accept):.0f}% of "
                      f"fakes let through.")
    drift = int((ev.drift.map(len) > 0).sum())
    if drift:
        r.caveats.append(f"{drift} rep(s) get a different gate verdict now than the firmware logged — either a "
                         "constant was retuned since capture, or a value sits on a threshold and the log's "
                         "rounding (peakAccel 2 dp, others 1 dp) flips it.")
    r.caveats.append("torsoDataOK can't be recomputed from the log, so the logged flag is used.")
    return r


# ============================ D. Stillness ============================

def fig_stillness(ctx):
    r = FigResult("D", "04_stillness_ema", "Stillness gate: hand tremor vs moving between stances")
    lines = ctx.ds.lines
    need = {"hub_pose": ("right_wrist", "rGyroMag"), "left_raw": ("left_wrist", "gyroMag"),
            "torso_pose": ("torso", "gyroMag")}
    cats = HOLD_LABELS + ["transition"]
    data = {}
    if len(lines):
        for kind, (node, col) in need.items():
            d = lines[(lines.kind == kind) & lines.label.isin(cats)]
            if len(d) and col in d:
                data[node] = d.assign(state=np.where(d.label == "transition", "transition", "hold"),
                                      ema=d[col].astype(float))
    if not data or not any((d.state == "transition").any() and (d.state == "hold").any() for d in data.values()):
        r.why_skipped = "no pose-stream lines labelled hold_<weapon> AND transition"
        return r
    nodes = [n for n in ("right_wrist", "left_wrist", "torso") if n in data]
    fig, axes = S.new_fig(1, len(nodes), sharey=True, squeeze=False)
    fig.subplots_adjust(left=0.08, right=0.98, top=0.83, bottom=0.14, wspace=0.08)
    thr = {n: ctx.fw.c(n, "STILLNESS_GYRO_THRESHOLD_DPS") for n in ("right_wrist", "left_wrist", "torso")}
    rng = np.random.default_rng(0)
    for ax, node in zip(axes[0], nodes):
        d = data[node]
        for i, st in enumerate(["hold", "transition"]):
            v = d.loc[d.state == st, "ema"].clip(lower=0.5).to_numpy()
            if len(v):
                ax.scatter(i + rng.uniform(-0.22, 0.22, len(v)), v, s=22, alpha=0.5,
                           color=S.BLUE if st == "hold" else S.ORANGE, linewidths=0)
                ax.hlines(np.median(v), i - 0.3, i + 0.3, color=S.INK, linewidth=3, zorder=4)
        ax.set_yscale("log")
        ax.set_xticks([0, 1])
        ax.set_xticklabels([f"stance hold\n(n={int((d.state == 'hold').sum())})",
                            f"transition\n(n={int((d.state == 'transition').sum())})"])
        ax.set_xlim(-0.6, 1.6)
        for val in sorted(set(thr.values())):
            owners = [n.replace("_", " ") for n, v in thr.items() if v == val]
            S.hthreshold(ax, val, f"{val:g} dps ({', '.join(owners)})", side="right",
                         color=S.INK if thr[node] == val else S.MUTED)
        ax.set_title(node.replace("_", " "), fontsize=22)
    axes[0][0].set_ylabel("gyroMag EMA (dps, log scale)")
    fig.suptitle(r.title, x=0.012, ha="left", fontsize=28, fontweight="bold", color=S.INK)
    S.footnote(fig, f"Black bar = median. Dark line = the threshold this node uses; grey = the other value "
                    f"in the firmware (wrists used 15 before). EMA α = {ctx.fw.rw('EMA_ALPHA'):g}.")
    r.files = S.save(fig, ctx.out, r.stem)
    r.status = "made"
    r.sources = sorted({s for d in data.values() for s in d.session.unique()}) + \
        _fw_src(ctx, "right_wrist", "left_wrist", "torso")
    r.counts = {f"{n} {st}": int((data[n].state == st).sum()) for n in nodes for st in ("hold", "transition")}
    rw = data.get("right_wrist")
    if rw is not None:
        h = rw[rw.state == "hold"].ema
        r.takeaway = (f"Right wrist during holds: {_pct(h <= thr['right_wrist']):.0f}% of pose samples under "
                      f"{thr['right_wrist']:g} dps, {_pct(h <= thr['torso']):.0f}% under {thr['torso']:g}.")
    r.caveats.append(f"Pose-stream samples every {ctx.fw.rw('POSE_LOG_INTERVAL_MS')} ms, so consecutive points "
                     "are correlated; n counts log lines, not independent holds.")
    return r


# ============================ E. Weapon detection ============================

def fig_weapon_separation(ctx):
    fw = ctx.fw
    refs = fw.weapon_refs
    r = FigResult("E", "05a_weapon_reference_separation", "Weapon references are far apart")
    n = len(refs)
    names = [f"{x['weapon'].replace('_', ' & ') if x['weapon'] == 'sword_shield' else x['weapon']}" for x in refs]
    if len({x["weapon"] for x in refs}) < n:  # several refs per weapon: number them
        names = [f"{nm} #{sum(1 for y in refs[:i] if y['weapon'] == x['weapon']) + 1}"
                 for i, (nm, x) in enumerate(zip(names, refs))]
    D = np.zeros((n, n))
    comp = {}
    for i, a in enumerate(refs):
        for k, b in enumerate(refs):
            d, dr, dl = weapon_distance(a["rEl"], a["rAz"], a["lEl"], a["lAz"], b)
            D[i, k] = d
            comp[(i, k)] = (dr, dl)
    match, margin = fw.rw("WEAPON_MATCH_MAX_DEG"), fw.rw("WEAPON_MATCH_MARGIN_DEG")

    fig, (axm, axb) = S.new_fig(1, 2, gridspec_kw={"width_ratios": [1, 1.25]})
    fig.subplots_adjust(left=0.12, right=0.97, top=0.82, bottom=0.14, wspace=0.38)
    cmap = LinearSegmentedColormap.from_list("seq", [S.SURFACE] + S.SEQ_BLUE[2:])
    axm.imshow(D, cmap=cmap, vmin=0, vmax=max(D.max(), 1))
    for i in range(n):
        for k in range(n):
            axm.text(k, i, "—" if i == k else f"{D[i, k]:.0f}°", ha="center", va="center", fontsize=22,
                     color=S.SURFACE if D[i, k] > 0.55 * D.max() else S.INK)
    axm.set_xticks(range(n))
    axm.set_yticks(range(n))
    axm.set_xticklabels(names, fontsize=17)
    axm.set_yticklabels(names, fontsize=17)
    axm.grid(False)
    for s in axm.spines.values():
        s.set_visible(False)
    axm.set_title("Pairwise distance", fontsize=22)

    pairs = [(i, k) for i in range(n) for k in range(i + 1, n) if refs[i]["weapon"] != refs[k]["weapon"]]
    pairs.sort(key=lambda p: D[p])
    y = np.arange(len(pairs))
    axb.barh(y, [D[p] for p in pairs], color=S.BLUE, height=0.55)
    for yi, p in zip(y, pairs):
        dr, dl = comp[p]
        axb.text(D[p] + 2, yi, f"{D[p]:.0f}°  (right {dr:.0f}°, left {dl:.0f}°)\n"
                               f"drift allowed toward the other: {max(D[p] - margin, 0) / 2:.0f}°",
                 va="center", fontsize=16, color=S.INK)
    axb.set_yticks(y)
    axb.set_yticklabels([f"{names[i]} ↔\n{names[k]}" for i, k in pairs], fontsize=16)
    axb.invert_yaxis()
    axb.set_xlim(0, D.max() * 2.15)
    S.vthreshold(axb, margin, f"WEAPON_MATCH_MARGIN_DEG = {margin:g}°", y=1.0)
    axb.set_xlabel("√(right angle² + left angle²), degrees")
    axb.grid(axis="y", visible=False)
    axb.set_title("Cross-weapon pairs", fontsize=22)
    fig.suptitle(r.title + " — acceptance zones can't overlap", x=0.012, ha="left", fontsize=28,
                 fontweight="bold", color=S.INK)
    S.footnote(fig, f"From WEAPON_REFS with classifyWeapon's distance. A pose matches if its nearest weapon is "
                    f"≤ {match:g}° away and ≥ {margin:g}° closer than any other. No logs needed.")
    r.files = S.save(fig, ctx.out, r.stem)
    r.status = "made"
    r.sources = _fw_src(ctx, "right_wrist")
    r.counts = {"references": n}
    if pairs:
        i, k = pairs[0]
        r.takeaway = (f"Closest cross-weapon pair is {names[i]} ↔ {names[k]} at {D[i, k]:.0f}°: a pose can drift "
                      f"{max(D[i, k] - margin, 0) / 2:.0f}° straight toward the other weapon and up to {match:g}° "
                      f"in any other direction, and can never match two weapons.")
    r.caveats = ["Distances between the stored references, not between measured poses; real holds scatter "
                 "a few degrees around each reference (see 05c)."]
    return r


def _attempts(hp, gap_s=2.0):
    """Splits hub pose lines into lock attempts: a >gap_s silence ends one (the
    pose stream stops at lock and restarts at reboot)."""
    hp = hp.sort_values(["session", "rx_time"])
    new = (hp.session != hp.session.shift()) | (hp.rx_time.diff().dt.total_seconds() > gap_s)
    return hp.assign(attempt=new.cumsum())


def fig_lock_attempt(ctx):
    fw = ctx.fw
    r = FigResult("E", "05b_weapon_lock_attempt", "Locking a weapon: distance to nearest reference")
    L = ctx.ds.lines
    hp = L[(L.kind == "hub_pose")] if len(L) else L
    if hp is None or hp.empty or "dist" not in hp or hp["dist"].isna().all():
        r.why_skipped = "no hub pose-stream lines in the el/az format (raw rEl=... nearest=... dist=...)"
        return r
    hp = _attempts(hp[hp["dist"].notna()])
    is_lock_result = (L["event"] == "weapon_locked") if "event" in L else False
    locks = L[(L.kind == "lock_ack") | ((L.kind == "result") & is_lock_result)]
    best, best_lock = None, None
    for _, a in hp.groupby("attempt"):
        end = a.rx_time.max()
        after = locks[(locks.rx_time >= end - pd.Timedelta(seconds=1)) & (locks.rx_time <= end + pd.Timedelta(seconds=2))]
        if len(after):
            best, best_lock = a, after.rx_time.min()  # latest attempt that locked wins (sorted order)
    if best is None:
        best = max((a for _, a in hp.groupby("attempt")), key=len)
    a = best
    t0 = a.rx_time.min()
    x = (a.rx_time - t0).dt.total_seconds().to_numpy()
    match, hold = fw.rw("WEAPON_MATCH_MAX_DEG"), fw.rw("WEAPON_LOCK_HOLD_MS") / 1000.0

    fig, ax = S.new_fig()
    fig.subplots_adjust(left=0.07, right=0.97, top=0.86, bottom=0.14)
    ax.plot(x, a["dist"], color=S.AXIS, linewidth=1.6, zorder=2)
    for w, color in S.WEAPON_COLOR.items():
        m = (a["nearest"] == w).to_numpy()
        if m.any():
            ax.scatter(x[m], a["dist"].to_numpy()[m], s=60, color=color, marker=S.WEAPON_MARKER[w], zorder=3,
                       edgecolors=S.SURFACE, linewidths=1, label=f"nearest = {w.replace('_', ' & ')}")
    held = a["candidate"].ne("unknown").to_numpy()
    if held.any():
        # contiguous runs of a held candidate
        edges = np.flatnonzero(np.diff(np.r_[0, held.astype(int), 0]))
        for s0, s1 in zip(edges[::2], edges[1::2]):
            ax.axvspan(x[s0], x[s1 - 1], color="#cde2fb", alpha=0.55, zorder=0, linewidth=0)
        last = a[held].iloc[-1]
        cand_start = (last.rx_time - pd.Timedelta(milliseconds=float(last.heldMs or 0)) - t0).total_seconds()
        lock_t = cand_start + hold
        ax.annotate("", xy=(lock_t, match * 1.6), xytext=(cand_start, match * 1.6),
                    arrowprops=dict(arrowstyle="<->", color=S.INK, lw=2))
        ax.text((cand_start + lock_t) / 2, match * 1.6, f" WEAPON_LOCK_HOLD_MS = {hold:g} s ", ha="center",
                va="bottom", fontsize=17, color=S.INK)
        if best_lock is not None:
            S.vthreshold(ax, (best_lock - t0).total_seconds(), f"locked: {last.candidate}", y=0.98, ha="right")
    S.hthreshold(ax, match, f"WEAPON_MATCH_MAX_DEG {match:g}°", side="right")
    ax.set_xlabel("Time since pose stream started (s)")
    ax.set_ylabel("Distance to nearest reference (°)")
    ax.set_ylim(0, min(max(a["dist"].max() * 1.08, match * 2.2), 180))
    ax.legend(loc="upper right", bbox_to_anchor=(1, 0.9))
    ax.set_title(r.title)
    S.footnote(fig, f"Shaded = a candidate weapon is held (all nodes fresh + stable, nearest weapon ≤ {match:g}° and "
                    f"≥ {fw.rw('WEAPON_MATCH_MARGIN_DEG'):g}° closer than the next). "
                    f"Hub pose stream, one point per {fw.rw('POSE_LOG_INTERVAL_MS')} ms.")
    r.files = S.save(fig, ctx.out, r.stem)
    r.status = "made"
    r.sources = [a.session.iloc[0]] + _fw_src(ctx, "right_wrist")
    r.counts = {"pose samples": len(a), "attempts in logs": int(hp.attempt.nunique())}
    r.takeaway = (f"Distance settles under {match:g}° and the lock fires after a continuous {hold:g} s hold."
                  if best_lock is not None else "This attempt did not end in a lock.")
    r.caveats.append("The pose stream is rate-limited, so short dropouts inside the grace period may not be visible.")
    return r


def fig_el_az(ctx):
    fw = ctx.fw
    r = FigResult("E", "05c_weapon_el_az_scatter", "Ready stances cluster around their references")
    L = ctx.ds.lines
    hp = L[(L.kind == "hub_pose") & L.label.isin(HOLD_LABELS)] if len(L) else L
    if hp is None or hp.empty or "rStable" not in hp:
        r.why_skipped = "no hub pose-stream lines labelled hold_<weapon>"
        return r
    # Only samples the firmware would classify (both wrists fresh + still) —
    # the rest is the arm moving into or out of the stance. Same filter as weapon_refs.py.
    hp = hp[(hp.rStable == 1) & (hp.l_fresh == 1) & (hp.l_stable == 1)]
    if hp.empty:
        r.why_skipped = "no stable (both wrists still) hub pose samples in the hold_<weapon> sessions"
        return r
    fig, axes = S.new_fig(1, 2)
    fig.subplots_adjust(left=0.07, right=0.97, top=0.83, bottom=0.13, wspace=0.2)
    for ax, (arm, el, az, rel, raz) in zip(axes, [("Right wrist (hub)", "rEl", "rAz", "rEl", "rAz"),
                                                  ("Left wrist", "l_el", "l_az", "lEl", "lAz")]):
        for w, color in S.WEAPON_COLOR.items():
            d = hp[hp.label == f"hold_{w}"]
            if len(d):
                ax.scatter(d[az], d[el], s=30, alpha=0.45, color=color, marker=S.WEAPON_MARKER[w], linewidths=0,
                           label=f"{w.replace('_', ' & ')} hold (n={len(d)})")
        for ref in fw.weapon_refs:
            w = ref["weapon"]
            ax.scatter([ref[raz]], [ref[rel]], s=380, color=S.WEAPON_COLOR.get(w, S.INK_2),
                       marker=S.WEAPON_MARKER.get(w, "o"), edgecolors=S.INK, linewidths=2.5, zorder=5)
        ax.margins(0.18)
        x0, x1 = ax.get_xlim()
        for ref in fw.weapon_refs:
            right_half = ref[raz] > (x0 + x1) / 2  # label toward the middle so it isn't clipped
            ax.annotate(f"{ref['weapon'].replace('_', ' & ')} ref", (ref[raz], ref[rel]),
                        xytext=(-14 if right_half else 14, 10), ha="right" if right_half else "left",
                        textcoords="offset points", fontsize=16, color=S.INK, zorder=6)
        ax.set_xlabel("Azimuth  atan2(y, x)  (°)")
        ax.set_ylabel("Elevation  asin(z)  (°)")
        ax.set_title(arm, fontsize=22)
    axes[0].legend(loc="lower left", fontsize=15)
    fig.suptitle(r.title, x=0.012, ha="left", fontsize=28, fontweight="bold", color=S.INK)
    S.footnote(fig, "Big outlined markers = WEAPON_REFS from the firmware. Small points = hub pose-stream samples "
                    "during labelled holds with both wrists still.")
    r.files = S.save(fig, ctx.out, r.stem)
    r.status = "made"
    r.sources = _sources(hp) + _fw_src(ctx, "right_wrist")
    r.counts = {lab: int((hp.label == lab).sum()) for lab in HOLD_LABELS if (hp.label == lab).any()}
    dists = []
    for _, row in hp.iterrows():
        w = row.label[len("hold_"):]
        own = [ref for ref in fw.weapon_refs if ref["weapon"] == w]
        if own and pd.notna(row.get("l_el")):
            dists.append(min(weapon_distance(row.rEl, row.rAz, row.l_el, row.l_az, ref)[0] for ref in own))
    if dists:
        r.takeaway = (f"Median distance from a labelled hold to its own reference: {np.median(dists):.1f}° "
                      f"(outer limit WEAPON_MATCH_MAX_DEG {fw.rw('WEAPON_MATCH_MAX_DEG'):g}°).")
    r.caveats.append("Azimuth wraps at ±180°; a stance near the wrap would appear split across both edges.")
    return r


# ============================ F. Sensor range ============================

def fig_peak_gyro(ctx):
    fw = ctx.fw
    r = FigResult("F", "06_peak_gyro_range", "Peak gyro vs sensor range")
    ev = ctx.ev
    if ev is None or ev.empty:
        r.why_skipped = "no Motion ENDED events"
        return r
    old_mask = ev.session.str.contains(OLD_GYRO_TAG)
    new, old = ev[~old_mask], ev[old_mask]
    fs_new = round(fw.gyro_full_scale_dps(), -1)  # 32768/16.4 = 1998 -> nominal 2000
    fs_old = 32768.0 / OLD_GYRO_SCALE_LSB_PER_DPS
    ceil_old = fs_old * np.sqrt(3)
    fig, ax = S.new_fig()
    fig.subplots_adjust(left=0.07, right=0.97, top=0.86, bottom=0.14)
    ceil_new = fs_new * np.sqrt(3)
    top = max(ev.peak.max(), ceil_old) * 1.12
    bins = np.linspace(0, top, 50)
    if len(new):
        ax.hist(new.peak, bins=bins, color=S.BLUE, alpha=0.8, edgecolor=S.SURFACE, linewidth=1.5,
                label=f"±{fs_new:.0f} dps build (n={len(new)})")
    if len(old):
        ax.hist(old.peak, bins=bins, color=S.ORANGE, alpha=0.6, edgecolor=S.SURFACE, linewidth=1.5,
                label=f"old ±{fs_old:.0f} dps build (n={len(old)})")
        r.caveats.append(f"Old peaks above ~{fs_old:.0f} dps on any axis were clipped by the sensor.")
    # Drawn with or without old data: it's the old firmware's hard limit, not a measurement.
    S.vthreshold(ax, ceil_old, f"old ±{fs_old:.0f} dps ceiling: {fs_old:.0f}·√3 = {ceil_old:.0f} dps", y=0.98,
                 ha="right")
    if ceil_new <= top:
        S.vthreshold(ax, ceil_new, f"current ceiling {ceil_new:.0f} dps", y=0.98, ha="right")
    ax.set_xlabel("Peak gyroMag in trimmed window (dps)")
    ax.set_ylabel("Motion events")
    ax.legend(loc="upper right")  # the old-ceiling label sits top-left of its line
    ax.set_title(r.title)
    S.footnote(fig, f"Current full scale ±{fs_new:.0f} dps per axis (GYRO_SCALE {fw.rw('GYRO_SCALE')} LSB/dps); "
                    f"magnitude ceiling {ceil_new:.0f} dps" + ("." if ceil_new <= top else ", off the right edge."))
    r.files = S.save(fig, ctx.out, r.stem)
    r.status = "made"
    r.sources = _sources(ev) + _fw_src(ctx, "right_wrist")
    r.counts = {"current build": len(new), "old ±250 build": len(old)}
    if len(new):
        r.takeaway = (f"Current-build peaks reach {new.peak.max():.0f} dps (p95 {new.peak.quantile(0.95):.0f}); "
                      f"{_pct(new.peak > ceil_old):.0f}% exceed what the old ±{fs_old:.0f} dps range could report.")
    if not len(old):
        r.caveats.append(f"No old ±{fs_old:.0f} dps captures found (session names containing '{OLD_GYRO_TAG}'), "
                         "so no overlay.")
    return r


# ============================ G. Pose singularity (SIMULATION) ============================

def fig_pose_singularity(ctx):
    r = FigResult("G", "07_pose_singularity_SIMULATION", "Why pitch/roll was replaced (simulation)")
    # Gravity tilted theta degrees away from the board's +z axis (towards +x),
    # then a +/-wob twist about z, the hand-tremor case. Question: how far does
    # each encoding's reported angle move, when the true direction moves at most
    # 2*wob*sin(theta)?
    wob = 1.0
    theta = np.linspace(0.25, 179.75, 719)
    phi = np.radians(np.linspace(-wob, wob, 81))
    T, P = np.meshgrid(np.radians(theta), phi, indexing="ij")
    gx, gy, gz = np.sin(T) * np.cos(P), np.sin(T) * np.sin(P), np.cos(T)

    def spread(deg):  # max-min along the twist sweep, unwrapped so +/-180 isn't a jump
        u = np.degrees(np.unwrap(np.radians(deg), axis=1))
        return u.max(axis=1) - u.min(axis=1)

    pitch, roll = pitch_roll_from_accel(gx, gy, gz)
    el, az = el_az_from_accel(gx, gy, gz)
    old = np.maximum(spread(pitch), spread(roll))
    new = np.maximum(spread(el), spread(az))
    true = np.array([grav_angle_deg(np.array([gx[i, 0], gy[i, 0], gz[i, 0]]),
                                    np.array([gx[i, -1], gy[i, -1], gz[i, -1]])) for i in range(len(theta))])

    # Where the real ready stances sit: tilt from +z = 90 - el, both arms of every reference.
    ref_tilts = [90 - ref[k] for ref in ctx.fw.weapon_refs for k in ("rEl", "lEl")]
    lo, hi = min(ref_tilts), max(ref_tilts)
    i90 = int(np.argmin(np.abs(theta - 90)))

    fig, ax = S.new_fig()
    fig.subplots_adjust(left=0.08, right=0.97, top=0.84, bottom=0.14)
    ax.axvspan(lo, hi, color=S.NEUTRAL_FILL, zorder=0)
    ax.text((lo + hi) / 2, 0.03, f"where the WEAPON_REFS stances sit (tilt {lo:.0f}°–{hi:.0f}°)", ha="center",
            va="bottom", transform=ax.get_xaxis_transform(), fontsize=17, color=S.INK_2)
    ax.plot(theta, old, color=S.ORANGE, linewidth=2.8, label="old: pitch = atan2(ay, az), roll = atan2(−ax, az)")
    ax.plot(theta, new, color=S.BLUE, linewidth=2.8, label="new: el = asin(z), az = atan2(y, x)")
    ax.plot(theta, true, color=S.INK_2, linewidth=2, linestyle=(0, (2, 3)), label="true movement of gravity")
    ax.set_yscale("log")
    ax.set_ylim(0.01, 400)
    ax.set_xlim(0, 180)
    ax.set_xticks([0, 30, 60, 90, 120, 150, 180])
    ax.set_xlabel(f"Tilt of gravity away from the board's z axis (°) — twist of ±{wob:g}° about z at each tilt")
    ax.set_ylabel("How far the reported angle moves (°, log)")
    ax.legend(loc="upper left", fontsize=17)
    ax.annotate(f"old: {old[i90]:.0f}°", (theta[i90], old[i90]), xytext=(16, -6), textcoords="offset points",
                fontsize=18, color=S.INK)
    ax.annotate(f"new: {new[i90]:.1f}°", (theta[i90], new[i90]), xytext=(16, 8), textcoords="offset points",
                fontsize=18, color=S.INK)
    ax.set_title(r.title.replace(" (simulation)", "") + ": it blows up where the stances are")
    S.simulation_banner(fig)
    S.footnote(fig, "Synthetic gravity vectors through the firmware's two formulas (torso_game.ino still uses "
                    "pitch/roll; wrists use el/az). Not sensor data.")
    r.files = S.save(fig, ctx.out, r.stem)
    r.status = "made"
    r.sources = _fw_src(ctx, "right_wrist", "torso") + ["(synthetic)"]
    r.counts = {"simulated tilts": len(theta), "twist steps per tilt": len(phi)}
    r.takeaway = (f"With gravity in the board's x/y plane (tilt 90°), a {true[i90]:.0f}° true twist moves old "
                  f"pitch/roll by {old[i90]:.0f}° but el/az by {new[i90]:.1f}°; every stored stance has a tilt "
                  f"of {lo:.0f}°–{hi:.0f}°, the region where the old formula is worst.")
    r.caveats = ["SIMULATION. Say so on screen.",
                 "el/az has its own singularity (azimuth is undefined when gravity is along z, tilt 0° or 180°); "
                 "no stored stance is near it."]
    return r


# ============================ H. Network ============================

def fig_network(ctx):
    L = ctx.ds.lines
    res = []
    r = FigResult("H", "08a_network_rtt", "Hub ↔ torso round-trip time")
    rtt = L[L.kind == "rtt"] if len(L) else L
    if rtt is None or rtt.empty:
        r.why_skipped = "no [rtt] lines (LATENCY_TEST=0)"
    else:
        rtt = rtt.assign(ms=rtt["us"] / 1000.0)
        settings = [s for s in ("off", "on") if (rtt.wifi_sleep == s).any()]
        fig, (ah, ac) = S.new_fig(1, 2)
        fig.subplots_adjust(left=0.07, right=0.97, top=0.84, bottom=0.14, wspace=0.22)
        xmax = rtt.ms.quantile(0.99) * 1.15
        bins = np.linspace(0, xmax, 60)
        parts = []
        for s in settings:
            v = rtt.loc[rtt.wifi_sleep == s, "ms"].sort_values().to_numpy()
            lab = f"WiFi sleep {s.upper()} (n={len(v)})"
            ah.hist(v, bins=bins, color=S.SLEEP_COLOR[s], alpha=0.6, edgecolor=S.SURFACE, linewidth=1, label=lab)
            ac.plot(v, np.arange(1, len(v) + 1) / len(v), color=S.SLEEP_COLOR[s], linewidth=2.6, label=lab)
            parts.append(f"sleep {s}: median {np.median(v):.1f} ms, p95 {np.percentile(v, 95):.1f} ms")
        for a in (ah, ac):
            a.set_xlim(0, xmax)
            a.set_xlabel("Round-trip time (ms)")
        ah.set_ylabel("Pings")
        ac.set_ylabel("Fraction of pings ≤ x")
        ac.set_ylim(0, 1.02)
        ah.legend(loc="upper right")
        ah.set_title("Histogram", fontsize=22)
        ac.set_title("Cumulative", fontsize=22)
        fig.suptitle(r.title, x=0.012, ha="left", fontsize=28, fontweight="bold", color=S.INK)
        beyond = int((rtt.ms > xmax).sum())
        S.footnote(fig, "PING every 100 ms on LINK_PORT; RTT timed on the hub's own micros()."
                   + (f" {beyond} pings beyond the axis (p99 × 1.15)." if beyond else ""))
        r.files = S.save(fig, ctx.out, r.stem)
        r.status = "made"
        r.sources = _sources(rtt) + _fw_src(ctx, "right_wrist", "torso")
        r.counts = {f"sleep {s}": int((rtt.wifi_sleep == s).sum()) for s in settings}
        r.takeaway = "; ".join(parts) + "."
        if len(settings) < 2:
            r.caveats.append("Only one wifi_sleep setting captured — no on/off comparison yet.")
        r.caveats.append("Includes up to one loop() pass of polling delay on each board.")
    res.append(r)

    r = FigResult("H", "08b_network_beacon_iat", "TorsoBeacon inter-arrival time at the hub")
    iat = L[L.kind == "beacon_iat"] if len(L) else L
    if iat is None or iat.empty:
        r.why_skipped = "no [beacon_iat] lines (LATENCY_TEST=0, or no weapon lock)"
    else:
        rows = [{"session": x.session, "wifi_sleep": x.wifi_sleep, "ms": u / 1000.0}
                for x in iat.itertuples() for u in x.us]
        v = pd.DataFrame(rows)
        settings = [s for s in ("off", "on") if (v.wifi_sleep == s).any()]
        period = ctx.fw.c("torso", "BEACON_INTERVAL_MS")
        fig, axes = S.new_fig(1, len(settings), sharey=True, squeeze=False)
        fig.subplots_adjust(left=0.07, right=0.97, top=0.84, bottom=0.14, wspace=0.08)
        xmax = max(v.ms.quantile(0.995) * 1.1, period * 2.5)
        bins = np.linspace(0, xmax, 70)
        parts = []
        for ax, s in zip(axes[0], settings):
            d = v.loc[v.wifi_sleep == s, "ms"]
            ax.hist(d.clip(upper=xmax), bins=bins, color=S.SLEEP_COLOR[s], edgecolor=S.SURFACE, linewidth=1)
            ax.set_yscale("log")
            S.vthreshold(ax, period, f"BEACON_INTERVAL_MS {period:g}", y=0.98)
            ax.set_title(f"WiFi sleep {s.upper()} (n={len(d)})", fontsize=22)
            ax.set_xlabel("Inter-arrival (ms)")
            parts.append(f"sleep {s}: p99 {d.quantile(0.99):.1f} ms, {100 * (d > 2 * period).mean():.1f}% > "
                         f"{2 * period:g} ms")
        axes[0][0].set_ylabel("Beacons (log scale)")
        fig.suptitle(r.title, x=0.012, ha="left", fontsize=28, fontweight="bold", color=S.INK)
        S.footnote(fig, "Last bin collects everything beyond the axis.")
        r.files = S.save(fig, ctx.out, r.stem)
        r.status = "made"
        r.sources = _sources(iat) + _fw_src(ctx, "right_wrist", "torso")
        r.counts = {f"sleep {s}": int((v.wifi_sleep == s).sum()) for s in settings}
        r.takeaway = "; ".join(parts) + "."
        if len(settings) < 2:
            r.caveats.append("Only one wifi_sleep setting captured — no on/off comparison yet.")
    res.append(r)

    r = FigResult("H", "08c_network_packet_loss", "Packet loss per stream")
    st = L[L.kind == "stats"] if len(L) else L
    if st is None or st.empty:
        r.why_skipped = "no [stats] lines"
    else:
        # wifi_sleep comes from the [cfg] boot line; a session whose listener
        # started after the boards booted has none, so it's "unknown", not dropped.
        st = st.assign(stream_name=st["from"] + " →\n" + st["to"] + "\n" + st["type"],
                       wifi_sleep=st["wifi_sleep"].fillna("unknown"))
        agg = st.groupby(["stream_name", "wifi_sleep"], dropna=False)[["rx", "lost"]].sum().reset_index()
        agg["pct"] = 100.0 * agg.lost / (agg.rx + agg.lost).where((agg.rx + agg.lost) > 0)
        agg = agg[(agg.rx + agg.lost) > 0]
        streams = sorted(agg.stream_name.unique())
        settings = [s for s in ("off", "on", "unknown") if (agg.wifi_sleep == s).any()]
        fig, ax = S.new_fig()
        fig.subplots_adjust(left=0.07, right=0.97, top=0.86, bottom=0.2)
        w = 0.8 / max(len(settings), 1)
        for j, s in enumerate(settings):
            d = agg[agg.wifi_sleep == s].set_index("stream_name").reindex(streams)
            xs = np.arange(len(streams)) + (j - (len(settings) - 1) / 2) * w
            lab = "WiFi sleep not logged (no [cfg] line)" if s == "unknown" else f"WiFi sleep {s.upper()}"
            ax.bar(xs, d.pct.fillna(0), width=w - 0.03, color=S.SLEEP_COLOR.get(s, S.INK_2), label=lab)
            for xi, p, n in zip(xs, d.pct, d.rx + d.lost):
                if pd.notna(p):
                    ax.text(xi, p, f"{p:.1f}%", ha="center", va="bottom", fontsize=14, color=S.INK_2)
        ax.set_xticks(range(len(streams)))
        ax.set_xticklabels(streams, fontsize=14)
        ax.set_ylabel("Lost (% of sent)")
        ax.set_ylim(0, max(agg.pct.max(), 1) * 1.25)  # headroom for the value labels + legend
        ax.legend(loc="upper left")
        ax.set_title(r.title + " (from sequence-number gaps)")
        S.footnote(fig, "Each receiver counts gaps in the sender's per-type seq number; tallied from [stats] "
                        "lines once a second.")
        r.files = S.save(fig, ctx.out, r.stem)
        r.status = "made"
        r.sources = _sources(st) + _fw_src(ctx, "right_wrist", "left_wrist", "torso")
        r.counts = {f"sleep {s} packets": int((agg[agg.wifi_sleep == s].rx + agg[agg.wifi_sleep == s].lost).sum())
                    for s in settings}
        worst = agg.sort_values("pct").iloc[-1]
        r.takeaway = (f"Worst stream: {worst.stream_name.replace(chr(10), ' ')} at {worst.pct:.1f}% "
                      f"(WiFi sleep {worst.wifi_sleep}).")
        r.caveats.append("A reboot restarts the sender's seq counter, which can read as a burst of loss or none.")
        if "unknown" in settings:
            r.caveats.append("Some sessions have no [cfg] line (listener started after the boards booted), so "
                             "their WiFi sleep setting isn't known.")
        if len([s for s in settings if s != "unknown"]) < 2:
            r.caveats.append("No on/off WiFi sleep comparison yet.")
    res.append(r)
    return res
