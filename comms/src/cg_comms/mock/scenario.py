"""Canned event timelines for the mock layer (COMMS_IMPLEMENTATION_PLAN.md §13).

A scenario is a flat, ordered list of (delay_ms, kind, payload) steps loaded from YAML. Each mock
(`firebeetle_sim.py`, `ultra96_sim.py`, `visualizer_sim.py`) interprets `kind`/`payload` in whatever
shape its own protocol needs — this module only owns the timeline and file format, not the meaning
of any particular step.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cg_comms.config import load_yaml


@dataclass(frozen=True)
class ScenarioStep:
    delay_ms: int
    kind: str
    payload: dict[str, Any]


def load_scenario(path: Path) -> list[ScenarioStep]:
    raw = load_yaml(path)
    steps_raw = raw.get("steps", [])
    return [
        ScenarioStep(
            delay_ms=int(step["delay_ms"]), kind=str(step["kind"]), payload=step.get("payload", {})
        )
        for step in steps_raw
    ]
