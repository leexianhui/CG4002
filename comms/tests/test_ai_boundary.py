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

from cg_comms.messages.enums import ActionClass, GuardState
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


def _confident(ai, model, index):
    """Softmax output that picks class `index` of `model` with 0.99."""
    p = [0.01 / (len(ai.C.CLASSES[model]) - 1)] * len(ai.C.CLASSES[model])
    p[index] = 0.99
    import numpy as np
    return np.asarray(p)


def test_ai_only_classes_become_idle(ai, monkeypatch):
    backend = ai.NumpyBackend(str(AI_DIR / "models"))
    for ai_class in ("SPIN_ATTACK", "UPPERCUT"):
        index = ai.C.CLASSES[ai.ACTION_MODEL].index(ai_class)
        monkeypatch.setattr(ai, "probs_for", lambda b, model, x, missing, i=index: (
            _confident(ai, model, i) if model == ai.ACTION_MODEL else _confident(ai, model, 0)))
        out = ai.handle_request(backend, _wire(_window(1)))
        assert out["action"] == "IDLE" and out["rawAction"] == ai_class


def test_guard_has_priority_and_maps_to_comms_names(ai, monkeypatch):
    """GUARD_FINAL says GUARDING twice in a row -> guard-only report (action IDLE + GUARDING);
    NOT_GUARDING goes out as the comms name IDLE."""
    backend = ai.NumpyBackend(str(AI_DIR / "models"))
    thrust = ai.C.CLASSES[ai.ACTION_MODEL].index("THRUST")
    monkeypatch.setattr(ai, "probs_for", lambda b, model, x, missing: (
        _confident(ai, model, thrust) if model == ai.ACTION_MODEL else _confident(ai, model, 1)))
    monkeypatch.setattr(ai, "_guard_state", {})
    first = ai.handle_request(backend, _wire(_window(1)))
    assert (first["action"], first["guardState"]) == ("THRUST", "IDLE")  # hysteresis: needs 2 windows
    second = ai.handle_request(backend, _wire(_window(2)))
    assert (second["action"], second["guardState"]) == ("IDLE", "GUARDING")


def test_placeholder_offhand_and_absent_torso_count_as_missing(ai):
    """Segment mode: the hub sends the right wrist only; the relay's constant off-hand placeholder
    and the absent torso must reach the model as the training mean, not as fake motion."""
    window = _wire(_window(5))
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


def test_segment_mode_window_is_accepted_by_ai_model(ai):
    """A hub segment (100 Hz source decimated to 50 Hz, 1.5 s) -> relay window -> AI model."""
    from test_segments import SegmentAssembler, segment_chunks, session

    subject = session()
    chunks, _ = segment_chunks(11, 75, weapon=1)
    assembler = SegmentAssembler()
    segment = [assembler.add(c, now=0.0) for c in chunks][-1]
    window = subject._segment_window(segment)
    out = ai.handle_request(ai.NumpyBackend(str(AI_DIR / "models")), _wire(window))
    assert out["type"] == "ACTION_RESULT" and out["requestId"] == window.request_id
    assert out["guardState"] in ("IDLE", "GUARDING")
