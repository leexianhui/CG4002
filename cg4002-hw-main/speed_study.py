"""Speed study: is the valid-action check biased against slow swings, and
which features stay the same across speeds while still separating real
strikes from fakes?

    python speed_study.py [--logs "2 players/logs"] [--out figures/speed_study]
    python speed_study.py --old-logs "2 players/logs/p1_v1" \
        --new-logs "2 players/logs/p1"

Needs RAW_STREAM sessions (raw_<time>_<action>_<slow|normal|fast>.csv) plus
their debug CSVs. See figpipe/raw.py for how strokes and features are found.
Writes figures, CSV tables and a README into --out. When --old-logs and
--new-logs are supplied, they explicitly identify the firmware generation;
otherwise the timestamp cutoff below is used for backwards compatibility.
"""

import argparse
import os

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from figpipe import replay as R
from figpipe import style as S
from figpipe.logs import REAL_ACTIONS
from figpipe.raw import PROFILE_POINTS, build_table, firmware_strokes

SKETCH = os.path.join("2 players", "p1_right_wrist", "p1_right_wrist.ino")
SPEEDS = ["slow", "normal", "fast"]
SPEED_COLOR = {"slow": S.SEQ_BLUE[2], "normal": S.SEQ_BLUE[6], "fast": S.SEQ_BLUE[11]}

MAGNITUDE = ["peak_gyro", "peak_jerk", "stroke_ms", "torso_peak"]
INVARIANT = ["rot_deg", "straightness", "peak_pos", "sustain", "reversals_rel", "n_peaks", "windup",
             "r_eff", "jerk_per_rot", "torso_rot", "torso_ratio", "torso_lag", "template_corr"]
FEATURE_NAME = {
    "peak_gyro": "peak gyro (dps)", "peak_jerk": "peak jerk (g)", "stroke_ms": "stroke duration (ms)",
    "torso_peak": "torso peak (dps)", "rot_deg": "rotation angle (deg)", "straightness": "straightness",
    "peak_pos": "peak position", "sustain": "half-peak width", "reversals_rel": "reversals (rel.)",
    "n_peaks": "peaks > 0.5", "windup": "wind-up", "r_eff": "effective radius", "jerk_per_rot": "jerk per rotation",
    "torso_rot": "torso rotation (deg)", "torso_ratio": "torso ÷ wrist rotation", "torso_lag": "torso lag",
    "template_corr": "template correlation",
}
LR_FEATURES = ["rot_deg", "straightness", "sustain", "reversals_rel", "n_peaks", "r_eff", "torso_ratio",
               "template_corr"]

# Direction rule: the stroke's rotation axis matches an action's strike axis
# (fixed by how the board is strapped on), its rotation angle sits in that
# action's range, and the torso isn't carrying the motion (body turn).
DIR_COS_MIN = 0.8
DIR_ROT_MARGIN = 0.10
DIR_ROT_PCT = (5, 95)
AXIS = ["axis_x", "axis_y", "axis_z"]


def fit_direction(tr):
    model = {}
    for act in REAL_ACTIONS:
        s = tr[(tr.label == act) & (tr.y == 1)]
        if len(s) < 3:
            continue
        V = s[AXIS].to_numpy()
        if act == "spin":  # spun both ways
            V = V * np.sign(V @ V[0])[:, None]
        c = V.mean(0)
        lo, hi = np.percentile(s.rot_deg, DIR_ROT_PCT)
        tmax = np.percentile(s.torso_ratio.dropna(), 95)
        model[act] = (c / np.linalg.norm(c), lo * (1 - DIR_ROT_MARGIN), hi * (1 + DIR_ROT_MARGIN),
                      max(1.5 * tmax, 0.3))
    return model


def predict_direction(model, te):
    out = np.zeros(len(te), bool)
    V = te[AXIS].to_numpy()
    for act, (c, lo, hi, tmax) in model.items():
        cs = V @ c
        if act == "spin":
            cs = np.abs(cs)
        out |= ((cs >= DIR_COS_MIN) & te.rot_deg.between(lo, hi).to_numpy()
                & ~(te.torso_ratio.to_numpy() > tmax))
    return out


