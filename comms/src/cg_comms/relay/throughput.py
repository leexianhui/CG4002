"""Per-player receive throughput and readable packet samples (comms assessment: "show kbps stats",
"maximise speed", "dummy sensor data for more than 1 minute", "logs convert hex to readable").

What is measured, and what is NOT (say this on the video -- it is the honest version):

    sensor goodput kbps = unique valid SENSOR_FRAMEs * 42 B payload * 8 / elapsed_s / 1000
    sensor frame kbps   = the same frames' full wire bytes (68 B each) * 8 / elapsed_s / 1000
    transport kbps      = every byte the BLE callback handed the relay * 8 / elapsed_s / 1000

These are application-layer rates at the RECEIVER, after CRC + GCM + replay checks. They are not the
BLE PHY bitrate, and the 42 B figure excludes header/tag/CRC so wrapper bytes never inflate goodput.

Loss uses the device's transmit sequence number. Every device -> laptop frame (sensor, heartbeat,
RFID, ACK, assessment responses) takes the next value of ONE per-session counter, and the firmware
consumes a seq even when the BLE stack refuses the notify. So over a span:

    generated = highest ext_seq - floor + 1      lost = generated - unique frames inside that span

estimates missing sequence allocations of ALL packet types in the observed span, including
on-chip notify refusals, rejected/corrupt frames and radio loss. It is NOT exact sensor loss and
cannot see losses before the first or after the last received sequence. Interval gaps remain
provisional under reordering; only cumulative reports can account for later arrivals.
For the whole run, `floor` is the first ext_seq
seen after streaming began. For one interval it is the previous interval's highest ext_seq + 1, so a
gap that straddles an interval boundary is charged to exactly one interval instead of vanishing
between them. A frame BELOW the floor (an RFID_EVENT retransmit reusing an older seq whose first copy
was lost) is still a unique frame received, but it is not counted against the current span -- it
would otherwise cancel out one genuinely lost frame. An exact reliable retransmit of a frame already
received is a duplicate and is excluded from both sides. Counting gaps between SENSOR seqs alone
would be wrong: heartbeats and ACKs legitimately occupy seqs in between. A seq lost at the very end
of a run (no later frame arrives) cannot be detected by this method and is not counted.

The whole-run figure (`throughput_run_summary`, `run_*` fields) is the authoritative one; interval
figures are for watching the rate live.

Output: one `throughput` line per interval (not per packet -- an uncluttered terminal is a
requirement), one `packet_sample` line decoding one real frame per interval (always in the JSONL
file; on the console only while toggled on with the D key), a whole-run summary when the session
ends, and `logs/throughput-p<N>-latest.json` (atomically replaced) that `cg-throughput-total`
combines across both relays for the concurrent 2-FireBeetle run.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from cg_comms.clock import wall_clock_unix_ms
from cg_comms.protocol.codec import DecodedPacket
from cg_comms.protocol.constants import (
    ACCEL_LSB_PER_G,
    CRC_SIZE,
    GCM_TAG_SIZE,
    GYRO_LSB_PER_DPS,
    HEADER_SIZE,
    PacketType,
)

logger = structlog.get_logger(__name__)

SENSOR_PAYLOAD_BYTES = 42  # SENSOR_FRAME plaintext (payloads.py); the goodput numerator


def _kbps(byte_count: int, elapsed_s: float) -> float:
    return round(byte_count * 8 / elapsed_s / 1000, 2) if elapsed_s > 0 else 0.0


@dataclass
class _Counters:
    transport_bytes: int = 0
    frames: int = 0  # unique, valid, decoded
    duplicates: int = 0  # valid reliable retransmits (excluded from `frames`)
    sensor_frames: int = 0
    sensor_frame_bytes: int = 0
    # Lowest ext_seq that belongs to this span (see module docstring). None until known: for the run
    # (and the first interval) it is set by the first frame; later intervals start with it preset.
    floor_ext_seq: int | None = None
    high_ext_seq: int | None = None
    late_frames: int = 0  # unique frames below the floor (older seq, first copy lost)
    drops: Counter[str] = field(default_factory=Counter)

    def add(self, ext_seq: int) -> None:
        """Records one unique frame's ext_seq against this span."""
        if self.floor_ext_seq is None:
            self.floor_ext_seq = ext_seq
        if ext_seq < self.floor_ext_seq:
            self.late_frames += 1
        elif self.high_ext_seq is None or ext_seq > self.high_ext_seq:
            self.high_ext_seq = ext_seq

    def generated(self) -> int | None:
        if self.floor_ext_seq is None or self.high_ext_seq is None:
            return None
        return self.high_ext_seq - self.floor_ext_seq + 1

    def lost(self) -> int | None:
        generated = self.generated()
        if generated is None:
            return None
        # Cannot go negative under the floor rule; the clamp only guards against a device re-sending
        # a seq outside the reliable path, which is a firmware bug to chase via `duplicates`/`drops`
        # rather than something to report as negative loss.
        return max(generated - (self.frames - self.late_frames), 0)


