"""Independent recovering engine link; bounded live traffic, no replay across connections."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog
import websockets

from cg_comms.messages.models import Action, CombatFeedback, WeaponChanged
from cg_comms.messages.validate import MessageDropped, parse_app_message
from cg_comms.relay.reconnect import ReconnectWorker

logger = structlog.get_logger(__name__)


@dataclass
class EngineLinkClient:
    url: str
    bearer_token: str
    on_combat_feedback: Callable[[CombatFeedback], Awaitable[None]]
    ca_cert_path: str | None = None
    _worker: ReconnectWorker = field(init=False)
    _outbox: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(32), init=False)

    def __post_init__(self) -> None:
        self._worker = ReconnectWorker("engine", self._serve)

    def start(self) -> None:
        self._worker.start()

    async def connect(self) -> None:
        self.start()
        try:
            await asyncio.wait_for(self._worker.connected.wait(), 10)
        except BaseException:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        await self._worker.stop()

    async def _serve(self) -> None:
        ssl_context = None
        if self.url.startswith("wss://"):
            ssl_context = ssl.create_default_context(cafile=self.ca_cert_path)
        async with websockets.connect(
            self.url, additional_headers={"Authorization": f"Bearer {self.bearer_token}"},
            ssl=ssl_context, open_timeout=5, close_timeout=2, ping_interval=5, ping_timeout=5,
        ) as ws:
            self._worker.connected.set()
            logger.info("engine_connected", url=self.url)

            async def receive() -> None:
                async for raw in ws:
                    parsed = parse_app_message(raw)
                    if isinstance(parsed, MessageDropped):
                        logger.warning("engine_link_bad_message", reason=parsed.reason.value)
                    elif isinstance(parsed, CombatFeedback):
                        await self.on_combat_feedback(parsed)  # only queues to the BLE session

            async def send() -> None:
                while True:
                    payload = await self._outbox.get()
                    await asyncio.wait_for(ws.send(payload), 3)

            tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                raise ConnectionError("engine EOF")
            finally:
                self._worker.connected.clear()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                while not self._outbox.empty():
                    self._outbox.get_nowait()

    async def send_action(self, action: Action) -> None:
        await self._send(action.model_dump_json(by_alias=True))

    async def send_weapon_changed(self, weapon_changed: WeaponChanged) -> None:
        await self._send(weapon_changed.model_dump_json(by_alias=True))

    async def _send(self, payload: str) -> None:
        if not self._worker.connected.is_set():
            logger.warning("engine_message_dropped", reason="offline", url=self.url)
            return
        try:
            self._outbox.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("engine_message_dropped", reason="capacity", url=self.url)
