"""The relay's real classifier client against ai/classifier_server.py running the supplied model.

Guards the Comms<->AI contract: requestId/timestamp/playerId echoed unchanged, replies parse as
ACTION_RESULT, AI-only classes never reach the engine as unknown actions.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest

from cg_comms.messages.enums import ActionClass, GuardState, Weapon
from cg_comms.messages.models import SensorWindow
from cg_comms.relay.ultra96 import Ultra96Client

pytest.importorskip("numpy")
AI_DIR = Path(__file__).resolve().parents[2] / "ai"


@pytest.fixture(scope="module")
def ai():
    sys.path.insert(0, str(AI_DIR))
    try:
        yield importlib.import_module("classifier_server")
    finally:
        sys.path.remove(str(AI_DIR))


def _wire(window: SensorWindow) -> dict:
    """Exactly what the relay puts on the wire (Ultra96Client uses model_dump_json)."""
    return json.loads(window.model_dump_json(by_alias=True))


def _window(request_id: int, samples: int = 100) -> SensorWindow:
    swing = [[0.1 * (i % 7), -0.2, 1.0, 300.0 * ((i % 10) - 5), -40.0, 25.0] for i in range(samples)]
    still = [[0.0, 0.0, 1.0, 0.5, -0.3, 0.1]] * samples
    return SensorWindow(timestamp=123_456 + request_id, player_id=2, request_id=request_id,
                        weapon="SPEAR", window_start_ts=1, window_end_ts=2, sample_rate_hz=50.0,
                        dominant=swing, offhand=still, stale_pct=0.0)


@pytest.mark.parametrize("fixed", [False, True], ids=["numpy", "fixed"])
async def test_relay_client_round_trips_through_ai_model(ai, fixed):
    backend = ai.NumpyBackend(str(AI_DIR / "models"), fixed=fixed)
    server = await asyncio.start_server(ai.make_server(backend, False), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = Ultra96Client("127.0.0.1", port, request_timeout_ms=2000, max_in_flight=2)
    try:
        await client.connect()
        for request_id, samples in ((7, 100), (8, 40)):  # 40: shorter windows are padded, not rejected
            window = _window(request_id, samples)
            result = await client.request(window)
            assert result is not None
            assert (result.request_id, result.timestamp, result.player_id) == (
                window.request_id, window.timestamp, window.player_id)
            assert isinstance(result.action, ActionClass)
            assert 0.0 <= result.action_confidence <= 1.0
            assert isinstance(result.guard_state, GuardState)
    finally:
        await client.disconnect()
        server.close()
        await server.wait_closed()


def _confident(ai, index):
    """Softmax output that picks ACTION class `index` with 0.99."""
    import numpy as np
    n = len(ai.C.CLASSES[ai.ACTION_MODEL])
    p = [0.01 / (n - 1)] * n
    p[index] = 0.99
    return np.asarray(p)


def test_every_ai_class_reaches_the_game_under_its_own_name(ai, monkeypatch):
    """SPIN_ATTACK/UPPERCUT used to be renamed IDLE; every ACTION_FINAL class is now a game action
    that the comms ActionClass parses, and those two are moves of every weapon."""
    from cg_comms.config import RulesConfig
    from cg_comms.messages.enums import ActionClass
    backend = ai.NumpyBackend(str(AI_DIR / "models"))
    moves = RulesConfig.load().moves
    for index, ai_class in enumerate(ai.C.CLASSES[ai.ACTION_MODEL]):
        monkeypatch.setattr(ai, "probs_for", lambda b, model, x, missing, i=index: _confident(ai, i))
        out = ai.handle_request(backend, _wire(_window(1)))
        assert out["action"] == out["rawAction"] == ai_class
        assert ActionClass(ai_class)
    attacks = set(ai.C.CLASSES[ai.ACTION_MODEL]) - {"IDLE"}
    for weapon, weapon_moves in moves.items():
        assert set(weapon_moves) == attacks, weapon  # one model, same 6 attacks for every weapon


def test_every_ai_attack_lands_as_a_combat_event_for_every_weapon():
    """An AI attack must survive the whole engine path: a valid move for the weapon, and a legal
    COMBAT_EVENT.action / cooldownAction (CombatAction once lacked SPIN_ATTACK/UPPERCUT)."""
    from cg_comms.config import RulesConfig
    from cg_comms.messages.models import CombatAction
    from typing import get_args
    combat = {a.value for a in get_args(CombatAction)}
    for weapon_moves in RulesConfig.load().moves.values():
        assert set(weapon_moves) <= combat


def test_low_confidence_attack_keeps_its_name(ai, monkeypatch):
    """The 0.6 floor lives in the relay/engine. The AI no longer renames a 0.65 swing IDLE."""
    import numpy as np
    backend = ai.NumpyBackend(str(AI_DIR / "models"))
    names = ai.C.CLASSES[ai.ACTION_MODEL]
    p = np.full(len(names), 0.35 / (len(names) - 1))
    p[names.index("HORIZONTAL_SWING")] = 0.65
    monkeypatch.setattr(ai, "probs_for", lambda *a: p)
    out = ai.handle_request(backend, _wire(_window(1)))
    assert (out["action"], out["actionConfidence"]) == ("HORIZONTAL_SWING", 0.65)


def test_every_prediction_logs_one_line(ai):
    lines = []
    backend = ai.NumpyBackend(str(AI_DIR / "models"), fixed=True)
    out = ai.handle_request(backend, _wire(_window(1)), log=lines.append)
    assert len(lines) == 1
    assert lines[0].startswith("[pred] P2 req=1 SPEAR -> ") and " -> %s " % out["action"] in lines[0]
    assert "cpu-fixed" in lines[0] and "2nd " in lines[0] and "3rd " in lines[0]


def test_guard_is_never_reported_by_the_ai(ai):
    """Guard comes from the hardware: the AI service loads ACTION_FINAL only and always says IDLE."""
    backend = ai.NumpyBackend(str(AI_DIR / "models"))
    assert list(backend.W) == ["ACTION_FINAL"]
    out = ai.handle_request(backend, _wire(_window(1)))
    assert (out["guardState"], out["guardConfidence"]) == ("IDLE", 0.0)


def test_placeholder_offhand_and_absent_torso_count_as_missing(ai):
    """A window without a measured left wrist / torso: the relay's constant off-hand placeholder and
    the absent torso must reach the model as the training mean, not as fake motion."""
    window = _wire(_window(5))
    assert window["torso"] is None
    x, missing = ai.build_window(window)
    assert x.shape == (100, 18) and missing == (6, 12)
    out = ai.handle_request(ai.NumpyBackend(str(AI_DIR / "models")), window, debug=True)
    assert out["missingSensors"] == ["offhand", "torso"]


def test_bad_requests_return_error_not_crash(ai):
    backend = ai.StubBackend()
    assert ai.handle_request(backend, {"type": "PING"})["type"] == "PONG"  # their health check
    assert ai.handle_request(backend, {"type": "HELLO"})["type"] == "ERROR"
    bad = _wire(_window(3)) | {"weapon": "AXE"}
    assert ai.handle_request(backend, bad) == {
        "type": "ERROR", "error": "unknown weapon 'AXE'", "requestId": 3}


def test_legacy_segment_window_is_accepted_by_ai_model(ai):
    """An old hub's dominant-only segment (1.5 s) -> relay window -> AI model still works."""
    from test_segments import SegmentAssembler, segment_chunks, session

    subject = session()
    chunks, _ = segment_chunks(11, 75, weapon=1)
    assembler = SegmentAssembler()
    segment = [assembler.add(c, now=0.0) for c in chunks][-1]
    window = subject._segment_window(segment)
    out = ai.handle_request(ai.NumpyBackend(str(AI_DIR / "models")), _wire(window))
    assert out["type"] == "ACTION_RESULT" and out["requestId"] == window.request_id


