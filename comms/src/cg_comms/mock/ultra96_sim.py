"""Ultra96 inference-service simulator — line-delimited JSON TCP server standing in for the real
Ultra96 (COMMS_IMPLEMENTATION_PLAN.md §13.2, §8).

Speaks the exact same wire protocol as the real service (relay/ultra96.py's docstring: relay code
never knows which one it's talking to). Classification is entirely canned, in one of these ways:

- `--scenario` (default when neither flag is given): each incoming SENSOR_WINDOW is answered from a
  cyclic scenario of ACTION_RESULT payloads, unconditionally — used by the no-hardware mock rig and
  the e2e tests.
- `--trigger-map` (docs/DEMO_TRIGGERED_ACTION_PLAN.md §5.2): each incoming SENSOR_WINDOW is answered
  from a (weapon, triggerId)-keyed table, so a button press always classifies as the row the player
  actually pressed instead of the next thing in a cycle.
- `--random`: each incoming SENSOR_WINDOW is answered with a uniformly random move drawn from THAT
  window's weapon's move set in shared/config/weapons.yaml. Drawing from the weapon's own moves (not
  from all of ActionClass) is deliberate: a (weapon, action) pair the weapon lacks is resolved by
  the engine as IGNORED, which would look like a random event that never reached the visualizer.
  Confidence is drawn above every confidence gate for the same reason (checked at startup).
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import logging
import random
from pathlib import Path
from typing import Any

import structlog

from cg_comms.config import RulesConfig, load_yaml
from cg_comms.logging_setup import configure_logging
from cg_comms.messages.enums import ActionClass, GuardState
from cg_comms.messages.models import ActionResult, SensorWindow
from cg_comms.messages.validate import MessageDropped, parse_app_message
from cg_comms.mock.scenario import ScenarioStep, load_scenario

logger = structlog.get_logger(__name__)


def _default_steps() -> list[ScenarioStep]:
    return [
        ScenarioStep(
            delay_ms=0, kind="action_result", payload={"action": "IDLE", "action_confidence": 1.0}
        )
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


# Random-mode confidence range. The lower bound must stay strictly above the relay's
# ultra96.min_confidence and the engine's match.yaml min_confidence (both 0.6 today), or a random
# event is silently dropped by a gate; `RandomClassifier.for_rules` refuses to start if it is not.
_RANDOM_CONFIDENCE_RANGE = (0.70, 0.99)


class RandomClassifier:
    """Answers each window with a random move valid for that window's weapon."""

    def __init__(self, moves: dict[str, list[str]], rng: random.Random) -> None:
        self._moves = moves
        self._rng = rng

    @classmethod
    def for_rules(cls, rules: RulesConfig, rng: random.Random) -> RandomClassifier:
        low, _high = _RANDOM_CONFIDENCE_RANGE
        if low <= rules.min_confidence:
            raise ValueError(
                f"random confidence floor {low} must exceed match.yaml min_confidence "
                f"{rules.min_confidence}, or random actions would be filtered before the visualizer"
            )
        # sorted(): dict order is YAML order today, but a stable order makes a seeded run
        # reproducible regardless of how the YAML happens to be laid out.
        moves = {weapon: sorted(m) for weapon, m in rules.moves.items() if m}
        return cls(moves, rng)

    def classify(self, window: SensorWindow) -> tuple[ActionResult, dict]:
        choices = self._moves.get(window.weapon.value)
        if not choices:
            # A window for a weapon with no moves cannot produce a resolvable action; answer IDLE
            # (which the relay forwards but the engine never turns into ActionRecognized) and say
            # so, rather than inventing an invalid pair.
            row: dict[str, Any] = {"action": "IDLE", "action_confidence": 1.0}
            return _row_to_action_result(
                row, timestamp=window.timestamp, player_id=window.player_id, request_id=window.request_id
            ), {"random": "no_moves_for_weapon"}
        row = {
            "action": self._rng.choice(choices),
            "action_confidence": round(self._rng.uniform(*_RANDOM_CONFIDENCE_RANGE), 3),
            "guard_state": "IDLE",
            "guard_confidence": 1.0,
        }
        result = _row_to_action_result(
            row, timestamp=window.timestamp, player_id=window.player_id, request_id=window.request_id
        )
        return result, {"random": f"{len(choices)} moves for {window.weapon.value}"}


