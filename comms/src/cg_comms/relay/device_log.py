"""Hardware debug-log CSVs: one file per player per match, under hardware/game/.

The hub forwards its own debug lines and its torso's and left wrist's as best-effort DEVICE_LOG
pieces (wire_protocol.md §3, hardware/gateway/include/gateway_log.h). This writer puts each line
back together from its pieces and appends it to the current match's CSV. A new file starts when the
relay starts and on every match reset (the engine's WEAPON_UNLOCK); it is created on its first line,
so a match with no hardware traffic leaves no empty file.

A lost piece never stalls a line: a line whose next line from the same board arrives first, or whose
pieces skip an index, is written with "..." where text is missing and complete=0.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any

import structlog

from cg_comms.protocol.constants import DEVICE_LOG_INDEX, DEVICE_LOG_MORE

logger = structlog.get_logger(__name__)

BOARDS: dict[int, str] = {0: "hub", 1: "torso", 2: "left_wrist"}
COLUMNS = ("rx_time", "board", "device_ms", "kind", "complete", "message")
_GAP = "..."
_KIND = re.compile(r"^\[?([A-Za-z_]\w*)")


def line_kind(message: str) -> str:
    """The line's leading word, for filtering: "[stats] to=..." -> "stats",
    "guard_wait el=..." -> "guard_wait", "guard_state=GUARDING" -> "guard_state"."""
    match = _KIND.match(message)
    return match.group(1) if match else ""


@dataclass
class _Partial:
    line_seq: int
    device_ms: int
    parts: dict[int, str] = field(default_factory=dict)
    last_index: int | None = None  # index of the piece without the MORE bit, once seen

    def text(self) -> tuple[str, bool]:
        end = self.last_index if self.last_index is not None else max(self.parts)
        pieces = [self.parts.get(i, _GAP) for i in range(end + 1)]
        complete = self.last_index is not None and len(self.parts) == end + 1
        if self.last_index is None:
            pieces.append(_GAP)
        return "".join(pieces), complete


@dataclass
class DeviceLogWriter:
    out_dir: Path
    player_id: int
    now: Callable[[], datetime] = datetime.now

    _file: IO[str] | None = field(default=None, init=False)
    _csv: Any = field(default=None, init=False)
    _match_started: datetime = field(init=False)
    _match_reason: str = field(default="relay start", init=False)
    _partials: dict[int, _Partial] = field(default_factory=dict, init=False)
    _failed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._match_started = self.now()

    @property
    def path(self) -> Path | None:
        return Path(self._file.name) if self._file is not None else None

    def start_match(self, reason: str) -> None:
        """Ends the current match's file; the next line opens a new one."""
        self._flush_partials()
        self._close_file()
        self._match_started = self.now()
        self._match_reason = reason

    def add_piece(self, fields: dict[str, Any]) -> None:
        source = int(fields["source"])
        line_seq = int(fields["line_seq"])
        part = int(fields["part"])
        index = part & DEVICE_LOG_INDEX
        text = bytes(fields["text"]).split(b"\0", 1)[0].decode("ascii", errors="replace")

        partial = self._partials.get(source)
        if partial is not None and partial.line_seq != line_seq:
            self._write_partial(source, partial)  # its remaining pieces were lost
            partial = None
        if partial is None:
            partial = _Partial(line_seq=line_seq, device_ms=int(fields["device_timestamp_ms"]))
            self._partials[source] = partial
        partial.parts[index] = text
        if not part & DEVICE_LOG_MORE:
            partial.last_index = index
            del self._partials[source]
            self._write_partial(source, partial)

    def close(self) -> None:
        self._flush_partials()
        self._close_file()

    def _flush_partials(self) -> None:
        for source, partial in list(self._partials.items()):
            self._write_partial(source, partial)
        self._partials.clear()

    def _write_partial(self, source: int, partial: _Partial) -> None:
        message, complete = partial.text()
        self._write_row(BOARDS.get(source, f"source{source}"), partial.device_ms, message, complete)

    def _write_row(self, board: str, device_ms: int | str, message: str, complete: bool) -> None:
        if self._failed:
            return
        try:
            if self._csv is None:
                self._open_file()
            self._csv.writerow([self.now().isoformat(timespec="milliseconds"), board, device_ms,
                                line_kind(message), int(complete), message])
            assert self._file is not None
            self._file.flush()  # readable mid-match, and nothing lost if the relay is killed
        except OSError as exc:
            # Debug output only: never let a disk problem take the relay session down.
            self._failed = True
            logger.warning("device_log_write_failed", error=str(exc), out_dir=str(self.out_dir))

    def _open_file(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"match_{self._match_started:%Y%m%d_%H%M%S}_p{self.player_id}"
        path = self.out_dir / f"{stem}.csv"
        n = 2
        while path.exists():
            path = self.out_dir / f"{stem}_{n}.csv"
            n += 1
        self._file = path.open("w", newline="", encoding="utf-8")
        self._csv = csv.writer(self._file)
        self._csv.writerow(COLUMNS)
        self._csv.writerow([self._match_started.isoformat(timespec="milliseconds"), "relay", "",
                            "match_start", 1, self._match_reason])
        logger.info("device_log_opened", player_id=self.player_id, path=str(path),
                    reason=self._match_reason)

    def _close_file(self) -> None:
        if self._file is not None:
            self._file.close()
        self._file = None
        self._csv = None
