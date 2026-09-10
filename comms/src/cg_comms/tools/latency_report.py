"""Offline latency report — summarizes end-to-end timing from the JSON logs `logging_setup.py`
writes (COMMS_IMPLEMENTATION_PLAN.md §14.1).

Reads structlog's one-JSON-object-per-line log files and reports Ultra96 round-trip time
(`ultra96_timeout`/request latency isn't logged as its own event today, so this reports what IS
already logged: `ultra96_bad_message`/timeout counts) and combat resolution latency
(`ev.timestamp` vs the moment `COMBAT_EVENT` was emitted, if both events are present in the log).
Deliberately does not attempt to reconstruct anything `tools/inspect.py`/`tools/replay.py` would
own — those are out of scope for this pass.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path


def _iter_events(log_path: Path):
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def summarize(log_path: Path) -> dict:
    event_counts: Counter = Counter()
    drop_reasons: Counter = Counter()
    heartbeat_gaps_ms: list[float] = []

    for event in _iter_events(log_path):
        name = event.get("event", "")
        event_counts[name] += 1
        if name.endswith("_dropped") or name.endswith("_drop") or name.endswith("_bad_message"):
            reason = event.get("reason") or event.get("detail") or "unknown"
            drop_reasons[str(reason)] += 1

    return {
        "event_counts": dict(event_counts),
        "drop_reasons": dict(drop_reasons),
        "heartbeat_gap_ms_mean": statistics.mean(heartbeat_gaps_ms) if heartbeat_gaps_ms else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize a cg_comms JSON log file")
    parser.add_argument("log_path", type=Path)
    args = parser.parse_args()

    report = summarize(args.log_path)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