def _scaled(raw: list[int]) -> dict[str, list[float]]:
    """One raw int16 IMU sample -> physical units, the same scaling windowing._scale_sample uses."""
    return {
        "accel_g": [round(v / ACCEL_LSB_PER_G, 3) for v in raw[:3]],
        "gyro_dps": [round(v / GYRO_LSB_PER_DPS, 1) for v in raw[3:6]],
    }


def describe_frame(frame: bytes, decoded: DecodedPacket) -> dict[str, Any]:
    """Hex -> readable: splits one wire frame into its fields and pairs them with the decrypted
    payload, so a log reader never has to count bytes by hand. The ciphertext is shown as hex (it is
    what actually went over the air); `fields` is the plaintext recovered from it, and for a
    SENSOR_FRAME `readings` is that plaintext converted to g and deg/s."""
    h = decoded.header
    body_end = len(frame) - CRC_SIZE
    tag_start = body_end - GCM_TAG_SIZE
    try:
        type_name = PacketType(h.packet_type).name
    except ValueError:
        type_name = "UNKNOWN"
    fields = {
        k: (v.hex() if isinstance(v, bytes) else list(v) if isinstance(v, tuple) else v)
        for k, v in decoded.fields.items()
    }
    readings = {
        name: _scaled(fields[name])
        for name in ("dominant", "offhand", "torso")
        if isinstance(fields.get(name), list) and len(fields[name]) == 6
    }
    return {
        "type": f"{type_name}(0x{h.packet_type:02X})",
        "seq": h.seq,
        "ext_seq": decoded.ext_seq,
        "player": h.player_id,
        # The header LEN field: ciphertext + 16 B GCM tag (a 42 B SENSOR_FRAME plaintext -> 58).
        "len_ct_plus_tag": h.payload_len,
        "frame_bytes": len(frame),
        # Only frames that passed decode reach here, so the CRC and GCM tag are known-good.
        "crc": f"0x{int.from_bytes(frame[body_end:], 'little'):04X} ok",
        "auth": "gcm ok",
        "fields": fields,
        **({"readings": readings} if readings else {}),
        "hex_header": frame[:HEADER_SIZE].hex(" "),
        "hex_ciphertext": frame[HEADER_SIZE:tag_start].hex(" "),
        "hex_tag": frame[tag_start:body_end].hex(" "),
        "hex_crc": frame[body_end:].hex(" "),
    }


