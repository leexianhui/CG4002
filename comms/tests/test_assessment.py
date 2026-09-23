"""Comms-assessment features: echo pipeline, verified file transfer, throughput meter, keyboard gating,
console tagging/filtering and the transfer fixture. The loopback tests run the real relay Session
against mock/firebeetle_sim.py, which answers the assessment packets through mock/assess_device.py
(the Python mirror of firmware_demo/lib/cg_protocol/cg_assess.h)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from unittest.mock import AsyncMock

import pytest
import structlog

from cg_comms.config import AssessmentConfig
from cg_comms.logging_setup import (
    _ConsoleEventFilter,
    _player_tagger,
    console_event_visible,
    set_console_event_visible,
)
from cg_comms.messages.enums import Weapon
from cg_comms.mock.assess_device import FileRxState, FileVerdict, build_echo_response
from cg_comms.mock.firebeetle_sim import FireBeetleSim
from cg_comms.protocol.codec import DecodedPacket, Header, encode_packet
from cg_comms.protocol.constants import Direction, PacketType
from cg_comms.protocol.crc import crc16, crc16_update
from cg_comms.relay.assess_echo import EchoPipeline, wait_for_echo
from cg_comms.relay.assess_file import MAX_FILE_BYTES, FileTooLarge, chunk_file, run_file_transfer
from cg_comms.relay.assess_keyboard import AssessKeyboard
from cg_comms.relay.reliability import LinkDegraded
from cg_comms.relay.session import Session, SessionConfig
from cg_comms.relay.throughput import ThroughputMeter, combine, describe_frame
from cg_comms.relay.ultra96 import Ultra96Client
from cg_comms.relay.windowing import PlayerWindower
from cg_comms.tools.assessment_fixture import DEFAULT_SIZE, build_fixture
from cg_comms.transport.loopback import LoopbackTransport

PSK = bytes(range(16))


# ---------------------------------------------------------------------------------------------
# EchoPipeline
# ---------------------------------------------------------------------------------------------


def _pipeline() -> EchoPipeline:
    return EchoPipeline(player_id=1, rng=random.Random(7))


def _device_answer(fields: dict) -> dict:
    return build_echo_response(fields, now_ms=1234)


def test_echo_verified_then_duplicate_is_info_not_warning():
    echo = _pipeline()
    pending, fields = echo.build_request(ord("a"))
    resp = _device_answer(fields)
    with structlog.testing.capture_logs() as logs:
        assert echo.on_response(resp) is pending
        assert echo.on_response(resp) is None  # the device re-sent it (our ACK was lost)
    assert pending.matched and pending.event.is_set()
    assert echo.stats() == {"sent": 0, "verified": 1, "mismatched": 0, "lost": 0, "in_flight": 0}
    dup = [e for e in logs if e["event"] == "assess_echo_duplicate_response"]
    assert dup and dup[0]["log_level"] == "info" and dup[0]["earlier_outcome"] == "verified"
    assert not [e for e in logs if e["event"] == "assess_echo_unknown_trace"]


def test_echo_mismatch_is_detected_against_send_time_expectation():
    echo = _pipeline()
    pending, fields = echo.build_request(ord("b"))
    resp = _device_answer(fields)
    resp["value_a"] = (resp["value_a"] + 1) if resp["value_a"] < 32767 else resp["value_a"] - 1
    assert echo.on_response(resp) is pending
    assert not pending.matched and "values" in (pending.mismatch or "")
    assert echo.mismatched == 1 and echo.verified == 0


def test_echo_wrong_keycode_or_transform_is_a_mismatch():
    echo = _pipeline()
    pending, fields = echo.build_request(ord("c"))
    resp = _device_answer(fields)
    resp["transform_id"] = resp["transform_id"] % 3 + 1
    echo.on_response(resp)
    assert not pending.matched and "transform_id" in (pending.mismatch or "")


async def test_echo_expire_counts_lost_and_late_response_is_info():
    echo = _pipeline()
    pending, fields = echo.build_request(ord("d"))
    echo.mark_sent()
    assert await wait_for_echo(pending, 0.01) is False
    echo.expire(pending.trace_id, 0.01)
    assert pending.event.is_set() and echo.lost == 1 and echo.stats()["in_flight"] == 0
    with structlog.testing.capture_logs() as logs:
        assert echo.on_response(_device_answer(fields)) is None
    assert echo.verified == 0  # a late response never flips a timed-out trace to verified
    assert [e["earlier_outcome"] for e in logs if e["event"] == "assess_echo_duplicate_response"] == [
        "timed_out"
    ]


def test_echo_expire_after_resolution_is_a_noop():
    echo = _pipeline()
    pending, fields = echo.build_request(ord("e"))
    echo.on_response(_device_answer(fields))
    echo.expire(pending.trace_id, 1.0)
    assert echo.lost == 0 and echo.verified == 1


def test_echo_unknown_trace_still_warns():
    echo = _pipeline()
    _pending, fields = echo.build_request(ord("f"))
    resp = _device_answer(fields)
    resp["trace_id"] = 999_999
    with structlog.testing.capture_logs() as logs:
        assert echo.on_response(resp) is None
    assert [e["log_level"] for e in logs if e["event"] == "assess_echo_unknown_trace"] == ["warning"]


def test_echo_abandon_all_retires_and_counts_each_once():
    echo = _pipeline()
    a, _ = echo.build_request(ord("g"))
    b, _ = echo.build_request(ord("h"))
    echo.abandon_all("test")
    echo.abandon_all("again")
    assert echo.lost == 2 and a.event.is_set() and b.event.is_set()


def test_echo_resolved_memory_is_bounded():
    from cg_comms.relay import assess_echo

    echo = _pipeline()
    for _ in range(assess_echo._RESOLVED_MEMORY + 50):
        _p, fields = echo.build_request(ord("i"))
        echo.on_response(_device_answer(fields))
    assert len(echo._resolved) == assess_echo._RESOLVED_MEMORY


# ---------------------------------------------------------------------------------------------
# File chunking, device receive state, CRC continuation
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("size", [1, 31, 32, 33, 1037, MAX_FILE_BYTES])
def test_chunk_file_edges_reassemble_through_device_state(size):
    data = build_fixture(size)
    chunks = chunk_file(data)
    assert len(chunks) == -(-size // 32)
    rx = FileRxState()
    for c in chunks:
        verdict, rx, _ = rx.evaluate(c)
        assert verdict is FileVerdict.APPENDED
    assert rx.complete and rx.data == data and rx.running_crc == crc16(data)
    assert rx.received_bytes == size


def test_chunk_file_rejects_empty_and_oversize():
    with pytest.raises(ValueError):
        chunk_file(b"")
    with pytest.raises(FileTooLarge):
        chunk_file(bytes(MAX_FILE_BYTES + 1))


def _chunk(**over) -> dict:
    base = {"transfer_id": 1, "chunk_index": 0, "total_chunks": 2, "chunk_len": 32, "reserved": 0, "data": bytes(32)}
    base.update(over)
    return base


@pytest.mark.parametrize("over, reason", [
    ({"reserved": 1}, "reserved!=0"),
    ({"chunk_len": 0}, "chunk_len"),
    ({"chunk_len": 33}, "chunk_len"),
    ({"total_chunks": 0}, "index>=total"),
    ({"chunk_index": 2}, "index>=total"),
    ({"chunk_len": 10}, "short_non_final"),
    ({"chunk_index": 1, "chunk_len": 4, "data": b"abcd" + b"\x01" + bytes(27)}, "padding!=0"),
])
def test_file_rx_malformed_leaves_state_unchanged(over, reason):
    rx = FileRxState()
    verdict, nxt, why = rx.evaluate(_chunk(**over))
    assert verdict is FileVerdict.MALFORMED and nxt is rx and why == reason


def test_file_rx_out_of_sequence_keeps_state_and_index0_restarts():
    rx = FileRxState()
    _, rx, _ = rx.evaluate(_chunk(data=b"\x11" * 32, total_chunks=3))
    verdict, same, _ = rx.evaluate(_chunk(chunk_index=2, total_chunks=3))
    assert verdict is FileVerdict.OUT_OF_SEQUENCE and same is rx
    verdict, fresh, _ = rx.evaluate(_chunk(data=b"\x22" * 32, total_chunks=3))
    assert verdict is FileVerdict.APPENDED and fresh.received_bytes == 32 and fresh.data == b"\x22" * 32


def test_crc16_update_continuation_equals_one_shot():
    data = build_fixture()
    crc = 0xFFFF
    for i in range(0, len(data), 32):
        crc = crc16_update(crc, data[i:i + 32])
    assert crc == crc16(data)
    assert crc16(b"123456789") == 0x29B1  # CRC-16/CCITT-FALSE check value


def test_fixture_is_deterministic_and_bounded():
    a, b = build_fixture(), build_fixture()
    assert a == b and len(a) == DEFAULT_SIZE and DEFAULT_SIZE % 32 != 0
    assert b"\x00" * 16 in a and bytes(range(0x80, 0x100)) in a
    with pytest.raises(ValueError):
        build_fixture(0)
    with pytest.raises(ValueError):
        build_fixture(MAX_FILE_BYTES + 1)


# ---------------------------------------------------------------------------------------------
# run_file_transfer against a scripted device (no transport)
# ---------------------------------------------------------------------------------------------


class _ScriptedDevice:
    """Plays the device end of run_file_transfer: applies each chunk to a FileRxState and queues the
    FILE_ACK, with knobs to lose responses, corrupt them, or fail the link."""

    def __init__(self, acks: asyncio.Queue) -> None:
        self.acks = acks
        self.rx = FileRxState()
        self.last_ack: dict | None = None
        self.lose_response_for: set[int] = set()  # chunk indices whose first FILE_ACK is lost
        self.corrupt_crc_at: int | None = None
        self.link_fails_at: int | None = None
        self.sends = 0
        self.resends = 0

    async def send_chunk(self, fields: dict):
        self.sends += 1
        index = fields["chunk_index"]
        if index == self.link_fails_at:
            raise LinkDegraded(index)
        _, self.rx, _ = self.rx.evaluate(fields)
        self.last_ack = {"transfer_id": fields["transfer_id"], "chunk_index": index, "running_crc": self.rx.running_crc,
                         "received_bytes": self.rx.received_bytes}
        if index == self.corrupt_crc_at:
            self.last_ack["running_crc"] ^= 0x0001
        if index in self.lose_response_for:
            self.lose_response_for.discard(index)
        else:
            self.acks.put_nowait(dict(self.last_ack))

        async def resend() -> None:
            # Exact retransmit: the device re-sends its last response, never re-applies the chunk.
            self.resends += 1
            assert self.last_ack is not None
            self.acks.put_nowait(dict(self.last_ack))

        return resend


async def _transfer(device: _ScriptedDevice, data: bytes, **kw):
    return await run_file_transfer(data, player_id=1, send_chunk=device.send_chunk,
                                   acks=device.acks, ack_timeout_s=0.05, **kw)


async def test_transfer_success_matches_source():
    acks: asyncio.Queue = asyncio.Queue()
    device = _ScriptedDevice(acks)
    data = build_fixture()
    r = await _transfer(device, data)
    assert r.ok and r.confirmed_bytes == len(data) and r.chunks_confirmed == r.total_chunks == 33
    assert r.device_crc == crc16(data) == r.source_crc
    assert r.sha256 == hashlib.sha256(data).hexdigest()
    assert device.rx.data == data


async def test_transfer_recovers_lost_file_ack_without_double_append():
    acks: asyncio.Queue = asyncio.Queue()
    device = _ScriptedDevice(acks)
    device.lose_response_for = {3, 10}
    data = build_fixture()
    r = await _transfer(device, data)
    assert r.ok and device.resends == 2 and device.rx.data == data


async def test_transfer_skips_stale_acks_and_discards_leftovers():
    acks: asyncio.Queue = asyncio.Queue()
    acks.put_nowait({"transfer_id": 0, "chunk_index": 5, "running_crc": 0, "received_bytes": 999})  # left from before
    device = _ScriptedDevice(acks)
    orig = device.send_chunk

    async def send_with_stale(fields):
        acks.put_nowait({"transfer_id": 0, "chunk_index": 99, "running_crc": 0, "received_bytes": 0})
        return await orig(fields)

    device.send_chunk = send_with_stale  # type: ignore[method-assign]
    r = await _transfer(device, build_fixture(100))
    assert r.ok


async def test_transfer_aborts_on_device_crc_mismatch():
    acks: asyncio.Queue = asyncio.Queue()
    device = _ScriptedDevice(acks)
    device.corrupt_crc_at = 4
    r = await _transfer(device, build_fixture())
    assert not r.ok and "mismatch at chunk 4" in r.reason and r.chunks_confirmed == 4
    assert r.confirmed_bytes == 4 * 32


async def test_transfer_aborts_on_link_failure():
    acks: asyncio.Queue = asyncio.Queue()
    device = _ScriptedDevice(acks)
    device.link_fails_at = 2
    r = await _transfer(device, build_fixture())
    assert not r.ok and r.reason.startswith("link:") and r.chunks_confirmed == 2


async def test_transfer_aborts_when_response_never_recovers():
    acks: asyncio.Queue = asyncio.Queue()
    device = _ScriptedDevice(acks)

    async def silent(fields):
        async def resend() -> None:
            return None
        return resend

    device.send_chunk = silent  # type: ignore[method-assign]
    r = await _transfer(device, build_fixture(40))
    assert not r.ok and "no FILE_ACK for chunk 0" in r.reason


async def test_transfer_out_of_sequence_device_state_aborts():
    acks: asyncio.Queue = asyncio.Queue()
    device = _ScriptedDevice(acks)
    # Device answers with its state unchanged (as it does for an out-of-sequence chunk).
    orig = device.send_chunk

    async def stuck(fields):
        if fields["chunk_index"] == 2:
            acks.put_nowait({"transfer_id": fields["transfer_id"], "chunk_index": 2, "running_crc": device.rx.running_crc,
                             "received_bytes": device.rx.received_bytes})

            async def resend() -> None:
                return None
            return resend
        return await orig(fields)

    device.send_chunk = stuck  # type: ignore[method-assign]
    r = await _transfer(device, build_fixture())
    assert not r.ok and "mismatch at chunk 2" in r.reason


# ---------------------------------------------------------------------------------------------
# Session <-> FireBeetleSim loopback (real codec, crypto, reliability, device mirror)
# ---------------------------------------------------------------------------------------------


def _session_config(**over) -> SessionConfig:
    base = dict(
        player_id=1, psk=PSK, handshake_samples=8, handshake_spacing_ms=10, ewma_alpha=0.1,
        outlier_rtt_multiplier=2.0, divergence_warn_ms=20.0, heartbeat_interval_ms=200,
        heartbeat_liveness_factor=10, reliability_base_timeout_ms=60, reliability_backoff_factor=2.0,
        reliability_max_attempts=5, reliability_jitter_pct=0.0, min_action_confidence=0.6,
        assessment_enabled=True, assess_seed=42, assess_echo_timeout_s=2.0, stats_interval_s=60.0,
    )
    base.update(over)
    return SessionConfig(**base)  # type: ignore[arg-type]


class _Rig:
    def __init__(self, **config_over) -> None:
        relay_t, device_t = LoopbackTransport.pair()
        self.windower = PlayerWindower(player_id=1, window_samples=4, stride_frames=2,
                                       max_stale_pct=100.0, max_span_deviation_pct=1e6,
                                       frame_rate_hz=50.0)
        self.ultra96 = Ultra96Client("127.0.0.1", 1, 100, 2)
        self.on_action = AsyncMock()
        self.session = Session(relay_t, _session_config(**config_over), self.windower, self.ultra96,
                               AsyncMock(), self.on_action)
        self.sim = FireBeetleSim(transport=device_t, psk=PSK, player_id=1)  # type: ignore[arg-type]
        self.tasks: list[asyncio.Task] = []

    async def __aenter__(self) -> _Rig:
        self.tasks = [asyncio.create_task(self.session.run()),
                      asyncio.create_task(self.sim.run([], loop_scenario=False))]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while not self.session.streaming:
            assert loop.time() < deadline, "session never reached streaming"
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *exc) -> None:
        self.session.stop()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


async def _sim_run_that_streams_forever(sim: FireBeetleSim) -> None:
    """FireBeetleSim.run([]) disconnects as soon as its empty scenario ends. Keep it alive with a
    slow heartbeat loop instead, like a real board that streams until powered off."""
    from cg_comms.mock.scenario import ScenarioStep

    await sim.run([ScenarioStep(delay_ms=100, kind="heartbeat", payload={})], loop_scenario=True)


class _LiveRig(_Rig):
    async def __aenter__(self) -> _LiveRig:
        self.tasks = [asyncio.create_task(self.session.run()),
                      asyncio.create_task(_sim_run_that_streams_forever(self.sim))]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while not self.session.streaming:
            assert loop.time() < deadline, "session never reached streaming"
            await asyncio.sleep(0.01)
        return self


async def test_loopback_echo_keystroke_verifies_through_device_mirror():
    async with _LiveRig() as rig:
        with structlog.testing.capture_logs() as logs:
            pending, verified = await rig.session.echo_keystroke(ord("k"))
        assert verified and pending.matched
        assert rig.session.echo.stats()["verified"] == 1
        # No weapon yet => the pipeline says so explicitly instead of silently stopping.
        assert any(e["event"] == "assess_echo_no_weapon" for e in logs)


async def test_loopback_echo_survives_lost_device_ack():
    async with _LiveRig() as rig:
        rig.sim.drop_assess_acks = 2
        with structlog.testing.capture_logs() as logs:
            pending, verified = await rig.session.echo_keystroke(ord("z"))
        assert verified
        assert [e for e in logs if e["event"] == "retransmit"]
        # Each retransmit made the device re-send its response; those are recognised, not warned.
        assert [e for e in logs if e["event"] == "assess_echo_duplicate_response"]
        assert not [e for e in logs if e["event"] == "assess_echo_unknown_trace"]
        assert rig.session.echo.stats()["verified"] == 1


async def test_loopback_file_transfer_verified_and_device_holds_exact_bytes():
    data = build_fixture()
    async with _LiveRig() as rig:
        r = await rig.session.send_file(data)
        assert r.ok, r.reason
        assert rig.sim.file_rx.data == data and rig.sim.file_rx.complete


async def test_loopback_file_transfer_with_lost_acks_does_not_double_append():
    data = build_fixture(200)
    async with _LiveRig() as rig:
        rig.sim.drop_assess_acks = 3
        r = await rig.session.send_file(data)
        assert r.ok, r.reason
        assert rig.sim.file_rx.data == data


async def test_loopback_file_rejects_bad_input_before_sending():
    async with _LiveRig() as rig:
        with pytest.raises(ValueError):
            await rig.session.send_file(b"")
        with pytest.raises(FileTooLarge):
            await rig.session.send_file(bytes(MAX_FILE_BYTES + 1))
        assert rig.sim.file_rx == FileRxState()
        assert not rig.session.assess_busy()


async def test_loopback_echo_blocked_while_file_transfer_holds_lock():
    async with _LiveRig() as rig:
        transfer = asyncio.create_task(rig.session.send_file(build_fixture()))
        await asyncio.sleep(0)  # let it take the lock
        assert rig.session.assess_busy()
        r = await transfer
        assert r.ok and not rig.session.assess_busy()


# ---------------------------------------------------------------------------------------------
# ThroughputMeter
# ---------------------------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def _decoded(ext_seq: int, kind: PacketType = PacketType.SENSOR_FRAME, duplicate: bool = False):
    header = Header(version=1, packet_type=int(kind), more_fragments=False, seq=ext_seq & 0xFFFF,
                    player_id=1, payload_len=42 if kind == PacketType.SENSOR_FRAME else 12)
    return DecodedPacket(header=header, ext_seq=ext_seq, fields={}, duplicate=duplicate)


def _feed(meter: ThroughputMeter, seqs, kind=PacketType.SENSOR_FRAME, duplicate=False):
    frame = bytes(68 if kind == PacketType.SENSOR_FRAME else 38)
    for s in seqs:
        meter.on_frame(frame, _decoded(s, kind, duplicate))


def test_meter_interval_floor_charges_boundary_gap_once():
    clock = _Clock()
    meter = ThroughputMeter(player_id=1, clock=clock)
    meter.start()
    _feed(meter, range(10, 20))  # 10..19, no loss
    clock.t += 1
    first = meter.report()
    assert first is not None and first["generated"] == 10 and first["lost"] == 0
    assert first["sensor_hz"] == 10.0 and first["goodput_kbps"] == round(10 * 42 * 8 / 1000, 2)
    _feed(meter, [22, 23])  # 20, 21 lost across the boundary
    clock.t += 1
    second = meter.report()
    assert second is not None and second["generated"] == 4 and second["lost"] == 2
    final = meter.report(final=True)
    assert final is not None and final["generated"] == 14 and final["lost"] == 2


def test_meter_late_frame_does_not_cancel_real_loss():
    clock = _Clock()
    meter = ThroughputMeter(player_id=1, clock=clock)
    meter.start()
    _feed(meter, [1, 2, 3])
    clock.t += 1
    meter.report()
    _feed(meter, [5, 6], kind=PacketType.HEARTBEAT)  # 4 lost
    _feed(meter, [2], kind=PacketType.RFID_EVENT)  # a late RFID retransmit re-using an old seq
    clock.t += 1
    interval = meter.report()
    assert interval is not None and interval["lost"] == 1 and interval["late_frames"] == 1


def test_meter_duplicates_are_excluded_and_counted():
    clock = _Clock()
    meter = ThroughputMeter(player_id=1, clock=clock)
    meter.start()
    _feed(meter, [1, 2, 3])
    _feed(meter, [3], duplicate=True)
    clock.t += 1
    r = meter.report()
    assert r is not None and r["frames"] == 3 and r["duplicates"] == 1 and r["lost"] == 0


def test_meter_ignores_traffic_before_start_and_reports_none():
    meter = ThroughputMeter(player_id=1, clock=_Clock())
    _feed(meter, [1, 2])
    meter.on_transport_bytes(100)
    assert meter.report() is None


def test_meter_writes_latest_json_atomically(tmp_path):
    clock = _Clock()
    path = tmp_path / "throughput-p1-latest.json"
    meter = ThroughputMeter(player_id=1, latest_path=path, clock=clock)
    meter.start()
    _feed(meter, range(5))
    clock.t += 1
    meter.report()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["player_id"] == 1 and not doc["final"] and doc["interval"]["sensor_frames"] == 5
    assert not path.with_suffix(".tmp").exists()


def test_combine_excludes_stale_and_final_relays():
    def doc(pid, t, final=False, kbps=10.0):
        return {"player_id": pid, "monotonic_s": t, "final": final,
                "interval": {"goodput_kbps": kbps, "frame_kbps": kbps * 2, "sensor_hz": 50.0}}

    both = combine([doc(1, 99), doc(2, 99)], now_monotonic=100, max_age_s=12)
    assert both["complete"] and both["total_goodput_kbps"] == 20.0
    stale = combine([doc(1, 99), doc(2, 50)], now_monotonic=100, max_age_s=12)
    assert not stale["complete"] and stale["total_goodput_kbps"] == 10.0
    assert stale["players_stale_or_final"] == [2]
    final = combine([doc(1, 99), doc(2, 99, final=True)], now_monotonic=100, max_age_s=12)
    assert not final["complete"]


def test_describe_frame_decodes_real_sensor_frame_to_physical_units():
    from cg_comms.protocol.codec import decode_packet
    from cg_comms.protocol.crypto import SequenceTracker

    fields = {"device_timestamp_ms": 5, "flags": 0, "weapon_id": 1,
              "dominant": (2048, 0, -1024, 164, 0, 0), "offhand": (0,) * 6, "torso": (0,) * 6}
    frame = encode_packet(packet_type=PacketType.SENSOR_FRAME, seq=7, ext_seq=7, player_id=1,
                          fields=fields, psk=PSK, session_id=3, direction=Direction.DEVICE_TO_LAPTOP)
    decoded = decode_packet(frame, psk=PSK, session_id=3, direction=Direction.DEVICE_TO_LAPTOP,
                            sequence_tracker=SequenceTracker())
    assert isinstance(decoded, DecodedPacket)
    d = describe_frame(frame, decoded)
    assert d["type"] == "SENSOR_FRAME(0x10)" and d["frame_bytes"] == 68
    assert d["len_ct_plus_tag"] == 42 + 16
    assert d["readings"]["dominant"] == {"accel_g": [1.0, 0.0, -0.5], "gyro_dps": [10.0, 0.0, 0.0]}
    assert d["hex_header"].startswith("c9")
    assert len(bytes.fromhex(d["hex_tag"].replace(" ", ""))) == 16


# ---------------------------------------------------------------------------------------------
# Keyboard gating (pure: no console)
# ---------------------------------------------------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.streaming = True
        self.busy = False
        self.echoes: list[int] = []
        self.files: list[bytes] = []
        self.gate = asyncio.Event()

    def assess_busy(self) -> bool:
        return self.busy

    async def echo_keystroke(self, keycode: int):
        self.echoes.append(keycode)
        await self.gate.wait()
        return object(), True

    async def send_file(self, data: bytes):
        self.files.append(data)
        await self.gate.wait()


def _keyboard(session, *, weapon=Weapon.SWORD, upstream=(True, True)) -> AssessKeyboard:
    return AssessKeyboard(player_id=1, get_session=lambda: session, upstream_ready=lambda: upstream,
                          current_weapon=lambda: weapon, file_bytes=b"abc", file_label="t")


async def test_keyboard_rejects_with_reason_and_never_queues():
    s = _FakeSession()
    kb = _keyboard(s)
    kb.handle_key("a")
    await asyncio.sleep(0)
    assert s.echoes == [ord("a")]
    with structlog.testing.capture_logs() as logs:
        kb.handle_key("b")  # first echo still in flight
        kb.handle_key("F")
    assert [e["reason"] for e in logs if e["event"] == "assess_key_rejected"] == ["BUSY", "BUSY"]
    s.gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert s.echoes == [ord("a")]  # nothing was queued
    await kb.cancel_op()


@pytest.mark.parametrize("setup, key, reason", [
    (lambda s: setattr(s, "streaming", False), "a", "OFFLINE"),
    (lambda s: setattr(s, "busy", True), "a", "BUSY"),
    (lambda s: None, "a", "NO_WEAPON"),
    (lambda s: None, "a", "UPSTREAM_OFFLINE"),
])
async def test_keyboard_reject_reasons(setup, key, reason):
    s = _FakeSession()
    setup(s)
    kb = _keyboard(s, weapon=None if reason == "NO_WEAPON" else Weapon.SWORD,
                   upstream=(True, False) if reason == "UPSTREAM_OFFLINE" else (True, True))
    with structlog.testing.capture_logs() as logs:
        kb.handle_key(key)
    assert [e["reason"] for e in logs if e["event"] == "assess_key_rejected"] == [reason]
    assert s.echoes == []


async def test_keyboard_no_session_is_offline_and_file_ignores_weapon_gate():
    kb = _keyboard(None)
    with structlog.testing.capture_logs() as logs:
        kb.handle_key("F")
    assert [e["reason"] for e in logs if e["event"] == "assess_key_rejected"] == ["OFFLINE"]
    s = _FakeSession()
    kb = _keyboard(s, weapon=None, upstream=(False, False))
    kb.handle_key("F")
    await asyncio.sleep(0)
    assert s.files == [b"abc"]
    await kb.cancel_op()


async def test_keyboard_toggles_packet_view_and_ignores_other_keys():
    kb = _keyboard(_FakeSession())
    before = console_event_visible("packet_sample")
    try:
        kb.handle_key("D")
        assert console_event_visible("packet_sample") is not before
        with structlog.testing.capture_logs() as logs:
            kb.handle_key("!")
        assert [e["event"] for e in logs] == ["assess_key_ignored"]
    finally:
        set_console_event_visible("packet_sample", before)


async def test_keyboard_run_returns_without_console():
    # Under pytest stdin is not a console, so run() must disable itself instead of blocking.
    await asyncio.wait_for(_keyboard(_FakeSession()).run(), 1.0)


# ---------------------------------------------------------------------------------------------
# Console tagging / filtering, config
# ---------------------------------------------------------------------------------------------


def test_player_tagger_prefixes_per_component_only():
    tag = _player_tagger(colors=False)
    assert tag(None, "info", {"event": "x", "component": "relay-p2"})["event"] == "[P2] x"
    assert tag(None, "info", {"event": "x", "component": "engine"})["event"] == "x"
    coloured = _player_tagger(colors=True)(None, "info", {"event": "x", "component": "relay-p1"})
    assert coloured["event"].startswith("\x1b[1;36m[P1]")


def test_console_filter_hides_packet_sample_by_default():
    f = _ConsoleEventFilter()

    def rec(msg):
        return logging.LogRecord("n", logging.INFO, "p", 1, msg, None, None)

    assert console_event_visible("packet_sample") is False
    assert f.filter(rec({"event": "packet_sample"})) is False
    assert f.filter(rec({"event": "throughput"})) is True
    assert f.filter(rec("plain string record")) is True


def test_assessment_config_rejects_typos_and_bad_values():
    with pytest.raises(ValueError):
        AssessmentConfig(enabled=True, echo_timeout=5)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        AssessmentConfig(stats_interval_s=0)
    assert AssessmentConfig().stats_interval_s == 5.0
