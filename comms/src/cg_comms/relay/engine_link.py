"""WSS client connecting the relay to the Game Engine at /relay/<player_id>
(COMMS_IMPLEMENTATION_PLAN.md §10.5).

Publishes ACTION and WEAPON_CHANGED; receives COMBAT_FEEDBACK to forward down to the FireBeetle.
Reconnects with the same backoff policy as the FireBeetle link (§6) — both are "a peer may vanish
and come back" problems.
"""

from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import structlog
import websockets

from cg_comms.messages.models import Action, CombatFeedback, WeaponChanged
from cg_comms.messages.validate import MessageDropped, parse_app_message

logger = structlog.get_logger(__name__)


@dataclass
class EngineLinkClient:
    url: str
    bearer_token: str
    on_combat_feedback: Callable[[CombatFeedback], Awaitable[None]]
    ca_cert_path: str | None = None
    _ws: "websockets.WebSocketClientProtocol | None" = field(default=None, init=False)
    _recv_task: "asyncio.Task[None] | None" = field(default=None, init=False)

    async def connect(self) -> None:
        ssl_context = None
        if self.url.startswith("wss://"):
            ssl_context = ssl.create_default_context(cafile=self.ca_cert_path) if self.ca_cert_path else ssl.create_default_context()
        self._ws = await websockets.connect(
            self.url,
            additional_headers={"Authorization": f"Bearer {self.bearer_token}"},
            ssl=ssl_context,
        )
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def disconnect(self) -> None:
        if self._recv_task is not None:
            self._recv_task.cancel()
            self._recv_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        async for raw in self._ws:
            parsed = parse_app_message(raw)
            if isinstance(parsed, MessageDropped):
                logger.warning("engine_link_bad_message", reason=parsed.reason.value, detail=parsed.detail)
                continue
            if isinstance(parsed, CombatFeedback):
                await self.on_combat_feedback(parsed)
            else:
                logger.warning("engine_link_unexpected_type", type=type(parsed).__name__)

    async def send_action(self, action: Action) -> None:
        await self._send(action.model_dump_json(by_alias=True))

    async def send_weapon_changed(self, weapon_changed: WeaponChanged) -> None:
        await self._send(weapon_changed.model_dump_json(by_alias=True))

    async def _send(self, payload: str) -> None:
        if self._ws is None:
            raise ConnectionError("engine link not connected")
        await self._ws.send(payload)