def add_template_corr(df):
    """Best correlation of a stroke's normalised profile with any action's mean
    strike profile. A strike is only compared with templates built from the
    OTHER speeds, so a high score means the shape carries across speeds."""
    P = np.vstack(df["_profile"].to_numpy())
    strikes = (df.role == "strike") & ~df.has_gap
    out = np.full(len(df), np.nan)
    for i in range(len(df)):
        best = -1.0
        for act in REAL_ACTIONS:
            m = strikes & (df.label == act)
            if df.role.iat[i] == "strike":
                m &= df.speed != df.speed.iat[i]
            if m.sum() < 3:
                continue
            tpl = P[m.to_numpy()].mean(0)
            best = max(best, float(np.corrcoef(P[i], tpl)[0, 1]))
        out[i] = best
    df["template_corr"] = out
    return df


def groups(df):
    ok = ~df.has_gap
    return (df[ok & (df.role == "strike")], df[ok & (df.cls == "fake")], df[ok & (df.role == "return")])


# ------------------------------------------------------------- bias

def bias_tables(df):
    st = df[df.role == "strike"]
    rate = (st.groupby(["label", "speed"]).agg(n=("fw_accept", "size"), accepted=("fw_accept", "sum"))
            .reindex(pd.MultiIndex.from_product([REAL_ACTIONS, SPEEDS], names=["label", "speed"])))
    rate["rate"] = rate.accepted / rate.n
    rej = st[~st.fw_accept].explode("fw_fails")
    fails = rej.groupby(["speed", "fw_fails"]).size().unstack("speed", fill_value=0).reindex(columns=SPEEDS,
                                                                                             fill_value=0)
    n_rej = st[~st.fw_accept].groupby("speed").size().reindex(SPEEDS, fill_value=0)
    trim = st[~st.has_gap].groupby(["label", "speed"]).trim_cov.median().unstack("speed").reindex(
        index=REAL_ACTIONS, columns=SPEEDS)
    dur = st.groupby(["label", "speed"]).stroke_ms.median().unstack("speed").reindex(index=REAL_ACTIONS,
                                                                                       columns=SPEEDS)
    neg = df[(df.cls == "fake") | (df.role == "return")]
    fp = neg.groupby(neg.cls.where(neg.cls == "fake", "return")).fw_accept.agg(["size", "sum"])
    return rate, fails, n_rej, trim, dur, fp


def fig_bias(rate, out):
    fig, ax = S.new_fig()
    fig.subplots_adjust(left=0.07, right=0.98, top=0.80, bottom=0.12)
    x = np.arange(len(REAL_ACTIONS))
    wdt = 0.27
    for k, sp in enumerate(SPEEDS):
        r = rate.xs(sp, level="speed").reindex(REAL_ACTIONS)
        bars = ax.bar(x + (k - 1) * wdt, 100 * r.rate.fillna(0), wdt * 0.92, color=SPEED_COLOR[sp], label=sp)
        for b, (acc, n) in zip(bars, zip(r.accepted, r.n)):
            if pd.notna(n):
                ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 1.5, f"{int(acc)}/{int(n)}",
                        ha="center", va="bottom", fontsize=14, color=S.INK_2)
    ax.set_xticks(x, REAL_ACTIONS)
    ax.set_ylabel("Real strikes accepted (%)")
    ax.set_ylim(0, 115)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=3)
    fig.suptitle("The current check accepts fast strikes and rejects slow ones", x=0.012, y=0.975, ha="left",
                 fontsize=28, fontweight="bold", color=S.INK)
    S.footnote(fig, "Firmware decisions logged live (2026-10-04, player 1, sword & shield). "
                    "One bar = one session of ~10 reps. No fake movement was accepted at any speed.")
    return S.save(fig, out, "01_accept_rate_by_speed")


def fig_fail_reasons(fails, n_rej, out):
    fig, ax = S.new_fig()
    fig.subplots_adjust(left=0.20, right=0.97, top=0.80, bottom=0.12)
    order = fails.sum(1).sort_values().index
    y = np.arange(len(order))
    h = 0.27
    for k, sp in enumerate(SPEEDS):
        share = 100 * fails.loc[order, sp] / max(n_rej[sp], 1)
        ax.barh(y + (k - 1) * h, share, h * 0.92, color=SPEED_COLOR[sp], label=f"{sp} ({n_rej[sp]} rejected)")
    ax.set_yticks(y, order)
    ax.set_xlabel("Share of rejected real strikes failing this gate (%)  — a strike can fail several")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=3)
    fig.suptitle("Why real strikes were rejected", x=0.012, y=0.975, ha="left", fontsize=28,
                 fontweight="bold", color=S.INK)
    S.footnote(fig, "no_candidate = the segmenter never opened a candidate over the strike.")
    return S.save(fig, out, "02_reject_reasons_by_speed")


