"""Operator command: lock a player's weapon on their hub now, from any stage of the game.

    python -m cg_comms.tools.set_weapon --config configs/engine.integration.yaml --player 1 --weapon SPEAR

The engine sends that player's hub the weapon (COMBAT_FEEDBACK WEAPON_SET_*, through the relay); the
hub locks it at once, during weapon selection or mid-match, bypassing the phone gate, and reports it
like any lock, so the engine and both phones follow. If the hub is offline it gets it on reconnect.
Uses the engine's bearer token, like reset_match.
"""

import argparse
import asyncio
import json
import ssl
from pathlib import Path

import websockets

from cg_comms.config import EngineSettings


async def set_weapon(settings: EngineSettings, host: str, player: int, weapon: str) -> None:
    scheme = "wss" if settings.use_tls else "ws"
    context = ssl.create_default_context(cafile=settings.cert_path) if settings.use_tls else None
    async with websockets.connect(
        f"{scheme}://{host}:{settings.port}/control", ssl=context,
        additional_headers={"Authorization": f"Bearer {settings.bearer_token}"},
        open_timeout=5, close_timeout=2,
    ) as ws:
        await ws.send(json.dumps({"type": "SET_WEAPON", "version": 1, "playerId": player,
                                  "weapon": weapon}))
        await asyncio.sleep(0.3)  # let the frame go out before closing
    print(f"SET_WEAPON P{player} {weapon} sent. Engine logs weapon_set; the relay then shows "
          f"weapon_changed weapon={weapon} once the hub has locked it.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", default="localhost", help="Engine hostname matching its certificate")
    parser.add_argument("--player", type=int, choices=(1, 2), required=True)
    parser.add_argument("--weapon", type=str.upper, choices=("SWORD", "SPEAR", "MACE"), required=True)
    args = parser.parse_args()
    try:
        settings = EngineSettings.from_yaml(args.config)
    except FileNotFoundError:
        parser.error(f"config file not found: {args.config}; run from comms/ "
                     "or pass an existing --config path")
    if not settings.bearer_token:
        parser.error("no bearer token: run from comms/ (reads .env.engine) "
                     "or set CG_ENGINE_BEARER_TOKEN")
    asyncio.run(set_weapon(settings, args.host, args.player, args.weapon))


if __name__ == "__main__":
    main()
