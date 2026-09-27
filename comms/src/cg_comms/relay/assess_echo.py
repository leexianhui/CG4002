"""Deterministic keyboard IMU round trip through the physical board and common action path.

b/c/d -> fixed editable B/C/D -> ASSESS_ECHO_REQ -> board IDENTITY response
-> verified returned sample -> normal SENSOR_WINDOW -> classifier -> ACTION -> Unity.
The full 42-byte sensor body is authenticated; only a verified returned sample is forwarded.

The simulator accepts a one-sample window. This is a communications fixture, not the
normal GPIO 40-sample capture or trained-model inference. GPIO windowing is unchanged.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

import structlog

from cg_comms.clock import monotonic_ms
from cg_comms.messages.enums import Weapon
from cg_comms.messages.models import SensorWindow
from cg_comms.protocol import codec
from cg_comms.protocol.constants import Direction, PacketType
from cg_comms.relay.assess_dummy import DummyVariant, for_key
from cg_comms.relay.assess_transform import apply_transform, transform_for
from cg_comms.relay.windowing import _scale_sample

logger = structlog.get_logger(__name__)

# A keystroke's echo must outlive one BLE round trip plus the device's ACK. The reliable sender
# already retries (200 ms base, x2, 5 attempts), so this only bounds how long we keep a trace alive
# waiting for the RESP before declaring it lost.
_ECHO_TIMEOUT_S = 6.0

# Sample rate reported on the synthetic window. The window carries a single sample, so this value is
# not a measurement of anything -- it exists because SensorWindow requires sampleRateHz > 0.
_SYNTHETIC_SAMPLE_RATE_HZ = 50.0

# How many resolved trace ids to remember for duplicate detection. A re-sent response can only arrive
# within the device's 20 s exact-frame cache window, far fewer keystrokes than this at human speed.
_RESOLVED_MEMORY = 256


@dataclass
class PendingEcho:
    trace_id: int
    keycode: int
    variant: DummyVariant
    expected: tuple[int, int, int]
    transform_id: int
    sent_ms: int
    event: asyncio.Event = field(default_factory=asyncio.Event)
    matched: bool = False
    mismatch: str | None = None
    returned: dict | None = None
    weapon: Weapon = Weapon.SWORD
    request_frame: bytes = b""
    request_seq: int = 0
    demo_trace_id: str | None = None


@dataclass
class EchoPipeline:
    """Owns the laptop end of the echo round trip for ONE player/session.

    Created by the Session so its lifetime matches the BLE connection: a reconnect gets a fresh one,
    and pending traces from the previous connection cannot leak into it.
    """

    player_id: int
    rng: random.Random
    _pending: dict[int, PendingEcho] = field(default_factory=dict, init=False)
    # trace_id -> how it was resolved, for the last _RESOLVED_MEMORY traces. Lets a duplicate or late
    # response be told apart from a genuinely unknown trace (which WOULD indicate a bug).
    _resolved: dict[int, str] = field(default_factory=dict, init=False)
    _next_trace_id: int = field(default=1, init=False)
    sent: int = field(default=0, init=False)
    verified: int = field(default=0, init=False)
    mismatched: int = field(default=0, init=False)
    lost: int = field(default=0, init=False)

    def _allocate_trace_id(self) -> int:
        trace_id = self._next_trace_id
        # u32 on the wire, and 0 is reserved as "no trace" so a zeroed field is never mistaken for
        # a real correlation id.
        self._next_trace_id = 1 if trace_id >= 0xFFFFFFFF else trace_id + 1
        return trace_id

    def build_request(self, keycode: int, weapon: Weapon = Weapon.SWORD) -> tuple[PendingEcho, dict]:
        """Load the saved variant and return its pending record and ASSESS_ECHO_REQ fields.

        The expected response is computed HERE, at send time, from the same transform table the
        firmware compiles in. Recomputing it from the response later would make the check circular.
        """
        variant = for_key(keycode)
        trace_id = self._allocate_trace_id()
        transform_id = transform_for(keycode)
        sent_ms = monotonic_ms()
        pending = PendingEcho(
            trace_id=trace_id,
            keycode=keycode,
            variant=variant,
            expected=apply_transform(transform_id, variant.values, keycode),
            transform_id=transform_id,
            sent_ms=sent_ms,
            weapon=weapon,
        )
        self._pending[trace_id] = pending
        fields: dict = {
            "trace_id": trace_id,
            "sent_ms": sent_ms & 0xFFFFFFFF,
            "keycode": keycode,
            "payload_kind": variant.kind,
            "value_a": variant.values[0],
            "value_b": variant.values[1],
            "value_c": variant.values[2],
            "flags": 0,
            "weapon_id": weapon.wire_value,
            "dominant_gyro": (164, 0, -82),
            "offhand": (0, 0, 2048, 0, 0, 0),
            "torso": (0, 0, 2048, 0, 0, 0),
        }
        return pending, fields

    def on_response(self, fields: dict) -> PendingEcho | None:
        """Matches an ASSESS_ECHO_RESP to its request and verifies the device's transform.

        Returns the pending record on a match (verified or not), or None if the trace is unknown --
        which happens legitimately for a response arriving after its timeout, or for a duplicate,
        and must not be treated as an error.
        """
        trace_id = fields["trace_id"]
        pending = self._pending.get(trace_id)
        if pending is None:
            outcome = self._resolved.get(trace_id)
            if outcome is not None:
                # Expected, not an error: the device re-sends its last response whenever our ACK for
                # the request was lost, and a response can also land after its trace timed out.
                logger.info(
                    "assess_echo_duplicate_response",
                    player_id=self.player_id,
                    trace_id=trace_id,
                    earlier_outcome=outcome,
                    action="ignored",
                )
            else:
                logger.warning("assess_echo_unknown_trace", player_id=self.player_id, trace_id=trace_id)
            return None
        got = (fields["value_a"], fields["value_b"], fields["value_c"])
        problems: list[str] = []
        if fields["keycode"] != pending.keycode:
            problems.append(f"keycode {fields['keycode']} != {pending.keycode}")
        if fields["payload_kind"] != pending.variant.kind:
            problems.append(f"payload_kind {fields['payload_kind']} != {pending.variant.kind}")
        if fields["transform_id"] != pending.transform_id:
            problems.append(f"transform_id {fields['transform_id']} != {pending.transform_id}")
        expected_fields = {
            "reserved": 0,
            "flags": 0,
            "weapon_id": pending.weapon.wire_value,
            "dominant_gyro": (164, 0, -82),
            "offhand": (0, 0, 2048, 0, 0, 0),
            "torso": (0, 0, 2048, 0, 0, 0),
        }
        for name, expected in expected_fields.items():
            if fields.get(name) != expected:
                problems.append(f"{name} mismatch")
        if got != pending.expected:
            problems.append(f"values {got} != expected {pending.expected}")

        rtt_ms = monotonic_ms() - pending.sent_ms
        if problems:
            pending.mismatch = "; ".join(problems)
            self.mismatched += 1
            # Loud on purpose: a mismatch means the bytes came back but the device did not do what
            # this laptop believes it does. That is a worse state than a dropped packet, and the one
            # thing a naive echo demo would happily report as success.
            logger.error(
                "assess_echo_mismatch",
                player_id=self.player_id,
                trace_id=trace_id,
                demo_trace_id=pending.demo_trace_id,
                variant=pending.variant.name,
                sent=pending.variant.values,
                received=got,
                expected=pending.expected,
                transform_id=pending.transform_id,
                keycode=pending.keycode,
                rtt_ms=rtt_ms,
                problems=pending.mismatch,
            )
        else:
            pending.returned = dict(fields)
            pending.matched = True
            self.verified += 1
            logger.info(
                "assess_echo_verified",
                player_id=self.player_id,
                trace_id=trace_id,
                demo_trace_id=pending.demo_trace_id,
                variant=pending.variant.name,
                sent=pending.variant.values,
                received=got,
                transform=pending.transform_id,
                sensor_raw={
                    "dominant": got + fields["dominant_gyro"],
                    "offhand": fields["offhand"],
                    "torso": fields["torso"],
                },
                keycode=pending.keycode,
                key=chr(pending.keycode) if 32 <= pending.keycode < 127 else None,
                device_timestamp_ms=fields["device_timestamp_ms"],
                rtt_ms=rtt_ms,
            )
        # Retire the trace as soon as it is resolved. Leaving it in `_pending` would leak one entry
        # per keystroke over a long demo, make `in_flight` meaningless, and let `abandon_all` later
        # re-count an already-resolved trace as lost.
        self._retire(trace_id, "verified" if pending.matched else "mismatch")
        pending.event.set()
        return pending

    def _retire(self, trace_id: int, outcome: str) -> None:
        self._pending.pop(trace_id, None)
        self._resolved[trace_id] = outcome
        while len(self._resolved) > _RESOLVED_MEMORY:
            del self._resolved[next(iter(self._resolved))]  # dicts keep insertion order: oldest first

    def forget(self, trace_id: int) -> None:
        self._retire(trace_id, "undelivered")

    def expire(self, trace_id: int, timeout_s: float) -> None:
        """Retires a trace whose response did not arrive within the echo timeout and counts it lost.
        No-op if it was resolved in the meantime (a response racing the timeout wins)."""
        pending = self._pending.get(trace_id)
        if pending is None:
            return
        self.lost += 1
        pending.mismatch = f"no response within {timeout_s:g} s"
        logger.error(
            "assess_echo_timeout",
            player_id=self.player_id,
            trace_id=trace_id,
            timeout_s=timeout_s,
            variant=pending.variant.name,
            keycode=pending.keycode,
        )
        self._retire(trace_id, "timed_out")
        pending.event.set()

    def abandon_all(self, reason: str) -> None:
        """Called when the link drops: every in-flight trace is lost, and waiting on them would
        otherwise hang until each individual timeout."""
        if not self._pending:
            return
        logger.warning(
            "assess_echo_abandoned",
            player_id=self.player_id,
            in_flight=len(self._pending),
            reason=reason,
        )
        self.lost += len(self._pending)
        for trace_id, pending in list(self._pending.items()):
            pending.mismatch = f"abandoned: {reason}"
            self._retire(trace_id, "abandoned")
            pending.event.set()

    def synthetic_window(self, pending: PendingEcho, *, weapon: Weapon, request_id: int) -> SensorWindow:
        """Scale the actual authenticated returned sensor sample; never synthesize from expectation."""
        if not pending.matched or pending.returned is None:
            raise ValueError("cannot forward an unverified echo")
        received = pending.returned
        dominant = (
            tuple(received[k] for k in ("value_a", "value_b", "value_c")) + received["dominant_gyro"]
        )
        now = monotonic_ms()
        return SensorWindow(
            timestamp=now,
            player_id=self.player_id,
            request_id=request_id,
            weapon=weapon,
            window_start_ts=now,
            window_end_ts=now,
            sample_rate_hz=_SYNTHETIC_SAMPLE_RATE_HZ,
            dominant=[_scale_sample(dominant)],
            offhand=[_scale_sample(received["offhand"])],
            stale_pct=0.0,
            trigger_id=None,
            demo_trace_id=pending.demo_trace_id,
        )

    def mark_sent(self) -> None:
        """Counts a request that actually reached the transport. Separate from build_request()
        because a send can fail after the trace is prepared, and counting it as sent then would
        overstate the denominator of the verified/sent ratio the demo reports."""
        self.sent += 1

    def stats(self) -> dict[str, int]:
        return {
            "sent": self.sent,
            "verified": self.verified,
            "mismatched": self.mismatched,
            "lost": self.lost,
            "in_flight": len(self._pending),
        }


def encode_echo_request(
    *, fields: dict, seq: int, player_id: int, psk: bytes, session_id: int, ext_seq: int
) -> bytes:
    """Encodes ASSESS_ECHO_REQ. Separate from Session so the encoding can be unit-tested without a
    transport, and so the packet is built by the same generic codec as every other type."""
    return codec.encode_packet(
        packet_type=PacketType.ASSESS_ECHO_REQ,
        seq=seq,
        player_id=player_id,
        fields=fields,
        psk=psk,
        session_id=session_id,
        direction=Direction.LAPTOP_TO_DEVICE,
        ext_seq=ext_seq,
    )


async def wait_for_echo(pending: PendingEcho, timeout_s: float = _ECHO_TIMEOUT_S) -> bool:
    """Waits for the response to one keystroke. Returns True if it came back verified."""
    try:
        await asyncio.wait_for(pending.event.wait(), timeout_s)
    except asyncio.TimeoutError:
        return False
    return pending.matched
