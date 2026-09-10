"""Unit tests for relay/reliability.py — ACK + retransmit for RELIABLE_PACKET_TYPES.

Derived from COMMS_IMPLEMENTATION_PLAN.md §6 ("Retransmit: timeout 200 ms base, exponential backoff
x2, max 5 attempts, jitter +-20%. After the final attempt, raise a LinkDegraded event rather than
failing silently") and cg_comms.config.ReliabilityConfig's defaults, which must match those numbers.

Backoff-schedule tests monkeypatch the `asyncio` name inside cg_comms.relay.reliability so that
`asyncio.wait_for` returns instantly (always raising TimeoutError, having recorded the timeout it was
given) instead of actually sleeping up to ~6 s worst-case across 5 attempts — this keeps the suite
fast and the recorded timeouts deterministic instead of depending on real wall-clock scheduling.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from cg_comms.config import ReliabilityConfig
from cg_comms.relay.reliability import LinkDegraded, ReliabilitySender
from cg_comms.relay import reliability as reliability_mod


def _patch_instant_timeouts(monkeypatch, recorded_timeouts_ms: list[float]) -> None:
    """Replaces cg_comms.relay.reliability's module-level `asyncio` with a stand-in whose
    `wait_for` records the timeout (in ms) it was given, closes the awaited coroutine to avoid a
    "never awaited" warning, and immediately raises TimeoutError — simulating an ACK that never
    arrives without actually waiting out the timeout."""

    async def fake_wait_for(awaitable, timeout):
        recorded_timeouts_ms.append(timeout * 1000)
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise asyncio.TimeoutError()

    fake_asyncio = types.SimpleNamespace(
        wait_for=fake_wait_for, Event=asyncio.Event, TimeoutError=asyncio.TimeoutError
    )
    monkeypatch.setattr(reliability_mod, "asyncio", fake_asyncio)


# --- config parity: §6's stated retransmit numbers --------------------------------------------------

def test_config_defaults_match_plan_ss6():
    cfg = ReliabilityConfig()
    assert cfg.retry_timeout_ms == 200
    assert cfg.retry_backoff_factor == pytest.approx(2.0)
    assert cfg.retry_max_attempts == 5
    assert cfg.retry_jitter_pct == pytest.approx(0.2)


# --- ACK before timeout: no retry at all -------------------------------------------------------------

async def test_ack_received_before_timeout_prevents_retry():
    sent: list[bytes] = []

    async def send_frame(frame: bytes) -> None:
        sent.append(frame)

    sender = ReliabilitySender(send_frame=send_frame, base_timeout_ms=50, max_attempts=5)
    task = asyncio.create_task(sender.send_reliable(seq=7, frame=b"hello"))
    await asyncio.sleep(0)  # let send_reliable run up to its first await
    sender.on_ack(7)
    await task
    assert sent == [b"hello"]


# --- retry-not-ACKed-in-time: a message is resent, growing the timeout by backoff_factor -----------

async def test_message_not_acked_within_timeout_is_retried(monkeypatch):
    recorded: list[float] = []
    _patch_instant_timeouts(monkeypatch, recorded)

    sent: list[bytes] = []

    async def send_frame(frame: bytes) -> None:
        sent.append(frame)

    sender = ReliabilitySender(
        send_frame=send_frame, base_timeout_ms=200, backoff_factor=2.0, max_attempts=5, jitter_pct=0.0
    )
    with pytest.raises(LinkDegraded):
        await sender.send_reliable(seq=1, frame=b"x")

    assert len(sent) == 5  # retried up to max_attempts, never ACKed
    assert recorded == pytest.approx([200, 400, 800, 1600, 3200])  # backoff x2 each attempt


async def test_backoff_uses_configured_factor_not_hardcoded_two(monkeypatch):
    recorded: list[float] = []
    _patch_instant_timeouts(monkeypatch, recorded)

    async def send_frame(frame: bytes) -> None:
        return None

    sender = ReliabilitySender(
        send_frame=send_frame, base_timeout_ms=100, backoff_factor=3.0, max_attempts=4, jitter_pct=0.0
    )
    with pytest.raises(LinkDegraded):
        await sender.send_reliable(seq=2, frame=b"y")

    assert recorded == pytest.approx([100, 300, 900, 2700])


async def test_backoff_timeouts_stay_within_jitter_tolerance(monkeypatch):
    recorded: list[float] = []
    _patch_instant_timeouts(monkeypatch, recorded)

    async def send_frame(frame: bytes) -> None:
        return None

    sender = ReliabilitySender(
        send_frame=send_frame, base_timeout_ms=200, backoff_factor=2.0, max_attempts=5, jitter_pct=0.2
    )
    with pytest.raises(LinkDegraded):
        await sender.send_reliable(seq=3, frame=b"z")

    expected_unjittered = [200, 400, 800, 1600, 3200]
    assert len(recorded) == len(expected_unjittered)
    for actual, expected in zip(recorded, expected_unjittered):
        assert expected * 0.8 <= actual <= expected * 1.2


# --- exhaustion: after retry_max_attempts with no ACK, the link is reported degraded ----------------

async def test_link_degraded_raised_after_max_attempts_exhausted(monkeypatch):
    recorded: list[float] = []
    _patch_instant_timeouts(monkeypatch, recorded)

    async def send_frame(frame: bytes) -> None:
        return None

    sender = ReliabilitySender(send_frame=send_frame, base_timeout_ms=10, max_attempts=5, jitter_pct=0.0)
    with pytest.raises(LinkDegraded) as exc_info:
        await sender.send_reliable(seq=42, frame=b"never-acked")
    assert exc_info.value.seq == 42


async def test_max_attempts_one_gives_a_single_attempt_before_degraded(monkeypatch):
    recorded: list[float] = []
    _patch_instant_timeouts(monkeypatch, recorded)

    sent: list[bytes] = []

    async def send_frame(frame: bytes) -> None:
        sent.append(frame)

    sender = ReliabilitySender(send_frame=send_frame, base_timeout_ms=10, max_attempts=1, jitter_pct=0.0)
    with pytest.raises(LinkDegraded):
        await sender.send_reliable(seq=5, frame=b"only-once")
    assert len(sent) == 1


async def test_pending_state_cleaned_up_after_exhaustion(monkeypatch):
    recorded: list[float] = []
    _patch_instant_timeouts(monkeypatch, recorded)

    async def send_frame(frame: bytes) -> None:
        return None

    sender = ReliabilitySender(send_frame=send_frame, base_timeout_ms=10, max_attempts=2, jitter_pct=0.0)
    with pytest.raises(LinkDegraded):
        await sender.send_reliable(seq=9, frame=b"x")
    assert sender._pending == {}
