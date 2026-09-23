"""Review regressions: real protocol paths and failure injection; no physical BLE claim."""

import asyncio
import hashlib
import json
import random
import socket
from unittest.mock import AsyncMock

import pytest
import structlog
import websockets
from test_assessment import _LiveRig

from cg_comms.config import EngineSettings, RelaySettings, RulesConfig
from cg_comms.engine.app import run_engine
from cg_comms.messages.enums import Weapon
from cg_comms.messages.models import SensorWindow
from cg_comms.messages.validate import dump_app_message
from cg_comms.mock.assess_device import build_echo_response
from cg_comms.mock.ultra96_sim import RandomClassifier, _handle_client
from cg_comms.protocol.codec import pack_payload
from cg_comms.protocol.constants import PacketType
from cg_comms.relay.assess_echo import EchoPipeline
from cg_comms.relay.engine_link import EngineLinkClient
from cg_comms.relay.throughput import summarize_trial
from cg_comms.tools.assessment_fixture import build_fixture


def test_echo_embeds_exact_sensor_layout_and_scales_returned_readings():
    pipeline = EchoPipeline(1, random.Random(7))
    pending, fields = pipeline.build_request(ord("b"), Weapon.MACE)
    sensor = dict(
        device_timestamp_ms=fields["sent_ms"],
        flags=fields["flags"],
        weapon_id=fields["weapon_id"],
        dominant=tuple(fields[k] for k in ("value_a", "value_b", "value_c")) + fields["dominant_gyro"],
        offhand=fields["offhand"],
        torso=fields["torso"],
    )
    assert pack_payload(PacketType.ASSESS_ECHO_REQ, fields)[6:] == pack_payload(
        PacketType.SENSOR_FRAME, sensor
    )
    response = build_echo_response(fields, 123)
    pipeline.on_response(response)
    window = pipeline.synthetic_window(pending, weapon=Weapon.MACE, request_id=99)
    assert window.dominant[0][:3] == [response[k] / 2048 for k in ("value_a", "value_b", "value_c")]
    assert window.dominant[0][3:] == [10, 0, -5]
    assert window.offhand == [[0, 0, 1, 0, 0, 0]]
    assert window.window_start_ts == window.window_end_ts


async def test_echo_recovers_lost_response_when_ack_arrived():
    async with _LiveRig(assess_echo_timeout_s=0.3, reliability_base_timeout_ms=500) as rig:
        original = rig.sim._send
        lost = 0

        async def send(kind, fields, direction):
            nonlocal lost
            if kind is PacketType.ASSESS_ECHO_RESP and lost == 0:
                lost += 1
                return
            await original(kind, fields, direction)

        rig.sim._send = send
        with structlog.testing.capture_logs() as events:
            pending, verified = await rig.session.echo_keystroke(ord("a"))
        assert verified and pending.matched and lost == 1
        assert any(e["event"] == "assess_echo_response_retry" for e in events)
        assert rig.session.echo.verified == 1


async def test_echo_timeout_late_response_does_not_forward():
    async with _LiveRig(assess_echo_timeout_s=0.09) as rig:
        rig.windower.on_weapon_confirmed(Weapon.SWORD)
        original = rig.sim._send
        delayed = []

        async def send(kind, fields, direction):
            if kind is PacketType.ASSESS_ECHO_RESP:
                delayed.append((kind, fields, direction))
                return
            await original(kind, fields, direction)

        rig.sim._send = send
        _, verified = await rig.session.echo_keystroke(ord("a"))
        assert not verified
        await original(*delayed[0])
        await asyncio.sleep(0.03)
        rig.on_action.assert_not_called()
        assert rig.session.echo.verified == 0


async def test_file_readback_hash_file_and_lost_ack(tmp_path):
    async with _LiveRig(throughput_latest_path=tmp_path / "latest.json") as rig:
        data = build_fixture(1037)
        rig.session.arm_assessment_ack_loss()
        with structlog.testing.capture_logs() as events:
            result = await rig.session.send_file(data)
        assert result.ok and result.returned_sha256 == hashlib.sha256(data).hexdigest()
        from pathlib import Path

        assert Path(result.received_path).read_bytes() == data
        assert any(e["event"] == "assess_fault_consumed" for e in events)
        assert any(e["event"] == "retransmit" for e in events)
        assert rig.sim.file_rx.data == data


async def test_file_readback_recovers_lost_response_and_ignores_wrong_transfer():
    async with _LiveRig() as rig:
        original = rig.sim._send
        lost = 0

        async def send(kind, fields, direction):
            nonlocal lost
            if kind is PacketType.ASSESS_FILE_DATA and lost == 0:
                lost += 1
                await original(kind, {**fields, "transfer_id": fields["transfer_id"] ^ 1}, direction)
                return
            await original(kind, fields, direction)

        rig.sim._send = send
        result = await rig.session.send_file(build_fixture(33))
        assert result.ok and result.returned_sha256 and lost == 1


