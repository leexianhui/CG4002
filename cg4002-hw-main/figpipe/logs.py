"""Loads listener CSVs (rx_time, source, line) into tidy pandas tables.

Session  = one CSV written by listener.py (debug_*.csv) or result_listener.py
           (result_*.csv).
Labels   = ground truth, which the boards can't know:
           1. the listener's --label suffix, e.g. debug_20261001_101500_vertical.csv,
              labels the whole session when it names a known label;
           2. logs/labels.csv (session,start,end,label) labels time ranges and
              overrides (1). start/end are laptop times, HH:MM:SS[.fff] on the
              session's date, or full ISO timestamps.
Excluded = basenames listed in logs/exclude.txt (one per line, # comments).
"""

import csv
import glob
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from .parsers import parse_line

REAL_ACTIONS = ["vertical", "horizontal", "diagonal", "uppercut", "thrust", "spin"]
FAKE_ACTIONS = ["fake_flick", "fake_handling", "fake_walking", "fake_body_turn", "guard_raise"]
ACTION_LABELS = REAL_ACTIONS + FAKE_ACTIONS
HOLD_LABELS = ["hold_sword_shield", "hold_spear", "hold_mace"]
OTHER_LABELS = ["transition"]
ALL_LABELS = ACTION_LABELS + HOLD_LABELS + OTHER_LABELS

SESSION_RE = re.compile(r"^(debug|result)_(\d{8}_\d{6})(?:_(.+))?\.csv$")


def label_from_suffix(suffix):
    """'vertical' / 'vertical_round2' -> 'vertical'; unknown -> None. Longest match wins."""
    if not suffix:
        return None
    # The listener label used for this recording is more descriptive than the
    # canonical fake-action name used by the classifier.  Keep both spellings
    # usable so the session is included in offline fitting and validation.
    if suffix == "fake_guard_raise" or suffix.startswith("fake_guard_raise_"):
        return "guard_raise"
    for lab in sorted(ALL_LABELS, key=len, reverse=True):
        if suffix == lab or suffix.startswith(lab + "_"):
            return lab
    return None


SPEEDS = ["slow", "normal", "fast"]


def speed_from_suffix(suffix):
    """'vertical_slow' / 'vertical_slow_round2' -> 'slow'; no speed tag -> None."""
    if not suffix:
        return None
    parts = suffix.split("_")
    return next((p for p in parts if p in SPEEDS), None)


def read_exclude(logs_dir):
    path = os.path.join(logs_dir, "exclude.txt")
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {ln.split("#", 1)[0].strip() for ln in f if ln.split("#", 1)[0].strip()}


def _parse_when(text, day):
    text = text.strip()
    if "T" in text or "-" in text:
        return pd.Timestamp(text)
    return pd.Timestamp(f"{day} {text}")


def read_label_ranges(logs_dir):
    """labels.csv -> DataFrame(session, start, end, label). Unknown labels are an error."""
    path = os.path.join(logs_dir, "labels.csv")
    cols = ["session", "start", "end", "label"]
    if not os.path.exists(path):
        return pd.DataFrame(columns=cols)
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r.get("session") or r["session"].startswith("#"):
                continue
            lab = r["label"].strip()
            if lab not in ALL_LABELS:
                raise ValueError(f"labels.csv: unknown label {lab!r} (known: {', '.join(ALL_LABELS)})")
            m = SESSION_RE.match(r["session"].strip())
            day = datetime.strptime(m.group(2), "%Y%m%d_%H%M%S").date() if m else None
            rows.append({"session": r["session"].strip(), "start": _parse_when(r["start"], day),
                         "end": _parse_when(r["end"], day), "label": lab})
    return pd.DataFrame(rows, columns=cols)


@dataclass
class Dataset:
    sessions: list = field(default_factory=list)   # dicts: name, kind, path, label
    excluded: list = field(default_factory=list)
    lines: pd.DataFrame = None     # every parsed line: session, rx_time, kind, node, fields...
    events: pd.DataFrame = None    # one row per hub Motion ENDED, with label + seg dump
    parse_errors: list = field(default_factory=list)


def _label_at(session, t, file_label, ranges):
    if len(ranges):
        hit = ranges[(ranges.session == session) & (ranges.start <= t) & (ranges.end >= t)]
        if len(hit):
            return hit.iloc[-1].label
    return file_label


def load(logs_dir):
    ds = Dataset()
    exclude = read_exclude(logs_dir)
    ranges = read_label_ranges(logs_dir)
    records = []
    for path in sorted(glob.glob(os.path.join(logs_dir, "**", "*.csv"), recursive=True)):
        name = os.path.basename(path)
        m = SESSION_RE.match(name)
        if not m:
            continue
        if name in exclude:
            ds.excluded.append(name)
            continue
        kind, suffix = m.group(1), m.group(3)
        file_label = label_from_suffix(suffix)
        ds.sessions.append({"name": name, "kind": kind, "path": path, "label": file_label, "suffix": suffix})
        cfg = {}  # latest [cfg] per node, tags the lines that follow
        with open(path, newline="", encoding="utf-8") as f:
            for n, row in enumerate(csv.DictReader(f)):
                try:
                    d = parse_line(row["line"])
                except ValueError as e:
                    ds.parse_errors.append(f"{name}:{n + 2}: {e}")
                    continue
                if d is None:
                    continue
                t = pd.Timestamp(row["rx_time"])
                if d["kind"] == "cfg":
                    cfg[d["node"]] = d
                c = cfg.get(d["node"], {})
                d.update({"session": name, "stream": kind, "rx_time": t, "row": n,
                          "wifi_sleep": c.get("wifi_sleep"), "latency_test": c.get("latency_test"),
                          "label": _label_at(name, t, file_label, ranges)})
                records.append(d)
    ds.lines = pd.DataFrame(records)
    if ds.lines.empty:
        ds.lines = pd.DataFrame(columns=["session", "stream", "rx_time", "row", "kind", "node", "label"])
    ds.events = assemble_events(ds.lines)
    return ds


def assemble_events(lines):
    """Groups each hub motion into one row: STARTED -> [candidate] closing ->
    Motion ENDED -> [seg] dump -> REJECTED?. The segment dump is kept as a
    DataFrame in the 'seg' column (None if DEBUG_SEGMENT_DUMP was off)."""
    hub = lines[(lines.node == "right_wrist") & (lines.stream == "debug")] if len(lines) else lines
    events = []
    for session, g in hub.groupby("session", sort=False) if len(hub) else []:
        started = closing = None
        cur = None
        for _, r in g.sort_values("row").iterrows():
            k = r.kind
            if k == "motion_started":
                started, closing, cur = r.t, None, None
            elif k == "candidate_closing":
                closing = r.reason
            elif k == "motion_ended":
                cur = {c: r[c] for c in r.index if not (isinstance(r[c], float) and pd.isna(r[c]))}
                cur.update({"startedMs": started, "closeReason": closing, "segRows": [], "rejectText": None})
                events.append(cur)
                started = closing = None
            elif k == "seg" and cur is not None:
                cur["segRows"].append({c: r[c] for c in ("i", "inTrim", "t", "ax", "ay", "az", "gx", "gy", "gz",
                                                        "gyroMag", "jerk")})
            elif k == "rejected" and cur is not None:
                cur["rejectText"] = r.text
    for e in events:
        rows = e.pop("segRows")
        e["seg"] = pd.DataFrame(rows) if rows else None
    return pd.DataFrame(events)


def label_class(label):
    if label in REAL_ACTIONS:
        return "real"
    if label in FAKE_ACTIONS:
        return "fake"
    return None
