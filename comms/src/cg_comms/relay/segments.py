"""SEGMENT_CHUNK reassembly -> one SENSOR_WINDOW per action segment (wire_protocol.md §3).

Event mode of the hardware hub (hardware/gateway, CG_GATEWAY_EVENT_MODE): instead of a 50 Hz
SENSOR_FRAME stream, the hub's own segmenter decides when an action happened and sends that
segment's dominant-wrist samples once, split into chunks. This module rebuilds each segment and fits
it to the classifier's input: `window_samples` rows at `frame_rate_hz` (ai/config.py WINDOW/FS).

Each chunk is ACKed by the session (duplicates included) before it reaches `SegmentAssembler`, so
this layer only has to tolerate duplicates, reordering and abandoned segments, never ask for resends.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

import structlog

from cg_comms.protocol.constants import SEGMENT_CHUNK_SAMPLES, SEGMENT_MAX_CHUNKS
from cg_comms.relay.windowing import scale_sample

logger = structlog.get_logger(__name__)

_AXES = 6

# Placeholder off-hand row: the hub sends the dominant wrist only, and SENSOR_WINDOW requires an
# offhand array. Gravity-down at rest, zero rotation; only a GUARD model would read it, and there is
# none yet (ai/classifier_server.py). Counted as stale in stalePct, never presented as a measurement.
OFFHAND_PLACEHOLDER: tuple[float, ...] = (0.0, 0.0, 1.0, 0.0, 0.0, 0.0)


@dataclass(frozen=True)
class Segment:
    segment_id: int
    weapon_id: int
    sample_rate_hz: int
    first_sample_ms: int  # device clock
    samples: list[tuple[int, ...]]  # raw int16 rows (ax, ay, az, gx, gy, gz)
    chunk_count: int
    assembly_ms: float  # first chunk received -> last chunk received, relay clock


@dataclass
class _Partial:
    chunk_count: int
    weapon_id: int
    sample_rate_hz: int
    started: float
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
        if not (1 <= count <= SEGMENT_MAX_CHUNKS and index < count
                and 1 <= sample_count <= SEGMENT_CHUNK_SAMPLES and rate > 0):
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
            partial = _Partial(count, int(fields["weapon_id"]), rate, now)
            self._partials[segment_id] = partial
        elif (count, rate) != (partial.chunk_count, partial.sample_rate_hz):
            logger.warning("segment_chunk_inconsistent", segment_id=segment_id, chunk_index=index)
            return None
        if index in partial.chunks:
            return None
        flat = fields["samples"]
        rows = [tuple(flat[i * _AXES:(i + 1) * _AXES]) for i in range(sample_count)]
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


def classifier_rows(segment: Segment, rate_hz: float, n: int) -> list[list[float]]:
    """Segment -> `n` dominant rows in g / deg/s at `rate_hz`, the classifier's input shape."""
    scaled = [scale_sample(row) for row in segment.samples]
    return fit_length(resample(scaled, float(segment.sample_rate_hz), rate_hz), n)
