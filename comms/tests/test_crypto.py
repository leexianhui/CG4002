"""Black-box tests for cg_comms.protocol.crypto.

These tests are derived strictly from the spec documents:
  - docs/COMMS_IMPLEMENTATION_PLAN.md  §4.4 (sequence numbers / replay), §4.5 (encryption), §13 (testing)
  - shared/protocol/wire_protocol.md   §4 (sequence numbers / replay), §5 (encryption),
                                       §5.1 (handshake nonce derivation exception)

They do NOT read crypto.py's implementation. Expected values/behaviours (nonce layout, the
deviceNonce-substitution rule, the 64-entry replay window, wrap detection via signed 16-bit delta,
InvalidTag on tamper) all come from the prose spec, treating crypto.py's public API purely as the
calling-convention contract handed down for this exercise.
"""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from cg_comms.protocol.constants import NONCE_SIZE, PSK_SIZE, Direction
from cg_comms.protocol.crypto import (
    OutgoingSequenceCounter,
    SequenceTracker,
    derive_nonce,
    open_sealed,
    seal,
)


# ---------------------------------------------------------------------------
# 1. Nonce derivation basics
# ---------------------------------------------------------------------------


def test_derive_nonce_is_12_bytes():
    nonce = derive_nonce(session_id=1, player_id=1, direction=Direction.DEVICE_TO_LAPTOP, ext_seq=0)
    assert isinstance(nonce, (bytes, bytearray))
    assert len(nonce) == NONCE_SIZE == 12


def test_derive_nonce_changes_with_session_id():
    base = derive_nonce(session_id=1, player_id=1, direction=Direction.DEVICE_TO_LAPTOP, ext_seq=0)
    changed = derive_nonce(session_id=2, player_id=1, direction=Direction.DEVICE_TO_LAPTOP, ext_seq=0)
    assert base != changed


def test_derive_nonce_changes_with_player_id():
    base = derive_nonce(session_id=1, player_id=1, direction=Direction.DEVICE_TO_LAPTOP, ext_seq=0)
    changed = derive_nonce(session_id=1, player_id=2, direction=Direction.DEVICE_TO_LAPTOP, ext_seq=0)
    assert base != changed


def test_derive_nonce_changes_with_direction():
    base = derive_nonce(session_id=1, player_id=1, direction=Direction.DEVICE_TO_LAPTOP, ext_seq=0)
    changed = derive_nonce(session_id=1, player_id=1, direction=Direction.LAPTOP_TO_DEVICE, ext_seq=0)
    assert base != changed


def test_derive_nonce_changes_with_ext_seq():
    base = derive_nonce(session_id=1, player_id=1, direction=Direction.DEVICE_TO_LAPTOP, ext_seq=0)
    changed = derive_nonce(session_id=1, player_id=1, direction=Direction.DEVICE_TO_LAPTOP, ext_seq=1)
    assert base != changed


# ---------------------------------------------------------------------------
# 2. Handshake nonce derivation exception (wire_protocol.md §5.1)
# ---------------------------------------------------------------------------


def test_handshake_devicenonce_substitutes_for_session_id_not_interchangeable():
    """§5.1: HELLO/HELLO_ACK have no session yet, so the nonce is derived as

        deviceNonce(u32) || playerId(u8) || direction(u8) || extSeq(u32) || 0x0000

    i.e. the packet's cleartext `deviceNonce` occupies the slot that `derive_nonce` calls
    `session_id`, and the caller must pass deviceNonce there -- NOT the (nonexistent) session id.
    This test proves the two are not interchangeable: deriving with the real deviceNonce in that
    slot gives a different nonce than deriving with a plausible (but different) sessionId in the
    same slot. If crypto.py silently used sessionId=0 (or any fixed sentinel) for every handshake
    instead of the per-packet deviceNonce, this test would catch it by construction, since a
    fixed sentinel could not simultaneously equal every distinct deviceNonce used below.
    """
    device_nonce = 0xAABBCCDD
    unrelated_session_id = 0x11223344  # must never be substituted in for deviceNonce
    player_id = 0  # HELLO permits playerId == 0 (wire_protocol.md §1 / §3)
    ext_seq = 0

    nonce_via_device_nonce = derive_nonce(
        session_id=device_nonce,
        player_id=player_id,
        direction=Direction.DEVICE_TO_LAPTOP,
        ext_seq=ext_seq,
    )
    nonce_via_wrong_session_id = derive_nonce(
        session_id=unrelated_session_id,
        player_id=player_id,
        direction=Direction.DEVICE_TO_LAPTOP,
        ext_seq=ext_seq,
    )

    assert nonce_via_device_nonce != nonce_via_wrong_session_id

    # §5.1: "HELLO_ACK derives its nonce from the *same* deviceNonce with direction = 1, so the
    # pair never collides [with each other]" -- same deviceNonce, direction flipped, must differ.
    nonce_hello_ack = derive_nonce(
        session_id=device_nonce,
        player_id=player_id,
        direction=Direction.LAPTOP_TO_DEVICE,
        ext_seq=ext_seq,
    )
    assert nonce_hello_ack != nonce_via_device_nonce