def fig_overlays(df, out):
    st = df[(df.role == "strike") & ~df.has_gap]
    fig, axes = S.new_fig(2, len(REAL_ACTIONS), sharey="row")
    fig.subplots_adjust(left=0.07, right=0.99, top=0.82, bottom=0.09, wspace=0.08, hspace=0.38)
    u = np.linspace(0, 100, PROFILE_POINTS)
    for j, act in enumerate(REAL_ACTIONS):
        top, bot = axes[0, j], axes[1, j]
        for sp in SPEEDS:
            g = st[(st.label == act) & (st.speed == sp)]
            for _, r in g.iterrows():
                n = r["stroke_ms"]
                top.plot(u / 100 * n, r["_profile"] * r["peak_gyro"], color=SPEED_COLOR[sp], lw=1, alpha=0.55)
                bot.plot(u, r["_profile"], color=SPEED_COLOR[sp], lw=1, alpha=0.35)
            if len(g):
                bot.plot(u, np.vstack(g["_profile"]).mean(0), color=SPEED_COLOR[sp], lw=3.2, label=sp)
        top.set_title(act, fontsize=20)
        for a in (top, bot):
            a.tick_params(labelsize=13)
        top.set_xlabel("ms", fontsize=14)
        bot.set_xlabel("% of stroke", fontsize=14)
    axes[0, 0].set_ylabel("rotation rate (dps)", fontsize=16)
    axes[1, 0].set_ylabel("÷ own peak", fontsize=16)
    axes[1, 0].legend(fontsize=14, loc="lower center")
    fig.suptitle("Same strike, different speed: raw (top) vs time- and peak-normalised (bottom)", x=0.012,
                 y=0.975, ha="left", fontsize=26, fontweight="bold", color=S.INK)
    S.footnote(fig, "Rotation about each stroke's own peak axis. Thin lines = reps, thick = mean per speed. "
                    "Strokes with a lost stream packet are left out.")
    return S.save(fig, out, "03_strike_profiles_by_speed")


# ------------------------------------------------------------ features

def eta2(values, labels):
    v = pd.Series(values)
    gm = v.mean()
    tot = ((v - gm) ** 2).sum()
    if tot == 0:
        return 0.0
    between = sum(len(g) * (g.mean() - gm) ** 2 for _, g in v.groupby(labels.to_numpy()))
    return float(between / tot)


def auc(pos, neg):
    pos, neg = pos.dropna(), neg.dropna()
    if len(pos) < 3 or len(neg) < 3:
        return np.nan
    a = roc_auc_score(np.r_[np.ones(len(pos)), np.zeros(len(neg))], np.r_[pos, neg])
    return max(a, 1 - a)


def rank_features(df):
    st, fk, rt = groups(df)
    rows = []
    for f in MAGNITUDE + INVARIANT:
        ratios, etas = [], []
        for act in REAL_ACTIONS:
            g = st[st.label == act]
            s, fa = g[g.speed == "slow"][f].median(), g[g.speed == "fast"][f].median()
            if pd.notna(s) and pd.notna(fa) and s != 0 and np.sign(s) == np.sign(fa):
                ratios.append(abs(np.log2(fa / s)))
            if g[f].notna().sum() > 5:
                etas.append(eta2(g[f].dropna(), g.loc[g[f].notna(), "speed"]))
        rows.append({
            "feature": f, "kind": "magnitude" if f in MAGNITUDE else "shape",
            "fast_over_slow": 2 ** np.median(ratios) if ratios else np.nan,
            "speed_eta2": np.median(etas) if etas else np.nan,
            "auc_strike_vs_fake": auc(st[f], fk[f]),
            "auc_slow_strike_vs_fake": auc(st[st.speed == "slow"][f], fk[f]),
            "auc_strike_vs_return": auc(st[f], rt[f]),
        })
    return pd.DataFrame(rows).set_index("feature")


