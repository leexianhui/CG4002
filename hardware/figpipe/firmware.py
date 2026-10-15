"""Reads tunable constants straight out of the .ino sources, and re-implements
the firmware's own pose maths, so figures follow every retune without edits.

Nothing here hard-codes a threshold value: if a constant is renamed or
removed, FirmwareError says which one and which file, instead of a figure
silently drawing a stale line.
"""

import math
import os
import re

import numpy as np

NODE_FILES = {
    "right_wrist": "right_wrist_game.ino",
    "left_wrist": "left_wrist_game.ino",
    "torso": "torso_game.ino",
}

# Global constants the figures depend on, per node.
REQUIRED = {
    "right_wrist": [
        "START_THRESHOLD", "JERK_START_THRESHOLD", "TORSO_GATE_MAX_NORMAL", "TORSO_GATE_MAX_SPIN",
        "MIN_TORSO_ENGAGEMENT", "RAW_MAX_DURATION_MS", "SWING_MAX_REVERSALS", "SPIN_MAX_REVERSALS",
        "SWING_MIN_SUSTAIN_FRACTION", "THRUST_MAX_ROTATION_DEG", "STILLNESS_GYRO_THRESHOLD_DPS",
        "WEAPON_MATCH_MAX_DEG", "WEAPON_MATCH_MARGIN_DEG", "WEAPON_LOCK_HOLD_MS", "WEAPON_DROPOUT_GRACE_MS",
        "SETTLE_FRACTION", "PRE_PEAK_MS", "POST_PEAK_MS", "PRETRIGGER_SAMPLES", "GYRO_SCALE",
        "DEBUG_PORT", "LINK_PORT", "POSE_PORT", "RESULT_PORT", "HUB_STATUS_INTERVAL_MS",
        "SAMPLE_PERIOD_US", "POSE_LOG_INTERVAL_MS", "EMA_ALPHA", "STILLNESS_HOLD_MS", "POSE_FRESHNESS_MS",
    ],
    "left_wrist": ["STILLNESS_GYRO_THRESHOLD_DPS", "POSE_BROADCAST_INTERVAL_MS", "EMA_ALPHA"],
    "torso": ["STILLNESS_GYRO_THRESHOLD_DPS", "POSE_BROADCAST_INTERVAL_MS", "BEACON_INTERVAL_MS", "EMA_ALPHA"],
}

# Function-local constants the gates use (right wrist only).
REQUIRED_LOCAL = {
    "isValidAttack": ["MIN_PEAK_GYRO_DPS", "MIN_PEAK_JERK_G", "MIN_DURATION", "MAX_DURATION", "MIN_SAMPLES"],
    "matchesKnownPattern": ["MIN_MEANINGFUL_ROTATION", "MIN_MEANINGFUL_JERK"],
    "computeShapeExtras": ["REVERSAL_DEADBAND_DPS"],
}

WEAPON_NAMES = {"SWORD_SHIELD": "sword_shield", "SPEAR": "spear", "MACE": "mace"}


class FirmwareError(RuntimeError):
    pass


_NUM = r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
CONST_RE = re.compile(
    r"^\s*(?:static\s+)?const\s+(?:unsigned\s+long|unsigned\s+int|unsigned|long|int|float|double|"
    r"uint\d+_t|int\d+_t)\s+(\w+)\s*=\s*(" + _NUM + r")[uUlLfF]*\s*;")
DEFINE_RE = re.compile(r"^\s*#define\s+(\w+)\s+(" + _NUM + r"|true|false)[uUlLfF]*\b")
STATIC_ASSERT_RE = re.compile(r"static_assert\(sizeof\((\w+)\)\s*==\s*(\d+)")
REF_ROW_RE = re.compile(r"\{\s*WEAPON_(\w+)\s*,\s*(" + _NUM + r")\s*,\s*(" + _NUM + r")\s*,\s*("
                        + _NUM + r")\s*,\s*(" + _NUM + r")\s*\}")
SPIN_RE = re.compile(r"spinLikely\s*=\s*rawShape\.totalRotation\s*>\s*(" + _NUM + r")")


def _num(text):
    if text in ("true", "false"):
        return text == "true"
    v = float(text)
    return int(v) if re.fullmatch(r"[-+]?\d+", text) else v


def _strip_code(line):
    """Drops // comments and string literals so brace counting isn't fooled."""
    line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
    line = re.sub(r"'(?:\\.|[^'\\])*'", "''", line)
    return line.split("//", 1)[0]


def parse_source(src):
    """Returns (globals, defines, locals_by_function, static_asserts) for one sketch.

    globals: `const T NAME = value;` at brace depth 0.
    defines: `#define NAME value` (first definition wins, so an #ifndef default
             is what's reported).
    locals_by_function: {function name: {NAME: value}} for consts inside bodies.
    """
    consts, defines, local, sizes = {}, {}, {}, {}
    depth, func = 0, None
    in_block_comment = False
    for raw in src.splitlines():
        line = raw
        if in_block_comment:
            if "*/" not in line:
                continue
            line = line.split("*/", 1)[1]
            in_block_comment = False
        if "/*" in line and "*/" not in line.split("/*", 1)[1]:
            in_block_comment = True
            line = line.split("/*", 1)[0]

        if m := DEFINE_RE.match(line):
            defines.setdefault(m.group(1), _num(m.group(2)))
        if m := STATIC_ASSERT_RE.search(line):
            sizes[m.group(1)] = int(m.group(2))
        if m := CONST_RE.match(line):
            if depth == 0:
                consts[m.group(1)] = _num(m.group(2))
            elif func:
                local.setdefault(func, {})[m.group(1)] = _num(m.group(2))

        code = _strip_code(line)
        if depth == 0:
            fm = re.match(r"^\s*[\w:<>\*&\s]+?\b(\w+)\s*\([^;]*$", code)
            if fm and fm.group(1) not in ("if", "for", "while", "switch"):
                func = fm.group(1)
        depth += code.count("{") - code.count("}")
        if depth == 0 and "}" in code:
            func = None
    return consts, defines, local, sizes


