"""Unit tests for relay/windowing.py — SENSOR_FRAME (50 Hz) -> SENSOR_WINDOW (10 Hz) assembly.

Derived from COMMS_IMPLEMENTATION_PLAN.md §7 (two ring buffers per player, emit every `strideFrames`
when the buffer is full, skip emission if stale-sample percentage exceeds `maxStalePct` or the
window's real time span deviates from nominal by more than `maxSpanDeviationPct`, and never emit at
all before a weapon has been confirmed) and the §13 testing-checklist bullet "windowing edge cases".

Most tests below use a deliberately small synthetic config (window_samples=5, stride_frames=1) so the
staleness/span arithmetic is easy to verify by hand; a dedicated parity test cross-checks that the
real defaults (cg_comms.config.WindowingConfig) and shared/config/sensors.yaml still match the plan's
stated 40/5/20/15/50 (window_samples/stride_frames/max_stale_pct/max_span_deviation_pct/frame_rate_hz).
"""
from __future__ import annotations

from cg_comms.config import WindowingConfig, load_yaml, shared_config_dir
from cg_comms.messages.enums import Weapon
from cg_comms.protocol.constants import FLAG_DOM_STALE
from cg_comms.relay.windowing import PlayerWindower

RAW_SAMPLE = (100, -50, 2048, 10, -10, 0)  # arbitrary but fixed raw int16 (ax,ay,az,gx,gy,gz) tuple


def make_windower(**overrides) -> PlayerWindower:
    defaults = dict(
        player_id=1,
        window_samples=5,
        stride_frames=1,
        max_stale_pct=20.0,
        max_span_deviation_pct=15.0,
        frame_rate_hz=50.0,
    )
    defaults.update(overrides)
    return PlayerWindower(**defaults)


def feed_all(windower: PlayerWindower, timestamps: list[int], flags: list[int] | None = None) -> list:
    """Ingests one frame per timestamp (same payload on both wrists); returns each frame's
    ingest_frame() return value, in order."""
    flags = flags or [0] * len(timestamps)
    return [windower.ingest_frame(ts, f, RAW_SAMPLE, RAW_SAMPLE) for ts, f in zip(timestamps, flags)]


# --- config parity: real defaults/yaml should still match the plan's 40/5/20/15/50 -----------------

def test_config_defaults_match_shared_sensors_yaml_and_plan():
    yaml_windowing = load_yaml(shared_config_dir() / "sensors.yaml")["windowing"]
    defaults = WindowingConfig()
    assert defaults.window_samples == yaml_windowing["window_samples"] == 40
    assert defaults.stride_frames == yaml_windowing["stride_frames"] == 5
    assert defaults.max_stale_pct == yaml_windowing["max_stale_pct"] == 20
    assert defaults.max_span_deviation_pct == yaml_windowing["max_span_deviation_pct"] == 15


# --- no weapon confirmed -> never emit, regardless of a full, fresh, well-spaced buffer ------------

def test_no_window_without_weapon_confirmed():
    w = make_windower()
    results = feed_all(w, [0, 20, 40, 60, 80])
    assert all(r is None for r in results)


# --- enough fresh samples at nominal spacing produces a window -------------------------------------

def test_window_emitted_when_buffer_full_fresh_and_on_stride():
    w = make_windower()
    w.on_weapon_confirmed(Weapon.SWORD)
    results = feed_all(w, [0, 20, 40, 60, 80])
    assert results[:-1] == [None, None, None, None]  # buffer not yet full for the first 4 frames
    window = results[-1]
    assert window is not None
    assert window.window_start_ts == 0
    assert window.window_end_ts == 80
    assert window.stale_pct == 0.0
    assert window.weapon == Weapon.SWORD
    assert len(window.dominant) == 5
    assert len(window.offhand) == 5


def test_request_id_increments_across_successive_windows():
    w = make_windower()
    w.on_weapon_confirmed(Weapon.SWORD)
    first = feed_all(w, [0, 20, 40, 60, 80])[-1]
    second = w.ingest_frame(100, 0, RAW_SAMPLE, RAW_SAMPLE)
    assert first.request_id == 0
    assert second.request_id == 1


# --- stride gating: emission is only even attempted on a stride boundary --------------------------

def test_no_window_off_stride_boundary_even_if_buffer_full():
    w = make_windower(stride_frames=2)
    w.on_weapon_confirmed(Weapon.SWORD)
    # 5 frames fills the buffer, but frame_count=5 is odd -> 5 % 2 != 0 -> no emission attempted.
    results = feed_all(w, [0, 20, 40, 60, 80])
    assert results == [None, None, None, None, None]
    # 6th frame: frame_count=6, 6 % 2 == 0, buffer full -> emits.
    window = w.ingest_frame(100, 0, RAW_SAMPLE, RAW_SAMPLE)
    assert window is not None
    assert window.window_start_ts == 20
    assert window.window_end_ts == 100


# --- staleness gating: exactly maxStalePct is allowed, one sample above is suppressed --------------

def test_stale_pct_exactly_at_threshold_is_not_suppressed():
    """window_samples=5 -> 10 total samples (dominant+offhand); 2 stale == 20.0% == max_stale_pct
    exactly. The plan's "do not emit if more than maxStalePct" is a strict `>` check, so exactly at
    the threshold must still emit."""
    w = make_windower()
    w.on_weapon_confirmed(Weapon.SWORD)
    flags = [FLAG_DOM_STALE, FLAG_DOM_STALE, 0, 0, 0]  # 2 of 5 dominant frames stale, 0 offhand
    results = feed_all(w, [0, 20, 40, 60, 80], flags=flags)
    window = results[-1]
    assert window is not None
    assert window.stale_pct == 20.0


def test_stale_pct_above_threshold_suppresses_emission():
    w = make_windower()
    w.on_weapon_confirmed(Weapon.SWORD)
    flags = [FLAG_DOM_STALE, FLAG_DOM_STALE, FLAG_DOM_STALE, 0, 0]  # 3 of 10 == 30% > 20%
    results = feed_all(w, [0, 20, 40, 60, 80], flags=flags)
    assert results[-1] is None


# --- span-deviation gating: exactly maxSpanDeviationPct is allowed, one step beyond is suppressed --

def test_span_deviation_exactly_at_threshold_is_not_suppressed():
    """nominal span = 1000 * (5-1) / 50 = 80 ms; 15% of 80 = 12 ms, so a real span of 92 ms sits
    exactly at the threshold and must still emit (strict `>` check, per §7)."""
    w = make_windower()
    w.on_weapon_confirmed(Weapon.SWORD)
    results = feed_all(w, [0, 20, 40, 60, 92])
    window = results[-1]
    assert window is not None
    assert window.window_end_ts - window.window_start_ts == 92


def test_span_deviation_beyond_threshold_suppresses_emission():
    w = make_windower()
    w.on_weapon_confirmed(Weapon.SWORD)
    results = feed_all(w, [0, 20, 40, 60, 93])  # deviation = 13/80 = 16.25% > 15%
    assert results[-1] is None