def fig_feature_map(rank, out):
    fig, ax = S.new_fig()
    fig.subplots_adjust(left=0.08, right=0.97, top=0.85, bottom=0.12)
    for f, r in rank.iterrows():
        c = S.ORANGE if r.kind == "magnitude" else S.BLUE
        ax.scatter(r.speed_eta2, r.auc_slow_strike_vs_fake, s=160, color=c, zorder=3)
        ax.annotate(FEATURE_NAME[f], (r.speed_eta2, r.auc_slow_strike_vs_fake), xytext=(9, 5),
                    textcoords="offset points", fontsize=14, color=S.INK_2)
    ax.scatter([], [], s=160, color=S.ORANGE, label="magnitude (what the gates use now)")
    ax.scatter([], [], s=160, color=S.BLUE, label="shape / normalised")
    ax.set_xlabel("Speed dependence  (share of within-action variance explained by speed, η²)  →  worse")
    ax.set_ylabel("Slow strikes vs fakes  (AUC, 0.5 = useless)")
    ax.set_xlim(-0.03, max(0.95, rank.speed_eta2.max() + 0.05))
    ax.set_ylim(0.45, 1.02)
    ax.legend(loc="lower right")
    fig.suptitle("Good features sit top-left: they separate real from fake and ignore speed", x=0.012,
                 y=0.975, ha="left", fontsize=26, fontweight="bold", color=S.INK)
    S.footnote(fig, "Median η² over the six actions. AUC folded so direction doesn't matter.")
    return S.save(fig, out, "04_feature_map")


# ------------------------------------------------------- method compare

def compare_methods(df):
    """Leave-one-speed-out: fit on two speeds, test on the third. Positives =
    strikes; negatives = fakes + returns to guard (fakes have no fast session)."""
    st, fk, rt = groups(df)
    data = pd.concat([st.assign(y=1), fk.assign(y=0), rt.assign(y=0)])
    data = data.dropna(subset=LR_FEATURES)
    rows = []
    for held in SPEEDS:
        tr, te = data[data.speed != held], data[data.speed == held]
        neg_te = te[te.y == 0]
        res = {"held_out": held, "strikes": int(te.y.sum()), "negatives": len(neg_te)}

        def score(name, pred):
            res[f"{name}_TPR"] = float(pred[te.y.to_numpy() == 1].mean())
            res[f"{name}_FPR"] = float(pred[te.y.to_numpy() == 0].mean()) if len(neg_te) else np.nan

        score("firmware", te.fw_accept.to_numpy(bool))
        for name, feats in (("magnitude_lr", ["peak_gyro", "peak_jerk", "stroke_ms"]), ("shape_lr", LR_FEATURES)):
            m = make_pipeline(StandardScaler(), LogisticRegression(class_weight="balanced", max_iter=2000))
            m.fit(tr[feats], tr.y)
            score(name, m.predict(te[feats]).astype(bool))
        # template: threshold that maximises balanced accuracy on the training speeds
        cand = np.linspace(0, 1, 101)
        ba = [(tr.template_corr[tr.y == 1] >= c).mean() + (tr.template_corr[tr.y == 0] < c).mean() for c in cand]
        thr = cand[int(np.argmax(ba))]
        res["template_thr"] = thr
        score("template", (te.template_corr >= thr).to_numpy())
        score("direction", predict_direction(fit_direction(tr), te))
        fake_te = (te.cls == "fake").to_numpy()
        for name in ("firmware", "direction"):
            p = te.fw_accept.to_numpy(bool) if name == "firmware" else predict_direction(fit_direction(tr), te)
            res[f"{name}_fakeFPR"] = float(p[fake_te].mean()) if fake_te.any() else np.nan
            res[f"{name}_returnFPR"] = float(p[(te.role == "return").to_numpy()].mean())
        rows.append(res)
    return pd.DataFrame(rows).set_index("held_out")


