"""Real localhost TCP/WS fault tests, plus BLE-lifetime isolation via the production relay loop."""

import asyncio
import socket
from unittest.mock import AsyncMock

import pytest
import websockets

from cg_comms.messages.models import Action, ActionResult, SensorWindow
from cg_comms.relay.engine_link import EngineLinkClient
from cg_comms.relay.ultra96 import Ultra96Client, Ultra96Unavailable


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def until(predicate, timeout=3):
    async def wait():
        while not predicate():
            await asyncio.sleep(.005)
    await asyncio.wait_for(wait(), timeout)


def window(pid=1, request_id=1):
    return SensorWindow(timestamp=100, player_id=pid, request_id=request_id, weapon="SWORD",
                        window_start_ts=0, window_end_ts=100, sample_rate_hz=50,
                        dominant=[[0.] * 6], offhand=[[0.] * 6], stale_pct=0)


def action(pid=1):
    return Action(timestamp=100, player_id=pid, weapon="SWORD", action="HORIZONTAL_SWING",
                  action_confidence=.9, guard_state="IDLE", guard_confidence=1)


@pytest.mark.parametrize("pid", [1, 2])
async def test_classifier_unavailable_start_then_three_close_restart_cycles(pid):
    port = free_port()
    client = Ultra96Client("127.0.0.1", port, 100, 2)
    client._worker.backoff_s = (.02, .04)
    active = set()
    max_active = 0

    async def handler(reader, writer):
        nonlocal max_active
        active.add(writer)
        max_active = max(max_active, len(active))
        try:
            while line := await reader.readline():
                received = SensorWindow.model_validate_json(line)
                assert received.player_id == pid
                result = ActionResult(timestamp=received.timestamp, player_id=received.player_id,
                                      request_id=received.request_id, action="IDLE", action_confidence=1,
                                      guard_state="IDLE", guard_confidence=1)
                writer.write((result.model_dump_json(by_alias=True) + "\n").encode())
                await writer.drain()
        finally:
            active.discard(writer)
            writer.close()
            await writer.wait_closed()

    client.start()
    worker = client._worker.task
    server = None
    try:
        await asyncio.sleep(.05)
        assert not client._worker.connected.is_set()
        with pytest.raises(Ultra96Unavailable):
            await client.request(window(pid))
        for cycle in range(3):
            for _ in range(3):
                client.start()
                assert client._worker.task is worker
            server = await asyncio.start_server(handler, "127.0.0.1", port)
            await until(client._worker.connected.is_set)
            assert (await client.request(window(pid, cycle))).player_id == pid
            server.close()
            for writer in list(active):
                writer.close()
            await asyncio.wait_for(server.wait_closed(), 1)
            await until(lambda: not client._worker.connected.is_set() and not active)
            server = None
        assert max_active == 1
        assert len(client._pending) == 0
    finally:
        await client.disconnect()
        if server:
            server.close()
            for writer in list(active):
                writer.close()
            await asyncio.wait_for(server.wait_closed(), 1)
        assert worker.done()
        assert client._worker.task is None


@pytest.mark.parametrize("pid", [1, 2])
async def test_engine_unavailable_start_then_three_close_restart_cycles(pid):
    port = free_port()
    client = EngineLinkClient(f"ws://127.0.0.1:{port}/relay/{pid}", "test", AsyncMock())
    client._worker.backoff_s = (.02, .04)
    received = []
    active = set()
    max_active = 0

    async def handler(ws):
        nonlocal max_active
        assert ws.request.path == f"/relay/{pid}"
        assert ws.request.headers["Authorization"] == "Bearer test"
        active.add(ws)
        max_active = max(max_active, len(active))
        try:
            async for raw in ws:
                received.append(Action.model_validate_json(raw))
        finally:
            active.discard(ws)

    client.start()
    worker = client._worker.task
    try:
        await asyncio.sleep(.05)
        for _ in range(100):
            await client.send_action(action(pid))
        assert client._outbox.empty()
        for cycle in range(3):
            client.start()
            assert client._worker.task is worker
            async with websockets.serve(handler, "127.0.0.1", port):
                await until(client._worker.connected.is_set)
                await client.send_action(action(pid))
                await until(lambda expected=cycle + 1: len(received) == expected)
                assert received[-1].player_id == pid
            await until(lambda: not client._worker.connected.is_set())
        assert max_active == 1
    finally:
        await client.disconnect()
        assert worker.done()
        assert client._outbox.empty()


async def test_classifier_silent_blackhole_times_out_then_reconnects():
    port = free_port()
    closed = asyncio.Event()

    async def silent(reader, writer):
        try:
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    async with await asyncio.start_server(silent, "127.0.0.1", port):
        client = Ultra96Client("127.0.0.1", port, 30, 1)
        try:
            await client.connect()
            assert await client.request(window()) is None
            await asyncio.wait_for(closed.wait(), 1)
            await until(lambda: not client._worker.connected.is_set())
            assert client.timeouts == 1 and not client._pending
        finally:
            await client.disconnect()


