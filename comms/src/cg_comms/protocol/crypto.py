"""AES-128-GCM seal/open, nonce derivation, and the per-direction replay window.

See shared/protocol/wire_protocol.md §4-§5 for the normative description this implements.
"""

from __future__ import annotations

import struct
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cg_comms.protocol.constants import Direction

_NONCE_STRUCT = struct.Struct("<IBBIH")  # sessionId u32 | playerId u8 | direction u8 | extSeq u32 | 0x0000
_REPLAY_WINDOW_SIZE: int = 64


def derive_nonce(session_id: int, player_id: int, direction: Direction, ext_seq: int) -> bytes:
    """12-byte AES-GCM nonce, never transmitted (wire_protocol.md §5)."""
    return _NONCE_STRUCT.pack(session_id, player_id, int(direction), ext_seq, 0x0000)


def seal(psk: bytes, header_aad: bytes, nonce: bytes, plaintext: bytes) -> bytes:
    """Returns ciphertext || 16-byte tag. `header_aad` is the 8-byte plaintext header, exactly as
    it appears on the wire, authenticating TYPE/SEQ/LEN/PLAYER_ID."""
    return AESGCM(psk).encrypt(nonce, plaintext, header_aad)


def open_sealed(psk: bytes, header_aad: bytes, nonce: bytes, ciphertext_and_tag: bytes) -> bytes:
    """Returns plaintext. Raises `cryptography.exceptions.InvalidTag` on tamper or wrong key/nonce —
    callers must catch that specific exception and treat it as a counted, logged drop, not let it
    reach the event loop as an unhandled error."""
    return AESGCM(psk).decrypt(nonce, ciphertext_and_tag, header_aad)


def _signed_delta16(new_seq: int, last_seq: int) -> int:
    delta = (new_seq - last_seq) & 0xFFFF
    if delta >= 0x8000:
        delta -= 0x10000
    return delta


@dataclass
class SequenceTracker:
    """Detects wrap, reorder, and replay for a 16-bit wire SEQ, maintaining a 32-bit extended
    sequence and a 64-entry sliding replay window, per direction (wire_protocol.md §4).

    One instance per (session, direction) — a fresh session (post-reconnect handshake) gets a
    fresh tracker, since the device's own SEQ counter resets on reboot.
    """

    _last_seq: int | None = field(default=None, init=False)
    _high: int = field(default=0, init=False)
    _highest_ext_seq: int = field(default=-1, init=False)
    _seen: set[int] = field(default_factory=set, init=False)
    # Exact authenticated reliable ciphertexts, retained beyond the 64 SENSOR_FRAME slots.
    # Bounded independently of stream rate; entries expire in codec after the retry horizon.
    reliable_frames: OrderedDict[bytes, tuple[float, Any]] = field(
        default_factory=OrderedDict, init=False, repr=False
    )
    # Highest reliable ext_seq applied, PER KIND (codec.reliable_kind). One shared watermark is wrong
    # once two reliable kinds can be in flight in the same direction at once: if the earlier one is
    # lost and the later delivered, a shared watermark rejects the earlier one's retransmit as a
    # replay and it is never applied. Mirrors firmware_demo/src/main.cpp (g_feedback_cache.highest).
    reliable_high: dict[str, int] = field(default_factory=dict, init=False)

    def infer(self, seq: int) -> int:
        """Reconstruct a nonce for a late reliable packet without advancing the sensor window."""
        if self._last_seq is None:
            return seq
        return self._highest_ext_seq + _signed_delta16(seq, self._last_seq)

    def accept(self, seq: int) -> int | None:
        """Returns the extended sequence number if `seq` should be accepted, else None (the
        packet is a replay, a duplicate, or too far behind the window floor)."""
        if self._last_seq is None:
            self._last_seq = seq
            self._highest_ext_seq = seq
            self._seen = {seq}
            return seq

        delta = _signed_delta16(seq, self._last_seq)
        if delta > 0:
            if seq < self._last_seq:
                self._high += 1
            self._last_seq = seq
            high = self._high
        elif seq > self._last_seq:
            # Late pre-wrap packet arriving after `_high` was already bumped for the wrap:
            # borrow from the previous epoch, or it reads as far-future and wedges the window.
            high = self._high - 1
        else:
            high = self._high
        ext_seq = (high << 16) | seq

        if ext_seq <= self._highest_ext_seq - _REPLAY_WINDOW_SIZE:
            return None
        if ext_seq in self._seen:
            return None

        self._seen.add(ext_seq)
        if ext_seq > self._highest_ext_seq:
            self._highest_ext_seq = ext_seq
            floor = self._highest_ext_seq - _REPLAY_WINDOW_SIZE
            self._seen = {s for s in self._seen if s > floor}
        return ext_seq


@dataclass
class OutgoingSequenceCounter:
    """Monotonic per-device, per-direction sequence counter for the encode path. The wire SEQ is
    the low 16 bits; extSeq (used for nonce derivation) never wraps within a session's lifetime."""

    _next_ext_seq: int = field(default=0, init=False)

    def next(self) -> tuple[int, int]:
        """Returns (seq16, ext_seq) and advances."""
        ext_seq = self._next_ext_seq
        self._next_ext_seq += 1
        return ext_seq & 0xFFFF, ext_seq
