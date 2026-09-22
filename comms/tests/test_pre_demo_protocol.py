"""Fault regressions for the actual codec/session boundary; no radio behavior is implied."""

import asyncio
from unittest.mock import AsyncMock

import pytest
import structlog
from test_windowing import RAW_SAMPLE, feed_all, make_windower

from cg_comms.messages.enums import Weapon
from cg_comms.protocol.codec import DecodedPacket, DropReason, decode_packet, encode_packet
from cg_comms.protocol.constants import FLAG_DOM_STALE, FLAG_MOTION_ACTIVE, Direction, PacketType
from cg_comms.protocol.crypto import OutgoingSequenceCounter, SequenceTracker, derive_nonce
from cg_comms.relay.session import HandshakeFailed, Session, SessionConfig
from cg_comms.relay.ultra96 import Ultra96Client
from cg_comms.transport.loopback import LoopbackTransport

PSK = bytes(range(16))


def packet(kind=PacketType.RFID_EVENT, seq=1, sid=10, pid=1, **fields):
    if not fields:
        fields = dict(device_timestamp_ms=100, weapon_id=1, reserved=0, tag_uid=b"123456")
    return encode_packet(packet_type=kind, seq=seq & 65535, ext_seq=seq,
                         player_id=pid, fields=fields, psk=PSK, session_id=sid,
                         direction=Direction.DEVICE_TO_LAPTOP)


def decode(frame, tracker, sid=10):
    return decode_packet(frame, psk=PSK, session_id=sid, direction=Direction.DEVICE_TO_LAPTOP,
                         sequence_tracker=tracker, reliable_duplicates=True)


def session(transport=None, pid=1):
    if transport is None:
        transport, _ = LoopbackTransport.pair()
    config = SessionConfig(pid, PSK, 8, 0, .1, 2, 20, 200, 3, 10, 2, 3, 0, .6)
    return Session(transport, config, make_windower(player_id=pid),
                   Ultra96Client("127.0.0.1", 1, 100, 2), AsyncMock(), AsyncMock())


@pytest.mark.parametrize("garbage", [False, True])
async def test_handshake_deadline_enforced_without_valid_response(garbage):
    relay, board = LoopbackTransport.pair()
    subject = session(relay)

    async def noise():
        while True:
            await board.send(b"garbage")
            await asyncio.sleep(.001)

    producer = asyncio.create_task(noise()) if garbage else None
    try:
        with pytest.raises(HandshakeFailed):
            await asyncio.wait_for(subject._next_decoded(PacketType.HELLO, .03), .3)
    finally:
        if producer:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)


async def test_coalesced_handshake_frames_are_not_lost():
    relay, board = LoopbackTransport.pair()
    subject = session(relay)
    subject.session_id = 10
    await board.send(packet(PacketType.TIME_SYNC_REQ, 1, t0=100)
                     + packet(PacketType.TIME_SYNC_REQ, 2, t0=120))
    first = await subject._next_decoded(PacketType.TIME_SYNC_REQ, .1)
    second = await subject._next_decoded(PacketType.TIME_SYNC_REQ, .1)
    assert [first.fields["t0"], second.fields["t0"]] == [100, 120]


def test_reliable_first_arrival_and_duplicate_survive_more_than_64_sensor_packets():
    tracker = SequenceTracker()
    lost = packet(seq=1)
    for seq in range(2, 202):
        assert isinstance(decode(packet(PacketType.ACK, seq, acked_seq=100), tracker), DecodedPacket)
    first = decode(lost, tracker)
    assert isinstance(first, DecodedPacket) and not first.duplicate
    retry = decode(lost, tracker)
    assert isinstance(retry, DecodedPacket) and retry.duplicate
    assert decode(packet(seq=1, weapon_id=2, device_timestamp_ms=101, reserved=0, tag_uid=b"123456"),
                  tracker).reason == DropReason.REPLAY