@dataclass
class ThroughputMeter:
    """Receive-side counters for one Session. Feed it from the read path; call `report()` on a
    timer. Pure bookkeeping -- it never blocks and never touches the transport."""

    player_id: int
    latest_path: Path | None = None
    clock: Callable[[], float] = time.monotonic  # injectable for exact-value tests
    _total: _Counters = field(default_factory=_Counters, init=False)
    _interval: _Counters = field(default_factory=_Counters, init=False)
    _started_at: float | None = field(default=None, init=False)
    _interval_at: float = field(default=0.0, init=False)
    _sample: tuple[bytes, DecodedPacket] | None = field(default=None, init=False)

    def start(self) -> None:
        """Called when streaming begins. Handshake traffic before this is deliberately excluded:
        it is not sustained data and would distort the first interval."""
        self._total = _Counters()
        self._interval = _Counters()
        self._started_at = self._interval_at = self.clock()
        self._sample = None

    @property
    def started(self) -> bool:
        return self._started_at is not None

    def on_transport_bytes(self, n: int) -> None:
        if self.started:
            self._total.transport_bytes += n
            self._interval.transport_bytes += n

    def on_drop(self, reason: str) -> None:
        if self.started:
            self._total.drops[reason] += 1
            self._interval.drops[reason] += 1

    def on_frame(self, frame: bytes, decoded: DecodedPacket) -> None:
        if not self.started:
            return
        is_sensor = decoded.header.packet_type == int(PacketType.SENSOR_FRAME)
        for c in (self._total, self._interval):
            if decoded.duplicate:
                c.duplicates += 1
                continue
            c.frames += 1
            c.add(decoded.ext_seq)
            if is_sensor:
                c.sensor_frames += 1
                c.sensor_frame_bytes += len(frame)
        if is_sensor and not decoded.duplicate:
            self._sample = (frame, decoded)

    def _summary(self, c: _Counters, elapsed_s: float) -> dict[str, Any]:
        generated = c.generated()
        lost = c.lost()
        return {
            "elapsed_s": round(elapsed_s, 2),
            "sensor_hz": round(c.sensor_frames / elapsed_s, 1) if elapsed_s > 0 else 0.0,
            "goodput_kbps": _kbps(c.sensor_frames * SENSOR_PAYLOAD_BYTES, elapsed_s),
            "frame_kbps": _kbps(c.sensor_frame_bytes, elapsed_s),
            "transport_kbps": _kbps(c.transport_bytes, elapsed_s),
            "sensor_frames": c.sensor_frames,
            "frames": c.frames,
            "loss_scope": "observed_all_packet_sequence_span_not_sensor_loss",
            "sequence_span": generated,
            "sequence_gaps": lost,
            "generated": generated,  # legacy name; not a sender-reported generated count
            "lost": lost,
            "lost_pct": (round(100 * lost / generated, 2) if generated and lost is not None else None),
            "late_frames": c.late_frames,
            "duplicates": c.duplicates,
            "drops": dict(c.drops),
        }

    def report(self, final: bool = False) -> dict[str, Any] | None:
        """Logs one interval (or, with final=True, the whole run) and refreshes the latest JSON.
        Returns the logged summary, or None if streaming never started."""
        if self._started_at is None:
            return None
        now = self.clock()
        interval = self._summary(self._interval, now - self._interval_at)
        total = self._summary(self._total, now - self._started_at)
        if final:
            logger.info("throughput_run_summary", player_id=self.player_id, **total)
        else:
            logger.info(
                "throughput",
                player_id=self.player_id,
                **interval,
                run_elapsed_s=total["elapsed_s"],
                run_goodput_kbps=total["goodput_kbps"],
                run_lost_pct=total["lost_pct"],
            )
            if self._sample is not None:
                # Console visibility is decided by logging_setup's console filter (D key), so the
                # JSONL file always carries one decoded frame per interval as evidence.
                logger.info("packet_sample", player_id=self.player_id, **describe_frame(*self._sample))
        self._write_latest(interval, total, final, now)
        # The next interval's span starts right after the highest seq seen so far, so a gap across
        # this boundary is charged to the next interval rather than to neither.
        high = self._total.high_ext_seq
        self._interval = _Counters(floor_ext_seq=None if high is None else high + 1)
        self._interval_at = now
        self._sample = None
        return total if final else interval

    def _write_latest(self, interval: dict, total: dict, final: bool, now: float) -> None:
        if self.latest_path is None:
            return
        doc = {
            "player_id": self.player_id,
            "written_unix_ms": wall_clock_unix_ms(),
            # Both relays run on the same laptop, and time.monotonic() is system-wide there, so the
            # two files' monotonic stamps are directly comparable for the overlap check.
            "monotonic_s": now,
            "session_started_monotonic_s": self._started_at,
            "final": final,
            "interval": interval,
            "run": total,
        }
        tmp = self.latest_path.with_suffix(".tmp")
        try:
            self.latest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(doc), encoding="utf-8")
            os.replace(tmp, self.latest_path)  # atomic: a reader never sees a half-written file
        except OSError as exc:
            # Windows refuses the replace while a reader holds the file open; the next interval
            # simply tries again. Never let a stats file take the relay down.
            logger.debug("throughput_latest_write_failed", error=str(exc))


