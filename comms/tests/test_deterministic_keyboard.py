"""Dual-trigger regression tests. Software simulation is not physical hardware evidence."""

import asyncio
import json
import random
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_assessment import _FakeSession, _keyboard, _LiveRig

from cg_comms.config import RulesConfig
from cg_comms.logging_setup import _assessment_console
from cg_comms.messages.enums import Weapon
from cg_comms.messages.validate import dump_app_message
from cg_comms.mock.assess_device import build_echo_response
from cg_comms.mock.scenario import ScenarioStep
from cg_comms.mock.ultra96_sim import DeterministicAssessmentClassifier, _handle_client
from cg_comms.protocol.constants import PacketType
from cg_comms.relay import assess_dummy
from cg_comms.relay.assess_dummy import for_key
from cg_comms.relay.assess_echo import EchoPipeline
from cg_comms.relay.assess_transform import apply_transform, transform_for
from cg_comms.tools.assessment_fixture import build_fixture


def _window(key="b"):
    pipeline = EchoPipeline(1, random.Random(1))
    pending, fields = pipeline.build_request(ord(key))
    pending.demo_trace_id = "P1-test-1"
    pipeline.on_response(build_echo_response(fields, 123))
    return pipeline.synthetic_window(pending, weapon=Weapon.SWORD, request_id=1)


@pytest.mark.parametrize("key,name", [("b", "B"), ("c", "C"), ("d", "D")])
def test_fixed_mapping_ignores_random_seed_and_repetition(key, name):
    values = []
    for seed in (1, 42, 987):
        pipeline = EchoPipeline(1, random.Random(seed))
        for _ in range(3):
            pending, fields = pipeline.build_request(ord(key))
            response = build_echo_response(fields, 123)
            pipeline.on_response(response)
            assert pending.matched and pending.variant.name == name
            assert response["transform_id"] == 0
            assert (
                tuple(response[k] for k in ("value_a", "value_b", "value_c")) == pending.variant.values
            )
            values.append(pending.variant.values)
    assert len(set(values)) == 1


def test_saved_B_equals_C_next_press_same_classifier_input_and_action(tmp_path, monkeypatch):
    source = Path(assess_dummy.__file__).read_text(encoding="utf-8")
    live = tmp_path / "assess_dummy.py"
    live.write_text(source, encoding="utf-8")
    monkeypatch.setattr(assess_dummy, "__file__", str(live))
    before = _window("b")
    c = _window("c")
    assert before.dominant != c.dominant
    live.write_text(source.replace("(2048, -1024, 512)", "(1000, -2000, 3000)"), encoding="utf-8")
    after = _window("b")
    assert after.dominant == c.dominant and after.offhand == c.offhand
    classifier = DeterministicAssessmentClassifier(RulesConfig.load())
    result_b, info_b = classifier.classify(after)
    result_c, info_c = classifier.classify(
        c.model_copy(
            update={"player_id": 2, "request_id": 99, "timestamp": 987654, "demo_trace_id": "P2-other-9"}
        )
    )
    assert info_b["input_sha256"] == info_c["input_sha256"]
    assert result_b.action == result_c.action


@pytest.mark.parametrize(
    "replacement",
    ["(40000, 0, 0)", "(True, 0, 0)", "(1, 2)", "[1, 2, 3]", '__import__("os").getpid()', "("],
)
def test_bad_live_edit_fails_before_any_send(tmp_path, replacement):
    source = Path(assess_dummy.__file__).read_text(encoding="utf-8")
    live = tmp_path / "bad.py"
    live.write_text(source.replace("(2048, -1024, 512)", replacement), encoding="utf-8")
    with pytest.raises((ValueError, SyntaxError, TypeError)):
        for_key(ord("b"), live)


@pytest.mark.parametrize("key", ["a", "e", "1", " ", "B", "C", "D"])
def test_non_IMU_keys_cannot_build_an_echo(key):
    with pytest.raises(ValueError):
        EchoPipeline(1, random.Random()).build_request(ord(key))


def test_identity_extremes_and_firmware_contract():
    for key in (98, 99, 100):
        assert transform_for(key) == 0
        assert apply_transform(0, (-32768, 32767, -1), key) == (-32768, 32767, -1)
    header = (
        Path(__file__).parents[2] / "firmware_demo/lib/cg_protocol/cg_assess_transform.h"
    ).read_text()
    assert "CG_ASSESS_TRANSFORM_IDENTITY    0" in header
    assert "keycode == 'b' || keycode == 'c' || keycode == 'd'" in header


async def test_a_needs_no_weapon_or_classifier_and_other_keys_cannot_send():
    session = _FakeSession()
    session.cycle_weapon = AsyncMock()
    keyboard = _keyboard(session, weapon=None, upstream=(False, True))
    keyboard.handle_key("a")
    await asyncio.sleep(0)
    session.cycle_weapon.assert_awaited_once()
    for key in ("e", "1", " ", "B", "C"):
        keyboard.handle_key(key)
    assert session.echoes == [] and session.files == []


