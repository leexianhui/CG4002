"""Behavior checks from the cleanup review."""

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from test_pre_demo_recovery import free_port, window

from cg_comms.messages.validate import MessageDropped, parse_app_message
from cg_comms.relay.ultra96 import Ultra96Client, Ultra96Unavailable
from cg_comms.tools import reset_match


@pytest.mark.parametrize("field,value", [("playerId", 2), ("timestamp", 101)])
async def test_extra_ai_fields_do_not_disable_correlation(field, value):
    port = free_port()

    async def peer(reader, writer):
        request = json.loads(await reader.readline())
        reply = {"type": "ACTION_RESULT", "version": 1, "timestamp": request["timestamp"],
                 "playerId": request["playerId"], "requestId": request["requestId"],
                 "action": "THRUST",
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
