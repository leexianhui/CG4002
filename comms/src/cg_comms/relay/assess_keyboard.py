"""Assessment keyboard (Windows console): a=board weapon cycle; b/c/d=fixed IMU B/C/D.
F=file roundtrip, L=one upload ACK loss, D=normal packet sample view, Q=stop keyboard.

One operation at a time, never queued. BUSY/OFFLINE/NO_WEAPON/UPSTREAM_OFFLINE
reject the press visibly. Weapon selection needs the board and engine, but no prior
weapon and no classifier. File operations need only the board. Uppercase D is distinct
from lowercase d. Physical GPIO controls and continuous sensor streaming are independent.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import structlog

from cg_comms.logging_setup import console_event_visible, set_console_event_visible
from cg_comms.messages.enums import Weapon
from cg_comms.relay.assess_file import FileTransferResult

logger = structlog.get_logger(__name__)

# Keyboard poll period. msvcrt has no awaitable API, so we poll; 20 ms is far below human typing
# latency and costs nothing measurable.
_POLL_S = 0.02
# msvcrt.getwch() returns one of these first for arrow/function keys; the NEXT char is the scan
# code and must be consumed too, or it would be read back as a spurious printable keystroke.
_EXTENDED_PREFIXES = ("\x00", "\xe0")


class _AssessSession(Protocol):
    """The slice of relay.session.Session the keyboard uses (a Protocol so tests can fake it)."""

    @property
    def streaming(self) -> bool: ...

    def assess_busy(self) -> bool: ...

    async def echo_keystroke(self, keycode: int) -> tuple[object, bool]: ...

    async def cycle_weapon(self) -> None: ...

    async def send_file(self, data: bytes) -> FileTransferResult: ...

    def arm_assessment_ack_loss(self) -> None: ...


@dataclass
class AssessKeyboard:
    player_id: int
    get_session: Callable[[], _AssessSession | None]
    # Echo needs the whole downstream path; file transfer only needs the FireBeetle link.
    upstream_ready: Callable[[], tuple[bool, bool]]  # (classifier connected, engine connected)
    current_weapon: Callable[[], Weapon | None]
    file_bytes: bytes
    file_label: str
    _op: asyncio.Task | None = field(default=None, init=False)
    _stopped: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    # -- key dispatch (pure: testable without a console) -------------------------------------------

    def reject_reason(self, *, for_echo: bool) -> str | None:
        """Why a key cannot be acted on right now, or None if it can."""
        if self._op is not None and not self._op.done():
            return "BUSY"
        session = self.get_session()
        if session is None or not session.streaming:
            return "OFFLINE"
        if session.assess_busy():
            return "BUSY"
        if for_echo:
            weapon = self.current_weapon()
            if weapon is None or weapon is Weapon.NONE:
                return "NO_WEAPON"
            classifier_up, engine_up = self.upstream_ready()
            if not (classifier_up and engine_up):
                return "UPSTREAM_OFFLINE"
        return None

    def handle_key(self, ch: str) -> None:
        """Acts on one decoded character. Never blocks: long work runs as a single task."""
        if ch == "Q":
            logger.info(
                "assess_keyboard_stopped",
                player_id=self.player_id,
                hint="relay still running; restart it to use the keyboard again",
            )
            self._stopped.set()
            return
        if ch == "D":
            visible = not console_event_visible("packet_sample")
            set_console_event_visible("packet_sample", visible)
            logger.info(
                "assess_packet_sample_console",
                player_id=self.player_id,
                visible=visible,
                note="the JSONL log always records it",
            )
            return
        if ch == "L":
            reason = self.reject_reason(for_echo=False)
            if reason is not None:
                self._log_reject(ch, reason)
                return
            session = self.get_session()
            if session is not None:
                session.arm_assessment_ack_loss()
            return
        if ch == "F":
            reason = self.reject_reason(for_echo=False)
            if reason is not None:
                self._log_reject("F", reason)
                return
            self._op = asyncio.create_task(self._run_file())
            return
        if ch == "a":
            reason = self.reject_reason(for_echo=False)
            if reason is None and not self.upstream_ready()[1]:
                reason = "UPSTREAM_OFFLINE"
            if reason is not None:
                self._log_reject(ch, reason)
                return
            self._op = asyncio.create_task(self._run_weapon())
            return
        if ch in ("b", "c", "d"):
            reason = self.reject_reason(for_echo=True)
            if reason is not None:
                self._log_reject(ch, reason)
                return
            self._op = asyncio.create_task(self._run_echo(ord(ch)))
            return
        logger.info(
            "assess_key_ignored",
            player_id=self.player_id,
            key=repr(ch),
            help="a = weapon | b/c/d = fixed IMU | F = file | L = ACK loss | D = packet view | Q = stop",
        )

    async def _run_weapon(self) -> None:
        session = self.get_session()
        if session is None:
            return
        try:
            await session.cycle_weapon()
        except Exception as exc:
            logger.error(
                "assess_weapon_failed", player_id=self.player_id, error=f"{type(exc).__name__}: {exc}"
            )

    def _log_reject(self, key: str, reason: str) -> None:
        hints = {
            "BUSY": "wait for the current round trip / transfer to finish",
            "OFFLINE": "FireBeetle not streaming (power, range, or still reconnecting)",
            "NO_WEAPON": "press a, or select a weapon on the board (RFID / GPIO4)",
            "UPSTREAM_OFFLINE": "relay is not connected to the Ultra96 classifier and/or engine",
        }
        logger.warning(
            "assess_key_rejected", player_id=self.player_id, key=key, reason=reason, hint=hints[reason]
        )

    async def _run_echo(self, keycode: int) -> None:
        session = self.get_session()
        if session is None:
            return
        try:
            await session.echo_keystroke(keycode)
        except Exception as exc:  # the transport can close mid-send; report, never crash the relay
            logger.error(
                "assess_echo_failed",
                player_id=self.player_id,
                keycode=keycode,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _run_file(self) -> None:
        session = self.get_session()
        if session is None:
            return
        logger.info(
            "assess_file_requested",
            player_id=self.player_id,
            file=self.file_label,
            total_bytes=len(self.file_bytes),
        )
        try:
            await session.send_file(self.file_bytes)  # logs assess_file_verified / _failed itself
        except ValueError as exc:  # empty / oversize: nothing was sent
            logger.error("assess_file_rejected", player_id=self.player_id, error=str(exc))
        except Exception as exc:
            logger.error(
                "assess_file_failed", player_id=self.player_id, reason=f"{type(exc).__name__}: {exc}"
            )

    # -- console loop ------------------------------------------------------------------------------

    async def run(self) -> None:
        """Polls the console until Q or cancellation. Returns immediately if no console is present."""
        if sys.platform != "win32":
            logger.warning("assess_keyboard_disabled", reason="keyboard control needs Windows (msvcrt)")
            return
        if not sys.stdin.isatty():
            logger.warning("assess_keyboard_disabled", reason="stdin is not a console")
            return
        import msvcrt

        logger.info(
            "assess_keyboard_ready",
            player_id=self.player_id,
            file=self.file_label,
            keys="a = weapon | b/c/d = fixed IMU | F = file | L = ACK loss | D = packets | Q = stop",
        )
        try:
            while not self._stopped.is_set():
                while msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch == "\x03":  # Ctrl+C read as a character: keep its usual meaning
                        raise KeyboardInterrupt
                    if ch in _EXTENDED_PREFIXES:
                        msvcrt.getwch()  # discard the scan code of an arrow/function key
                        continue
                    self.handle_key(ch)
                    if self._stopped.is_set():
                        break
                await asyncio.sleep(_POLL_S)
        finally:
            await self.cancel_op()

    async def cancel_op(self) -> None:
        op, self._op = self._op, None
        if op is not None and not op.done():
            op.cancel()
            await asyncio.gather(op, return_exceptions=True)
