"""Game Engine process entrypoint (COMMS_IMPLEMENTATION_PLAN.md §10, §11).

Wires the WSS server, connection registry, and single-writer GameStateTask together: inbound
frames are routed to typed events and submitted to the task; the task's callbacks push outbound
COMBAT_EVENT/MATCH_STATE broadcasts and per-relay COMBAT_FEEDBACK back through the same registry.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

import structlog

from cg_comms.config import EngineSettings
from cg_comms.engine.broadcaster import broadcast_to_visualizers, send_to_relay
from cg_comms.engine.game_state_task import GameStateTask
from cg_comms.engine.inbound import LinkReset, ResetMatch, route_inbound
from cg_comms.engine.rules import Rules
from cg_comms.engine.ws_server import (
    CONTROL_PATH,
    RELAY_PATHS,
    VISUALIZER_PATHS,
    ConnectionRegistry,
    WsServerConfig,
    build_ssl_context,
    run_ws_server,
)
from cg_comms.logging_setup import configure_logging
from cg_comms.messages.models import ActionRecognized, CombatEvent, CombatFeedback
from cg_comms.messages.models import MatchState as WireMatchState
from cg_comms.messages.validate import dump_app_message

logger = structlog.get_logger(__name__)


async def run_engine(settings: EngineSettings) -> None:
    rules = Rules.load()
    if settings.reorder_delay_ms != rules.config.reorder_delay_ms:
        logger.warning("engine_reorder_config_ignored", configured=settings.reorder_delay_ms,
                       effective=rules.config.reorder_delay_ms, source="shared/config/match.yaml")
    registry = ConnectionRegistry(send_queue_depth=settings.send_queue_depth)
    stop_event = asyncio.Event()

    async def on_combat_event(event: CombatEvent) -> None:
        await broadcast_to_visualizers(registry, event)

    async def on_match_state(state: WireMatchState) -> None:
        await broadcast_to_visualizers(registry, state)
        if state.reason == "reset":
            control = registry.connection_for_path(CONTROL_PATH)
            if control is not None:
                await control.enqueue(dump_app_message(state))

    async def on_combat_feedback(player_id: int, feedback: CombatFeedback) -> None:
        await send_to_relay(registry, player_id, feedback)

    async def on_action_recognized(event: ActionRecognized) -> None:
        await broadcast_to_visualizers(registry, event)

    task = GameStateTask(
        rules=rules,
        reorder_delay_ms=rules.config.reorder_delay_ms,
        late_drop_ms=settings.late_drop_ms,
        on_combat_event=on_combat_event,
        on_match_state=on_match_state,
        on_combat_feedback=on_combat_feedback,
        on_action_recognized=on_action_recognized,
        on_weapon_changed=lambda event: broadcast_to_visualizers(registry, event),
        presence=registry.relay_presence,
    )

    async def on_message(path: str, raw: str) -> None:
        if path == CONTROL_PATH:
            try:
                command = json.loads(raw)
            except ValueError:
                command = None
            if command == {"type": "RESET_MATCH", "version": 1}:
                logger.info("match_reset_requested", path=path)
                await task.submit(ResetMatch())
            else:
                logger.warning("control_command_rejected")
            return
        event = route_inbound(path, raw)
        if event is not None:
            await task.submit(event)

    async def on_connect(path: str) -> None:
        if path in RELAY_PATHS or path in VISUALIZER_PATHS:
            pid = RELAY_PATHS.get(path, VISUALIZER_PATHS.get(path))
            await task.submit(LinkReset(pid, "action" if path in RELAY_PATHS else "position"))
        if path in VISUALIZER_PATHS:
            # §7.4: a phone that attaches after the match has been running otherwise sees only
            # placeholder text until the next HP change, which in a P1-only run never comes.
            conn = registry.connection_for_path(path)
            if conn is not None:
                await conn.enqueue(dump_app_message(task.current_match_state()))
        # Any connect (relay or visualizer) can flip `MATCH_STATE.connected` or is otherwise worth
        # re-announcing, so broadcast the current snapshot to both visualizers immediately rather
        # than waiting for the next game event.
        await broadcast_to_visualizers(registry, task.current_match_state())

    async def on_disconnect(path: str) -> None:
        await broadcast_to_visualizers(registry, task.current_match_state())

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
        run_ws_server(ws_config, registry, on_message, stop_event, on_connect=on_connect, on_disconnect=on_disconnect),
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