def test_bad_auth_does_not_consume_sequence_or_poison_new_session():
    tracker = SequenceTracker()
    wrong_session = decode(packet(seq=1000, sid=20), tracker)
    assert wrong_session.reason == DropReason.AUTH_FAILED
    assert isinstance(decode(packet(seq=1), tracker), DecodedPacket)
    fresh = SequenceTracker()
    assert isinstance(decode(packet(seq=0, sid=20), fresh, sid=20), DecodedPacket)
    assert decode(packet(seq=1), fresh, sid=20).reason == DropReason.AUTH_FAILED


def test_generated_dropped_frames_burn_sequence_and_nonce_and_players_are_isolated():
    counters = [OutgoingSequenceCounter(), OutgoingSequenceCounter()]
    trackers = [SequenceTracker(), SequenceTracker()]
    nonces = set()
    for pid in (2, 1):  # either connection order has independent state
        for index in range(200):
            seq, ext = counters[pid - 1].next()
            nonce = derive_nonce(10, pid, Direction.DEVICE_TO_LAPTOP, ext)
            assert nonce not in nonces
            nonces.add(nonce)
            if index % 3:  # simulates stack rejection after encoding
                decoded = decode(packet(PacketType.ACK, seq, pid=pid, acked_seq=42), trackers[pid - 1])
                assert isinstance(decoded, DecodedPacket)
                assert decoded.ext_seq == ext


async def test_wrong_player_does_not_consume_own_sequence():
    subject = session()
    subject.session_id = 10
    subject._time_sync.seed(0)
    await subject._handle_frame(packet(seq=1000, pid=2))
    await subject._handle_frame(packet(seq=0, pid=1))
    subject.on_weapon_changed.assert_awaited_once()


@pytest.mark.parametrize("streaming", [False, True])
async def test_device_hello_restart_after_many_packets_forces_fresh_connection(streaming):
    relay, board = LoopbackTransport.pair()
    subject = session(relay)
    subject.session_id = 10
    for i in range(200):
        subject._rx_tracker.accept(i)
    hello = packet(PacketType.HELLO, 0, device_mac=b"123456", player_id=1,
                   protocol_version=1, firmware_version=1, device_nonce=777, reserved=0)
    with pytest.raises(HandshakeFailed, match="restart"):
        if streaming:
            await subject._handle_frame(hello)
        else:
            await board.send(hello)  # e.g. first HELLO_ACK was lost; device starts a new attempt
            await subject._next_decoded(PacketType.TIME_SYNC_REQ, .1)
    fresh = session()
    fresh.session_id = 10
    fresh._time_sync.seed(0)
    await fresh._handle_frame(packet(seq=0))
    fresh.on_weapon_changed.assert_awaited_once()


async def test_session_shutdown_cancels_inflight_classifier_and_feedback_workers():
    subject = session()
    subject._handshake = AsyncMock()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    classification = asyncio.create_task(slow())
    subject._classification_tasks.add(classification)
    running = asyncio.create_task(subject.run())
    await started.wait()
    await asyncio.sleep(.01)
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)
    assert cancelled.is_set() and classification.done()


async def test_duplicate_rfid_is_reacked_once_effect_and_ack_is_never_acked():
    subject = session()
    subject.session_id = 10
    subject._time_sync.seed(0)
    subject._send_ack = AsyncMock()
    frame = packet()
    await subject._handle_frame(frame)
    await subject._handle_frame(frame)
    assert subject._send_ack.await_count == 2
    assert subject.on_weapon_changed.await_count == 1
    ack = packet(PacketType.ACK, 2, acked_seq=1)
    await subject._handle_frame(ack)
    await subject._handle_frame(ack)
    assert subject._send_ack.await_count == 2


