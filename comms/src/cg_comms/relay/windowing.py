"""SENSOR_FRAME (50 Hz) -> SENSOR_WINDOW (10 Hz, or one-per-capture) assembly
(COMMS_IMPLEMENTATION_PLAN.md §7, docs/DEMO_TRIGGERED_ACTION_PLAN.md §3.2).

Two ring buffers per player (dominant/off-hand wrist), sized from shared/config/sensors.yaml.
Skips emission (never sends a partial/garbage window) if too many samples are stale or the actual
span deviates too far from the nominal stride timing, and sends nothing at all until a weapon has
been confirmed by an RFID_EVENT.

In `trigger_mode` (comms-demo only), stride-based continuous emission is replaced by an
arm/count/emit/refractory state machine keyed on CG_FLAG_MOTION_ACTIVE: exactly one SensorWindow is
emitted per tagged button-press capture, carrying the firmware's trigger_id. `trigger_mode=False`
(the default, and the only mode the real game uses) is bit-for-bit the original stride behaviour.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import structlog

from cg_comms.messages.enums import Weapon
from cg_comms.messages.models import SensorWindow
from cg_comms.protocol.constants import (
    ACCEL_LSB_PER_G,
    FLAG_DOM_STALE,
    FLAG_MOTION_ACTIVE,
    FLAG_OFF_STALE,
    FLAG_TRIGGER_ID_MASK,
    FLAG_TRIGGER_ID_SHIFT,
    GYRO_LSB_PER_DPS,
)

logger = structlog.get_logger(__name__)

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
    trigger_mode: bool = False
    _request_ids: _RequestIdCounter = field(default_factory=_RequestIdCounter, init=False)
    _dominant: "deque[_RingSample]" = field(init=False)
    _offhand: "deque[_RingSample]" = field(init=False)
    _frame_count: int = field(default=0, init=False)
    _current_weapon: Weapon | None = field(default=None, init=False)
    # Trigger-mode-only state (docs/DEMO_TRIGGERED_ACTION_PLAN.md §3.2). Unused when trigger_mode=False.
    _armed: bool = field(default=False, init=False)
    _capture_remaining: int = field(default=0, init=False)
    _capture_trigger_id: int = field(default=0, init=False)
    _refractory: bool = field(default=False, init=False)
    _capture_weapon: Weapon | None = field(default=None, init=False)
    _capture_id: int = field(default=0, init=False)
    _last_continuous_suppression: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._dominant = deque(maxlen=self.window_samples)
        self._offhand = deque(maxlen=self.window_samples)

    def on_weapon_confirmed(self, weapon: Weapon | None) -> None:
        self._current_weapon = weapon

    def reset_session(self) -> None:
        if self._armed:
            self._log_suppression("too_few", self._capture_trigger_id, ended_by="session_lost")
        self._dominant.clear()
        self._offhand.clear()
        self._frame_count = 0
        self._current_weapon = None
        self._armed = self._refractory = False
        self._last_continuous_suppression = None
        self._capture_remaining = 0
        self._capture_weapon = None
        # Request/capture counters belong to the relay lifetime, not a BLE session.

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
        """Feeds one decoded SENSOR_FRAME's dominant/off-hand samples.

        `trigger_mode=False` (default): returns a SensorWindow if this frame completes a stride and
        the window passes the staleness/span checks, else None — bit-for-bit the original behaviour.

        `trigger_mode=True`: returns a SensorWindow (with `trigger_id` set) exactly once per tagged
        button-press capture, else None. See docs/DEMO_TRIGGERED_ACTION_PLAN.md §3.2.
        """
        if self.trigger_mode:
            tagged = bool(flags & FLAG_MOTION_ACTIVE)
            trigger = (flags & FLAG_TRIGGER_ID_MASK) >> FLAG_TRIGGER_ID_SHIFT
            if self._armed and (not tagged or trigger != self._capture_trigger_id):
                self._log_suppression("too_few", self._capture_trigger_id,
                                      ended_by="untagged" if not tagged else "trigger_changed")
                self._armed = False
                self._dominant.clear()
                self._offhand.clear()
            if tagged and not self._armed and not self._refractory:
                self._dominant.clear()
                self._offhand.clear()
        self._dominant.append(_RingSample(engine_ts_ms, dominant_raw, bool(flags & FLAG_DOM_STALE)))
        self._offhand.append(_RingSample(engine_ts_ms, offhand_raw, bool(flags & FLAG_OFF_STALE)))
        self._frame_count += 1

        if self.trigger_mode:
            return self._ingest_frame_triggered(engine_ts_ms, flags)

        if self._frame_count % self.stride_frames != 0:
            return None
        window, reason, _extra = self._build_window_if_gates_pass(engine_ts_ms, trigger_id=None)
        if window is None and self._frame_count >= self.window_samples:
            # Continuous mode re-checks every stride (5x/s): log a reason when it starts, not each
            # time it repeats, or e.g. "no_weapon" before the weapon locks floods the console.
            if reason != self._last_continuous_suppression:
                self._log_suppression(reason, None)
            self._last_continuous_suppression = reason
        elif window is not None:
            self._last_continuous_suppression = None
        return window

    def _ingest_frame_triggered(self, engine_ts_ms: int, flags: int) -> SensorWindow | None:
        tagged = bool(flags & FLAG_MOTION_ACTIVE)

        if self._refractory:
            # Ignore MOTION_ACTIVE (a stuck-low button) until an untagged frame is seen; firmware
            # guarantees untagged frames between presses, so this is belt-and-braces only.
            if not tagged:
                self._refractory = False
            return None

        if not self._armed:
            if not tagged:
                return None
            self._armed = True
            self._capture_trigger_id = (flags & FLAG_TRIGGER_ID_MASK) >> FLAG_TRIGGER_ID_SHIFT
            self._capture_remaining = self.window_samples
            self._capture_weapon = self._current_weapon
            self._capture_id += 1
            logger.info("capture_started", player_id=self.player_id, trigger_id=self._capture_trigger_id,
                        capture_id=self._capture_id, timestamp=engine_ts_ms)

        self._capture_remaining -= 1
        if self._capture_remaining > 0:
            return None

        trigger_id = self._capture_trigger_id
        self._armed = False
        self._refractory = True
        window, reason, _extra = self._build_window_if_gates_pass(engine_ts_ms, trigger_id=trigger_id)
        if window is None:
            self._log_suppression(reason, trigger_id)
        return window

    def _log_suppression(self, reason: str | None, trigger_id: int | None, **extra) -> None:
        count = len(self._dominant)
        expected = 1000.0 * (self.window_samples - 1) / self.frame_rate_hz
        span = (self._dominant[-1].engine_ts_ms - self._dominant[0].engine_ts_ms) if count else 0
        stale = sum(s.stale for s in self._dominant) + sum(s.stale for s in self._offhand)
        logger.info("window_suppressed", reason=reason, player_id=self.player_id,
                    trigger_id=trigger_id, capture_id=self._capture_id,
                    weapon=self._current_weapon.value if self._current_weapon else None,
                    capture_weapon=self._capture_weapon.value if self._capture_weapon else None,
                    frame_count=count, expected_frame_count=self.window_samples,
                    span_ms=span, expected_span_ms=expected,
                    deviation_pct=100 * abs(span - expected) / expected if expected else 0,
                    max_span_deviation_pct=self.max_span_deviation_pct,
                    stale_pct=100 * stale / (2 * count) if count else 0,
                    max_stale_pct=self.max_stale_pct, **extra)

    def _build_window_if_gates_pass(
        self, engine_ts_ms: int, trigger_id: int | None
    ) -> tuple[SensorWindow | None, str | None, dict]:
        """Returns (window, suppression_reason, extra_log_fields). suppression_reason/extra are
        only meaningful when window is None — used to make `window_suppressed` diagnosable
        (was previously player_id/trigger_id only, so a weapon-gate failure and a span-gate
        failure were indistinguishable in the log)."""
        if len(self._dominant) < self.window_samples:
            return None, "too_few", {"sample_count": len(self._dominant)}
        weapon = self._capture_weapon if self.trigger_mode else self._current_weapon
        if weapon is None:
            return None, "no_weapon", {}

        stale_count = sum(1 for s in self._dominant if s.stale) + sum(1 for s in self._offhand if s.stale)
        total_samples = len(self._dominant) + len(self._offhand)
        stale_pct = 100.0 * stale_count / total_samples
        if stale_pct > self.max_stale_pct:
            return None, "stale", {"stale_pct": stale_pct}

        span_ms = self._dominant[-1].engine_ts_ms - self._dominant[0].engine_ts_ms
        nominal_span_ms = 1000.0 * (self.window_samples - 1) / self.frame_rate_hz
        if nominal_span_ms > 0:
            deviation_pct = 100.0 * abs(span_ms - nominal_span_ms) / nominal_span_ms
            if deviation_pct > self.max_span_deviation_pct:
                return None, "span", {"span_ms": span_ms, "deviation_pct": deviation_pct}

        return (
            SensorWindow(
                timestamp=engine_ts_ms,
                player_id=self.player_id,  # type: ignore[arg-type]
                request_id=self._request_ids.next(),
                weapon=weapon,  # type: ignore[arg-type]
                window_start_ts=self._dominant[0].engine_ts_ms,
                window_end_ts=self._dominant[-1].engine_ts_ms,
                sample_rate_hz=self.frame_rate_hz,
                dominant=[_scale_sample(s.values) for s in self._dominant],
                offhand=[_scale_sample(s.values) for s in self._offhand],
                stale_pct=stale_pct,
                trigger_id=trigger_id,
            ),
            None,
            {},
        )
