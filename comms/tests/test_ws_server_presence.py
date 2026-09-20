"""Regression tests for E6: ConnectionRegistry.relay_presence() and run_ws_server's on_connect/
on_disconnect hooks (docs/DEMO_TRIGGERED_ACTION_PLAN.md §7.3/§7.4).
"""
from __future__ import annotations

import asyncio
import socket

import pytest
import websockets

from cg_comms.engine.ws_server import ConnectionRegistry, WsServerConfig, run_ws_server

BEARER_TOKEN = "presence-test-token"


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_until_connectable(host: str, port: int, timeout: float = 5.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last_exc: Exception | None = None
    while loop.time() < deadline:
        try:
            _reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError as exc:
            last_exc = exc
            await asyncio.sleep(0.05)
    raise TimeoutError(f"{host}:{port} not connectable within {timeout}s") from last_exc


@pytest.mark.asyncio
async def test_relay_presence_reflects_two_connected_clients() -> None:
    host, port = "127.0.0.1", _free_tcp_port()
    registry = ConnectionRegistry(send_queue_depth=8)
    stop_event = asyncio.Event()

    async def on_message(_path: str, _raw: str) -> None:
        return None

    ws_config = WsServerConfig(
        host=host, port=port, bearer_token=BEARER_TOKEN, send_queue_depth=8, ssl_context=None
    )
    server_task = asyncio.create_task(run_ws_server(ws_config, registry, on_message, stop_event))
    try:
        await _wait_until_connectable(host, port)

        assert registry.relay_presence() == {"1": False, "2": False}

        async with websockets.connect(
            f"ws://{host}:{port}/relay/1", additional_headers={"Authorization": f"Bearer {BEARER_TOKEN}"}
        ):
            async with websockets.connect(
                f"ws://{host}:{port}/relay/2",
                additional_headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
            ):
                await asyncio.sleep(0.1)  # let both handler coroutines reach registry.register
                assert registry.relay_presence() == {"1": True, "2": True}

            await asyncio.sleep(0.1)  # let /relay/2's handler reach registry.unregister
            assert registry.relay_presence() == {"1": True, "2": False}
    finally:
        stop_event.set()
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_on_connect_and_on_disconnect_hooks_fire_with_path() -> None:
    host, port = "127.0.0.1", _free_tcp_port()
    registry = ConnectionRegistry(send_queue_depth=8)
    stop_event = asyncio.Event()
    connected_paths: list[str] = []
    disconnected_paths: list[str] = []

    async def on_message(_path: str, _raw: str) -> None:
        return None

    async def on_connect(path: str) -> None:
        connected_paths.append(path)

    async def on_disconnect(path: str) -> None:
        disconnected_paths.append(path)

    ws_config = WsServerConfig(
        host=host, port=port, bearer_token=BEARER_TOKEN, send_queue_depth=8, ssl_context=None
    )
    server_task = asyncio.create_task(
        run_ws_server(ws_config, registry, on_message, stop_event, on_connect=on_connect, on_disconnect=on_disconnect)
    )
    try:
        await _wait_until_connectable(host, port)

        async with websockets.connect(
            f"ws://{host}:{port}/visualizer/1",
            additional_headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
        ):
            await asyncio.sleep(0.1)
            assert connected_paths == ["/visualizer/1"]
            assert disconnected_paths == []

        await asyncio.sleep(0.1)
        assert disconnected_paths == ["/visualizer/1"]
    finally:
        stop_event.set()
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):
            pass
