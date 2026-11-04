"""SEGMENT_CHUNK reassembly -> one SENSOR_WINDOW per action segment (wire_protocol.md §3).

Event mode of the hardware hub (hardware/gateway, CG_GATEWAY_EVENT_MODE): instead of a 50 Hz
SENSOR_FRAME stream, the hub's own segmenter decides when an action happened and sends one window
for it, split into chunks. Current hubs send the classifier's exact input: 100 rows at 50 Hz of all
three IMUs (right wrist, left wrist, torso), with flags saying which sensors were measured. Older hubs
(flags 0) sent the right wrist only. This module rebuilds each segment and fits every sensor to the
classifier's input: `window_samples` rows at `frame_rate_hz` (ai/config.py WINDOW/FS).

Each chunk is ACKed by the session (duplicates included) before it reaches `SegmentAssembler`, so
this layer only has to tolerate duplicates, reordering and abandoned segments, never ask for resends.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

import structlog

from cg_comms.protocol.constants import (
    SEGMENT_CHUNK_SAMPLES,
    SEGMENT_CHUNK_SAMPLES_3IMU,
    SEGMENT_FLAG_DOM_VALID,
    SEGMENT_FLAG_OFF_VALID,
    SEGMENT_FLAG_THREE_IMU,
    SEGMENT_FLAG_TORSO_VALID,
    SEGMENT_MAX_CHUNKS,
)
from cg_comms.relay.windowing import scale_sample

logger = structlog.get_logger(__name__)

_IMU_AXES = 6
# Column blocks of a three-IMU row, and the flag that says each one was measured.
DOMINANT, OFFHAND, TORSO = 0, 1, 2
_VALID_FLAG = {DOMINANT: SEGMENT_FLAG_DOM_VALID, OFFHAND: SEGMENT_FLAG_OFF_VALID,
               TORSO: SEGMENT_FLAG_TORSO_VALID}

# Placeholder off-hand row for a window without a measured left wrist (a legacy hub, or a left
# wrist that dropped out): SENSOR_WINDOW requires an offhand array. Gravity-down at rest, zero
# rotation. ai/classifier_server.py treats a constant block as absent and feeds the model its
# training mean instead. Counted as stale in stalePct, never presented as a measurement.
OFFHAND_PLACEHOLDER: tuple[float, ...] = (0.0, 0.0, 1.0, 0.0, 0.0, 0.0)


def _layout(flags: int) -> tuple[int, int]:
    """(axes per row, max rows per chunk) for a chunk's flags."""
    if flags & SEGMENT_FLAG_THREE_IMU:
        return 3 * _IMU_AXES, SEGMENT_CHUNK_SAMPLES_3IMU
    return _IMU_AXES, SEGMENT_CHUNK_SAMPLES


@dataclass(frozen=True)
class Segment:
    segment_id: int
    weapon_id: int
    sample_rate_hz: int
    first_sample_ms: int  # device clock
    # raw int16 rows: (ax, ay, az, gx, gy, gz) for a legacy segment; three-IMU segments append the
    # off-hand and torso blocks (18 values per row)
    samples: list[tuple[int, ...]]
    chunk_count: int
    assembly_ms: float  # first chunk received -> last chunk received, relay clock
    flags: int = 0  # SEGMENT_CHUNK.flags, the same on every chunk

    @property
    def three_imu(self) -> bool:
        return bool(self.flags & SEGMENT_FLAG_THREE_IMU)

    def measured(self, block: int) -> bool:
        """Whether this sensor's columns are measurements (legacy: only the dominant wrist exists)."""
        if not self.three_imu:
            return block == DOMINANT
        return bool(self.flags & _VALID_FLAG[block])


@dataclass
class _Partial:
    chunk_count: int
    weapon_id: int
    sample_rate_hz: int
    started: float
    flags: int
    chunks: dict[int, tuple[int, list[tuple[int, ...]]]] = field(default_factory=dict)


