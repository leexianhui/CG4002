"""NTP-style time synchronisation (COMMS_IMPLEMENTATION_PLAN.md §5.2/§5.3).

Two estimators live here:
  * `estimate_offset` — the full four-timestamp NTP form. Only the *device* ever has all four
    timestamps (t0/t3 are its own clock), so this is used device-side — i.e. by
    mock/firebeetle_sim.py standing in for the FireBeetle, not by the relay.
  * `estimate_offset_laptop_side` — the relay's own independent cross-check (§5.3: "have the
    laptop maintain its own independent estimate and normalise on ingest anyway"). The relay only
    ever observes (t0, t1, t2) for a TIME_SYNC exchange — it has no device-side arrival timestamp —
    so it approximates one-way transit using its own processing delay as an RTT proxy.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TimeSyncSample:
    """Full four-timestamp NTP sample, as seen by whichever side initiated the exchange."""

    t0: int  # requester clock, at send
    t1: int  # responder clock, at receive
    t2: int  # responder clock, at send
    t3: int  # requester clock, at arrival of the response


def _offset_and_rtt(sample: TimeSyncSample) -> tuple[float, float]:
    offset = ((sample.t1 - sample.t0) + (sample.t2 - sample.t3)) / 2
    rtt = (sample.t3 - sample.t0) - (sample.t2 - sample.t1)
    return offset, rtt


def estimate_offset(samples: list[TimeSyncSample], outlier_rtt_multiplier: float) -> float:
    """Discards samples whose RTT exceeds the median RTT times `outlier_rtt_multiplier`, then takes
    the offset of the lowest-RTT remaining sample (the least asymmetric one)."""
    if not samples:
        raise ValueError("estimate_offset requires at least one sample")
    pairs = [_offset_and_rtt(s) for s in samples]
    median_rtt = statistics.median(rtt for _, rtt in pairs)
    threshold = median_rtt * outlier_rtt_multiplier
    survivors = [(offset, rtt) for offset, rtt in pairs if rtt <= threshold] or pairs
    best_offset, _ = min(survivors, key=lambda pair: pair[1])
    return best_offset


@dataclass(frozen=True)
class LaptopSideSample:
    t0: int  # device clock, at TIME_SYNC_REQ send
    t1: int  # laptop clock, at TIME_SYNC_REQ receive
    t2: int  # laptop clock, at TIME_SYNC_RESP send


def estimate_offset_laptop_side(samples: list[LaptopSideSample], outlier_rtt_multiplier: float) -> float:
    """The relay's own cross-check estimate. Deliberately conservative: it exists to detect gross
    divergence from the device's self-reported offset, not to replace it."""
    if not samples:
        raise ValueError("estimate_offset_laptop_side requires at least one sample")
    proxies = [(s.t1 - s.t0, s.t2 - s.t1) for s in samples]
    median_rtt = statistics.median(rtt for _, rtt in proxies)
    threshold = median_rtt * outlier_rtt_multiplier
    survivors = [(offset, rtt) for offset, rtt in proxies if rtt <= threshold] or proxies
    best_offset, _ = min(survivors, key=lambda pair: pair[1])
    return best_offset


@dataclass
class TimeSyncEstimator:
    """Tracks one device's clock offset (engine_ms - device_ms) via EWMA across repeated
    single-sample resyncs (30 s cadence, per §5.2)."""

    ewma_alpha: float
    offset_ms: float | None = field(default=None, init=False)

    def seed(self, initial_offset_ms: float) -> None:
        """Sets the offset from the handshake burst, before any EWMA smoothing has happened."""
        self.offset_ms = initial_offset_ms

    def update(self, sample_offset_ms: float) -> float:
        """Feeds one single-shot resync sample into the EWMA and returns the new smoothed offset."""
        if self.offset_ms is None:
            self.offset_ms = sample_offset_ms
        else:
            self.offset_ms = self.ewma_alpha * sample_offset_ms + (1 - self.ewma_alpha) * self.offset_ms
        return self.offset_ms

    def to_engine_ms(self, device_ms: int) -> int:
        if self.offset_ms is None:
            raise RuntimeError("TimeSyncEstimator has not been seeded yet")
        return int(device_ms + self.offset_ms)

    def check_divergence(self, laptop_side_offset_ms: float, warn_threshold_ms: float) -> bool:
        """Returns True (and the caller should log a warning + trust the device-derived offset,
        per §5.3) when the laptop's own cross-check estimate diverges from the seeded/EWMA offset
        by more than `warn_threshold_ms`."""
        if self.offset_ms is None:
            return False
        return abs(laptop_side_offset_ms - self.offset_ms) > warn_threshold_ms