async def test_cancel_connect_cleans_reconnect_owner():
    client = Ultra96Client("127.0.0.1", free_port(), 100, 1)
    connecting = asyncio.create_task(client.connect())
    await until(lambda: client._worker.task is not None)
    owner = client._worker.task
    connecting.cancel()
    await asyncio.gather(connecting, return_exceptions=True)
    assert owner.done() and client._worker.task is None


async def test_concurrent_registry_replacement_closes_all_superseded_connections():
    from cg_comms.engine.ws_server import ConnectionRegistry

    class Socket:
        closed = False

        async def close(self):
            await asyncio.sleep(.01)
            self.closed = True

        async def send(self, _payload):
            pass

    registry = ConnectionRegistry(8)
    sockets = [Socket() for _ in range(4)]
    original = await registry.register("/relay/1", sockets[0])
    replacements = await asyncio.gather(*(registry.register("/relay/1", ws) for ws in sockets[1:]))
    current = registry.connection_for_path("/relay/1")
    assert all(ws.closed for ws in sockets[:-1])
    assert current.websocket is sockets[-1]
    assert original.writer_task.done()
    assert all(conn.writer_task.done() for conn in replacements[:-1])
    await registry.unregister(current)


async def test_ble_reconnect_does_not_replace_upstream_workers(monkeypatch):
    from cg_comms.config import RelaySettings
    from cg_comms.relay import app
    from cg_comms.relay.session import Session

    clients = []
    engines = []
    sessions = []
    original_ultra = app.Ultra96Client
    original_engine = app.EngineLinkClient

    def classifier(**kwargs):
        value = original_ultra(**kwargs)
        value._worker.backoff_s = (.01, .02)
        clients.append(value)
        return value

    def engine(**kwargs):
        value = original_engine(**kwargs)
        value._worker.backoff_s = (.01, .02)
        engines.append(value)
        return value

    async def fake_ble_run(self):
        sessions.append(self)
        assert self.config.player_id == 2
        await until(lambda: clients[0]._worker.connected.is_set()
                    and engines[0]._worker.connected.is_set())
        if len(sessions) < 4:
            await asyncio.sleep(.01)
            raise ConnectionError("injected BLE disconnect")
        await asyncio.Event().wait()

    monkeypatch.setattr(app, "Ultra96Client", classifier)
    monkeypatch.setattr(app, "EngineLinkClient", engine)
    monkeypatch.setattr(Session, "run", fake_ble_run)
    settings = RelaySettings(player_id=2, psk_hex=bytes(range(16)).hex())
    settings.transport.kind = "loopback"
    settings.ultra96.port = free_port()
    settings.engine_link.port = free_port()
    settings.engine_link.use_tls = False
    settings.heartbeat.reconnect_backoff_ms = [10]
    connections = {"classifier": 0, "engine": 0}

    async def classifier_peer(reader, writer):
        connections["classifier"] += 1
        try:
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    async def engine_peer(ws):
        connections["engine"] += 1
        await ws.wait_closed()

    classifier_server = await asyncio.start_server(classifier_peer, "127.0.0.1", settings.ultra96.port)
    engine_server = await websockets.serve(engine_peer, "127.0.0.1", settings.engine_link.port)
    task = asyncio.create_task(app.run_relay(settings, use_tunnel=False))
    try:
        await until(lambda: len(sessions) >= 4)
        assert len(clients) == len(engines) == 1
        assert clients[0]._worker.task is not None and engines[0]._worker.task is not None
        assert len({id(s._rx_tracker) for s in sessions}) == 4
        assert connections == {"classifier": 1, "engine": 1}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        classifier_server.close()
        engine_server.close()
        await classifier_server.wait_closed()
        await engine_server.wait_closed()
    assert clients[0]._worker.task is None and engines[0]._worker.task is None


async def test_authenticated_reset_broadcasts_both_visualizers_keeps_links_and_rejects_bad_auth():
    import json

    from cg_comms.config import EngineSettings
    from cg_comms.engine.app import run_engine

    port = free_port()
    settings = EngineSettings(host="127.0.0.1", port=port, use_tls=False, bearer_token="test")
    engine = asyncio.create_task(run_engine(settings))
    connections = []

    async def connect(path, token="test"):
        async def attempt():
            while True:
                try:
                    return await websockets.connect(f"ws://127.0.0.1:{port}{path}",
                        additional_headers={"Authorization": f"Bearer {token}"})
                except OSError:
                    await asyncio.sleep(.01)
        ws = await asyncio.wait_for(attempt(), 2)
        connections.append(ws)
        return ws

    async def reset_snapshot(ws):
        while True:
            data = json.loads(await asyncio.wait_for(ws.recv(), 2))
            if data.get("reason") == "reset":
                return data

    try:
        bad = await connect("/control", "wrong")
        await asyncio.wait_for(bad.wait_closed(), 1)
        assert bad.close_code == 1008
        relay = await connect("/relay/1")
        p1 = await connect("/visualizer/1")
        p2 = await connect("/visualizer/2")
        # RESET_MATCH on an otherwise authorised gameplay channel is not a control command.
        await p1.send('{"type":"RESET_MATCH","version":1}')
        control = await connect("/control")
        await control.send('{"type":"RESET_MATCH","version":1}')
        states = await asyncio.gather(*(reset_snapshot(ws) for ws in (p1, p2, control)))
        assert all(s["hp"] == {"1": 100, "2": 100} for s in states)
        assert all(s["connected"]["1"] for s in states)
        await relay.send(action().model_dump_json(by_alias=True))
        while True:
            result = json.loads(await asyncio.wait_for(p1.recv(), 2))
            if result["type"] == "ACTION_RECOGNIZED":
                assert result["weapon"] == "SWORD"
                break
    finally:
        for ws in connections:
            await ws.close()
        engine.cancel()
        await asyncio.gather(engine, return_exceptions=True)


