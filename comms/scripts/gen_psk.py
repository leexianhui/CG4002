#!/usr/bin/env python3
"""Generates a fresh 128-bit pre-shared key and prints it as hex (COMMS_IMPLEMENTATION_PLAN.md §4.2).

Never commit the key. Copy the printed hex into comms/.env as CG_RELAY_PSK_HEX (and burn the same
value into the matching FireBeetle's firmware config) — it never appears in any file this repo
tracks.
"""

from __future__ import annotations

import secrets

from cg_comms.protocol.constants import PSK_SIZE


def main() -> None:
    print(secrets.token_hex(PSK_SIZE))


if __name__ == "__main__":
    main()