@pytest.mark.parametrize("reason", ["no_weapon", "stale", "span", "too_few"])
def test_window_suppression_reports_actual_gate_and_values(reason):
    subject = make_windower(trigger_mode=True)
    if reason != "no_weapon":
        subject.on_weapon_confirmed(Weapon.SWORD)
    flags = FLAG_MOTION_ACTIVE | (FLAG_DOM_STALE if reason == "stale" else 0)
    timestamps = [0, 40, 80, 120, 160] if reason == "span" else [0, 20, 40, 60, 80]
    if reason == "too_few":
        timestamps = [0, 20, 40]
    with structlog.testing.capture_logs() as logs:
        feed_all(subject, timestamps, [flags] * len(timestamps))
        if reason == "too_few":
            assert subject.ingest_frame(60, 0, RAW_SAMPLE, RAW_SAMPLE) is None
    entry = next(log for log in logs if log["event"] == "window_suppressed")
    assert entry["reason"] == reason
    assert entry["player_id"] == 1 and entry["trigger_id"] == 0
    assert entry["expected_frame_count"] == 5
    assert entry["expected_span_ms"] == 80
    fields = {"weapon", "span_ms", "deviation_pct", "stale_pct", "frame_count", "capture_id"}
    assert fields <= entry.keys()


def test_partial_capture_cannot_fill_with_untagged_or_different_trigger_frames():
    subject = make_windower(trigger_mode=True)
    subject.on_weapon_confirmed(Weapon.SWORD)
    assert all(x is None for x in feed_all(subject, [0, 20, 40], [FLAG_MOTION_ACTIVE] * 3))
    assert all(x is None for x in feed_all(subject, [60, 80, 100], [0] * 3))
    complete = feed_all(subject, [120, 140, 160, 180, 200], [FLAG_MOTION_ACTIVE | 16] * 5)
    assert len([x for x in complete if x]) == 1


async def test_slow_classifier_does_not_block_sensor_ack_and_weapon_is_snapshot():
    subject = session()
    subject.session_id = 10
    subject._time_sync.seed(0)
    subject.windower.on_weapon_confirmed(Weapon.SWORD)
    gate = asyncio.Event()

    async def classify(window):
        from cg_comms.messages.models import ActionResult
        await gate.wait()
        return ActionResult(timestamp=window.timestamp, player_id=1, request_id=window.request_id,
                            action="HORIZONTAL_SWING", action_confidence=.9,
                            guard_state="IDLE", guard_confidence=1)

    subject.ultra96.request = classify
    for i in range(5):
        sensor = packet(PacketType.SENSOR_FRAME, i, device_timestamp_ms=i * 20, flags=0,
                        weapon_id=1, dominant=RAW_SAMPLE, offhand=RAW_SAMPLE, torso=RAW_SAMPLE)
        await asyncio.wait_for(subject._handle_frame(sensor), .1)
    subject._send_ack = AsyncMock()
    await subject._handle_frame(packet(seq=5, device_timestamp_ms=90, weapon_id=3,
                                       reserved=0, tag_uid=b"123456"))
    subject._send_ack.assert_awaited_once_with(5)
    gate.set()
    await asyncio.gather(*subject._classification_tasks)
    action = subject.on_action_result.call_args.args[0]
    assert action.weapon == Weapon.SWORD
    assert subject.windower.current_weapon == Weapon.MACE


async def test_ble_disconnect_and_overflow_wake_reader_without_unbounded_queue():
    from cg_comms.transport.ble import BleTransport
    transport = BleTransport("test", "notify", "write")
    for _ in range(257):
        transport._on_notify(0, bytearray(b"sample"))
    assert transport._overflow and transport._queue.qsize() == 1
    assert [item async for item in transport.frames()] == []


async def test_ble_subscription_negotiated_mtu_and_serial_writes(monkeypatch):
    from cg_comms.transport import ble
    sent = []

    class Client:
        is_connected = True
        mtu_size = 23
        sending = False

        def __init__(self, *args, **kwargs):
            pass

        async def connect(self):
            pass

        async def start_notify(self, *_args):
            self.mtu_size = 247

        async def write_gatt_char(self, _uuid, frame, response):
            assert not self.sending
            self.sending = True
            await asyncio.sleep(.01)
            sent.append(frame)
            self.sending = False

        async def disconnect(self):
            self.is_connected = False

    monkeypatch.setattr(ble, "BleakClient", Client)
    transport = ble.BleTransport("test", "notify", "write")
    await transport.connect()
    assert transport.mtu == 244
    await asyncio.gather(*(transport.send(bytes([i])) for i in range(4)))
    assert sent == [bytes([i]) for i in range(4)]
    await transport.disconnect()