@dataclass
class SegmentAssembler:
    """One per relay session. `add()` returns the completed Segment when its last chunk arrives."""

    timeout_ms: float = 2000.0
    max_partials: int = 4
    _partials: dict[int, _Partial] = field(default_factory=dict, init=False)
    # Recently completed ids, so a late duplicate chunk never classifies a segment twice.
    _completed: deque[int] = field(default_factory=lambda: deque(maxlen=32), init=False)

    def add(self, fields: dict, now: float | None = None) -> Segment | None:
        now = time.monotonic() if now is None else now
        self.expire(now)
        segment_id = int(fields["segment_id"])
        index, count = int(fields["chunk_index"]), int(fields["chunk_count"])
        sample_count = int(fields["sample_count"])
        rate = int(fields["sample_rate_hz"])
        flags = int(fields.get("flags", 0))
        axes, max_rows = _layout(flags)
        if not (1 <= count <= SEGMENT_MAX_CHUNKS and index < count
                and 1 <= sample_count <= max_rows and rate > 0):
            logger.warning("segment_chunk_invalid", segment_id=segment_id, chunk_index=index,
                           chunk_count=count, sample_count=sample_count, sample_rate_hz=rate)
            return None
        if segment_id in self._completed:
            return None
        partial = self._partials.get(segment_id)
        if partial is None:
            if len(self._partials) >= self.max_partials:
                oldest = min(self._partials, key=lambda k: self._partials[k].started)
                self._abandon(oldest, "capacity")
            partial = _Partial(count, int(fields["weapon_id"]), rate, now, flags)
            self._partials[segment_id] = partial
        elif (count, rate, flags) != (partial.chunk_count, partial.sample_rate_hz, partial.flags):
            logger.warning("segment_chunk_inconsistent", segment_id=segment_id, chunk_index=index)
            return None
        if index in partial.chunks:
            return None
        flat = fields["samples"]
        rows = [tuple(flat[i * axes:(i + 1) * axes]) for i in range(sample_count)]
        partial.chunks[index] = (int(fields["first_sample_ms"]), rows)
        if len(partial.chunks) < partial.chunk_count:
            return None

        del self._partials[segment_id]
        self._completed.append(segment_id)
        samples = [row for i in range(partial.chunk_count) for row in partial.chunks[i][1]]
        return Segment(
            segment_id=segment_id,
            weapon_id=partial.weapon_id,
            sample_rate_hz=partial.sample_rate_hz,
            first_sample_ms=partial.chunks[0][0],
            samples=samples,
            chunk_count=partial.chunk_count,
            assembly_ms=1000.0 * (now - partial.started),
            flags=partial.flags,
        )

    def expire(self, now: float) -> None:
        for segment_id in [k for k, p in self._partials.items()
                           if 1000.0 * (now - p.started) > self.timeout_ms]:
            self._abandon(segment_id, "timeout")

    def reset(self) -> None:
        self._partials.clear()
        self._completed.clear()

    def _abandon(self, segment_id: int, reason: str) -> None:
        partial = self._partials.pop(segment_id)
        logger.warning("segment_abandoned", segment_id=segment_id, reason=reason,
                       chunks_received=len(partial.chunks), chunk_count=partial.chunk_count)


def resample(rows: list[list[float]], from_hz: float, to_hz: float) -> list[list[float]]:
    """Linear interpolation onto a `to_hz` grid spanning the same time as `rows`."""
    if from_hz == to_hz or len(rows) < 2:
        return [list(r) for r in rows]
    n_out = int(round((len(rows) - 1) * to_hz / from_hz)) + 1
    out = []
    for k in range(n_out):
        pos = k * from_hz / to_hz
        i = min(int(pos), len(rows) - 2)
        frac = pos - i
        out.append([a + (b - a) * frac for a, b in zip(rows[i], rows[i + 1], strict=True)])
    return out


def fit_length(rows: list[list[float]], n: int) -> list[list[float]]:
    """Exactly `n` rows with the action centred: a longer segment keeps its middle, a shorter one is
    padded on both sides by repeating its first/last row (the wrist held still before/after)."""
    if len(rows) >= n:
        start = (len(rows) - n) // 2
        return [list(r) for r in rows[start:start + n]]
    before = (n - len(rows)) // 2
    after = n - len(rows) - before
    return [list(rows[0])] * before + [list(r) for r in rows] + [list(rows[-1])] * after


def classifier_rows(
    segment: Segment, rate_hz: float, n: int, block: int = DOMINANT
) -> list[list[float]]:
    """Segment -> `n` rows of one sensor (`block`) in g / deg/s at `rate_hz`, the classifier's input
    shape. A current hub's window already is exactly that; resample/fit only touch legacy segments."""
    lo = block * _IMU_AXES
    scaled = [scale_sample(row[lo:lo + _IMU_AXES]) for row in segment.samples]
    return fit_length(resample(scaled, float(segment.sample_rate_hz), rate_hz), n)


@dataclass(frozen=True)
class ClassifierInput:
    dominant: list[list[float]]
    offhand: list[list[float]]  # OFFHAND_PLACEHOLDER rows when the left wrist was not measured
    torso: list[list[float]] | None  # None when the torso was not measured
    stale_pct: float  # share of the three sensors not measured


def classifier_input(segment: Segment, rate_hz: float, n: int) -> ClassifierInput:
    """All three sensors of a segment, as SENSOR_WINDOW carries them."""
    measured = [segment.measured(b) for b in (DOMINANT, OFFHAND, TORSO)]
    offhand = (classifier_rows(segment, rate_hz, n, OFFHAND) if measured[OFFHAND]
               else [list(OFFHAND_PLACEHOLDER) for _ in range(n)])
    torso = classifier_rows(segment, rate_hz, n, TORSO) if measured[TORSO] else None
    if segment.three_imu:
        stale_pct = 100.0 * measured.count(False) / 3
    else:
        stale_pct = 50.0  # legacy: dominant + placeholder off-hand, the historical value
    return ClassifierInput(classifier_rows(segment, rate_hz, n, DOMINANT), offhand, torso, stale_pct)
