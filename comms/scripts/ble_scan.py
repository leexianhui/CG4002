#!/usr/bin/env python
"""Finds the demo firmware's BLE address, for pasting into a relay config's transport.ble_address.

Windows' bleak backend addresses peripherals by MAC, but the MAC is not printed on the board and is
not the same as the USB serial port -- it has to be discovered by scanning. The firmware advertises
CG_DEMO_BLE_DEVICE_NAME (firmware_demo/include/cg_config.h), so match on that rather than making the
user eyeball a list of neighbourhood devices.

Usage:
  python scripts/ble_scan.py                    # show CG4002-* boards
  python scripts/ble_scan.py --all              # show everything advertising nearby
  python scripts/ble_scan.py --name CG4002-Demo-P1

Exits nonzero when nothing matched, so this can gate a bring-up script.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from bleak import BleakScanner

_DEFAULT_PREFIX = "CG4002"


async def scan(timeout: float, prefix: str | None) -> int:
    print(f"scanning for {timeout:.0f}s...", file=sys.stderr)
    devices = await BleakScanner.discover(timeout=timeout)

    matches = [
        d for d in devices if prefix is None or (d.name or "").upper().startswith(prefix.upper())
    ]
    if not matches:
        print(f"no device advertising a name starting with {prefix!r} found.", file=sys.stderr)
        # The overwhelmingly common cause, and invisible from here: an ESP32 that has already been
        # connected to stops advertising, so a board mid-session looks identical to a dead board.
        print(
            "if the board is powered and running, check that no other client (a previous cg-relay, "
            "or Windows' Bluetooth settings page) is still connected to it -- a connected ESP32 "
            "stops advertising and cannot be found by a scan. Re-plug it and retry.",
            file=sys.stderr,
        )
        print(f"({len(devices)} other device(s) were visible, so the adapter itself is fine.)", file=sys.stderr)
        return 1

    for d in matches:
        print(f"{d.address}  {d.name or '<no name>'}")
    if len(matches) == 1:
        print(f'\npaste into configs/relay.p1.yaml:\n  ble_address: "{matches[0].address}"', file=sys.stderr)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan for the CG4002 demo firmware's BLE address")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument(
        "--name",
        default=_DEFAULT_PREFIX,
        help=f"name prefix to match (default {_DEFAULT_PREFIX!r})",
    )
    parser.add_argument("--all", action="store_true", help="list every visible device, unfiltered")
    args = parser.parse_args()

    sys.exit(asyncio.run(scan(args.timeout, None if args.all else args.name)))


if __name__ == "__main__":
    main()
