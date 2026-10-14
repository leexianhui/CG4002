"""Capture/replay one selected hub's diagnostic UDP packets, without sending any packets.

Not a live gameplay adapter. Does not connect to the engine, classifier or other player boards.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import selectors
import socket
import time
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from cg_comms.relay.hardware_probe import decode_hub_datagram, ports_for_player

MAX_DATAGRAM = 65535
MAX_CAPTURE_BYTES = 8 * 1024 * 1024


def packet_record(data: bytes, stream: str, player: int) -> dict[str, object]:
    try:
        return dict(valid=True, decoded=decode_hub_datagram(data, stream, player))
    except ValueError as error:
        return dict(valid=False, error=str(error))


def capture(
    path: Path, player: int, gateway_ip: str, *, bind: str = "0.0.0.0", duration: float = 60,
    ports: dict[str, int] | None = None,
) -> Counter[str]:
    """Exclusive output; bounded duration/size; only the chosen source IP is recorded.

    `ports` is a test seam for ephemeral loopback ports, not a new hardware protocol.
    """
    gateway_ip = str(ipaddress.IPv4Address(gateway_ip))
    if not 0 < duration <= 600:
        raise ValueError("duration must be above zero and at most 600 seconds")
    defaults = ports_for_player(player)
    ports = defaults if ports is None else ports
    if set(ports) != set(defaults):
        raise ValueError("expected pose and raw ports")
    stats: Counter[str] = Counter()
    with ExitStack() as stack:
        selector = stack.enter_context(selectors.DefaultSelector())
        for stream, port in ports.items():
            sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
            # Don't steal an existing listener's bind on Windows.
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((bind, port))
            sock.setblocking(False)
            selector.register(sock, selectors.EVENT_READ, stream)
        path.parent.mkdir(parents=True, exist_ok=True)
        output = stack.enter_context(path.open("x", encoding="utf-8", newline="\n"))
        started_utc = datetime.now(timezone.utc).isoformat()
        output.write(json.dumps(dict(format="cg4002-hub-probe", version=1, player_id=player,
            gateway_ip=gateway_ip, ports=ports, started_utc=started_utc)) + "\n")
        output.flush()
        started = time.monotonic()
        try:
            while time.monotonic() - started < duration and output.tell() < MAX_CAPTURE_BYTES:
                for key, _ in selector.select(timeout=min(0.1, duration)):
                    data, source = key.fileobj.recvfrom(MAX_DATAGRAM)
                    if source[0] != gateway_ip:
                        stats["other_source_ignored"] += 1
                        continue
                    record = packet_record(data, key.data, player)
                    record.update(stream=key.data, source_ip=source[0], source_port=source[1],
                                  elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                                  raw_hex=data.hex())
                    # Binary hex remains available even when decoding rejects an unknown layout.
                    line = json.dumps(record) + "\n"
                    if output.tell() + len(line.encode("utf-8")) > MAX_CAPTURE_BYTES:
                        stats["size_limit"] += 1
                        return stats
                    output.write(line)
                    output.flush()
                    stats[str(record["decoded"]["kind"]) if record["valid"] else "rejected"] += 1
        except KeyboardInterrupt:
            pass
    return stats


def replay(path: Path) -> Counter[str]:
    """Re-decode captured bytes; never trust the capture's previously decoded fields."""
    if path.stat().st_size > MAX_CAPTURE_BYTES:
        raise ValueError("capture exceeds size limit")
    stats: Counter[str] = Counter()
    with path.open(encoding="utf-8") as capture_file:
        header = json.loads(capture_file.readline())
        if (not isinstance(header, dict) or header.get("format") != "cg4002-hub-probe"
                or header.get("version") != 1):
            raise ValueError("unsupported capture format")
        ports_for_player(header["player_id"])
        for line in capture_file:
            record = json.loads(line)
            if record["source_ip"] != header["gateway_ip"]:
                raise ValueError("capture source differs from selected gateway")
            decoded = packet_record(bytes.fromhex(record["raw_hex"]), record["stream"],
                                    header["player_id"])
            stats[str(decoded["decoded"]["kind"]) if decoded["valid"] else "rejected"] += 1
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--player", type=int, choices=(1, 2))
    parser.add_argument("--gateway-ip", help="IPv4 address of the single selected hub")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--out", type=Path,
                        help="New JSONL capture path; existing files are never overwritten")
    parser.add_argument("--replay", type=Path,
                        help="Re-decode an existing capture without opening sockets")
    args = parser.parse_args()
    try:
        if args.replay:
            stats = replay(args.replay)
        else:
            if args.player is None or not args.gateway_ip or args.out is None:
                parser.error("capture requires --player, --gateway-ip and --out")
            print("Receive-only diagnostic probe. No gameplay or feedback traffic is sent.", flush=True)
            stats = capture(args.out, args.player, args.gateway_ip,
                            bind=args.bind, duration=args.duration)
            print(f"Capture saved: {args.out}")
        print(json.dumps(dict(stats), sort_keys=True))
        if not stats.get("hub_status") and not stats.get("raw_batch"):
            print("No valid hub packets observed; the live gateway interface is not verified.")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Probe failed: {error}\n")


if __name__ == "__main__":
    main()