async def test_authenticated_but_corrupted_readback_is_not_verified():
    async with _LiveRig() as rig:
        original = rig.sim._send

        async def send(kind, fields, direction):
            if kind is PacketType.ASSESS_FILE_DATA:
                fields = dict(fields)
                fields["data"] = bytes([fields["data"][0] ^ 1]) + fields["data"][1:]
            await original(kind, fields, direction)

        rig.sim._send = send
        result = await rig.session.send_file(build_fixture(32))
        assert not result.ok and "SHA-256 mismatch" in result.reason


async def test_stop_cancels_session_owned_assessment_operation():
    async with _LiveRig() as rig:
        rig.sim._send_assess_ack = AsyncMock()  # no ACK ever reaches relay
        operation = asyncio.create_task(rig.session.send_file(build_fixture()))
        await asyncio.sleep(0.03)
        rig.session.stop()
        await asyncio.wait_for(rig.tasks[0], 2)
        assert operation.cancelled()


def _observation(pid, at, frames, session=0):
    return {
        "player_id": pid,
        "monotonic_s": at,
        "session_started_monotonic_s": session,
        "interval": {"elapsed_s": 5, "sensor_frames": 250},
        "run": {"sensor_frames": frames},
    }


def test_paired_report_counts_only_observed_session_and_actual_common_duration():
    samples = {pid: [_observation(pid, t + pid, 50 * t) for t in range(0, 71, 5)] for pid in (1, 2)}
    result = summarize_trial(samples)
    assert result["over_60_seconds_both"] and result["common_coverage_s"] == 69
    assert result["players"]["1"]["goodput_kbps"] == 16.8
    samples[2][-1]["session_started_monotonic_s"] = 999
    assert not summarize_trial(samples)["over_60_seconds_both"]


def test_trace_is_omitted_for_normal_serialization():
    window = SensorWindow(
        timestamp=1,
        player_id=1,
        request_id=2,
        weapon=Weapon.SWORD,
        window_start_ts=1,
        window_end_ts=1,
        sample_rate_hz=50,
        dominant=[[0] * 6],
        offhand=[[0] * 6],
        stale_pct=0,
    )
    assert "demoTraceId" not in json.loads(dump_app_message(window))


def test_environment_player_id_parses_without_yaml(monkeypatch):
    monkeypatch.setenv("CG_RELAY_PLAYER_ID", "2")
    assert RelaySettings(_env_file=None).player_id == 2


async def test_entire_keyboard_board_classifier_engine_visualizer_pipeline():
    """Actual TCP classifier + engine WebSocket; only the physical BLE board/phone are simulated."""
    token = "assessment-test-token"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        engine_port = listener.getsockname()[1]
    classifier = RandomClassifier.for_rules(RulesConfig.load(), random.Random(7))
    server = await asyncio.start_server(
        lambda r, w: _handle_client(r, w, [], None, classifier), "127.0.0.1", 0
    )
    classifier_port = server.sockets[0].getsockname()[1]
    engine = asyncio.create_task(
        run_engine(
            EngineSettings(
                _env_file=None, host="127.0.0.1", port=engine_port, use_tls=False, bearer_token=token
            )
        )
    )
    link = EngineLinkClient(f"ws://127.0.0.1:{engine_port}/relay/1", token, AsyncMock())
    try:
        await link.connect()
        async with websockets.connect(
            f"ws://127.0.0.1:{engine_port}/visualizer/1",
            additional_headers={"Authorization": f"Bearer {token}"},
        ) as phone:
            async with _LiveRig() as rig:
                rig.ultra96.port = classifier_port
                rig.ultra96.request_timeout_ms = 2000  # local-test budget, not gameplay change
                await rig.ultra96.connect()
                rig.windower.on_weapon_confirmed(Weapon.SWORD)
                rig.session.on_action_result = link.send_action
                try:
                    pending, verified = await rig.session.echo_keystroke(ord("a"))
                    assert verified

                    async def recognized():
                        async for raw in phone:
                            event = json.loads(raw)
                            if event["type"] == "ACTION_RECOGNIZED":
                                return event

                    event = await asyncio.wait_for(recognized(), 3)
                    assert event["demoTraceId"] == pending.demo_trace_id
                    assert event["playerId"] == 1 and event["weapon"] == "SWORD"
                    assert event["action"] in classifier._moves["SWORD"]
                finally:
                    await rig.ultra96.disconnect()
    finally:
        await link.disconnect()
        engine.cancel()
        await asyncio.gather(engine, return_exceptions=True)
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("chunks", [[b'{"type":', b'"SENSOR_WINDOW"}'], [b'{"type":"SENSOR_WINDOW"}']])
async def test_classifier_rejects_unterminated_json_at_eof(chunks):
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    reader.feed_eof()
    writer = AsyncMock()
    writer.write = lambda _: pytest.fail("partial/unterminated message produced a reply")
    writer.close = lambda: None
    await _handle_client(reader, writer, [], None)
