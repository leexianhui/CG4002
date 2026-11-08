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
