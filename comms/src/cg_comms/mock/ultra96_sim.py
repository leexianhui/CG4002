"""Ultra96 inference-service simulator — line-delimited JSON TCP server standing in for the real
Ultra96 (COMMS_IMPLEMENTATION_PLAN.md §13.2, §8).

Speaks the exact same wire protocol as the real service (relay/ultra96.py's docstring: relay code
never knows which one it's talking to). Classification is entirely canned, in one of two ways:

- `--scenario` (default when neither flag is given): each incoming SENSOR_WINDOW is answered from a
  cyclic scenario of ACTION_RESULT payloads, unconditionally — used by the no-hardware mock rig and
  the e2e tests.
- `--trigger-map` (docs/DEMO_TRIGGERED_ACTION_PLAN.md §5.2): each incoming SENSOR_WINDOW is answered
  from a (weapon, triggerId)-keyed table, so a button press always classifies as the row the player
  actually pressed instead of the next thing in a cycle.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import logging
from pathlib import Path
from typing import Any

import structlog

from cg_comms.config import load_yaml
from cg_comms.logging_setup import configure_logging
from cg_comms.messages.enums import ActionClass, GuardState
from cg_comms.messages.models import ActionResult, SensorWindow
from cg_comms.messages.validate import MessageDropped, parse_app_message
from cg_comms.mock.scenario import ScenarioStep, load_scenario

logger = structlog.get_logger(__name__)


def _default_steps() -> list[ScenarioStep]:
    return [
        ScenarioStep(delay_ms=0, kind="action_result", payload={"action": "IDLE", "action_confidence": 1.0})
    ]


def load_trigger_map(path: Path) -> dict[str, Any]:
    """Loads a demo_trigger_map.yaml (§5.2): `{"default": {...}, "triggers": {WEAPON: {id: {...}}}}`.
    Row dicts are kept as raw payload dicts, same shape as ScenarioStep.payload, so both paths in
    `_classify` share the same `.get(...)`-with-defaults construction of ActionResult."""
    raw = load_yaml(path)
    return {"default": raw.get("default", {}), "triggers": raw.get("triggers", {})}


def _row_to_action_result(
    row: dict[str, Any], *, timestamp: int, player_id: int, request_id: int
) -> ActionResult:
    return ActionResult(
        timestamp=timestamp,
        player_id=player_id,  # type: ignore[arg-type]
        request_id=request_id,
        action=ActionClass(row.get("action", "IDLE")),
        action_confidence=float(row.get("action_confidence", 1.0)),
        guard_state=GuardState(row.get("guard_state", "IDLE")),
        guard_confidence=float(row.get("guard_confidence", 1.0)),
    )


def _classify_from_trigger_map(trigger_map: dict[str, Any], window: SensorWindow) -> tuple[ActionResult, dict]:
    weapon_rows = trigger_map["triggers"].get(window.weapon.value, {})
    row = weapon_rows.get(window.trigger_id) if window.trigger_id is not None else None
    matched = row is not None
    if row is None:
        row = trigger_map["default"]
    result = _row_to_action_result(
        row, timestamp=window.timestamp, player_id=window.player_id, request_id=window.request_id
    )
    return result, {"matched_row": row if matched else "default"}


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    steps: list[ScenarioStep],
    trigger_map: dict[str, Any] | None,
) -> None:
    cycle = itertools.cycle(steps if steps else _default_steps())
    try:
        while True:
            line = await reader.readline()
            if not line:
                return
            parsed = parse_app_message(line.decode("utf-8").strip())
            if isinstance(parsed, MessageDropped):
                logger.warning("ultra96_sim_bad_message", reason=parsed.reason.value, detail=parsed.detail)
                continue
            if not isinstance(parsed, SensorWindow):
                logger.warning("ultra96_sim_unexpected_type", msg_type=type(parsed).__name__)
                continue

            if trigger_map is not None:
                result, match_info = _classify_from_trigger_map(trigger_map, parsed)
            else:
                step = next(cycle)
                result = _row_to_action_result(
                    step.payload, timestamp=parsed.timestamp, player_id=parsed.player_id,
                    request_id=parsed.request_id,
                )
                match_info = {"matched_row": step.payload}

            line_out = result.model_dump_json(by_alias=True) + "\n"
            writer.write(line_out.encode("utf-8"))
            await writer.drain()
            # The classifier stand-in is the one hop with no other observer: the relay logs the
            # window going out and the action coming back, but nothing showed that this process
            # is alive and cycling the scenario. Without this, a wedged sim is indistinguishable
            # from a relay that stopped producing windows.
            logger.info(
                "ultra96_sim_classified",
                request_id=parsed.request_id,
                player_id=parsed.player_id,
                trigger_id=parsed.trigger_id,
                action=result.action.value,
                action_confidence=result.action_confidence,
                guard_state=result.guard_state.value,
                **match_info,
            )
    except (ConnectionResetError, OSError):
        logger.warning("ultra96_sim_client_disconnected")
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 2)
        except (OSError, asyncio.TimeoutError):
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Ultra96 inference service simulator")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--scenario", type=Path, default=None)
    parser.add_argument(
        "--trigger-map", type=Path, default=None,
        help="Classify from SENSOR_WINDOW's (weapon, triggerId) via this table "
        "(docs/DEMO_TRIGGERED_ACTION_PLAN.md §5.2) instead of cycling --scenario.",
    )
    parser.add_argument("--logs-dir", type=Path, default=Path("logs"))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    configure_logging("ultra96-sim", args.logs_dir, level=getattr(logging, args.log_level.upper()))

    steps = load_scenario(args.scenario) if args.scenario is not None else []
    trigger_map = load_trigger_map(args.trigger_map) if args.trigger_map is not None else None
    logger.info(
        "ultra96_sim_starting", host=args.host, port=args.port, scenario_steps=len(steps),
        trigger_map=str(args.trigger_map) if args.trigger_map is not None else None,
    )

    async def _main() -> None:
        server = await asyncio.start_server(
            lambda r, w: _handle_client(r, w, steps, trigger_map), args.host, args.port
        )
        async with server:
            await server.serve_forever()

    asyncio.run(_main())


if __name__ == "__main__":
    main()