def combine(docs: list[dict], now_monotonic: float, max_age_s: float) -> dict[str, Any]:
    """Aggregates the two relays' latest intervals. A stale or final document is reported, not
    silently summed -- adding a live rate to a dead relay's last rate would overstate the total."""
    fresh = [d for d in docs if not d["final"] and 0 <= now_monotonic - d["monotonic_s"] <= max_age_s]
    fresh_ids = {d["player_id"] for d in fresh}
    return {
        "rate_scope": "sum_of_recent_intervals_not_synchronized_trial",
        "players_fresh": sorted(fresh_ids),
        "players_stale_or_final": sorted(
            d["player_id"] for d in docs if d["player_id"] not in fresh_ids
        ),
        "total_goodput_kbps": round(sum(d["interval"]["goodput_kbps"] for d in fresh), 2),
        "total_frame_kbps": round(sum(d["interval"]["frame_kbps"] for d in fresh), 2),
        "total_sensor_hz": round(sum(d["interval"]["sensor_hz"] for d in fresh), 1),
        "complete": fresh_ids == {1, 2},
    }


def summarize_trial(observations: dict[int, list[dict]]) -> dict[str, Any]:
    """Cumulative-counter deltas; report per-player windows and their common coverage honestly."""
    players = {}
    starts, ends = [], []
    errors = []
    for pid in (1, 2):
        records = observations.get(pid, [])
        if len(records) < 2:
            errors.append(f"P{pid}: insufficient data")
            continue
        first, last = records[0], records[-1]
        sessions = {r.get("session_started_monotonic_s") for r in records}
        if None in sessions or len(sessions) != 1:
            errors.append(f"P{pid}: session changed or unidentified")
            continue
        start, end = first["monotonic_s"], last["monotonic_s"]
        dt = end - start
        gaps = [b["monotonic_s"] - a["monotonic_s"] for a, b in zip(records, records[1:], strict=False)]
        count = last["run"]["sensor_frames"] - first["run"]["sensor_frames"]
        if dt <= 0 or count < 0:
            errors.append(f"P{pid}: invalid counters")
            continue
        # A prolonged no-data interval is not an uninterrupted robustness pass.
        expected_period = max(first["interval"]["elapsed_s"], last["interval"]["elapsed_s"], 1)
        if gaps and max(gaps) > max(12, expected_period * 2.5):
            errors.append(f"P{pid}: missing summary interval")
        if any(r["interval"]["sensor_frames"] == 0 for r in records[1:]):
            errors.append(f"P{pid}: interval without sensor frames")
        players[str(pid)] = {
            "start_monotonic_s": start,
            "end_monotonic_s": end,
            "elapsed_s": round(dt, 3),
            "sensor_frames": count,
            "goodput_kbps": _kbps(count * SENSOR_PAYLOAD_BYTES, dt),
            "frame_kbps": _kbps(count * 68, dt),
        }
        starts.append(start)
        ends.append(end)
    overlap = max(0.0, min(ends) - max(starts)) if len(starts) == 2 else 0.0
    return {
        "players": players,
        "common_coverage_s": round(overlap, 3),
        "over_60_seconds_both": overlap > 60 and not errors,
        "errors": errors,
        "rate_scope": "per-player cumulative deltas over listed windows; not PHY bitrate",
        "loss_scope": "sequence gaps cannot establish exact sensor loss",
        "observations": observations,
    }