def parse_weapon_refs(src):
    m = re.search(r"WEAPON_REFS\[\]\s*=\s*\{(.*?)\};", src, re.S)
    if not m:
        raise FirmwareError("WEAPON_REFS table not found")
    refs = []
    for w, rel, raz, lel, laz in REF_ROW_RE.findall(m.group(1)):
        refs.append({"weapon": WEAPON_NAMES.get(w, w.lower()), "rEl": float(rel), "rAz": float(raz),
                     "lEl": float(lel), "lAz": float(laz)})
    if not refs:
        raise FirmwareError("WEAPON_REFS table has no parsable rows")
    return refs


class Firmware:
    """Constants for all three nodes. Access: fw.c('right_wrist', 'START_THRESHOLD')."""

    def __init__(self, firmware_dir):
        self.dir = firmware_dir
        self.paths, self.values, self.local, self.sizes = {}, {}, {}, {}
        for node, fname in NODE_FILES.items():
            path = os.path.join(firmware_dir, fname)
            if not os.path.exists(path):
                raise FirmwareError(f"{path} not found")
            with open(path, encoding="utf-8", errors="replace") as f:
                src = f.read()
            consts, defines, local, sizes = parse_source(src)
            merged = dict(defines)
            merged.update(consts)
            self.paths[node] = path
            self.values[node] = merged
            self.local[node] = local
            self.sizes.update(sizes)
            if node == "right_wrist":
                self.weapon_refs = parse_weapon_refs(src)
                sm = SPIN_RE.search(src)
                if not sm:
                    raise FirmwareError("spinLikely threshold (rawShape.totalRotation > N) not found")
                self.values[node]["SPIN_RAW_ROTATION_DEG"] = _num(sm.group(1))

        for node, names in REQUIRED.items():
            for n in names:
                if n not in self.values[node]:
                    raise FirmwareError(f"{n} not found in {NODE_FILES[node]}")
        for func, names in REQUIRED_LOCAL.items():
            body = self.local["right_wrist"].get(func, {})
            for n in names:
                if n not in body:
                    raise FirmwareError(f"{n} not found inside {func}() in {NODE_FILES['right_wrist']}")

    def c(self, node, name):
        return self.values[node][name]

    def rw(self, name):
        return self.values["right_wrist"][name]

    def fn(self, func, name):
        return self.local["right_wrist"][func][name]

    def gyro_full_scale_dps(self, node="right_wrist"):
        return 32768.0 / self.c(node, "GYRO_SCALE")


# ---------------- Firmware pose maths (right_wrist_game.ino) ----------------

def grav_from_el_az(el_deg, az_deg):
    """gravFromElAz(): unit vector for an (el, az) pair in degrees."""
    el, az = np.radians(el_deg), np.radians(az_deg)
    return np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])


def grav_angle_deg(a, b):
    """gravAngleDeg(): real angle between two unit gravity directions."""
    return math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(a, b))))))


def weapon_distance(r_el, r_az, l_el, l_az, ref):
    """classifyWeapon()'s per-reference distance: sqrt(rightAngle^2 + leftAngle^2)."""
    d_r = grav_angle_deg(grav_from_el_az(r_el, r_az), grav_from_el_az(ref["rEl"], ref["rAz"]))
    d_l = grav_angle_deg(grav_from_el_az(l_el, l_az), grav_from_el_az(ref["lEl"], ref["lAz"]))
    return math.hypot(d_r, d_l), d_r, d_l


def classify_weapon(r_el, r_az, l_el, l_az, refs, max_deg, margin_deg):
    """classifyWeapon(): nearest weapon if it's within max_deg and at least
    margin_deg closer than the runner-up weapon. Returns (weapon or None,
    nearest, dist, margin); margin is 999 with a single weapon, as on the board."""
    per = {}
    for ref in refs:
        d = weapon_distance(r_el, r_az, l_el, l_az, ref)[0]
        per[ref["weapon"]] = min(d, per.get(ref["weapon"], math.inf))
    ranked = sorted(per.items(), key=lambda kv: kv[1])
    nearest, dist = ranked[0]
    margin = ranked[1][1] - dist if len(ranked) > 1 else 999.0
    ok = dist <= max_deg and margin >= margin_deg
    return (nearest if ok else None), nearest, dist, margin


def el_az_from_accel(ax, ay, az):
    """New pose encoding: el = asin(z), az = atan2(y, x) of the unit accel vector."""
    n = np.sqrt(ax * ax + ay * ay + az * az)
    return np.degrees(np.arcsin(np.clip(az / n, -1, 1))), np.degrees(np.arctan2(ay, ax))


def pitch_roll_from_accel(ax, ay, az):
    """Old pose encoding (still used by torso_game.ino): pitch = atan2(ay, az), roll = atan2(-ax, az)."""
    return np.degrees(np.arctan2(ay, az)), np.degrees(np.arctan2(-ax, az))