@pytest.mark.parametrize("lost_ack,lost_reply", [(0, 0), (2, 0), (0, 1), (1, 1)])
async def test_weapon_cycle_once_per_key_and_shared_physical_state(lost_ack, lost_reply):
    async with _LiveRig(assess_echo_timeout_s=0.3) as rig:
        rig.sim.drop_assess_acks = lost_ack
        original = rig.sim._send

        async def send(kind, fields, direction):
            nonlocal lost_reply
            if kind is PacketType.ASSESS_WEAPON_RESP and lost_reply:
                lost_reply -= 1
                return
            await original(kind, fields, direction)

        rig.sim._send = send
        for expected in (Weapon.SWORD, Weapon.SPEAR, Weapon.MACE, Weapon.SWORD):
            await rig.session.cycle_weapon()
            assert rig.windower.current_weapon is expected
            assert rig.sim._weapon_id == expected.wire_value
        # Physical selection remains authoritative; keyboard continues from that board state.
        # A real debounced GPIO edge cannot follow the prior command in the same millisecond.
        await asyncio.sleep(0.01)
        await rig.sim._play_step(
            ScenarioStep(delay_ms=0, kind="rfid_event", payload={"weapon": "SPEAR"})
        )
        await asyncio.sleep(0.02)
        assert rig.windower.current_weapon is Weapon.SPEAR
        await rig.session.cycle_weapon()
        assert rig.windower.current_weapon is Weapon.MACE


async def test_cached_keyboard_reply_cannot_undo_newer_physical_selection():
    async with _LiveRig(assess_echo_timeout_s=0.3) as rig:
        original = rig.sim._send
        lost = False

        async def send(kind, fields, direction):
            nonlocal lost
            if kind is PacketType.ASSESS_WEAPON_RESP and not lost:
                lost = True
                await asyncio.sleep(0.01)
                await rig.sim._play_step(
                    ScenarioStep(delay_ms=0, kind="rfid_event", payload={"weapon": "MACE"})
                )
                return  # response lost; independent physical selection reaches relay first
            await original(kind, fields, direction)

        rig.sim._send = send
        await rig.session.cycle_weapon()
        assert lost and rig.windower.current_weapon is Weapon.MACE
        assert rig.sim._weapon_id == Weapon.MACE.wire_value


async def test_stop_cancels_weapon_wait_and_releases_operation():
    async with _LiveRig() as rig:
        original = rig.sim._send

        async def send(kind, fields, direction):
            if kind is not PacketType.ASSESS_WEAPON_RESP:
                await original(kind, fields, direction)

        rig.sim._send = send
        operation = asyncio.create_task(rig.session.cycle_weapon())
        await asyncio.sleep(0.04)
        rig.session.stop()
        await asyncio.wait_for(rig.tasks[0], 2)
        assert operation.cancelled() and not rig.session.assess_busy()
        assert rig.session._weapon_reply is None


async def test_old_firmware_rejects_new_keyboard_but_file_still_works():
    async with _LiveRig() as rig:
        rig.session._firmware_version = 2
        with pytest.raises(ValueError, match="firmware version 3"):
            await rig.session.cycle_weapon()
        with pytest.raises(ValueError, match="firmware version 3"):
            await rig.session.echo_keystroke(ord("b"))
        assert (await rig.session.send_file(b"old file path stays compatible")).ok


async def test_deterministic_mode_only_selects_keyboard_windows():
    classifier = DeterministicAssessmentClassifier(RulesConfig.load())
    reader = asyncio.StreamReader()
    keyboard = _window()
    gpio = keyboard.model_copy(update={"trigger_id": 2, "demo_trace_id": None})
    # Even a GPIO window with a diagnostic trace keeps its physical trigger mapping.
    traced_gpio = gpio.model_copy(update={"demo_trace_id": "gpio-debug"})
    for window in (keyboard, gpio, traced_gpio):
        reader.feed_data((dump_app_message(window) + "\n").encode())
    reader.feed_eof()
    replies = []
    writer = AsyncMock()
    writer.write = lambda data: replies.append(json.loads(data))
    writer.close = lambda: None
    table = {"default": {}, "triggers": {"SWORD": {2: {"action": "THRUST"}}}}
    await _handle_client(reader, writer, [], table, assessment_classifier=classifier)
    assert replies[0]["action"] == classifier.classify(keyboard)[0].action.value
    assert replies[1]["action"] == replies[2]["action"] == "THRUST"


def test_console_shortening_preserves_full_event_and_failures():
    event = {
        "event": "assess_echo_sent",
        "key": "b",
        "variant": "B",
        "values": (1, 2, 3),
        "demo_trace_id": "P1-abc-1",
        "transform_name": "IDENTITY",
        "seq": 123,
    }
    console = _assessment_console(None, "info", event)
    assert "seq" not in console and event["seq"] == 123
    assert console["key"] == "b" and console["values"] == (1, 2, 3)
    failure = {**event, "event": "assess_echo_mismatch", "problems": "wrong values"}
    assert _assessment_console(None, "error", failure) == failure


async def test_sensor_stream_continues_during_keyboard_and_file_operations():
    async with _LiveRig() as rig:

        async def sensors():
            for _ in range(30):
                await rig.sim._play_step(
                    ScenarioStep(
                        delay_ms=0,
                        kind="sensor_frame",
                        payload={"dominant": [2048, 0, 0, 0, 0, 0], "offhand": [0, 0, 2048, 0, 0, 0]},
                    )
                )
                await asyncio.sleep(0.02)  # normal 50 Hz schedule, independent of the operations

        streaming = asyncio.create_task(sensors())
        try:
            await rig.session.cycle_weapon()
            assert (await rig.session.echo_keystroke(ord("b")))[1]
            rig.session.arm_assessment_ack_loss()
            assert (await rig.session.send_file(build_fixture())).ok
            await streaming
            await asyncio.sleep(0.02)
            assert rig.session.meter.report(final=True)["sensor_frames"] == 30
        finally:
            streaming.cancel()
            await asyncio.gather(streaming, return_exceptions=True)