@pytest.mark.parametrize("order", [(1, 2), (2, 1)])
async def test_two_player_protocol_streams_and_one_board_reconnect(order, monkeypatch):
    from cg_comms.config import RelaySettings
    from cg_comms.mock.firebeetle_sim import FireBeetleSim
    from cg_comms.mock.scenario import ScenarioStep
    from cg_comms.protocol.constants import FLAG_MOTION_ACTIVE, FLAG_TRIGGER_ID_SHIFT
    from cg_comms.relay import app
    from cg_comms.timer_resolution import high_resolution_timers
    from cg_comms.transport.loopback import LoopbackTransport

    classifier_port, engine_port = free_port(), free_port()
    boards = {1: [], 2: []}
    relay_tasks = []
    links = {1: 0, 2: 0}
    classified = {1: 0, 2: 0}
    actions = {1: 0, 2: 0}
    psk = bytes(range(16))

    def build_transport(settings):
        pid = settings.player_id
        relay, device = LoopbackTransport.pair()
        sim = FireBeetleSim(device, psk, pid)
        steps = [ScenarioStep(delay_ms=20, kind="rfid_event", payload={"weapon": "SWORD"})]
        for i in range(50):
            flags = FLAG_MOTION_ACTIVE | (pid << FLAG_TRIGGER_ID_SHIFT) if i < 40 else 0
            steps.append(ScenarioStep(delay_ms=20, kind="sensor_frame",
                payload={"dominant": [0] * 6, "offhand": [0] * 6, "flags": flags}))
        boards[pid].append(asyncio.create_task(sim.run(steps, loop_scenario=True)))
        return relay

    async def classifier_peer(reader, writer):
        try:
            while line := await reader.readline():
                sample = SensorWindow.model_validate_json(line)
                assert sample.trigger_id == sample.player_id
                assert len(sample.dominant) == 40
                classified[sample.player_id] += 1
                result = ActionResult(timestamp=sample.timestamp, player_id=sample.player_id,
                    request_id=sample.request_id, action="HORIZONTAL_SWING", action_confidence=1,
                    guard_state="IDLE", guard_confidence=1)
                writer.write((result.model_dump_json(by_alias=True) + "\n").encode())
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def engine_peer(ws):
        import json
        pid = int(ws.request.path.rsplit("/", 1)[1])
        links[pid] += 1
        async for raw in ws:
            msg = json.loads(raw)
            assert msg["playerId"] == pid
            if msg["type"] == "ACTION":
                actions[pid] += 1

    monkeypatch.setattr(app, "_build_transport", build_transport)
    with high_resolution_timers():
        async with await asyncio.start_server(classifier_peer, "127.0.0.1", classifier_port):
            async with websockets.serve(engine_peer, "127.0.0.1", engine_port):
                try:
                    for pid in order:
                        settings = RelaySettings(player_id=pid, psk_hex=psk.hex())
                        settings.ultra96.port = classifier_port
                        settings.engine_link.port = engine_port
                        settings.engine_link.use_tls = False
                        settings.windowing.trigger_mode = True
                        settings.heartbeat.reconnect_backoff_ms = [10]
                        relay_tasks.append(asyncio.create_task(app.run_relay(settings, False)))
                        await asyncio.sleep(.05)
                    await until(lambda: all(v >= 1 for v in actions.values()), timeout=6)
                    prior = dict(actions)
                    boards[1][-1].cancel()
                    await asyncio.gather(boards[1][-1], return_exceptions=True)
                    await until(lambda: actions[1] > prior[1] and actions[2] > prior[2], timeout=6)
                    assert len(boards[1]) >= 2 and len(boards[2]) == 1
                    assert links == {1: 1, 2: 1}
                    assert all(v >= 2 for v in classified.values())
                finally:
                    for task in relay_tasks + boards[1] + boards[2]:
                        task.cancel()
                    await asyncio.gather(*relay_tasks, *boards[1], *boards[2], return_exceptions=True)
