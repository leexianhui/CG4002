"""Device-side assessment logic in Python: the mirror of firmware_demo/lib/cg_protocol/cg_assess.h.

Used by mock/firebeetle_sim.py so the relay's assessment features (keyboard echo pipeline, file
transfer) can be exercised end to end over loopback/TCP with no board attached, and by the tests to
pin down the file-receive contract. The two implementations must stay behaviourally identical; the
contract itself is documented at the top of cg_assess.h.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from cg_comms.protocol.crc import crc16_update
from cg_comms.protocol.payloads import ASSESS_FILE_CHUNK_DATA_LEN
from cg_comms.relay.assess_transform import apply_transform, transform_for


def build_echo_response(req: dict, now_ms: int) -> dict:
    """ASSESS_ECHO_REQ fields -> ASSESS_ECHO_RESP fields (cg_assess_build_echo_resp)."""
    transform_id = transform_for(req["keycode"])
    a, b, c = apply_transform(
        transform_id, (req["value_a"], req["value_b"], req["value_c"]), req["keycode"]
    )
    return {
        "trace_id": req["trace_id"],
        "device_timestamp_ms": now_ms & 0xFFFFFFFF,
        "keycode": req["keycode"],
        "payload_kind": req["payload_kind"],
        "transform_id": transform_id,
        "reserved": 0,
        **{name: req[name] for name in ("flags", "weapon_id", "dominant_gyro", "offhand", "torso")},
        "value_a": a,
        "value_b": b,
        "value_c": c,
    }


class FileVerdict(Enum):
    MALFORMED = 0  # no ACK, no answer
    APPENDED = 1  # state advanced; answer + ACK
    OUT_OF_SEQUENCE = 2  # state unchanged; answer (so the laptop sees why) + ACK


@dataclass(frozen=True)
class FileRxState:
    """CgAssessFileRx. Immutable so `evaluate` is visibly pure: the caller commits the returned
    state only after the FILE_ACK was actually sent, exactly like the firmware."""

    transfer_id: int = 0
    active: bool = False
    total_chunks: int = 0
    next_index: int = 0
    running_crc: int = 0xFFFF
    received_bytes: int = 0
    data: bytes = b""  # actual retained bytes, also held in bounded RAM by the firmware

    @property
    def complete(self) -> bool:
        return self.active and self.next_index == self.total_chunks

    def evaluate(self, chunk: dict) -> tuple[FileVerdict, FileRxState, str]:
        index, total, length = chunk["chunk_index"], chunk["total_chunks"], chunk["chunk_len"]
        data: bytes = chunk["data"]
        if chunk["reserved"] != 0:
            return FileVerdict.MALFORMED, self, "reserved!=0"
        if not 1 <= length <= ASSESS_FILE_CHUNK_DATA_LEN:
            return FileVerdict.MALFORMED, self, "chunk_len"
        if not 0 < chunk.get("transfer_id", 0) <= 0xFFFFFFFF:
            return FileVerdict.MALFORMED, self, "transfer_id"
        if total == 0 or total > 128 or index >= total:
            return FileVerdict.MALFORMED, self, "index>=total"
        if index + 1 < total and length != ASSESS_FILE_CHUNK_DATA_LEN:
            return FileVerdict.MALFORMED, self, "short_non_final"
        if any(data[length:]):
            return FileVerdict.MALFORMED, self, "padding!=0"

        if index == 0:
            base = FileRxState(active=True, total_chunks=total, transfer_id=chunk["transfer_id"])
        elif (
            not self.active
            or chunk["transfer_id"] != self.transfer_id
            or total != self.total_chunks
            or index != self.next_index
        ):
            return FileVerdict.OUT_OF_SEQUENCE, self, "out_of_sequence"
        else:
            base = self
        valid = data[:length]
        return (
            FileVerdict.APPENDED,
            replace(
                base,
                running_crc=crc16_update(base.running_crc, valid),
                received_bytes=base.received_bytes + length,
                next_index=index + 1,
                data=base.data + valid,
            ),
            "ok",
        )
