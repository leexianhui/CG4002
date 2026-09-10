"""One-shot JSON serialize + fan-out to registered connections (COMMS_IMPLEMENTATION_PLAN.md §11.2).

`dump_app_message` runs exactly once per call; the resulting string is shared by every matching
connection's queue — no reserialization per recipient, and each connection's own DropOldestQueue
absorbs backpressure independently.
"""

from __future__ import annotations

from cg_comms.engine.ws_server import VISUALIZER_PATHS, ConnectionRegistry
from cg_comms.messages.models import AppMessage
from cg_comms.messages.validate import dump_app_message


async def broadcast_to_visualizers(registry: ConnectionRegistry, message: AppMessage) -> None:
    payload = dump_app_message(message)
    for conn in registry.connections_for_paths(VISUALIZER_PATHS):
        await conn.enqueue(payload)


async def send_to_relay(registry: ConnectionRegistry, player_id: int, message: AppMessage) -> None:
    payload = dump_app_message(message)
    conn = registry.connection_for_path(f"/relay/{player_id}")
    if conn is not None:
        await conn.enqueue(payload)