def fig_methods(cmp, out):
    methods = [("firmware", "current firmware"), ("magnitude_lr", "fitted on magnitude"),
               ("shape_lr", "fitted on shape"), ("template", "profile template"),
               ("direction", "direction + angle rule")]
    fig, axes = S.new_fig(1, 2, sharey=True)
    fig.subplots_adjust(left=0.07, right=0.98, top=0.76, bottom=0.12, wspace=0.08)
    x = np.arange(len(SPEEDS))
    wdt = 0.17
    cols = [S.MUTED, S.ORANGE, S.SEQ_BLUE[3], S.AQUA, S.SEQ_BLUE[11]]
    for ax, metric, title in ((axes[0], "TPR", "Real strikes accepted"), (axes[1], "FPR", "Fakes + returns accepted")):
        for k, ((m, name), c) in enumerate(zip(methods, cols)):
            v = 100 * cmp[f"{m}_{metric}"].reindex(SPEEDS)
            ax.bar(x + (k - 2) * wdt, v.fillna(0), wdt * 0.92, color=c, label=name)
        ax.set_xticks(x, [f"{s}\n(held out)" for s in SPEEDS])
        ax.set_title(title, fontsize=22)
        ax.set_ylim(0, 105)
    axes[0].set_ylabel("%")
    axes[0].legend(loc="lower left", bbox_to_anchor=(0, 1.08), ncol=5, fontsize=15)
    fig.suptitle("Trained on two speeds, tested on the third", x=0.012, y=0.975, ha="left", fontsize=28,
                 fontweight="bold", color=S.INK)
    S.footnote(fig, "Negatives at 'fast' are returns to guard only (no fast fake sessions were recorded).")
    return S.save(fig, out, "05_methods_leave_one_speed_out")


# ---------------------------------------- replay of the new firmware check

def cutoff_score(rep_train):
    """The Phase-2 separate-cutoffs rule on replay features, for comparison:
    axis cos >= DIR_COS_MIN AND angle in range AND torso under its limit."""
    m = fit_direction(rep_train.rename(columns={"ux": "axis_x", "uy": "axis_y", "uz": "axis_z", "rot": "rot_deg"})
                      .assign(y=(rep_train.role == "strike").astype(int)))

    def score(r):
        u = np.array([r.ux, r.uy, r.uz])
        for act, (c, lo, hi, tmax) in m.items():
            cs = abs(u @ c) if act == "spin" else u @ c
            if cs >= DIR_COS_MIN and lo <= r.rot <= hi and not (r.torso_ratio > tmax):
                return True, act, np.nan
        return False, None, np.nan
    return score


# Sessions recorded before the stroke firmware was flashed: their logged
# decisions are the ORIGINAL firmware's, the baseline for "old".
ORIGINAL_FIRMWARE_BEFORE = "20261004_1600"


def fit_all(rep, table):
    """Rotation templates + limit and thrust template + limit, as
    fit_strike_model.py fits them."""
    model = R.fit_templates(R.strikes_for_fit(rep))
    limit = R.tune_max_dist(rep, table, model)
    thrust = R.fit_thrust(R.thrusts_for_fit(rep))
    thrust_limit = R.tune_thrust_dist(rep, model, limit, thrust)
    return model, limit, thrust, thrust_limit


def _fold_rates(d):
    """strike = real strikes accepted (rotation strikes + thrust pushes);
    fake = fake-session rotation strokes overlapping an accept; between =
    between-rep rotation strokes of the other real sessions overlapping one."""
    ok = ~d.has_gap.astype(bool)
    rot = d.kind == "rot"
    strikes = d[ok & (d.role == "strike") & ((rot & (d.label != R.THRUST_ACTION))
                                             | (~rot & (d.label == R.THRUST_ACTION)))]
    fake = d[ok & rot & (d.cls == "fake")]
    between = d[ok & rot & (d.cls == "real") & (d.role != "strike") & (d.label != R.THRUST_ACTION)]
    return {"strike": strikes.hit.mean(), "fake": fake.hit.mean() if len(fake) else np.nan,
            "between": between.hit.mean() if len(between) else np.nan}


