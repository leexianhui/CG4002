"""Unit tests for relay/timesync.py — NTP-style offset/RTT estimation and EWMA smoothing.

Derived from COMMS_IMPLEMENTATION_PLAN.md §5.2 ("Offset estimation": the four-timestamp NTP form,
run 8x at 100 ms spacing, median-RTT x2 outlier rejection, lowest-RTT-sample selection, single-shot
resync every 30 s fed into an alpha=0.1 EWMA) and §5.3 (the laptop's independent cross-check
estimate and its >20 ms divergence-warning threshold), plus the §13 testing-checklist bullet
"time-sync outlier rejection". Numeric thresholds (outlier_rtt_multiplier=2.0, ewma_alpha=0.1,
handshake_samples=8, handshake_spacing_ms=100, divergence_warn_ms=20.0) are cross-checked against
cg_comms.config.TimeSyncConfig's defaults.

NOTE on outlier-rejection boundary testing: because both estimators ultimately select the *lowest
RTT* survivor, a sample sitting exactly at the median x multiplier threshold can never be the
observable "winner" unless it is also the unique minimum RTT among all samples — but anything at
the threshold is, almost by construction, not the minimum once genuinely-good (lower-RTT) samples
exist. That makes the "<=" vs "<" boundary at the threshold itself unobservable through the public
`estimate_offset`/`estimate_offset_laptop_side` API alone (it would require reaching into the
private `_offset_and_rtt` helper, which defeats black-box testing). This file therefore tests the
clearly-specified, observable behaviors — "exceeds median x2 is excluded", "lowest RTT wins" — and
flags the exact-boundary case as an open question rather than asserting on an implementation detail.
"""
from __future__ import annotations

import pytest

from cg_comms.config import TimeSyncConfig
from cg_comms.relay.timesync import (
    LaptopSideSample,
    TimeSyncEstimator,
    TimeSyncSample,
    estimate_offset,
    estimate_offset_laptop_side,
)

TRUE_OFFSET_MS = 500


def make_sample(t0: int, delay_out: float, delay_back: float, proc_ms: float = 0.0,
                offset: float = TRUE_OFFSET_MS) -> TimeSyncSample:
    """Builds a four-timestamp NTP sample for a device whose clock is `offset` ms behind the
    responder's, given one-way transit delays in each direction and a responder processing delay.
    When delay_out == delay_back (the classic NTP symmetric-delay assumption), the standard
    offset formula recovers `offset` exactly, independent of the delay/processing magnitudes."""
    t1 = t0 + offset + delay_out
    t2 = t1 + proc_ms
    t3 = t2 - offset + delay_back
    return TimeSyncSample(t0=int(t0), t1=int(t1), t2=int(t2), t3=int(t3))


# --- config parity: §5.2/§5.3 numeric constants ----------------------------------------------------

def test_config_defaults_match_plan_ss5():
    cfg = TimeSyncConfig()
    assert cfg.handshake_samples == 8
    assert cfg.handshake_spacing_ms == 100
    assert cfg.resync_interval_s == 30
    assert cfg.ewma_alpha == pytest.approx(0.1)
    assert cfg.outlier_rtt_multiplier == pytest.approx(2.0)
    assert cfg.divergence_warn_ms == pytest.approx(20.0)


# --- estimate_offset: correctness under symmetric delay --------------------------------------------

def test_offset_recovered_exactly_under_symmetric_delay():
    samples = [make_sample(t0=i * 100, delay_out=15, delay_back=15, proc_ms=5) for i in range(8)]
    result = estimate_offset(samples, outlier_rtt_multiplier=2.0)
    assert result == pytest.approx(TRUE_OFFSET_MS)


def test_offset_recovered_with_realistic_handshake_burst():
    """8 samples at 100 ms spacing (handshake_samples/handshake_spacing_ms), varying but symmetric
    per-sample network delay — the actual handshake shape described in §5.2."""
    delays = [10, 12, 9, 11, 13, 10, 12, 11]
    samples = [
        make_sample(t0=i * 100, delay_out=d, delay_back=d, proc_ms=2) for i, d in enumerate(delays)
    ]
    result = estimate_offset(samples, outlier_rtt_multiplier=2.0)
    assert result == pytest.approx(TRUE_OFFSET_MS)


def test_estimate_offset_raises_on_empty_samples():
    with pytest.raises(ValueError):
        estimate_offset([], outlier_rtt_multiplier=2.0)


# --- outlier rejection: rtt > median x2 must not influence the estimate ----------------------------

def test_outlier_sample_excluded_from_offset_estimate():
    """7 good samples (rtt=20ms, offset=500 exactly) plus 1 asymmetric-delay outlier whose rtt (200)
    is 10x the batch median (20) and whose naive offset (595, skewed by the asymmetry) must not be
    selected."""
    good = [make_sample(t0=i * 100, delay_out=10, delay_back=10, proc_ms=1) for i in range(7)]
    bad = make_sample(t0=700, delay_out=195, delay_back=5, proc_ms=0)  # rtt=200, offset skews to 595
    result = estimate_offset(good + [bad], outlier_rtt_multiplier=2.0)
    assert result == pytest.approx(TRUE_OFFSET_MS)
    assert result != pytest.approx(595)


