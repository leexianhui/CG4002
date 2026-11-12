"""Mace/spear preparation and recovery signatures from the November P2 captures.

These checks supplement positive template matching: torso involvement alone
does not distinguish a committed attack from moving the weapon into position.
Keep the thresholds in sync with handlingReject() in both wrist sketches.
"""

import math


def rotation_reject(weapon, action, axis, push, peak, duration_ms):
    if weapon not in ("mace", "spear"):
        return None
    if (weapon == "spear" and action == "uppercut" and axis[0] >= 0.20
            and duration_ms >= 600):
        return "return"
    # Signed lift axis and backwards hand motion, including slower raises.
    # Apply to recorded diagonal patterns too: some old exemplars were lifts.
    if action in ("uppercut", "diagonal") and axis[2] >= 0.60:
        if (axis[0] <= 0.40 and axis[1] >= 0.20 and
                (push[2] <= (-0.10 if weapon == "mace" else -0.25) or
                 (weapon == "spear" and peak >= 400 and push[2] <= -0.08))):
            return "guard_raise"
        if peak < 300 and duration_ms >= 700:
            return "guard_raise"
        # Spear's short lift before a diagonal has little/forward hand push.
        if weapon == "spear" and axis[0] <= -0.45 and peak < 300:
            return "guard_raise"
    if action in ("vertical", "diagonal"):
        # Slow lowering after a ready-position lift is not a downward attack.
        if peak < 250 and duration_ms >= 700:
            return "return"
        # Spear uppercut recovery has a different signed axis from a real cut.
        # This also works when the preceding uppercut was missed or delayed.
        if (weapon == "spear" and push[1] >= 0.65 and
                sum(a * b for a, b in zip(axis, (-0.360, -0.290, -0.887))) >=
                math.cos(math.radians(15))):
            return "return"
        # p2_v10 spear pullback: the return rotates around the same signed
        # vertical axis as the template, but is much faster and has a strong
        # positive-Y hand vector.
        if (weapon == "spear" and peak >= 400 and duration_ms <= 450
                and axis[0] <= -0.25 and axis[1] <= -0.20 and axis[2] <= -0.80
                and push[1] >= 0.90):
            return "return"
    return None


# ---------------- Commitment gate (all weapons, both players) ----------------
# A real attack is a committed movement. These reject what the 2026-11-12 P1
# captures showed being accepted: wrist flicks, raising the weapon to the
# ready position, slow lowering/back-swings, and short flick "thrusts".
# Keep the constants and rules in sync with rotationCommitReject() /
# pushCommitReject() in both hub sketches (tests/test_handling.py checks).
STRIKE_MIN_ROT_DEG = 60.0          # flicks rotate 22-32 deg; no real stroke < 83
# Slow AND long = lowering / back-swing / raise. Two tiers: back-swings peak
# 150-300 dps over 640-1270 ms; real uppercuts can peak ~280-320 dps but finish
# within ~680 ms. Spins (>= SPIN_MIN_ROT_DEG) are long by nature and exempt.
SLOW_MAX_PEAK_DPS = 250.0
SLOW_MIN_DURATION_MS = 650.0
SLOWER_MAX_PEAK_DPS = 320.0
SLOWER_MIN_DURATION_MS = 750.0
SPIN_MIN_ROT_DEG = 250.0
RAISE_AXIS_MIN_Z = 0.60            # uppercut-shaped (lift) rotation axis
RAISE_MAX_ROT_DEG = 100.0
RAISE_MAX_TORSO_RATIO = 0.10
RAISE_MIN_TORSO_RATIO = 0.05       # below this a lift is never an uppercut
THRUST_MIN_REACH_M = 0.40          # flick "thrusts" reach 0.16-0.40 m
THRUST_MIN_PEAK_G = 0.90
THRUST_MAX_WRIST_DPS = 300.0       # flicks snap the wrist at 300-520 dps...
THRUST_FLICK_MAX_REACH_M = 0.70    # ...but travel <= ~0.55 m; hard real thrusts reach 0.60-0.99
CUT_MIN_TORSO_RATIO = 0.08         # real horizontal/diagonal cuts >= 0.10, flicks 0.03-0.07
CUT_TORSO_EXEMPT_ROT_DEG = 130.0   # ...except full-power cuts: P1 sword cuts 153-178 deg at
CUT_TORSO_EXEMPT_PEAK_DPS = 450.0  # 550-770 dps with torso 0.05-0.10; flicks turn 68-120 deg


def rotation_commit_reject(peak, rot, duration_ms, axis, torso_ratio):
    if rot < STRIKE_MIN_ROT_DEG:
        return "flick"
    if rot < SPIN_MIN_ROT_DEG and (
            (peak < SLOW_MAX_PEAK_DPS and duration_ms >= SLOW_MIN_DURATION_MS) or
            (peak < SLOWER_MAX_PEAK_DPS and duration_ms >= SLOWER_MIN_DURATION_MS)):
        return "slow"
    if axis[2] >= RAISE_AXIS_MIN_Z and (
            torso_ratio < RAISE_MIN_TORSO_RATIO or
            (rot < RAISE_MAX_ROT_DEG and torso_ratio < RAISE_MAX_TORSO_RATIO)):
        return "guard_raise"
    return None


def push_commit_reject(peak_g, reach_m, wrist_gyro_peak=None):
    if reach_m < THRUST_MIN_REACH_M or peak_g < THRUST_MIN_PEAK_G:
        return "short"
    if (wrist_gyro_peak is not None and wrist_gyro_peak > THRUST_MAX_WRIST_DPS and
            reach_m < THRUST_FLICK_MAX_REACH_M):
        return "flick"
    return None


def torso_quiet_cut(action, torso_ratio, rot=0.0, peak=0.0):
    """A horizontal/diagonal cut with almost no torso turn is a wrist flick,
    unless it is a full-power cut (arm-driven swings use little torso)."""
    if rot >= CUT_TORSO_EXEMPT_ROT_DEG and peak >= CUT_TORSO_EXEMPT_PEAK_DPS:
        return False
    return action in ("horizontal", "diagonal") and torso_ratio < CUT_MIN_TORSO_RATIO
