"""Re-evaluates every validity gate of onMotionEnd() from the features logged
on a Motion ENDED line, using the CURRENT firmware constants.

The firmware's REJECTED line only names the first failure; this reports all
of them. torsoDataOK can't be recomputed (it depends on the beacon ring
buffer), so the logged flag is used as-is.
"""

import pandas as pd

# Display order. The first NAMED_BY_REJECT gates are the ones the firmware's
# REJECTED line names, in its if/else order; the rest print a bare "REJECTED".
GATES = [
    ("torso_data", "Torso data present"),
    ("torso_engagement", "Torso engagement (floor)"),
    ("raw_duration", "Raw duration cap"),
    ("swing_shape", "Swing shape"),
    ("spin_reversal", "Spin reversals"),
    ("torso_ceiling", "Torso ceiling"),
    ("magnitude", "Magnitude (isValidAttack)"),
    ("pattern", "Pattern (matchesKnownPattern)"),
]
NAMED_BY_REJECT = 5

# Logged flag for each recomputed gate, for the drift check.
LOGGED_FLAG = {
    "torso_ceiling": "torsoCeilingOK", "torso_engagement": "torsoEngagementOK", "raw_duration": "rawDurationOK",
    "swing_shape": "swingShapeOK", "spin_reversal": "spinReversalOK", "magnitude": "magOK", "pattern": "shapeOK",
}


def evaluate(e, fw):
    """e: one Motion ENDED record (dict/Series). Returns {gate: bool, 'accept': bool, ...}."""
    spin = e["rawTotalRot"] > fw.rw("SPIN_RAW_ROTATION_DEG")
    thrust = e["totalRot"] < fw.rw("THRUST_MAX_ROTATION_DEG")
    n_trim = e["trimEnd"] - e["trimStart"] + 1

    magnitude = (fw.fn("isValidAttack", "MIN_DURATION") <= e["dur"] <= fw.fn("isValidAttack", "MAX_DURATION")
                 and n_trim >= fw.fn("isValidAttack", "MIN_SAMPLES")
                 and e["peak"] >= fw.fn("isValidAttack", "MIN_PEAK_GYRO_DPS")
                 and e["peakAccel"] >= fw.fn("isValidAttack", "MIN_PEAK_JERK_G"))
    pattern = (e["totalRot"] > fw.fn("matchesKnownPattern", "MIN_MEANINGFUL_ROTATION")
               or e["peakAccel"] > fw.fn("matchesKnownPattern", "MIN_MEANINGFUL_JERK"))
    ceiling = fw.rw("TORSO_GATE_MAX_SPIN") if spin else fw.rw("TORSO_GATE_MAX_NORMAL")

    g = {
        "torso_data": bool(e["torsoDataOK"]),
        "torso_engagement": bool(spin or e["torsoPeak"] >= fw.rw("MIN_TORSO_ENGAGEMENT")),
        "torso_ceiling": bool(e["torsoPeak"] < ceiling),
        "raw_duration": bool(e["rawDur"] <= fw.rw("RAW_MAX_DURATION_MS")),
        "swing_shape": bool(spin or thrust or (e["reversals"] <= fw.rw("SWING_MAX_REVERSALS")
                                               and e["sustainFrac"] >= fw.rw("SWING_MIN_SUSTAIN_FRACTION"))),
        "spin_reversal": bool((not spin) or e["reversals"] <= fw.rw("SPIN_MAX_REVERSALS")),
        "magnitude": bool(magnitude),
        "pattern": bool(pattern),
    }
    g["accept"] = all(g[k] for k, _ in GATES)
    g["spin_bucket"] = bool(spin)
    g["thrust_bucket"] = bool(thrust and not spin)
    g["bucket"] = "spin" if spin else ("thrust" if thrust else "swing")
    g["logged_accept"] = bool(e["magOK"] and e["shapeOK"] and e["torsoOK"] and e["rawDurationOK"]
                              and e["swingShapeOK"] and e["spinReversalOK"])
    g["drift"] = [k for k, flag in LOGGED_FLAG.items() if bool(e[flag]) != g[k]]
    return g


def evaluate_all(events, fw):
    if events is None or events.empty:
        return pd.DataFrame()
    rows = [evaluate(e, fw) for _, e in events.iterrows()]
    return pd.concat([events.reset_index(drop=True), pd.DataFrame(rows)], axis=1)