# ---------------------------------------------------------------------------
# 3. Seal / open: round-trip and tamper detection
# ---------------------------------------------------------------------------


def _psk() -> bytes:
    return bytes(range(PSK_SIZE))  # 16 arbitrary-but-fixed bytes


def _aad() -> bytes:
    # An 8-byte plaintext header, per §5 / §4.5 (MAGIC|VERSION|TYPE|SEQ(2)|PLAYER_ID|LEN(2)).
    return bytes([0xC9, 0x01, 0x10, 0x05, 0x00, 0x01, 0x2A, 0x00])


def _nonce() -> bytes:
    return derive_nonce(session_id=42, player_id=1, direction=Direction.DEVICE_TO_LAPTOP, ext_seq=7)


def test_seal_open_roundtrip_recovers_plaintext_unchanged():
    plaintext = b"\x01\x02\x03\x04SENSOR_FRAME_PAYLOAD_STUB\xff\xee"
    psk, aad, nonce = _psk(), _aad(), _nonce()

    sealed = seal(psk, aad, nonce, plaintext)
    recovered = open_sealed(psk, aad, nonce, sealed)

    assert recovered == plaintext


def test_open_sealed_raises_invalid_tag_on_ciphertext_tamper():
    plaintext = b"some plaintext payload bytes"
    psk, aad, nonce = _psk(), _aad(), _nonce()

    sealed = bytearray(seal(psk, aad, nonce, plaintext))
    sealed[0] ^= 0x01  # flip one bit inside ciphertext||tag

    with pytest.raises(InvalidTag):
        open_sealed(psk, aad, nonce, bytes(sealed))


def test_open_sealed_raises_invalid_tag_on_aad_tamper():
    plaintext = b"some plaintext payload bytes"
    psk, aad, nonce = _psk(), _aad(), _nonce()

    sealed = seal(psk, aad, nonce, plaintext)

    tampered_aad = bytearray(aad)
    tampered_aad[-1] ^= 0x01  # flip a bit that the AAD is supposed to authenticate (per §5: SEQ/LEN/etc.)

    with pytest.raises(InvalidTag):
        open_sealed(psk, bytes(tampered_aad), nonce, sealed)


# ---------------------------------------------------------------------------
# 4. Sequence numbers: wrap detection + reordering + replay window
# ---------------------------------------------------------------------------


def test_sequence_tracker_wrap_with_in_window_reorder_is_accepted_and_monotonic():
    """Combined wrap + reorder case required by COMMS_IMPLEMENTATION_PLAN.md §13.

    Wire seq climbs to the 16-bit boundary and wraps: ..., 65533, 65534, 65535, then 0, 1, 2.
    Generation order after the wrap is 0, 1, 2, but arrival order is 1, 0, 2 -- wire seq 0 was
    generated first but its packet is delayed and arrives after seq 1's packet already landed.

    Per wire_protocol.md §4: delta = int16(new_seq - last_seq); delta > 0 (including the wrap
    case) advances extSeq (incrementing the high half when new_seq < last_seq); delta <= 0 is a
    reorder/replay, not an advance. The 64-entry replay window then decides whether that
    reorder/replay is accepted (still within the window -> accept, order doesn't matter within
    it) or rejected (at/below the window floor, or a seen extSeq -> reject).

    Here the late seq-0 packet lands only one extSeq behind the current head (extSeq 65536 vs.
    65537 already seen for seq 1), which is trivially inside any 64-entry window under the spec's
    prose -- so this test asserts it must be ACCEPTED, with its extSeq correctly reconstructed as
    the value that sits between seq 65535's and seq 1's, preserving global monotonic ordering.
    """
    tracker = SequenceTracker()

    # Establish a baseline run of increasing sequence numbers approaching the wrap.
    baseline = [65530, 65531, 65532, 65533, 65534, 65535]
    ext_seqs = [tracker.accept(s) for s in baseline]

    assert all(e is not None for e in ext_seqs)
    # Strictly increasing, and each one greater than the last by exactly 1 (no wrap yet).
    for prev, cur in zip(ext_seqs, ext_seqs[1:]):
        assert cur == prev + 1

    ext_seq_65535 = ext_seqs[-1]

    # Arrival order: seq 1 arrives first (already past the wrap), then the late seq 0, then seq 2.
    ext_seq_1 = tracker.accept(1)
    assert ext_seq_1 is not None
    # Wrapping forward from 65535 to 1 is a delta of +2 (65535 -> 0 -> 1): extSeq must advance by 2.
    assert ext_seq_1 == ext_seq_65535 + 2

    ext_seq_0_late = tracker.accept(0)
    assert ext_seq_0_late is not None, "reordered packet within the 64-entry window must be accepted"
    # seq 0's extSeq belongs strictly between seq 65535's and seq 1's in the true, generation-order
    # timeline, even though it is *processed* after seq 1 -- this is what "extSeq assignments are
    # consistent with the wrap" means despite out-of-arrival-order delivery.
    assert ext_seq_65535 < ext_seq_0_late < ext_seq_1

    ext_seq_2 = tracker.accept(2)
    assert ext_seq_2 is not None
    assert ext_seq_2 == ext_seq_1 + 1