_ROW_COLOURS = {1: "\x1b[36m", 2: "\x1b[35m"}  # P1 cyan, P2 magenta -- same as the relay consoles
_RESET = "\x1b[0m"


def main_total() -> None:
    """`cg-throughput-total`: prints P1 + P2 + TOTAL once a second from the two latest JSONs. Opens
    no socket and reads only files, so it cannot disturb either relay."""
    import sys

    from cg_comms.config import find_repo_root
    from cg_comms.logging_setup import enable_ansi_on_windows

    parser = argparse.ArgumentParser(description="Combine both relays' throughput (read-only)")
    parser.add_argument(
        "--logs-dir",
        type=Path,
        default=None,
        help="Directory holding throughput-p1/p2-latest.json (default: comms/logs)",
    )
    parser.add_argument(
        "--max-age-s",
        type=float,
        default=12.0,
        help="Ignore a relay whose latest interval is older than this",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--duration-s",
        type=float,
        default=None,
        help="Record a bounded paired run (use 75 for >60 s common coverage)",
    )
    parser.add_argument("--output", type=Path, default=None, help="Save bounded-run JSON evidence")
    args = parser.parse_args()
    if args.duration_s is not None and (not 0 < args.duration_s <= 3600 or args.once):
        parser.error("--duration-s must be in (0, 3600] and cannot be used with --once")
    if args.output is not None and args.duration_s is None:
        parser.error("--output requires --duration-s")
    trial_started = time.monotonic()
    observations: dict[int, list[dict]] = {1: [], 2: []}
    logs_dir = args.logs_dir if args.logs_dir is not None else find_repo_root() / "comms" / "logs"
    colour = sys.stdout.isatty() and enable_ansi_on_windows()

    def paint(pid: int, text: str) -> str:
        return f"{_ROW_COLOURS[pid]}{text}{_RESET}" if colour else text

    while True:
        if colour:
            print("\x1b[2J\x1b[H", end="")
        docs = []
        for pid in (1, 2):
            path = logs_dir / f"throughput-p{pid}-latest.json"
            try:
                docs.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                print(paint(pid, f"P{pid}  (no data yet: {path})"))
        now = time.monotonic()
        for d in docs:
            i, r = d["interval"], d["run"]
            age = now - d["monotonic_s"]
            state = "FINAL" if d["final"] else (f"STALE {age:.0f}s" if age > args.max_age_s else "live")
            print(
                paint(
                    d["player_id"],
                    f"P{d['player_id']}  {i['sensor_hz']:6.1f} Hz  "
                    f"goodput={i['goodput_kbps']:6.2f} kbps  "
                    f"frame={i['frame_kbps']:6.2f} kbps  seq-gap={i['lost_pct']}%  "
                    f"run={r['elapsed_s']:.0f}s avg={r['goodput_kbps']:.2f} kbps "
                    f"seq-gap={r['lost_pct']}%  [{state}]",
                )
            )
        for d in docs:
            if (
                args.duration_s is not None
                and not d["final"]
                and 0 <= now - d["monotonic_s"] <= args.max_age_s
            ):
                records = observations[d["player_id"]]
                if not records or records[-1]["monotonic_s"] != d["monotonic_s"]:
                    records.append(d)
        t = combine(docs, now, args.max_age_s)
        print(
            f"TOTAL {t['total_sensor_hz']:6.1f} Hz  goodput={t['total_goodput_kbps']:6.2f} kbps  "
            f"frame={t['total_frame_kbps']:6.2f} kbps  "
            + (
                "both boards live"
                if t["complete"]
                else f"INCOMPLETE (live: {t['players_fresh']}, not live: {t['players_stale_or_final']})"
            )
        )
        print("-" * 78)
        if args.duration_s is not None and now - trial_started >= args.duration_s:
            summary = summarize_trial(observations)
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(json.dumps(summary, indent=2))
            return
        if args.once:
            return
        time.sleep(1.0)


if __name__ == "__main__":
    main_total()
