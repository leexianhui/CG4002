"""TLS WebSocket server accepting relay (/relay/<id>) and visualizer (/visualizer/<id>) connections
(COMMS_IMPLEMENTATION_PLAN.md §11).

Bearer-token auth happens before path routing; a duplicate connection to an already-occupied path
replaces the old one (closes it) rather than being rejected, since a relay/visualizer reconnect is
expected to look exactly like this.
"""

from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import structlog
import websockets
from websockets.server import WebSocketServerProtocol

from cg_comms.relay.backpressure import DropOldestQueue

logger = structlog.get_logger(__name__)

RELAY_PATHS: dict[str, int] = {"/relay/1": 1, "/relay/2": 2}
VISUALIZER_PATHS: dict[str, int] = {"/visualizer/1": 1, "/visualizer/2": 2}


def build_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


def check_bearer_token(headers: "websockets.Headers", expected: str) -> bool:
    return headers.get("Authorization", "") == f"Bearer {expected}"


@dataclass
class Connection:
    path: str
    websocket: WebSocketServerProtocol
    send_queue: "DropOldestQueue[str]"
    writer_task: "asyncio.Task[None] | None" = field(default=None, init=False)

    async def enqueue(self, payload: str) -> None:
        self.send_queue.put_nowait(payload)


@dataclass
class ConnectionRegistry:
    send_queue_depth: int
    _connections: dict[str, Connection] = field(default_factory=dict, init=False)

    async def register(self, path: str, websocket: WebSocketServerProtocol) -> Connection:
        existing = self._connections.get(path)
        if existing is not None:
            logger.warning("ws_duplicate_path_replaced", path=path)
            await self._close(existing)
        conn = Connection(
            path=path, websocket=websocket, send_queue=DropOldestQueue(maxsize=self.send_queue_depth)
        )
        conn.writer_task = asyncio.create_task(self._writer_loop(conn))
        self._connections[path] = conn
        return conn

    async def unregister(self, conn: Connection) -> None:
        if self._connections.get(conn.path) is conn:
            del self._connections[conn.path]
        await self._close(conn)

    async def _close(self, conn: Connection) -> None:
        if conn.writer_task is not None:
            conn.writer_task.cancel()
        try:
            await conn.websocket.close()
        except websockets.ConnectionClosed:
            pass

    async def _writer_loop(self, conn: Connection) -> None:
        while True:
            payload = await conn.send_queue.get()
            try:
                await conn.websocket.send(payload)
            except websockets.ConnectionClosed:
                return

    def connections_for_paths(self, paths: dict[str, int]) -> list[Connection]:
        return [conn for path, conn in self._connections.items() if path in paths]

    def connection_for_path(self, path: str) -> Connection | None:
        return self._connections.get(path)


@dataclass
class WsServerConfig:
    host: str
    port: int
    bearer_token: str
    send_queue_depth: int
    ssl_context: ssl.SSLContext | None


async def run_ws_server(
    config: WsServerConfig,
    registry: ConnectionRegistry,
    on_message: Callable[[str, str], Awaitable[None]],
    stop_event: asyncio.Event,
) -> None:
    """Serves until `stop_event` is set. `on_message(path, raw_json)` fires for every inbound text
    frame on a recognized relay/visualizer path."""

    async def handler(websocket: WebSocketServerProtocol) -> None:
        path = websocket.request.path
        if path not in RELAY_PATHS and path not in VISUALIZER_PATHS:
            logger.warning("ws_unknown_path", path=path)
            await websocket.close(code=1008, reason="unknown path")
            return
        if not check_bearer_token(websocket.request.headers, config.bearer_token):
            logger.warning("ws_auth_rejected", path=path)
            await websocket.close(code=1008, reason="unauthorized")
            return

        conn = await registry.register(path, websocket)
        # "Did the relay/phone actually attach?" is the first question at demo time and the engine
        # had no answer: websockets' own "connection open" line carries no path, so four connections
        # were four identical lines. Log the path, which identifies who arrived.
        logger.info("ws_client_connected", path=path)
        try:
            async for raw in websocket:
                text = raw if isinstance(raw, str) else raw.decode("utf-8")
                await on_message(path, text)
        except websockets.ConnectionClosed:
            pass
        finally:
            await registry.unregister(conn)
            logger.info("ws_client_disconnected", path=path)

    async with websockets.serve(handler, config.host, config.port, ssl=config.ssl_context):
        logger.info("ws_server_listening", host=config.host, port=config.port, tls=config.ssl_context is not None)
        await stop_event.wait()
