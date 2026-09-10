"""Ultra96 inference-service simulator — line-delimited JSON TCP server standing in for the real
Ultra96 (COMMS_IMPLEMENTATION_PLAN.md §13.2, §8).

Speaks the exact same wire protocol as the real service (relay/ultra96.py's docstring: relay code
never knows which one it's talking to). Classification is entirely canned: each incoming
SENSOR_WINDOW is answered from a cyclic scenario of ACTION_RESULT payloads rather than by running
a model.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import logging
from pathlib import Path

import structlog

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


async def _handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, steps: list[ScenarioStep]
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

            step = next(cycle)
            result = ActionResult(
                timestamp=parsed.timestamp,
                player_id=parsed.player_id,
                request_id=parsed.request_id,
                action=ActionClass(step.payload.get("action", "IDLE")),
                action_confidence=float(step.payload.get("action_confidence", 1.0)),
                guard_state=GuardState(step.payload.get("guard_state", "IDLE")),
                guard_confidence=float(step.payload.get("guard_confidence", 1.0)),
            )
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
                action=result.action.value,
                action_confidence=result.action_confidence,
                guard_state=result.guard_state.value,
            )
    except (ConnectionResetError, OSError):
        logger.warning("ultra96_sim_client_disconnected")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ultra96 inference service simulator")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--scenario", type=Path, default=None)
    parser.add_argument("--logs-dir", type=Path, default=Path("logs"))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    configure_logging("ultra96-sim", args.logs_dir, level=getattr(logging, args.log_level.upper()))

    steps = load_scenario(args.scenario) if args.scenario is not None else []
    logger.info("ultra96_sim_starting", host=args.host, port=args.port, scenario_steps=len(steps))

    async def _main() -> None:
        server = await asyncio.start_server(lambda r, w: _handle_client(r, w, steps), args.host, args.port)
        async with server:
            await server.serve_forever()

    asyncio.run(_main())


if __name__ == "__main__":
    main()
