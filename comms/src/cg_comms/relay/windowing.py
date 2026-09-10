"""SENSOR_FRAME (50 Hz) -> SENSOR_WINDOW (10 Hz) assembly (COMMS_IMPLEMENTATION_PLAN.md §7).

Two ring buffers per player (dominant/off-hand wrist), sized from shared/config/sensors.yaml.
Skips emission (never sends a partial/garbage window) if too many samples are stale or the actual
span deviates too far from the nominal stride timing, and sends nothing at all until a weapon has
been confirmed by an RFID_EVENT.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from cg_comms.messages.enums import Weapon
from cg_comms.messages.models import SensorWindow
from cg_comms.protocol.constants import ACCEL_LSB_PER_G, FLAG_DOM_STALE, FLAG_OFF_STALE, GYRO_LSB_PER_DPS

_ACCEL_AXES = 3  # ax, ay, az; remaining three axes are gx, gy, gz


def _scale_sample(raw: tuple[int, ...]) -> list[float]:
    """Converts one raw int16 IMU sample (ax, ay, az, gx, gy, gz) to physical units (g, deg/s) —
    shared/config/sensors.yaml. Comms converts to float only here, for the SENSOR_WINDOW app
    message; the wire SENSOR_FRAME payload stays raw ints (§4.3)."""
    accel = [v / ACCEL_LSB_PER_G for v in raw[:_ACCEL_AXES]]
    gyro = [v / GYRO_LSB_PER_DPS for v in raw[_ACCEL_AXES:]]
    return accel + gyro


@dataclass
class _RingSample:
    engine_ts_ms: int
    values: tuple[int, ...]
    stale: bool


@dataclass
class _RequestIdCounter:
    _next: int = field(default=0, init=False)

    def next(self) -> int:
        value = self._next
        self._next = (self._next + 1) & 0xFFFFFFFF
        return value


@dataclass
class PlayerWindower:
    """One relay process handles exactly one player's FireBeetle, so one instance per relay."""

    player_id: int
    window_samples: int
    stride_frames: int
    max_stale_pct: float
    max_span_deviation_pct: float
    frame_rate_hz: float
    _request_ids: _RequestIdCounter = field(default_factory=_RequestIdCounter, init=False)
    _dominant: "deque[_RingSample]" = field(init=False)
    _offhand: "deque[_RingSample]" = field(init=False)
    _frame_count: int = field(default=0, init=False)
    _current_weapon: Weapon | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._dominant = deque(maxlen=self.window_samples)
        self._offhand = deque(maxlen=self.window_samples)

    def on_weapon_confirmed(self, weapon: Weapon | None) -> None:
        self._current_weapon = weapon

    @property
    def current_weapon(self) -> Weapon | None:
        return self._current_weapon

    def ingest_frame(
        self,
        engine_ts_ms: int,
        flags: int,
        dominant_raw: tuple[int, ...],
        offhand_raw: tuple[int, ...],
    ) -> SensorWindow | None:
        """Feeds one decoded SENSOR_FRAME's dominant/off-hand samples. Returns a SensorWindow if
        this frame completes a stride and the window passes the staleness/span checks, else None."""
        self._dominant.append(_RingSample(engine_ts_ms, dominant_raw, bool(flags & FLAG_DOM_STALE)))
        self._offhand.append(_RingSample(engine_ts_ms, offhand_raw, bool(flags & FLAG_OFF_STALE)))
        self._frame_count += 1

        if self._frame_count % self.stride_frames != 0:
            return None
        if len(self._dominant) < self.window_samples:
            return None
        if self._current_weapon is None:
            return None

        stale_count = sum(1 for s in self._dominant if s.stale) + sum(1 for s in self._offhand if s.stale)
        total_samples = len(self._dominant) + len(self._offhand)
        stale_pct = 100.0 * stale_count / total_samples
        if stale_pct > self.max_stale_pct:
            return None

        span_ms = self._dominant[-1].engine_ts_ms - self._dominant[0].engine_ts_ms
        nominal_span_ms = 1000.0 * (self.window_samples - 1) / self.frame_rate_hz
        if nominal_span_ms > 0:
            deviation_pct = 100.0 * abs(span_ms - nominal_span_ms) / nominal_span_ms
            if deviation_pct > self.max_span_deviation_pct:
                return None

        return SensorWindow(
            timestamp=engine_ts_ms,
            player_id=self.player_id,  # type: ignore[arg-type]
            request_id=self._request_ids.next(),
            weapon=self._current_weapon,  # type: ignore[arg-type]
            window_start_ts=self._dominant[0].engine_ts_ms,
            window_end_ts=self._dominant[-1].engine_ts_ms,
            sample_rate_hz=self.frame_rate_hz,
            dominant=[_scale_sample(s.values) for s in self._dominant],
            offhand=[_scale_sample(s.values) for s in self._offhand],
            stale_pct=stale_pct,
        )
