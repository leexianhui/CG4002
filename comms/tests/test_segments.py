"""Event-mode uplink: SEGMENT_CHUNK codec behaviour, relay reassembly, resample/fit, and the session
path that turns one hardware-accepted segment into exactly one classifier request."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
import structlog
from test_windowing import make_windower

from cg_comms.messages.enums import Weapon
from cg_comms.messages.models import ActionResult
from cg_comms.protocol.codec import DecodedPacket, DropReason, decode_packet, encode_packet
from cg_comms.protocol.constants import SEGMENT_CHUNK_SAMPLES, Direction, PacketType
from cg_comms.protocol.crypto import SequenceTracker
from cg_comms.relay.segments import SegmentAssembler, classifier_rows, fit_length, resample
from cg_comms.relay.session import Session, SessionConfig
from cg_comms.relay.ultra96 import Ultra96Client
from cg_comms.transport.loopback import LoopbackTransport

PSK = bytes(range(16))


def chunk_fields(segment_id=7, index=0, count=1, rows=None, first_ms=1000, weapon=1, rate=50):
    if rows is None:
        rows = [(i, -i, 2048, 16 * i, 0, 0) for i in range(SEGMENT_CHUNK_SAMPLES)]
    flat = [v for row in rows for v in row] + [0] * (6 * (SEGMENT_CHUNK_SAMPLES - len(rows)))
    return dict(segment_id=segment_id, chunk_index=index, chunk_count=count, weapon_id=weapon, flags=0,
                sample_rate_hz=rate, first_sample_ms=first_ms, sample_count=len(rows), reserved=0,
                samples=tuple(flat))


def segment_chunks(segment_id, total_rows, rate=50, weapon=1, first_ms=1000):
    """Splits `total_rows` synthetic samples into chunk field dicts, the way the hub does."""
    rows = [(i, -i, 2048, i % 300, 0, -i % 50) for i in range(total_rows)]
    count = -(-total_rows // SEGMENT_CHUNK_SAMPLES)
    return [chunk_fields(segment_id, i, count, rows[i * 16:(i + 1) * 16],
                         first_ms + i * 16 * 1000 // rate, weapon, rate) for i in range(count)], rows


def frame(fields, seq, kind=PacketType.SEGMENT_CHUNK, sid=10):
    return encode_packet(packet_type=kind, seq=seq & 0xFFFF, ext_seq=seq, player_id=1, fields=fields,
                         psk=PSK, session_id=sid, direction=Direction.DEVICE_TO_LAPTOP)


def decode(raw, tracker):
    return decode_packet(raw, psk=PSK, session_id=10, direction=Direction.DEVICE_TO_LAPTOP,
                         sequence_tracker=tracker, reliable_duplicates=True)


# -- codec -----------------------------------------------------------------------------------------


def test_chunk_is_234_bytes_and_fits_the_ble_mtu():
    assert len(frame(chunk_fields(), 1)) == 234


def test_windowed_chunks_lost_then_retransmitted_after_later_ones_are_accepted():
    """Up to four chunks are in flight: chunk 0 lost, 1-3 arrive, then 0's retransmit must still be
    accepted (a per-type watermark would call it a replay), and an exact resend is a duplicate."""
    tracker = SequenceTracker()
    frames = [frame(chunk_fields(index=i, count=4), seq=i + 1) for i in range(4)]
    for raw in frames[1:]:
        assert isinstance(decode(raw, tracker), DecodedPacket)
    late = decode(frames[0], tracker)
    assert isinstance(late, DecodedPacket) and not late.duplicate
    again = decode(frames[0], tracker)
    assert isinstance(again, DecodedPacket) and again.duplicate


def test_rfid_watermark_is_unaffected_by_interleaved_chunks():
    tracker = SequenceTracker()
    rfid = dict(device_timestamp_ms=1, weapon_id=1, reserved=0, tag_uid=b"123456")
    assert isinstance(decode(frame(rfid, 5, PacketType.RFID_EVENT), tracker), DecodedPacket)
    assert isinstance(decode(frame(chunk_fields(), 4), tracker), DecodedPacket)
    stale = decode(frame(rfid, 3, PacketType.RFID_EVENT), tracker)
    assert stale.reason is DropReason.REPLAY


def test_forged_chunk_does_not_poison_the_window():
    tracker = SequenceTracker()
    raw = bytearray(frame(chunk_fields(), 9))
    raw[20] ^= 1
    from cg_comms.protocol import crc
    raw[-2:] = crc.crc16(bytes(raw[:-2])).to_bytes(2, "little")
    assert decode(bytes(raw), tracker).reason is DropReason.AUTH_FAILED
    assert isinstance(decode(frame(chunk_fields(), 9), tracker), DecodedPacket)


# -- assembler -------------------------------------------------------------------------------------


def test_reassembles_out_of_order_with_duplicates_exactly_once():
    chunks, rows = segment_chunks(42, 70)
    assembler = SegmentAssembler()
    order = [chunks[3], chunks[0], chunks[3], chunks[4], chunks[2], chunks[0]]
    assert all(assembler.add(c, now=0.0) is None for c in order)
    segment = assembler.add(chunks[1], now=0.1)
    assert segment is not None and segment.samples == rows and segment.first_sample_ms == 1000
    assert segment.chunk_count == 5 and segment.weapon_id == 1
    assert all(assembler.add(c, now=0.2) is None for c in chunks)  # late duplicates: no second segment


def test_timeout_discards_partial_so_a_lone_late_chunk_never_completes():
    chunks, _ = segment_chunks(1, 40)
    assembler = SegmentAssembler(timeout_ms=2000)
    assembler.add(chunks[0], now=0.0)
    assembler.add(chunks[1], now=1.0)
    with structlog.testing.capture_logs() as logs:
        assert assembler.add(chunks[2], now=2.5) is None
    assert [e["reason"] for e in logs if e["event"] == "segment_abandoned"] == ["timeout"]


def test_capacity_abandons_the_oldest_partial():
    assembler = SegmentAssembler(max_partials=2)
    for sid in (1, 2, 3):
        assembler.add(segment_chunks(sid, 40)[0][0], now=float(sid))
    assert sorted(assembler._partials) == [2, 3]


@pytest.mark.parametrize("bad", [dict(chunk_count=0), dict(chunk_index=2, chunk_count=2),
                                 dict(sample_count=0), dict(sample_count=17), dict(sample_rate_hz=0),
                                 dict(chunk_count=33)])
def test_invalid_chunk_fields_are_rejected(bad):
    fields = {**chunk_fields(count=2), **bad}
    assert SegmentAssembler().add(fields, now=0.0) is None


def test_inconsistent_chunk_count_within_a_segment_is_rejected():
    assembler = SegmentAssembler()
    assembler.add(chunk_fields(index=0, count=2), now=0.0)
    assert assembler.add(chunk_fields(index=1, count=3), now=0.0) is None
    assert assembler.add(chunk_fields(index=1, count=2), now=0.0) is not None


# -- resample / fit ---------------------------------------------------------------------------------


def test_resample_100_to_50_keeps_span_and_endpoints():
    rows = [[float(i)] * 6 for i in range(101)]
    out = resample(rows, 100.0, 50.0)
    assert len(out) == 51 and out[0][0] == 0.0 and out[-1][0] == 100.0 and out[1][0] == 2.0


def test_resample_interpolates_between_samples():
    out = resample([[0.0] * 6, [10.0] * 6, [20.0] * 6], 50.0, 100.0)
    assert [r[0] for r in out] == [0.0, 5.0, 10.0, 15.0, 20.0]


def test_fit_centres_long_and_edge_pads_short():
    long = [[float(i)] * 6 for i in range(130)]
    assert [r[0] for r in fit_length(long, 100)][:2] == [15.0, 16.0]
    short = [[float(i)] * 6 for i in range(1, 5)]
    assert [r[0] for r in fit_length(short, 9)] == [1, 1, 1, 2, 3, 4, 4, 4, 4]


def test_classifier_rows_scale_to_physical_units_and_exact_length():
    chunks, _ = segment_chunks(5, 48)
    assembler = SegmentAssembler()
    segment = [assembler.add(c, now=0.0) for c in chunks][-1]
    rows = classifier_rows(segment, 50.0, 100)
    assert len(rows) == 100 and all(len(r) == 6 for r in rows)
    assert rows[50][2] == pytest.approx(1.0)  # 2048 counts = 1 g


# -- session -----------------------------------------------------------------------------------------


def session(segment_mode=True):
    transport, _ = LoopbackTransport.pair()
    config = SessionConfig(1, PSK, 8, 0, .1, 2, 20, 200, 3, 10, 2, 3, 0, .6, segment_mode=segment_mode)
    subject = Session(transport, config, make_windower(window_samples=100, stride_frames=10),
                      Ultra96Client("127.0.0.1", 1, 100, 2), AsyncMock(), AsyncMock())
    subject.session_id = 10
    subject._time_sync.seed(0)
    subject._send_ack = AsyncMock()
    return subject


async def test_one_segment_one_classifier_request_every_chunk_acked():
    subject = session()
    requests = []

    async def classify(window):
        requests.append(window)
        return ActionResult(timestamp=window.timestamp, player_id=1, request_id=window.request_id,
                            action="VERTICAL_SWING", action_confidence=.9, guard_state="IDLE",
                            guard_confidence=1)

    subject.ultra96.request = classify
    chunks, _ = segment_chunks(99, 75, weapon=2, first_ms=5000)
    frames = [frame(c, seq=i + 1) for i, c in enumerate(chunks)]
    with structlog.testing.capture_logs() as logs:
        for raw in [frames[1], frames[0], frames[1]] + frames[2:]:
            await subject._handle_frame(raw)
        await asyncio.gather(*subject._classification_tasks)
    assert len(requests) == 1
    window = requests[0]
    assert len(window.dominant) == 100 and len(window.offhand) == 100
    assert window.weapon == Weapon.SPEAR and window.sample_rate_hz == 50.0
    assert window.window_start_ts == 5000 and window.window_end_ts == 5000 + 1480
    assert window.timestamp >= window.window_end_ts  # stamped on arrival, so the engine keeps it
    assert subject._send_ack.await_count == len(frames) + 1  # the duplicate is ACKed again
    action = subject.on_action_result.call_args.args[0]
    assert action.action.value == "VERTICAL_SWING" and action.weapon == Weapon.SPEAR
    events = [e["event"] for e in logs]
    assert events.count("segment_received") == 1 and events.count("segment_classified") == 1
    classified = next(e for e in logs if e["event"] == "segment_classified")
    assert classified["segment_id"] == 99 and classified["passes_gate"] is True
    assert subject._segment_requests == {}


async def test_segment_without_any_weapon_is_suppressed_not_classified():
    subject = session()
    subject.ultra96.request = AsyncMock()
    chunks, _ = segment_chunks(3, 16, weapon=0)
    with structlog.testing.capture_logs() as logs:
        await subject._handle_frame(frame(chunks[0], 1))
    subject.ultra96.request.assert_not_called()
    assert any(e["event"] == "segment_suppressed" and e["reason"] == "no_weapon" for e in logs)


async def test_segment_falls_back_to_confirmed_weapon():
    subject = session()
    subject.windower.on_weapon_confirmed(Weapon.MACE)
    window = subject._segment_window(SegmentAssembler().add(segment_chunks(3, 16, weapon=0)[0][0], 0.0))
    assert window.weapon == Weapon.MACE


async def test_segment_mode_off_acks_but_never_classifies():
    subject = session(segment_mode=False)
    subject.ultra96.request = AsyncMock()
    await subject._handle_frame(frame(segment_chunks(3, 16)[0][0], 1))
    subject._send_ack.assert_awaited_once_with(1)
    subject.ultra96.request.assert_not_called()


async def test_classifier_capacity_drop_does_not_leak_segment_bookkeeping():
    subject = session()
    subject.ultra96.max_in_flight = 0
    with structlog.testing.capture_logs() as logs:
        await subject._handle_frame(frame(segment_chunks(8, 16)[0][0], 1))
    assert any(e["event"] == "window_dropped" and e["reason"] == "classifier_capacity" for e in logs)
    assert subject._segment_requests == {}