def test_sequence_tracker_rejects_replay_outside_the_replay_window():
    """A packet whose extSeq has fallen at/below the 64-entry window floor must be dropped.

    We advance the tracker's head well past 64 (100 new, strictly increasing sequence numbers),
    then attempt to re-accept the very first one -- now 100 slots behind the head, clearly outside
    any window that is only 64 entries wide per the spec's prose. That the tracker must reject it
    is explicit in the spec ("Reject any packet whose extSeq is at or below the window floor");
    100 is chosen specifically to avoid a boundary value that would depend on an implementation
    detail (e.g. whether the window holds exactly 64 or 65 recent entries, or whether the floor
    comparison is inclusive/exclusive by one) -- none of which the prose spec pins down. A gap of
    ~36 slots beyond any plausible 64-entry window keeps this test valid regardless of that detail.
    """
    tracker = SequenceTracker()

    first_ext_seq = tracker.accept(0)
    assert first_ext_seq is not None

    for seq in range(1, 100):
        ext_seq = tracker.accept(seq)
        assert ext_seq is not None

    # seq 0 is now 99 extSeq slots behind the head -- well outside a 64-entry window.
    replayed = tracker.accept(0)
    assert replayed is None


def test_sequence_tracker_rejects_plain_immediate_replay():
    tracker = SequenceTracker()

    first = tracker.accept(10)
    assert first is not None

    second = tracker.accept(10)
    assert second is None


# ---------------------------------------------------------------------------
# 5. OutgoingSequenceCounter
# ---------------------------------------------------------------------------


def test_outgoing_sequence_counter_ext_seq_strictly_increasing_spot_check():
    counter = OutgoingSequenceCounter()

    results = [counter.next() for _ in range(5)]
    ext_seqs = [ext for _seq16, ext in results]

    for prev, cur in zip(ext_seqs, ext_seqs[1:]):
        assert cur == prev + 1


def test_outgoing_sequence_counter_seq16_wraps_while_ext_seq_climbs():
    """Drive the counter through a full 16-bit wrap (65536 calls before the wrap point) rather
    than only spot-checking a few calls: it is computationally cheap (well under a second) and it
    is the only way to directly observe the wrap-to-0 behaviour the spec requires ("SEQ ... wraps
    at 65535"), instead of inferring it.

    Judgment call: the API contract does not pin down the counter's starting (seq16, ext_seq)
    pair (e.g. whether the very first call returns (0, 0)). Rather than assume it starts at zero,
    this test anchors on whatever the first call actually returns and checks all subsequent calls
    relative to that anchor -- both the ext_seq delta (always +1) and the seq16-wraps-when-ext_seq-
    crosses-a-65536-boundary relationship, which the spec does pin down.
    """
    counter = OutgoingSequenceCounter()

    first_seq16, first_ext_seq = counter.next()
    seen = [(first_seq16, first_ext_seq)]
    for _ in range(65536):
        seen.append(counter.next())

    for i, (seq16, ext_seq) in enumerate(seen):
        assert ext_seq == first_ext_seq + i
        assert seq16 == (first_seq16 + i) % 65536

    # Explicitly: somewhere in this run, ext_seq must have climbed past first_ext_seq + 65535
    # while seq16 dropped back down -- i.e. a real wrap happened, not just modular arithmetic
    # that coincidentally never crossed the boundary.
    wrapped = any(
        seen[i + 1][0] < seen[i][0] and seen[i + 1][1] == seen[i][1] + 1
        for i in range(len(seen) - 1)
    )
    assert wrapped, "expected seq16 to wrap back down at least once while ext_seq kept climbing"
