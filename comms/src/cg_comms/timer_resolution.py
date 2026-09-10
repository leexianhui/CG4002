"""Windows timer-resolution guard, for processes that must pace traffic at tens of hertz.

Windows' default system timer tick is ~15.6ms, and `asyncio.sleep()` cannot resolve finer than one
tick: `await asyncio.sleep(0.02)` returns after ~31ms, so a nominally 50 Hz sender actually emits at
~32 Hz. That is not a cosmetic timing wobble here -- `relay/windowing.py` validates each window's
measured span against the nominal span implied by `frame_rate_hz` (`max_span_deviation_pct`, 15% by
default). At ~32 Hz a 40-sample window spans ~1216ms against a 780ms nominal, a ~56% deviation, so
*every* window is discarded and the rig streams sensor frames while producing no SENSOR_WINDOWs, no
ACTION_RESULTs, and no combat -- with every link reporting healthy.

`timeBeginPeriod(1)` raises the resolution process-wide (a documented, reversible Windows API; the
cost is slightly higher power draw while held, which is irrelevant for a demo rig). It is a no-op on
every other platform, where `asyncio.sleep()` already resolves well under a millisecond.

Only the *sending* mocks need this. The relay and engine are event-driven -- they react to arriving
frames rather than generating them on a schedule -- so their timing follows the device's.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

_PERIOD_MS = 1


@contextmanager
def high_resolution_timers() -> Iterator[None]:
    """Raises the OS timer resolution for the duration of the block, on Windows. No-op elsewhere.

    Failure to raise it is deliberately non-fatal: the caller still runs, just with coarse pacing.
    Callers that depend on the finer resolution should check `timer_resolution_available()` and warn.
    """
    raised = _begin()
    try:
        yield
    finally:
        if raised:
            _end()


def timer_resolution_available() -> bool:
    """True when this platform either needs no adjustment or can actually apply one."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        return hasattr(ctypes, "windll")
    except Exception:
        return False


def _begin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return ctypes.windll.winmm.timeBeginPeriod(_PERIOD_MS) == 0
    except Exception:
        return False


def _end() -> None:
    try:
        import ctypes

        ctypes.windll.winmm.timeEndPeriod(_PERIOD_MS)
    except Exception:
        pass