def replay_study(table, sessions):
    """Leave-one-speed-out replay of the new firmware logic (rotation strokes
    + push strokes). Returns the per-fold summary, held-out decisions, per
    action x speed accept rates (original firmware vs new), the final models
    fitted on every session, and the stroke-truncation check."""
    sessions = [s for s in sessions if s.label is not None]
    rep = R.label_strokes(pd.concat([R.replay_session(s) for s in sessions], ignore_index=True), table)
    folds, held_dec = [], []
    for held in SPEEDS:
        tr, te = rep[rep.speed != held], rep[rep.speed == held]
        model, limit, thrust, thrust_limit = fit_all(tr, table[table.speed != held])
        dec = R.mark_hits(R.decide(te, model, limit, thrust=thrust, thrust_max=thrust_limit))
        dec = dec.assign(fold=held, limit=limit, thrust_limit=thrust_limit)
        cut = R.mark_hits(R.decide(te[te.kind == "rot"], model, score=cutoff_score(tr[tr.kind == "rot"])))
        row = {"held_out": held, "limit": limit, "thrust_limit": thrust_limit}
        for name, d in (("cutoffs", cut), ("template", dec)):
            for k, v in _fold_rates(d).items():
                row[f"{name}_{k}"] = v
        folds.append(row)
        held_dec.append(dec)
    folds = pd.DataFrame(folds).set_index("held_out")
    dec = pd.concat(held_dec)
    ok = ~dec.has_gap.astype(bool)
    rot = dec.kind == "rot"
    new = dec[ok & (dec.role == "strike") & ((rot & (dec.label != R.THRUST_ACTION))
                                             | (~rot & (dec.label == R.THRUST_ACTION)))]
    if "firmware" in table:
        old = table[(table.firmware == "old") & (table.role == "strike") & ~table.has_gap]
    else:
        old = table[(table.role == "strike") & ~table.has_gap & (table.session < ORIGINAL_FIRMWARE_BEFORE)]
    by_action = pd.concat({"old": old.groupby(["label", "speed"]).fw_accept.mean(),
                           "new": new.groupby(["label", "speed"]).hit.mean()}, axis=1).unstack("speed")
    final = fit_all(rep, table)
    st = rep[(rep.kind == "rot") & (rep.role == "strike") & ~rep.has_gap.astype(bool)]
    trunc = ((st.t1 < st.tbl_t1 - 30) | (st.t0 > st.tbl_t0 + 30)).groupby(st.speed).mean()
    return folds, dec, by_action, final, trunc


def fig_old_vs_new(by_action, out):
    fig, axes = S.new_fig(1, 3, sharey=True)
    fig.subplots_adjust(left=0.06, right=0.99, top=0.78, bottom=0.14, wspace=0.06)
    x = np.arange(len(REAL_ACTIONS))
    for ax, sp in zip(axes, SPEEDS):
        old = 100 * by_action[("old", sp)].reindex(REAL_ACTIONS)
        new = 100 * by_action[("new", sp)].reindex(REAL_ACTIONS)
        ax.bar(x - 0.2, old.fillna(0), 0.38, color=S.MUTED, label="current firmware")
        ax.bar(x + 0.2, new.fillna(0), 0.38, color=SPEED_COLOR["fast"], label="new check (held-out speed)")
        ax.set_xticks(x, REAL_ACTIONS, rotation=35, ha="right", fontsize=15)
        ax.set_title(sp, fontsize=22)
        ax.set_ylim(0, 105)
    axes[0].set_ylabel("Real strikes accepted (%)")
    axes[0].legend(loc="lower left", bbox_to_anchor=(0, 1.1), ncol=2)
    fig.suptitle("New check vs current firmware, per action and speed", x=0.012, y=0.975, ha="left",
                 fontsize=28, fontweight="bold", color=S.INK)
    S.footnote(fig, "Current firmware: the original gates' logged decisions (first collection). New check: "
                    "templates fitted on the other two speeds, replayed on this speed's raw stream.")
    return S.save(fig, out, "06_old_vs_new_by_action")