def test_ai_teams_test_windows_survive_the_whole_relay_path(ai):
    """End to end on real data: the AI team's held-out windows, quantised to wire units exactly as
    the hub does (int16, 2048 counts/g, 16.4 counts/deg/s), split into three-IMU SEGMENT_CHUNKs,
    reassembled by the relay, turned into a SENSOR_WINDOW, sent as JSON and classified in fixed point
    (the FPGA's maths). Every channel must land where the model expects it: accuracy stays at the
    AI team's level instead of the 39/68 seen with the right wrist alone."""
    import numpy as np
    from test_segments import assemble, imu3_chunks, session

    backend = ai.NumpyBackend(str(AI_DIR / "models"), fixed=True)
    test = np.load(AI_DIR / "models" / "ACTION_FINAL" / "test_data.npz")
    mean, std = backend.norm["ACTION_FINAL"]
    scale = np.array([2048.0] * 3 + [16.4] * 3)
    subject = session()
    subject.windower.on_weapon_confirmed(Weapon.SWORD)
    correct = 0
    for i, (x, label) in enumerate(zip(test["X"] * std + mean, test["y"], strict=True)):
        counts = np.clip(np.round(x.reshape(100, 3, 6) * scale), -32768, 32767).astype(int)
        rows = [tuple(r) for r in counts.reshape(100, 18).tolist()]
        window = subject._segment_window(assemble(imu3_chunks(1000 + i, rows)))
        out = ai.handle_request(backend, _wire(window), debug=True)
        assert out["missingSensors"] == []
        correct += out["rawAction"] == ai.C.ACTION_CLASSES[int(label)]
    assert correct >= 63, correct  # 64/68 straight from the floats; quantisation may cost one
