"""Operator-only match reset using the existing engine bearer-token model."""

import argparse
import asyncio
import ssl
from pathlib import Path

import websockets

from cg_comms.config import EngineSettings


async def reset(settings: EngineSettings, host: str) -> None:
    scheme = "wss" if settings.use_tls else "ws"
    context = ssl.create_default_context(cafile=settings.cert_path) if settings.use_tls else None
    async with websockets.connect(
        f"{scheme}://{host}:{settings.port}/control", ssl=context,
        additional_headers={"Authorization": f"Bearer {settings.bearer_token}"},
        open_timeout=5, close_timeout=2,
    ) as ws:
        await ws.send('{"type":"RESET_MATCH","version":1}')
        # The engine's single writer sends the confirmation after publishing both phone snapshots.
        response = await asyncio.wait_for(ws.recv(), 5)
        print(response)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", default="localhost", help="Engine hostname matching its certificate")
    args = parser.parse_args()
    asyncio.run(reset(EngineSettings.from_yaml(args.config), args.host))


if __name__ == "__main__":
    main()