def fig_distance(dec, out):
    dec = dec[(dec.kind == "rot") & (dec.label != R.THRUST_ACTION)]  # rotation templates only
    ok = ~dec.has_gap.astype(bool)
    groups_ = [(f"strikes\n{sp}", dec[ok & (dec.role == "strike") & (dec.speed == sp)], SPEED_COLOR[sp])
               for sp in SPEEDS]
    groups_ += [("fakes", dec[ok & (dec.cls == "fake")], S.ORANGE),
                ("between\nreps", dec[ok & (dec.cls == "real") & (dec.role != "strike")], S.MUTED)]
    fig, ax = S.new_fig()
    fig.subplots_adjust(left=0.08, right=0.97, top=0.84, bottom=0.14)
    rng = np.random.default_rng(0)
    for i, (name, g, c) in enumerate(groups_):
        y = np.clip(g.dist.to_numpy(float), 0.05, 50)
        ax.scatter(i + rng.uniform(-0.28, 0.28, len(y)), y, s=36, color=c, alpha=0.7,
                   edgecolor="none", zorder=3)
        sup = g[g.reason == "return"]
        if len(sup):
            ax.scatter(i + rng.uniform(-0.28, 0.28, len(sup)), np.clip(sup.dist, 0.05, 50), s=90,
                       facecolor="none", edgecolor=S.INK, linewidth=1.2, zorder=4)
    ax.set_yscale("log")
    ticks = [0.25, 0.5, 1, 2, 4, 8]
    ax.set_yticks(ticks, [f"{v:g}" for v in ticks])
    ax.minorticks_off()
    ax.set_xticks(range(len(groups_)), [g[0] for g in groups_])
    lim = dec.groupby("fold").limit.first()
    S.hthreshold(ax, float(lim.median()), f"limit (per held-out speed: {', '.join(f'{v:g}' for v in lim)})",
                 side="left")
    ax.scatter([], [], s=90, facecolor="none", edgecolor=S.INK, label="rejected as a return (context)")
    ax.legend(loc="upper left")
    ax.set_ylabel("Distance to nearest strike template")
    fig.suptitle("Strikes sit near a template at every speed; fakes don't", x=0.012, y=0.975, ha="left",
                 fontsize=28, fontweight="bold", color=S.INK)
    S.footnote(fig, "Held-out distances: each speed scored with templates fitted on the other two. "
                    "Below the limit = accepted unless it's the return of the stroke before.")
    return S.save(fig, out, "07_template_distance")


# ---------------------------------------------------------------- main

