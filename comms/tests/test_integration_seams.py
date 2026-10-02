"""Regressions at the seams a real AI classifier and the real firmware will plug into."""

import asyncio
import json

from test_pre_demo_recovery import free_port, window

from cg_comms.messages.models import ActionResult
from cg_comms.messages.validate import parse_app_message
from cg_comms.relay.ultra96 import Ultra96Client

_RESULT = {"type": "ACTION_RESULT", "version": 1, "timestamp": 100, "playerId": 1, "requestId": 1,
           "action": "THRUST", "actionConfidence": 0.9, "guardState": "IDLE",
           "guardConfidence": 0.8}


def test_action_result_ignores_unknown_ai_keys():
    # app_messages.md §3.2: a newer model's extra output must not make every reply unparseable.
    parsed = parse_app_message(json.dumps({**_RESULT, "probs": [0.1, 0.9], "modelVer": "v2"}))
    assert isinstance(parsed, ActionResult) and parsed.action.value == "THRUST"


async def test_invalid_utf8_line_is_dropped_without_losing_the_connection():
    port = free_port()

    async def peer(reader, writer):
        await reader.readline()
        writer.write(b"\xff\xfe not json\n" + json.dumps(_RESULT).encode() + b"\n")
        await writer.drain()
        await reader.read()

    async with await asyncio.start_server(peer, "127.0.0.1", port):
        client = Ultra96Client("127.0.0.1", port, 1000, 1)
        try:
            await client.connect()
            result = await client.request(window())
            assert result is not None and result.request_id == 1
            assert client.is_connected
        finally:
            await client.disconnect()


async def test_sim_skips_invalid_utf8_line_and_answers_the_next_window():
    from unittest.mock import AsyncMock

    from cg_comms.mock.ultra96_sim import _handle_client

    reader = asyncio.StreamReader()
    reader.feed_data(b"\xff\xfe not json\n" + window().model_dump_json(by_alias=True).encode() + b"\n")
    reader.feed_eof()
    written: list[bytes] = []
    writer = AsyncMock()
    writer.write = written.append
    writer.close = lambda: None
    await _handle_client(reader, writer, [], None)
    assert len(written) == 1 and json.loads(written[0])["requestId"] == 1
