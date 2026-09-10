"""Game Engine process entrypoint (COMMS_IMPLEMENTATION_PLAN.md §10, §11).

Wires the WSS server, connection registry, and single-writer GameStateTask together: inbound
frames are routed to typed events and submitted to the task; the task's callbacks push outbound
COMBAT_EVENT/MATCH_STATE broadcasts and per-relay COMBAT_FEEDBACK back through the same registry.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

import structlog

from cg_comms.config import EngineSettings
from cg_comms.engine.broadcaster import broadcast_to_visualizers, send_to_relay
from cg_comms.engine.game_state_task import GameStateTask
from cg_comms.engine.inbound import route_inbound
from cg_comms.engine.rules import Rules
from cg_comms.engine.ws_server import ConnectionRegistry, WsServerConfig, build_ssl_context, run_ws_server
from cg_comms.logging_setup import configure_logging
from cg_comms.messages.models import CombatEvent, CombatFeedback
from cg_comms.messages.models import MatchState as WireMatchState

logger = structlog.get_logger(__name__)


async def run_engine(settings: EngineSettings) -> None:
    rules = Rules.load()
    registry = ConnectionRegistry(send_queue_depth=settings.send_queue_depth)
    stop_event = asyncio.Event()

    async def on_combat_event(event: CombatEvent) -> None:
        await broadcast_to_visualizers(registry, event)

    async def on_match_state(state: WireMatchState) -> None:
        await broadcast_to_visualizers(registry, state)

    async def on_combat_feedback(player_id: int, feedback: CombatFeedback) -> None:
        await send_to_relay(registry, player_id, feedback)

    task = GameStateTask(
        rules=rules,
        reorder_delay_ms=rules.config.reorder_delay_ms,
        late_drop_ms=settings.late_drop_ms,
        on_combat_event=on_combat_event,
        on_match_state=on_match_state,
        on_combat_feedback=on_combat_feedback,
    )

    async def on_message(path: str, raw: str) -> None:
        event = route_inbound(path, raw)
        if event is not None:
            await task.submit(event)

    ssl_context = build_ssl_context(settings.cert_path, settings.key_path) if settings.use_tls else None
    ws_config = WsServerConfig(
        host=settings.host,
        port=settings.port,
        bearer_token=settings.bearer_token,
        send_queue_depth=settings.send_queue_depth,
        ssl_context=ssl_context,
    )

    await task.start_match()
    await asyncio.gather(
        run_ws_server(ws_config, registry, on_message, stop_event),
        task.run(stop_event),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="CG4002 Game Engine")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--log-level", default="INFO", help="Level for the JSONL log file")
    parser.add_argument(
        "--console-level",
        default=None,
        help="Level for the console mirror; defaults to --log-level.",
    )
    args = parser.parse_args()

    settings = EngineSettings.from_yaml(args.config)
    log_path = configure_logging(
        "engine",
        settings.logs_dir,
        level=getattr(logging, args.log_level.upper()),
        console_level=(
            None if args.console_level is None else getattr(logging, args.console_level.upper())
        ),
    )
    logger.info(
        "engine_starting",
        listen=f"{settings.host}:{settings.port}",
        tls=settings.use_tls,
        log_path=str(log_path),
    )

    asyncio.run(run_engine(settings))


if __name__ == "__main__":
    main()