def test_lowest_rtt_survivor_is_preferred_over_others():
    """Among several samples that all survive the outlier filter, the one with the lowest RTT must
    be the one whose offset is returned — not the mean, not the first, not the last."""
    low_rtt_sample = make_sample(t0=0, delay_out=2, delay_back=2, proc_ms=0, offset=100.0)  # rtt=4
    mid_rtt_sample = make_sample(t0=100, delay_out=10, delay_back=10, proc_ms=0, offset=250.0)  # rtt=20
    high_rtt_sample = make_sample(t0=200, delay_out=15, delay_back=15, proc_ms=0, offset=400.0)  # rtt=30
    # Order shuffled so the result can't be "just picks the first" by accident.
    result = estimate_offset([high_rtt_sample, low_rtt_sample, mid_rtt_sample], outlier_rtt_multiplier=2.0)
    assert result == pytest.approx(100.0)


# --- laptop-side cross-check estimator (§5.3) -------------------------------------------------------

def test_estimate_offset_laptop_side_excludes_outlier_and_prefers_low_proxy_rtt():
    """Proxy rtt here is the laptop's own processing delay (t2 - t1), not a real network RTT.
    7 good samples share offset=50 (t1 - t0) and small processing delays; 1 outlier has a wildly
    different offset (999) and a processing delay far above the batch median, and must be excluded."""
    good = [LaptopSideSample(t0=i * 100, t1=i * 100 + 50, t2=i * 100 + 50 + 5) for i in range(7)]
    bad = LaptopSideSample(t0=700, t1=700 + 999, t2=700 + 999 + 500)
    result = estimate_offset_laptop_side(good + [bad], outlier_rtt_multiplier=2.0)
    assert result == pytest.approx(50.0)
    assert result != pytest.approx(999.0)


def test_estimate_offset_laptop_side_raises_on_empty_samples():
    with pytest.raises(ValueError):
        estimate_offset_laptop_side([], outlier_rtt_multiplier=2.0)


# --- TimeSyncEstimator: EWMA smoothing (alpha=0.1, §5.2) --------------------------------------------

def test_ewma_seed_sets_initial_offset_without_smoothing():
    estimator = TimeSyncEstimator(ewma_alpha=0.1)
    estimator.seed(1000.0)
    assert estimator.offset_ms == pytest.approx(1000.0)


def test_ewma_first_update_without_prior_seed_takes_the_raw_sample():
    """Before any seed/update, offset_ms is None; the first update() call has nothing to blend
    with, so it must adopt the raw sample rather than blending against a nonexistent prior."""
    estimator = TimeSyncEstimator(ewma_alpha=0.1)
    result = estimator.update(1234.0)
    assert result == pytest.approx(1234.0)
    assert estimator.offset_ms == pytest.approx(1234.0)


def test_ewma_update_smooths_rather_than_overwrites():
    estimator = TimeSyncEstimator(ewma_alpha=0.1)
    estimator.seed(1000.0)
    updated = estimator.update(1010.0)
    expected = 0.1 * 1010.0 + 0.9 * 1000.0  # 1001.0
    assert updated == pytest.approx(expected)
    assert updated != pytest.approx(1010.0)  # must not simply overwrite with the latest sample

    updated_again = estimator.update(1010.0)
    expected_again = 0.1 * 1010.0 + 0.9 * expected  # 1001.9
    assert updated_again == pytest.approx(expected_again)


def test_ewma_repeated_updates_converge_slowly_toward_a_step_change():
    """A sequence of identical new-value samples should pull the smoothed estimate toward it
    gradually (geometric decay), never jumping there in one step."""
    estimator = TimeSyncEstimator(ewma_alpha=0.1)
    estimator.seed(0.0)
    previous = 0.0
    for _ in range(5):
        current = estimator.update(1000.0)
        assert current > previous  # monotonically approaching 1000
        assert current < 1000.0  # but never reaching it in a handful of steps
        previous = current


def test_to_engine_ms_before_seed_raises_runtime_error():
    estimator = TimeSyncEstimator(ewma_alpha=0.1)
    with pytest.raises(RuntimeError):
        estimator.to_engine_ms(1000)


def test_to_engine_ms_applies_seeded_offset():
    estimator = TimeSyncEstimator(ewma_alpha=0.1)
    estimator.seed(500.0)
    assert estimator.to_engine_ms(10_000) == 10_500


# --- divergence check (§5.3: warn if laptop's cross-check diverges by more than 20 ms) --------------

def test_check_divergence_before_seed_is_false():
    estimator = TimeSyncEstimator(ewma_alpha=0.1)
    assert estimator.check_divergence(laptop_side_offset_ms=1000.0, warn_threshold_ms=20.0) is False


def test_check_divergence_within_threshold_is_false():
    estimator = TimeSyncEstimator(ewma_alpha=0.1)
    estimator.seed(500.0)
    assert estimator.check_divergence(laptop_side_offset_ms=515.0, warn_threshold_ms=20.0) is False


def test_check_divergence_exactly_at_threshold_is_false():
    """Boundary: the check is `abs(diff) > warn_threshold_ms`, so a diff exactly equal to the
    threshold must NOT count as divergence."""
    estimator = TimeSyncEstimator(ewma_alpha=0.1)
    estimator.seed(500.0)
    assert estimator.check_divergence(laptop_side_offset_ms=520.0, warn_threshold_ms=20.0) is False


def test_check_divergence_beyond_threshold_is_true():
    estimator = TimeSyncEstimator(ewma_alpha=0.1)
    estimator.seed(500.0)
    assert estimator.check_divergence(laptop_side_offset_ms=520.01, warn_threshold_ms=20.0) is True
