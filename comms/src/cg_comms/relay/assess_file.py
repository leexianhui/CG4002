"""Laptop -> FireBeetle file transfer with end-to-end integrity check (comms assessment: "file transfer
to verify transmission").

Stop-and-wait over ASSESS_FILE_CHUNK / ASSESS_FILE_ACK (payloads.py; device contract at the top of
firmware_demo/lib/cg_protocol/cg_assess.h). For each chunk:

    1. send it through the Session's ReliabilitySender (identical-ciphertext retransmit until the
       device's transport ACK arrives, else LinkDegraded);
    2. wait for the device's FILE_ACK{chunk_index, running_crc, received_bytes};
    3. check received_bytes == bytes sent so far AND running_crc == crc16(file[:received_bytes]),
       both computed HERE from the source bytes, before moving on.

Step 3 is the integrity evidence: the CRC is computed by the FireBeetle over the bytes it actually
decrypted, and compared by the laptop against the source. A transport ACK alone is never reported as
success. What this does NOT do (say so on the video): the device keeps only a running CRC-16, not the
file, so there is no readback and no device-side SHA-256. The SHA-256 printed here identifies the
SOURCE file; the device-side proof is CRC-16/CCITT-FALSE plus exact length, checked after every chunk.

Failure handling (every path aborts and reports; none claims success):
  - chunk never ACKed (LinkDegraded) or transport gone      -> abort, "link"
  - FILE_ACK with the wrong received_bytes / running_crc   -> abort, "device state mismatch"
    (this is also how an out-of-sequence chunk shows up: the device answers with its state unchanged)
  - ACKed but FILE_ACK never arrives (response lost, ACK got through) -> the identical frame is sent
    again; the device recognises the exact retransmit, re-ACKs and re-sends its last response without
    re-applying the chunk. Bounded by _RESPONSE_RECOVERY_ATTEMPTS, then abort.
There is no resume: the next attempt starts again at chunk 0, which the device treats as a new transfer.

A FILE_ACK for any other chunk_index (a late re-sent response for an earlier chunk) is ignored, not
treated as an error. A stale ack can at worst cause a spurious mismatch ABORT -- it can never make a
transfer report success for bytes the device does not hold, because success needs every chunk's
length and CRC to agree with the source.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from cg_comms.protocol.crc import crc16, crc16_update
from cg_comms.protocol.payloads import ASSESS_FILE_CHUNK_DATA_LEN
from cg_comms.relay.reliability import LinkDegraded
from cg_comms.transport.base import TransportClosed

logger = structlog.get_logger(__name__)

# The firmware holds no file bytes, so this cap is not a RAM limit on the device; it keeps a
# stop-and-wait demo transfer to a few seconds and matches the plan's proposed 4 KiB bound.
MAX_FILE_BYTES = 4096
# total_chunks is a u16 on the wire; MAX_FILE_BYTES keeps us far below it, asserted in chunk_file.
_MAX_CHUNKS = 0xFFFF
# Wait for the FILE_ACK after the transport ACK. The device sends FILE_ACK *before* its ACK over the
# same ordered link, so in the normal case it is already queued when send_reliable returns.
_FILE_ACK_TIMEOUT_S = 1.0
_RESPONSE_RECOVERY_ATTEMPTS = 2


class FileTooLarge(ValueError):
    pass


@dataclass(frozen=True)
class FileTransferResult:
    ok: bool
    reason: str
    total_bytes: int
    total_chunks: int
    chunks_confirmed: int  # chunks whose FILE_ACK matched the source
    confirmed_bytes: int
    elapsed_s: float
    sha256: str  # of the SOURCE (see module docstring)
    source_crc: int
    device_crc: int | None  # the device's last reported running CRC

    @property
    def bytes_per_s(self) -> float:
        return round(self.confirmed_bytes / self.elapsed_s, 1) if self.elapsed_s > 0 else 0.0


def chunk_file(data: bytes) -> list[dict]:
    """Splits `data` into ASSESS_FILE_CHUNK field dicts: 32-byte chunks, the last one short and
    zero-padded (the device rejects non-zero padding as malformed)."""
    if not data:
        raise ValueError("file is empty; there is nothing to transfer")
    if len(data) > MAX_FILE_BYTES:
        raise FileTooLarge(f"file is {len(data)} B; the assessment transfer cap is {MAX_FILE_BYTES} B")
    total = -(-len(data) // ASSESS_FILE_CHUNK_DATA_LEN)
    assert total <= _MAX_CHUNKS
    chunks = []
    for index in range(total):
        piece = data[index * ASSESS_FILE_CHUNK_DATA_LEN:(index + 1) * ASSESS_FILE_CHUNK_DATA_LEN]
        chunks.append({
            "chunk_index": index,
            "total_chunks": total,
            "chunk_len": len(piece),
            "reserved": 0,
            "data": piece.ljust(ASSESS_FILE_CHUNK_DATA_LEN, b"\x00"),
        })
    return chunks


async def run_file_transfer(
    data: bytes,
    *,
    player_id: int,
    send_chunk: Callable[[dict], Awaitable[Callable[[], Awaitable[None]]]],
    acks: asyncio.Queue[dict],
    ack_timeout_s: float = _FILE_ACK_TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
) -> FileTransferResult:
    """Runs one transfer. `send_chunk(fields)` encodes the chunk under a fresh seq, sends it reliably
    and returns a `resend()` coroutine function that re-sends that SAME frame reliably (used only for
    response recovery). Both raise LinkDegraded / TransportClosed on link failure. `acks` receives
    the decoded FILE_ACK field dicts; the caller must hold the Session's assessment send lock.

    Raises ValueError/FileTooLarge before sending anything if `data` cannot be transferred.
    """
    chunks = chunk_file(data)
    sha = hashlib.sha256(data).hexdigest()
    source_crc = crc16(data)
    total = len(chunks)
    # Anything already queued belongs to an earlier transfer (or an earlier re-sent response).
    stale = 0
    while not acks.empty():
        acks.get_nowait()
        stale += 1
    logger.info("assess_file_start", player_id=player_id, total_bytes=len(data), total_chunks=total,
                sha256=sha, source_crc=f"0x{source_crc:04X}", stale_acks_discarded=stale)

    started = clock()
    running_crc = 0xFFFF
    confirmed_bytes = 0
    device_crc: int | None = None
    next_progress = 0.25

    def result(ok: bool, reason: str, confirmed_chunks: int) -> FileTransferResult:
        return FileTransferResult(
            ok=ok, reason=reason, total_bytes=len(data), total_chunks=total,
            chunks_confirmed=confirmed_chunks, confirmed_bytes=confirmed_bytes,
            elapsed_s=round(clock() - started, 3), sha256=sha, source_crc=source_crc,
            device_crc=device_crc,
        )

    for fields in chunks:
        index = fields["chunk_index"]
        expect_bytes = confirmed_bytes + fields["chunk_len"]
        expect_crc = crc16_update(running_crc, fields["data"][:fields["chunk_len"]])
        try:
            resend = await send_chunk(fields)
            ack = await _await_file_ack(acks, index, ack_timeout_s)
            recoveries = 0
            while ack is None and recoveries < _RESPONSE_RECOVERY_ATTEMPTS:
                recoveries += 1
                logger.warning("assess_file_ack_missing", player_id=player_id, chunk_index=index,
                               recovery_attempt=recoveries,
                               action="re-sending identical chunk; device re-sends its last response")
                await resend()
                ack = await _await_file_ack(acks, index, ack_timeout_s)
        except (LinkDegraded, TransportClosed, ConnectionError) as exc:
            r = result(False, f"link: chunk {index} not delivered ({type(exc).__name__})", index)
            _log_result(player_id, r)
            return r
        if ack is None:
            r = result(False, f"no FILE_ACK for chunk {index} after "
                              f"{_RESPONSE_RECOVERY_ATTEMPTS} recovery attempts", index)
            _log_result(player_id, r)
            return r

        device_crc = ack["running_crc"]
        if ack["received_bytes"] != expect_bytes or ack["running_crc"] != expect_crc:
            # An out-of-sequence chunk lands here too (device state unchanged => byte count lags).
            r = result(False, (f"device state mismatch at chunk {index}: device reports "
                               f"received_bytes={ack['received_bytes']} crc=0x{ack['running_crc']:04X}, "
                               f"source expects received_bytes={expect_bytes} crc=0x{expect_crc:04X}"),
                       index)
            _log_result(player_id, r)
            return r
        running_crc = expect_crc
        confirmed_bytes = expect_bytes
        if (index + 1) / total >= next_progress and index + 1 < total:
            logger.info("assess_file_progress", player_id=player_id, chunks=f"{index + 1}/{total}",
                        confirmed_bytes=confirmed_bytes, device_crc=f"0x{device_crc:04X}")
            while (index + 1) / total >= next_progress:
                next_progress += 0.25

    # Every chunk's length and CRC agreed; the last one covers the whole file.
    assert confirmed_bytes == len(data) and running_crc == source_crc
    r = result(True, "device CRC-16 and length match the source after every chunk", total)
    _log_result(player_id, r)
    return r


async def _await_file_ack(acks: asyncio.Queue[dict], index: int, timeout_s: float) -> dict | None:
    """Returns the first FILE_ACK for `index` within `timeout_s`, skipping acks for other chunks."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        try:
            ack = await asyncio.wait_for(acks.get(), remaining)
        except asyncio.TimeoutError:
            return None
        if ack["chunk_index"] == index:
            return ack
        logger.debug("assess_file_ack_ignored", chunk_index=ack["chunk_index"], waiting_for=index,
                     reason="re-sent response for another chunk")


def _log_result(player_id: int, r: FileTransferResult) -> None:
    fields = dict(
        player_id=player_id, reason=r.reason, total_bytes=r.total_bytes,
        chunks=f"{r.chunks_confirmed}/{r.total_chunks}", confirmed_bytes=r.confirmed_bytes,
        elapsed_s=r.elapsed_s, bytes_per_s=r.bytes_per_s, sha256=r.sha256,
        source_crc=f"0x{r.source_crc:04X}",
        device_crc=None if r.device_crc is None else f"0x{r.device_crc:04X}",
    )
    if r.ok:
        logger.info("assess_file_verified", **fields)
    else:
        logger.error("assess_file_failed", **fields,
                     hint="nothing is resumed: the next transfer restarts at chunk 0")
