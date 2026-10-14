"""One parser per firmware log-line format. Each takes the full raw line (with
its "[tag]" prefix) and returns a dict, or None if the line isn't that format.

parse_line() tries them all and adds "kind" and "node" keys.
"""

import re

PREFIX_RE = re.compile(r"^\[(wrist|torso|left_wrist|right_wrist)\]\s?(.*)$", re.S)
# Board prefix -> node name. The hub tags its debug stream "[wrist]" and its
# result stream "[right_wrist]".
NODE_OF_TAG = {"wrist": "right_wrist", "right_wrist": "right_wrist", "torso": "torso", "left_wrist": "left_wrist"}

KV_RE = re.compile(r"(\w+)=(\[[^\]]*\]|[^\s|()]+)")


def _val(s):
    if re.fullmatch(r"[-+]?\d+", s):
        return int(s)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?", s) or s in ("nan", "inf", "-inf"):
        return float(s)
    return s


def kv(text):
    return {k: _val(v) for k, v in KV_RE.findall(text)}


def split_prefix(line):
    """'[torso] foo' -> ('torso', 'foo'); unprefixed -> (None, line)."""
    m = PREFIX_RE.match(line.rstrip("\r\n"))
    return (m.group(1), m.group(2)) if m else (None, line.rstrip("\r\n"))


ENDED_KEYS = ["weapon", "raw", "rawDur", "trimmed", "peak", "dur", "totalRot", "rawTotalRot", "peakAccel",
              "axes", "sustainFrac", "reversals", "rampMs", "magOK", "shapeOK", "spinLikely", "thrustLikely",
              "swingShapeOK", "spinReversalOK", "torsoPeak", "torsoDataOK", "torsoCeilingOK",
              "torsoEngagementOK", "torsoOK", "rawDurationOK"]


def parse_motion_ended(line):
    tag, body = split_prefix(line)
    if not body.startswith("Motion ENDED."):
        return None
    d = kv(body)
    missing = [k for k in ENDED_KEYS if k not in d]
    if missing:
        raise ValueError(f"Motion ENDED line missing {missing}: {line!r}")
    a, b = (int(x) for x in d.pop("trimmed").strip("[]").split(","))
    d["trimStart"], d["trimEnd"] = a, b
    if "sent" in d:  # whole raw buffer for a spin, else the trim; absent in older firmware
        d["sentStart"], d["sentEnd"] = (int(x) for x in d.pop("sent").strip("[]").split(","))
    return d


# Keys on every Stroke line. Rotation strokes (kind=rot) add px/py/pz,
# torsoRot, torsoRatio, dPush, dTorso; push strokes (kind=push) add reach and
# dReach. Lines from before push strokes have no kind (all rotation).
STROKE_KEYS = ["t0", "t1", "peak", "rot", "ax", "ay", "az", "beacons", "nearest", "dist", "dAxis", "dRot",
               "reason", "accept"]


def parse_stroke(line):
    """Hub 'Stroke i=N ...' — one per stroke in a candidate (stroke + template
    firmware; see onMotionEnd())."""
    tag, body = split_prefix(line)
    m = re.match(r"^Stroke i=(\d+) (.*)$", body)
    if not m:
        return None
    d = kv(m.group(2))
    missing = [k for k in STROKE_KEYS if k not in d]
    if missing:
        raise ValueError(f"Stroke line missing {missing}: {line!r}")
    d["i"] = int(m.group(1))
    # The stroke's own kind (rot/push). Stored as stroke_kind: parse_line()
    # uses "kind" for the line type. Lines from before push strokes have none.
    d["kind_logged"] = "kind" in d
    d["stroke_kind"] = d.pop("kind", "rot")
    return d


def parse_candidate_ended(line):
    """Hub 'Candidate ENDED ...' — closes each candidate (stroke firmware)."""
    tag, body = split_prefix(line)
    if not body.startswith("Candidate ENDED "):
        return None
    d = kv(body)
    for k in ("raw", "rawDur", "strokes", "accepted", "reason"):
        if k not in d:
            raise ValueError(f"Candidate ENDED line missing {k}: {line!r}")
    return d


SEG_RE = re.compile(r"^\[seg\] i=(\d+)(\*?)\s+t=(\d+)\s+(.*)$")


def parse_seg(line):
    """Hub '[seg] i=N[*] t=...' and torso '[seg] i=N t=...' (no trim mark)."""
    tag, body = split_prefix(line)
    m = SEG_RE.match(body)
    if not m:
        return None
    d = kv(m.group(4))
    d.update({"i": int(m.group(1)), "inTrim": m.group(2) == "*", "t": int(m.group(3))})
    return d


def parse_candidate_closing(line):
    tag, body = split_prefix(line)
    m = re.match(r"^\[candidate\] closing: (settled|timeout|bufferFull)\s*$", body)
    return {"reason": m.group(1)} if m else None


def parse_motion_started(line):
    tag, body = split_prefix(line)
    m = re.match(r"^Motion STARTED at (\d+)\s*$", body)
    return {"t": int(m.group(1))} if m else None


