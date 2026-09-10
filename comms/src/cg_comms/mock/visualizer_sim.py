"""Visualizer simulator — WSS client standing in for the real visualizer UI
(COMMS_IMPLEMENTATION_PLAN.md §13.3, §11).

Sends POSITION updates from a scenario timeline and logs every broadcast message the engine sends
back (COMBAT_EVENT / MATCH_STATE / COOLDOWN_STATE) — enough to exercise the engine's WSS server and
broadcaster without a real UI.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import logging
import ssl
from pathlib import Path

import structlog
import websockets

from cg_comms.clock import monotonic_ms
from cg_comms.logging_setup import configure_logging
from cg_comms.messages.models import CombatEvent, MatchState, Position
from cg_comms.messages.validate import MessageDropped, parse_app_message
from cg_comms.mock.scenario import load_scenario

logger = structlog.get_logger(__name__)


async def _recv_loop(ws: "websockets.WebSocketClientProtocol") -> None:
    async for raw in ws:
        parsed = parse_app_message(raw)
        if isinstance(parsed, MessageDropped):
            logger.warning("visualizer_sim_bad_message", reason=parsed.reason.value, detail=parsed.detail)
            continue
        # This process stands in for the phone, so it is where a human confirms the whole chain
        # worked. A bare type name can't do that: "CombatEvent" and "MatchState" look identical
        # whether the engine adjudicated a 10-damage sword hit or echoed an unchanged 100/100. Log
        # the fields that actually prove adjudication happened.
        if isinstance(parsed, CombatEvent):
            logger.info(
                "visualizer_sim_combat_event",
                attacker=parsed.attacker_id,
                defender=parsed.defender_id,
                weapon=parsed.weapon.value,
                action=parsed.action.value,
                outcome=parsed.outcome.value,
                damage=parsed.damage,
                attacker_hp=parsed.attacker_hp,
                defender_hp=parsed.defender_hp,
                range_m=round(parsed.range_m, 2),
            )
        elif isinstance(parsed, MatchState):
            logger.info(
                "visualizer_sim_match_state",
                phase=parsed.phase.value,
                hp_p1=parsed.hp.get("1"),
                hp_p2=parsed.hp.get("2"),
                winner=parsed.winner_id,
            )
        else:
            logger.info("visualizer_sim_received", msg_type=type(parsed).__name__)


async def run_visualizer_sim(
    url: str, bearer_token: str, player_id: int, scenario_path: Path, ca_cert_path: str | None = None
) -> None:
    steps = load_scenario(scenario_path)
    ssl_context = None
    if url.startswith("wss://"):
        ssl_context = ssl.create_default_context(cafile=ca_cert_path) if ca_cert_path else ssl.create_default_context()
    async with websockets.connect(
        url, additional_headers={"Authorization": f"Bearer {bearer_token}"}, ssl=ssl_context
    ) as ws:
        recv_task = asyncio.create_task(_recv_loop(ws))
        try:
            for step in itertools.cycle(steps):
                await asyncio.sleep(step.delay_ms / 1000)
                if step.kind != "position":
                    logger.warning("visualizer_sim_unknown_step_kind", kind=step.kind)
                    continue
                position = Position(
                    timestamp=int(step.payload.get("timestamp", monotonic_ms())),
                    player_id=player_id,  # type: ignore[arg-type]
                    x=float(step.payload["x"]),
                    y=float(step.payload["y"]),
                    heading=float(step.payload["heading"]),
                )
                await ws.send(position.model_dump_json(by_alias=True))
        except websockets.ConnectionClosed:
            logger.warning("visualizer_sim_connection_closed")
        finally:
            recv_task.cancel()


def main() -> None:
    parser = argparse.ArgumentParser(description="Visualizer simulator")
    parser.add_argument("--url", required=True, help="wss://host:port/visualizer/<id>")
    parser.add_argument("--bearer-token", required=True)
    parser.add_argument("--player-id", type=int, required=True)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--ca-cert", type=str, default=None, help="path to a CA cert to trust (e.g. certs/engine.crt for the self-signed dev cert)")
    parser.add_argument("--logs-dir", type=Path, default=Path("logs"))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    configure_logging(
        f"visualizer-sim-p{args.player_id}", args.logs_dir, level=getattr(logging, args.log_level.upper())
    )
    logger.info("visualizer_sim_starting", url=args.url, player_id=args.player_id)

    asyncio.run(run_visualizer_sim(args.url, args.bearer_token, args.player_id, args.scenario, args.ca_cert))


if __name__ == "__main__":
    main()
