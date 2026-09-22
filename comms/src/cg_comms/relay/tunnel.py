"""Supervises the SSH tunnel subprocess to the Ultra96 board (COMMS_IMPLEMENTATION_PLAN.md §8).

Development/remote-access only: at match time the Ultra96 is a whitelisted device reachable directly
over the venue WiFi, so no tunnel is needed on the match path (`Ultra96Config.tunnel_enabled` defaults
to False). This supervisor exists for coding, deploying, and debugging the Ultra96 while it sits on the
NUS SoC Research Subnet off-site — assume the tunnel drops at least once during a long dev session; a
supervised subprocess turns that from a lost session into a bounded stall. Skippable entirely via relay
`--no-tunnel` when a tunnel is already open on the expected local port.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class TunnelConfig:
    ssh_jump_user: str
    ssh_jump_host: str
    ssh_target_user: str
    ssh_target_host: str
    local_port: int
    remote_port: int
    connect_timeout_s: float = 30.0
    restart_backoff_s: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 10.0)

    def build_command(self) -> list[str]:
        return [
            "ssh",
            "-J",
            f"{self.ssh_jump_user}@{self.ssh_jump_host}",
            "-N",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "ExitOnForwardFailure=yes",
            "-L",
            f"127.0.0.1:{self.local_port}:127.0.0.1:{self.remote_port}",
            f"{self.ssh_target_user}@{self.ssh_target_host}",
        ]


def _is_connectable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


@dataclass
class TunnelSupervisor:
    """Starts the tunnel, waits for it to become connectable, and restarts it with backoff if the
    subprocess exits unexpectedly."""

    config: TunnelConfig
    _process: "asyncio.subprocess.Process | None" = field(default=None, init=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _supervise_task: "asyncio.Task[None] | None" = field(default=None, init=False)

    async def start(self, wait_ready: bool = True) -> None:
        if self._supervise_task is None:
            self._supervise_task = asyncio.create_task(self._run())
        if wait_ready:
            await self._wait_connectable()

    async def stop(self) -> None:
        self._stop_event.set()
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
        if self._supervise_task is not None:
            self._supervise_task.cancel()
            await asyncio.gather(self._supervise_task, return_exceptions=True)
            self._supervise_task = None
        if self._process is not None and self._process.returncode is None:
            try:
                await asyncio.wait_for(self._process.wait(), 3)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

    async def _wait_connectable(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.connect_timeout_s
        while loop.time() < deadline:
            if _is_connectable(self.config.local_port):
                return
            await asyncio.sleep(0.2)
        raise TimeoutError(f"ssh tunnel did not become connectable within {self.config.connect_timeout_s}s")

    async def _run(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            logger.info("tunnel_starting", attempt=attempt)
            self._process = await asyncio.create_subprocess_exec(*self.config.build_command())
            returncode = await self._process.wait()
            if self._stop_event.is_set():
                return
            logger.warning("tunnel_exited", returncode=returncode, attempt=attempt)
            backoff = self.config.restart_backoff_s[min(attempt, len(self.config.restart_backoff_s) - 1)]
            attempt += 1
            await asyncio.sleep(backoff)
