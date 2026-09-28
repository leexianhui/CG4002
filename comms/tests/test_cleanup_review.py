"""Behavior checks for the cleanup review, including rejected-request retries."""

import asyncio
import json
import random
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_pre_demo_recovery import free_port, window

from cg_comms.messages.validate import MessageDropped, parse_app_message
from cg_comms.mock.firebeetle_sim import FireBeetleSim
from cg_comms.protocol.codec import encode_packet
from cg_comms.protocol.constants import Direction, PacketType
from cg_comms.relay.assess_echo import EchoPipeline
from cg_comms.relay.ultra96 import Ultra96Client, Ultra96Unavailable
from cg_comms.tools import reset_match


@pytest.mark.parametrize("bad_fields", [{"trace_id": 0}, {"weapon_id": 0}, {"flags": 1}])
async def test_mock_rejects_malformed_echo_on_every_retry(bad_fields):
    # Use the actual authenticated decode -> application handler path, not a handler-only mock.
    _, fields = EchoPipeline(1, random.Random(0)).build_request(ord("b"))
    fields.update(bad_fields)
    key = bytes(range(16))  # test-only key
    frame = encode_packet(packet_type=PacketType.ASSESS_ECHO_REQ, seq=10, ext_seq=10,
                          player_id=1, fields=fields, psk=key, session_id=123,
                          direction=Direction.LAPTOP_TO_DEVICE)

    async def frames():
        yield frame
        yield frame

    board = FireBeetleSim(SimpleNamespace(frames=frames), key, 1)
    board.session_id = 123
    board._send = AsyncMock()
    await board._drain_incoming()
    board._send.assert_not_awaited()
    assert not board._rx_tracker.reliable_frames
    assert not board._rx_tracker.reliable_high


@pytest.mark.parametrize("field,value", [("playerId", 2), ("timestamp", 101),
                                        ("demoTraceId", "wrong-trace")])
async def test_extra_ai_fields_do_not_disable_correlation(field, value):
    port = free_port()

    async def peer(reader, writer):
        request = json.loads(await reader.readline())
        reply = {"type": "ACTION_RESULT", "version": 1, "timestamp": request["timestamp"],
                 "playerId": request["playerId"], "requestId": request["requestId"],
                 "demoTraceId": request.get("demoTraceId"), "action": "THRUST",
                 "actionConfidence": 1, "guardState": "IDLE", "guardConfidence": 1,
                 "modelVersion": "extra-output"}
        reply[field] = value
        try:
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(peer, "127.0.0.1", port):
        client = Ultra96Client("127.0.0.1", port, 1000, 1)
        try:
            await client.connect()
            with pytest.raises(Ultra96Unavailable, match=field + " not echoed unchanged"):
                await client.request(window())
        finally:
            await client.disconnect()


def test_ai_extra_keys_do_not_make_missing_required_fields_valid():
    assert isinstance(parse_app_message(json.dumps({
        "type": "ACTION_RESULT", "version": 1, "timestamp": 100, "modelVersion": "v2"
    })), MessageDropped)


def test_reset_missing_config_is_operator_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["reset_match", "--config", str(tmp_path / "missing.yaml")])
    with pytest.raises(SystemExit) as exc:
        reset_match.main()
    assert exc.value.code == 2
    assert "config file not found" in capsys.readouterr().err


def test_reset_missing_token_is_operator_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["reset_match", "--config", "placeholder.yaml"])
    monkeypatch.setattr(reset_match.EngineSettings, "from_yaml",
                        lambda _: SimpleNamespace(bearer_token=""))
    with pytest.raises(SystemExit) as exc:
        reset_match.main()
    assert exc.value.code == 2
    assert "no bearer token" in capsys.readouterr().err
