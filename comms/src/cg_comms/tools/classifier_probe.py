"""Measure the existing classifier path without forwarding any actions to the engine.

Use while the relay/SSH/BLE setup is running but no buttons are being pressed. This opens its own
classifier connection, uses high request IDs, and changes no live relay or engine settings.
The larger diagnostic timeout is for measuring the latency tail, not a gameplay recommendation.
"""

import argparse
import asyncio
import json
import logging
import math
import secrets
from pathlib import Path

from cg_comms.clock import monotonic_ms
from cg_comms.config import RelaySettings
from cg_comms.logging_setup import configure_logging
from cg_comms.messages.models import SensorWindow
from cg_comms.relay.ultra96 import Ultra96Client, Ultra96Unavailable


def summarize(latencies: list[float]) -> dict[str, float]:
    if not latencies:
        return {}
    ordered = sorted(latencies)
    return {label: round(ordered[math.ceil(len(ordered) * quantile) - 1], 3)
            for label, quantile in (("p50", .5), ("p95", .95), ("p99", .99), ("max", 1))}


async def probe(settings: RelaySettings, samples: int, timeout_ms: int,
                interval_ms: int) -> dict[str, object]:
    client = Ultra96Client(settings.ultra96.host, settings.ultra96.port, timeout_ms, 1)
    first_id = 0x80000000 + secrets.randbelow(0x7fffffff - samples)
    latencies = []
    unavailable = 0
    loop = asyncio.get_running_loop()
    try:
        await client.connect()
        for index in range(samples):
            now = monotonic_ms()
            window = SensorWindow(
                timestamp=now, player_id=settings.player_id, request_id=first_id + index,
                weapon="SWORD", trigger_id=1, window_start_ts=now - 780, window_end_ts=now,
                sample_rate_hz=50, stale_pct=0,
                dominant=[[.02, -.04, 1.01, 12.3, -4.5, .6]] * 40,
                offhand=[[.03, -.02, .99, -2.1, 3.4, .5]] * 40,
            )
            started = loop.time()
            try:
                result = await client.request(window)
                if result is not None:
                    latencies.append((loop.time() - started) * 1000)
            except Ultra96Unavailable:
                unavailable += 1
            if index + 1 < samples:
                await asyncio.sleep(interval_ms / 1000)
    finally:
        await client.disconnect()
    return dict(player_id=settings.player_id, attempted=samples, completed=len(latencies),
                timeouts=client.timeouts, unavailable=unavailable,
                diagnostic_timeout_ms=timeout_ms, first_request_id=first_id,
                request_call_ms=summarize(latencies), forwarded_to_engine=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--timeout-ms", type=int, default=500)
    parser.add_argument("--interval-ms", type=int, default=1000)
    args = parser.parse_args()
    if not 1 <= args.samples <= 10000 or args.timeout_ms <= 0 or args.interval_ms < 0:
        parser.error("samples must be 1..10000, timeout positive, and interval nonnegative")
    settings = RelaySettings.from_yaml(args.config)
    configure_logging(f"classifier-probe-p{settings.player_id}", Path("logs"),
                      console_level=logging.WARNING)
    print(json.dumps(asyncio.run(probe(settings, args.samples, args.timeout_ms, args.interval_ms)),
                     indent=2))


if __name__ == "__main__":
    main()
