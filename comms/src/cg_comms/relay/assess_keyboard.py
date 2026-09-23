"""Relay-laptop keyboard for the comms assessment (docs/COMMS_ASSESSMENT_CHECKLIST.md).

Keys (typed into the relay's own console window, which must have focus):

    letters / digits / space   one ASSESS_ECHO_REQ -> FireBeetle transform -> Ultra96 -> visualizer
    F  (Shift+f)               send the assessment file to the FireBeetle, verified chunk by chunk
    D  (Shift+d)               show/hide the decoded `packet_sample` line on the console
    Q  (Shift+q)               stop listening to the keyboard (the relay keeps running; Ctrl+C quits)

The uppercase F/D/Q are commands so that every LOWERCASE letter stays a keystroke: the transform
table is keyed on the keycode, and the professor should be able to type anything.

One operation at a time, never queued. A key pressed while an echo or file transfer is running is
REJECTED with a reason printed on screen rather than buffered: a hidden backlog would make the demo's
"one keystroke -> one visualizer event" claim false, and would keep sending minutes after the
professor stopped typing. Rejection reasons:

    BUSY              an assessment packet is already in flight (echo round trip or file transfer)
    OFFLINE           no FireBeetle session is streaming (board off, out of range, reconnecting)
    NO_WEAPON         echo only: no weapon confirmed yet, so the engine could not emit ActionRecognized
    UPSTREAM_OFFLINE  echo only: the relay is not connected to the Ultra96 classifier or the engine

The checks are a snapshot, so a link can still drop between the check and the send. That is fine:
every later stage reports its own failure (assess_echo_undelivered / assess_echo_timeout /
window_dropped / assess_file_failed), so the key press is never silently lost.

Windows only (msvcrt). On any other platform, or when stdin is not a console (e.g. output redirected
under a test runner), the keyboard is disabled with one log line and the relay runs normally.
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
            logger.info("assess_keyboard_stopped", player_id=self.player_id,
                        hint="relay still running; restart it to use the keyboard again")
            self._stopped.set()
            return
        if ch == "D":
            visible = not console_event_visible("packet_sample")
            set_console_event_visible("packet_sample", visible)
            logger.info("assess_packet_sample_console", player_id=self.player_id,
                        visible=visible, note="the JSONL log always records it")
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
        if ch == " " or (ch.isascii() and ch.isalnum()):
            reason = self.reject_reason(for_echo=True)
            if reason is not None:
                self._log_reject(ch, reason)
                return
            self._op = asyncio.create_task(self._run_echo(ord(ch)))
            return
        logger.info("assess_key_ignored", player_id=self.player_id, key=repr(ch),
                    help="letters/digits/space = echo, F = file, D = packet view, Q = stop keyboard")

    def _log_reject(self, key: str, reason: str) -> None:
        hints = {
            "BUSY": "wait for the current round trip / transfer to finish",
            "OFFLINE": "FireBeetle not streaming (power, range, or still reconnecting)",
            "NO_WEAPON": "confirm a weapon on the board first (RFID tap / weapon button)",
            "UPSTREAM_OFFLINE": "relay is not connected to the Ultra96 classifier and/or engine",
        }
        logger.warning("assess_key_rejected", player_id=self.player_id, key=key, reason=reason,
                       hint=hints[reason])

    async def _run_echo(self, keycode: int) -> None:
        session = self.get_session()
        if session is None:
            return
        try:
            await session.echo_keystroke(keycode)
        except Exception as exc:  # the transport can close mid-send; report, never crash the relay
            logger.error("assess_echo_failed", player_id=self.player_id, keycode=keycode,
                         error=f"{type(exc).__name__}: {exc}")

    async def _run_file(self) -> None:
        session = self.get_session()
        if session is None:
            return
        logger.info("assess_file_requested", player_id=self.player_id, file=self.file_label,
                    total_bytes=len(self.file_bytes))
        try:
            await session.send_file(self.file_bytes)  # logs assess_file_verified / _failed itself
        except ValueError as exc:  # empty / oversize: nothing was sent
            logger.error("assess_file_rejected", player_id=self.player_id, error=str(exc))
        except Exception as exc:
            logger.error("assess_file_failed", player_id=self.player_id,
                         reason=f"{type(exc).__name__}: {exc}")

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

        logger.info("assess_keyboard_ready", player_id=self.player_id, file=self.file_label,
                    keys="letters/digits/space = echo | F = file | L = one ACK-loss test | D = packet view | Q = stop keyboard")
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