def parse_raw_sample(line):
    """'[raw] t=ms ax= ... jerk= [state=ACTIVE|IDLE]' (hub + torso DEBUG_RAW_STREAM)."""
    tag, body = split_prefix(line)
    if not body.startswith("[raw] t="):
        return None
    return kv(body)


def parse_hub_pose(line):
    """Hub pose stream: 'raw rEl= rAz= rGyroMag= rStable= | left el= az= fresh= stable= |
    torso pitch= roll= fresh= stable= | nearest=X [dist=F] candidate=X [heldMs=N]'."""
    tag, body = split_prefix(line)
    if not body.startswith("raw rEl="):
        return None
    parts = [p.strip() for p in body.split("|")]
    if len(parts) != 4:
        return None
    d = kv(parts[0])
    d.update({"l_" + k: v for k, v in kv(parts[1]).items()})
    d.update({"t_" + k: v for k, v in kv(parts[2]).items()})
    d.update(kv(parts[3]))
    d.setdefault("dist", None)
    d.setdefault("heldMs", None)
    return d


def parse_left_raw(line):
    """Left wrist: 'raw el= az= gyroMag= stable= gx= gy= gz= imuFails=N' (gyroMag is the EMA)."""
    tag, body = split_prefix(line)
    if not body.startswith("raw el="):
        return None
    return kv(body)


def parse_torso_pose(line):
    """Torso: 'raw pitch= roll= gyroMag= stable=' (gyroMag is the EMA)."""
    tag, body = split_prefix(line)
    if not body.startswith("raw pitch="):
        return None
    return kv(body)


def parse_rejected(line):
    tag, body = split_prefix(line)
    if not body.startswith("REJECTED"):
        return None
    return {"text": body[len("REJECTED"):].lstrip(" —-").strip()}


def parse_result(line):
    """Port-4213 milestone lines."""
    tag, body = split_prefix(line)
    body = body.strip()
    if body == "all_nodes_ready":
        return {"event": "all_nodes_ready"}
    if m := re.match(r"^action_accepted (.*)$", body):
        return {"event": "action_accepted", **kv(m.group(1))}
    if m := re.match(r"^weapon_locked=(\w+)$", body):
        return {"event": "weapon_locked", "weapon": m.group(1)}
    if m := re.match(r"^weapon_candidate_lost \(was (\w+) (.*)\)$", body):
        return {"event": "weapon_candidate_lost", "was": m.group(1), **kv(m.group(2))}
    if m := re.match(r"^weapon_candidate=(\w+) (.*)$", body):
        return {"event": "weapon_candidate", "weapon": m.group(1), **kv(m.group(2))}
    return None


STATS_RE = re.compile(r"^\[stats\] to=(\w+) from=(\w+) type=(\w+) rx=(\d+) lost=(\d+)\s*$")


def parse_stats(line):
    tag, body = split_prefix(line)
    m = STATS_RE.match(body)
    if not m:
        return None
    return {"to": m.group(1), "from": m.group(2), "type": m.group(3), "rx": int(m.group(4)),
            "lost": int(m.group(5))}


def parse_rtt(line):
    tag, body = split_prefix(line)
    m = re.match(r"^\[rtt\] us=(\d+)\s*$", body)
    return {"us": int(m.group(1))} if m else None


def parse_beacon_iat(line):
    tag, body = split_prefix(line)
    m = re.match(r"^\[beacon_iat\] us=([\d,]+)\s*$", body)
    return {"us": [int(v) for v in m.group(1).split(",") if v]} if m else None


def parse_cfg(line):
    tag, body = split_prefix(line)
    if not body.startswith("[cfg] "):
        return None
    d = kv(body)
    return d if "wifi_sleep" in d else None


def parse_lock_ack(line):
    """Torso / left wrist: 'Weapon locked by hub: weapon=N — ...' (N = WeaponType enum)."""
    tag, body = split_prefix(line)
    m = re.match(r"^Weapon locked by hub: weapon=(\d+)", body)
    return {"weaponId": int(m.group(1))} if m else None


PARSERS = [
    ("motion_ended", parse_motion_ended),
    ("stroke", parse_stroke),
    ("candidate_ended", parse_candidate_ended),
    ("seg", parse_seg),
    ("candidate_closing", parse_candidate_closing),
    ("motion_started", parse_motion_started),
    ("raw_sample", parse_raw_sample),
    ("hub_pose", parse_hub_pose),
    ("left_raw", parse_left_raw),
    ("torso_pose", parse_torso_pose),
    ("rejected", parse_rejected),
    ("result", parse_result),
    ("stats", parse_stats),
    ("rtt", parse_rtt),
    ("beacon_iat", parse_beacon_iat),
    ("cfg", parse_cfg),
    ("lock_ack", parse_lock_ack),
]


def parse_line(line):
    """Returns {'kind', 'node', ...fields} or None for lines no figure uses."""
    tag, _ = split_prefix(line)
    for kind, fn in PARSERS:
        d = fn(line)
        if d is not None:
            d["kind"] = kind
            d["node"] = NODE_OF_TAG.get(tag)
            return d
    return None