def md(df, fmt="{:.2f}"):
    d = df.copy()
    for c in d.columns:
        if d[c].dtype.kind == "f":
            d[c] = d[c].map(lambda v: "" if pd.isna(v) else fmt.format(v))
    return d.to_markdown()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--logs", default=os.path.join("2 players", "logs"))
    ap.add_argument("--old-logs", help="folder containing sessions recorded with the old firmware")
    ap.add_argument("--new-logs", help="folder containing sessions recorded with the new firmware")
    ap.add_argument("--out", default=os.path.join("figures", "speed_study"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    if bool(args.old_logs) != bool(args.new_logs):
        ap.error("--old-logs and --new-logs must be supplied together")

    if args.old_logs and args.new_logs:
        old_df, old_sessions, _ = build_table(args.old_logs)
        new_df, new_sessions, _ = build_table(args.new_logs)
        old_df["firmware"] = "old"
        new_df["firmware"] = "new"
        df = pd.concat([old_df, new_df], ignore_index=True)
        sessions = old_sessions + new_sessions
        log_dirs = [args.old_logs, args.new_logs]
    else:
        df, sessions, _ = build_table(args.logs)
        # Backward-compatible mode: infer the firmware generation from the
        # timestamp, as the original script did.
        df["firmware"] = np.where(df.session < ORIGINAL_FIRMWARE_BEFORE, "old", "new")
        log_dirs = [args.logs]

    df = add_template_corr(df[df.label.notna()].reset_index(drop=True))
    df.drop(columns=["_profile", "_jerk_profile"]).to_csv(os.path.join(args.out, "strokes.csv"), index=False)

    # The bias analysis needs the ORIGINAL firmware's logged decisions.
    orig = df[df.firmware == "old"].reset_index(drop=True)
    rate, fails, n_rej, trim, dur, fp = bias_tables(orig)
    rank = rank_features(orig)
    cmp = compare_methods(orig)
    for name, t in (("accept_rate", rate), ("reject_reasons", fails), ("trim_coverage", trim),
                    ("stroke_ms", dur), ("feature_rank", rank), ("methods", cmp)):
        t.to_csv(os.path.join(args.out, f"{name}.csv"))

    fig_bias(rate, args.out)
    fig_fail_reasons(fails, n_rej, args.out)
    fig_overlays(orig, args.out)
    fig_feature_map(rank, args.out)
    fig_methods(cmp, args.out)

    folds, dec, by_action, (model, limit, thrust, thrust_limit), trunc = replay_study(df, sessions)
    folds.to_csv(os.path.join(args.out, "replay_folds.csv"))
    by_action.to_csv(os.path.join(args.out, "replay_by_action.csv"))
    model.assign(limit=limit).to_csv(os.path.join(args.out, "strike_model.csv"), index=False)
    pd.DataFrame([{**thrust, "limit": thrust_limit}]).to_csv(os.path.join(args.out, "thrust_model.csv"), index=False)
    fig_old_vs_new(by_action, args.out)
    fig_distance(dec, args.out)

    # Sessions recorded on the current sketch's firmware (Stroke lines carry
    # kind=): does the hub decide what the replay decides, with the model
    # that's in the sketch? Older stroke-firmware sessions used other models.
    live = None
    fws = {}
    for log_dir in log_dirs:
        fws.update({s: g for s, g in firmware_strokes(log_dir).items() if g.kind_logged.any()})
    if fws:
        sk_model, sk_limit, sk_thrust, sk_thrust_limit = R.model_from_sketch(SKETCH)
        rep_live = pd.concat([R.replay_session(s) for s in sessions if s.name in fws], ignore_index=True)
        live = R.compare_live(R.decide(rep_live, sk_model, sk_limit, thrust=sk_thrust, thrust_max=sk_thrust_limit),
                              fws)
        live.to_csv(os.path.join(args.out, "live_vs_replay.csv"), index=False)

    loss = pd.DataFrame([{"session": s.name, **{f"lost_{k}": v for k, v in s.lost_batches.items()},
                          "hub_batches": s.hub.seq.nunique()} for s in sessions]).set_index("session")
    n_gap = df.groupby("session").has_gap.agg(["size", "sum"]).rename(columns={"size": "strokes",
                                                                               "sum": "with_gap"})
    loss = loss.join(n_gap)
    with open(os.path.join(args.out, "README.md"), "w", encoding="utf-8") as f:
        f.write("# Speed study (generated by speed_study.py)\n\n")
        f.write("## Firmware accept rate, real strikes\n\n" + md(rate, "{:.0%}") + "\n\n")
        f.write("Fakes / returns to guard accepted:\n\n" + fp.to_markdown() + "\n\n")
        f.write("## Gates failed by rejected strikes (count; a strike can fail several)\n\n"
                + fails.to_markdown() + "\n\n")
        f.write("## Share of strike rotation inside the fixed 150/350 ms trim (median)\n\n" + md(trim) + "\n\n")
        f.write("## Strike duration (median ms)\n\n" + md(dur, "{:.0f}") + "\n\n")
        f.write("## Feature ranking\n\nfast_over_slow = median over actions of fast ÷ slow medians "
                "(1 = speed-invariant). speed_eta2 = share of within-action variance explained by speed. "
                "AUCs folded (0.5 useless, 1 perfect).\n\n" + md(rank.sort_values("speed_eta2")) + "\n\n")
        f.write("## Leave-one-speed-out comparison\n\n" + md(cmp) + "\n\n")
        f.write("## Replay of the new firmware check (leave-one-speed-out)\n\nRates per held-out speed: "
                "strike = real strikes accepted, fake = fake strokes accepted, between = strokes between reps "
                "(returns to guard etc.) accepted. cutoffs = separate-cutoffs rule; template = "
                "nearest-template score (the firmware).\n\n" + md(folds) + "\n\n")
        f.write("Real strikes accepted per action (old firmware vs new, held-out speed):\n\n"
                + md(by_action) + "\n\n")
        f.write("Strikes cut short by the candidate close (share):\n\n" + md(trunc.to_frame("truncated"))
                + "\n\n")
        f.write(f"Final model (all sessions), limit {limit:g}:\n\n" + md(model.set_index("action")) + "\n\n")
        f.write(f"Thrust template (push strokes), limit {thrust_limit:g}:\n\n"
                + md(pd.DataFrame([thrust]).set_index("action")) + "\n\n")
        if live is not None and len(live):
            found = live[live.fw_found]
            agree = (found.replay_accept == found.fw_accept).mean() if len(found) else np.nan
            f.write("## Live firmware vs replay (stroke firmware sessions)\n\n"
                    f"{len(live)} replay strokes, {len(found)} matched to a firmware Stroke line; accept/reject "
                    f"agreement {agree:.0%}. Disagreements:\n\n"
                    + found[found.replay_accept != found.fw_accept].to_markdown(index=False) + "\n\n")
        f.write("## Stream loss\n\n" + loss.to_markdown() + "\n")
    print(open(os.path.join(args.out, "README.md"), encoding="utf-8").read())


if __name__ == "__main__":
    main()
