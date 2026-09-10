"""Monotonic and wall-clock helpers, plus device-clock <-> engine-time conversion.

Engine time is milliseconds on the relay/engine process's own clock. Device (FireBeetle) timestamps
are converted into this frame at relay ingest using the offset relay/timesync.py maintains — no
other module does clock arithmetic (COMMS_IMPLEMENTATION_PLAN.md §5.3).
"""

from __future__ import annotations

import time


def monotonic_ms() -> int:
    """Monotonic milliseconds, immune to wall-clock adjustments (NTP steps, DST). Used wherever a
    duration or ordering is measured (heartbeat liveness, reorder buffer watermarks)."""
    return int(time.monotonic() * 1000)


def wall_clock_unix_ms() -> int:
    """Wall-clock milliseconds since the Unix epoch, for HELLO_ACK.serverEpochUnixMs and any
    timestamp that must be meaningful outside this process's lifetime (log file names, JSONL)."""
    return int(time.time() * 1000)


def split_epoch_ms(epoch_unix_ms: int) -> tuple[int, int]:
    """Splits a 64-bit epoch-ms value into (lo, hi) uint32 halves, per HELLO_ACK's wire layout."""
    return epoch_unix_ms & 0xFFFFFFFF, (epoch_unix_ms >> 32) & 0xFFFFFFFF


def join_epoch_ms(lo: int, hi: int) -> int:
    """Inverse of split_epoch_ms."""
    return (hi << 32) | lo


def device_to_engine_ms(device_timestamp_ms: int, offset_ms: float) -> int:
    """Converts a device-clock timestamp to engine time using the device's current clock offset
    (relay.timesync's EWMA-smoothed estimate, defined as engine_time - device_time)."""
    return int(device_timestamp_ms + offset_ms)