def _classify_from_trigger_map(
    trigger_map: dict[str, Any], window: SensorWindow
) -> tuple[ActionResult, dict]:
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
    random_classifier: RandomClassifier | None = None,
) -> None:
    cycle = itertools.cycle(steps if steps else _default_steps())
    loop = asyncio.get_running_loop()
    try:
        while True:
            line = await reader.readline()
            if not line:
                return
            if not line.endswith(b"\n"):
                logger.warning("ultra96_sim_bad_message", reason="unterminated_line")
                return
            received_at = loop.time()
            # Bytes: invalid UTF-8 is one MALFORMED_JSON drop, not a torn-down relay connection.
            parsed = parse_app_message(line.strip())
            if isinstance(parsed, MessageDropped):
                logger.warning(
                    "ultra96_sim_bad_message", reason=parsed.reason.value, detail=parsed.detail
                )
                continue
            if not isinstance(parsed, SensorWindow):
                logger.warning("ultra96_sim_unexpected_type", msg_type=type(parsed).__name__)
                continue

            if random_classifier is not None:
                result, match_info = random_classifier.classify(parsed)
            elif trigger_map is not None:
                result, match_info = _classify_from_trigger_map(trigger_map, parsed)
            else:
                step = next(cycle)
                result = _row_to_action_result(
                    step.payload,
                    timestamp=parsed.timestamp,
                    player_id=parsed.player_id,
                    request_id=parsed.request_id,
                )
                match_info = {"matched_row": step.payload}

            line_out = result.model_dump_json(by_alias=True) + "\n"
            prepared_at = loop.time()
            writer.write(line_out.encode("utf-8"))
            await writer.drain()
            sent_at = loop.time()
            # The classifier stand-in is the one hop with no other observer: the relay logs the
            # window going out and the action coming back, but nothing showed that this process
            # is alive and cycling the scenario. Without this, a wedged sim is indistinguishable
            # from a relay that stopped producing windows.
            logger.info(
                "ultra96_sim_classified",
                request_id=parsed.request_id,
                weapon=parsed.weapon.value,
                player_id=parsed.player_id,
                trigger_id=parsed.trigger_id,
                action=result.action.value,
                action_confidence=result.action_confidence,
                guard_state=result.guard_state.value,
                window_timestamp_ms=parsed.timestamp,
                processing_ms=round((prepared_at - received_at) * 1000, 3),
                write_drain_ms=round((sent_at - prepared_at) * 1000, 3),
                service_ms=round((sent_at - received_at) * 1000, 3),
                **match_info,
            )
    except (ConnectionResetError, OSError, ValueError, UnicodeError):
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--scenario", type=Path, default=None)
    mode.add_argument(
        "--trigger-map",
        type=Path,
        default=None,
        help="Classify from SENSOR_WINDOW's (weapon, triggerId) via this table "
        "(docs/DEMO_TRIGGERED_ACTION_PLAN.md §5.2) instead of cycling --scenario.",
    )
    mode.add_argument(
        "--random",
        action="store_true",
        help="Answer every window with a random move valid for its weapon "
        "(shared/config/weapons.yaml). Exclusive with --scenario and --trigger-map.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Seed for --random; omitted => fresh seed, logged so the run can be replayed.",
    )
    parser.add_argument("--logs-dir", type=Path, default=Path("logs"))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    configure_logging("ultra96-sim", args.logs_dir, level=getattr(logging, args.log_level.upper()))

    steps = load_scenario(args.scenario) if args.scenario is not None else []
    trigger_map = load_trigger_map(args.trigger_map) if args.trigger_map is not None else None
    random_classifier: RandomClassifier | None = None
    seed: int | None = None
    if args.random:
        seed = args.seed if args.seed is not None else random.SystemRandom().getrandbits(64)
        random_classifier = RandomClassifier.for_rules(RulesConfig.load(), random.Random(seed))
    logger.info(
        "ultra96_sim_starting",
        host=args.host,
        port=args.port,
        scenario_steps=len(steps),
        trigger_map=str(args.trigger_map) if args.trigger_map is not None else None,
        random=args.random,
        seed=seed,
    )

    async def _main() -> None:
        server = await asyncio.start_server(
            lambda r, w: _handle_client(r, w, steps, trigger_map, random_classifier),
            args.host,
            args.port,
        )
        async with server:
            await server.serve_forever()

    asyncio.run(_main())


if __name__ == "__main__":
    main()
