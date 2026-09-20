"""Relay entrypoint (COMMS_IMPLEMENTATION_PLAN.md §3, §5.1): wires the FireBeetle transport,
Ultra96 client, engine link, and one Session together, then supervises reconnects for the lifetime of
the process. Both the Ultra96 client and the engine link normally talk directly to the shared Ultra96
over the venue WiFi at match time; the SSH tunnel (optional, off by default) is a dev/remote-access
convenience for when the Ultra96 is off-site on the NUS SoC Research Subnet instead.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

import structlog

from cg_comms.config import RelaySettings, find_repo_root
from cg_comms.logging_setup import configure_logging
from cg_comms.messages.models import Action, ActionResult, CombatFeedback, WeaponChanged
from cg_comms.relay.engine_link import EngineLinkClient
from cg_comms.relay.session import Session, SessionConfig
from cg_comms.relay.tunnel import TunnelConfig, TunnelSupervisor
from cg_comms.relay.ultra96 import Ultra96Client
from cg_comms.relay.windowing import PlayerWindower
from cg_comms.transport.base import ITransport
from cg_comms.transport.ble import BleTransport
from cg_comms.transport.loopback import LoopbackTransport
from cg_comms.transport.tcp import TcpTransport

logger = structlog.get_logger(__name__)


def _build_transport(settings: RelaySettings) -> ITransport:
    t = settings.transport
    if t.kind == "ble":
        if not t.ble_address or not t.ble_notify_char_uuid or not t.ble_write_char_uuid:
            raise ValueError("ble transport requires ble_address, ble_notify_char_uuid, ble_write_char_uuid")
        return BleTransport(t.ble_address, t.ble_notify_char_uuid, t.ble_write_char_uuid)
    if t.kind == "tcp":
        return TcpTransport(t.tcp_host, t.tcp_port)
    if t.kind == "loopback":
        transport, _peer = LoopbackTransport.pair()
        return transport
    raise ValueError(f"unknown transport kind: {t.kind!r}")


async def run_relay(settings: RelaySettings, use_tunnel: bool) -> None:
    tunnel: TunnelSupervisor | None = None
    if use_tunnel and settings.ultra96.tunnel_enabled and not settings.ultra96.use_mock:
        u = settings.ultra96
        if not u.tunnel_ssh_jump_user or not u.tunnel_ssh_target_host:
            raise ValueError("ssh tunnel requires tunnel_ssh_jump_user and tunnel_ssh_target_host")
        tunnel = TunnelSupervisor(
            TunnelConfig(
                ssh_jump_user=u.tunnel_ssh_jump_user,
                ssh_jump_host=u.tunnel_ssh_jump_host,
                ssh_target_user=u.tunnel_ssh_target_user,
                ssh_target_host=u.tunnel_ssh_target_host,
                local_port=u.tunnel_local_port,
                remote_port=u.tunnel_remote_port,
                connect_timeout_s=u.tunnel_connect_timeout_s,
            )
        )
        await tunnel.start()

    ultra96 = Ultra96Client(
        host=settings.ultra96.host,
        port=settings.ultra96.port,
        request_timeout_ms=settings.ultra96.request_timeout_ms,
        max_in_flight=settings.ultra96.max_in_flight,
    )
    await ultra96.connect()

    windower = PlayerWindower(
        player_id=settings.player_id,
        window_samples=settings.windowing.window_samples,
        stride_frames=settings.windowing.stride_frames,
        max_stale_pct=settings.windowing.max_stale_pct,
        max_span_deviation_pct=settings.windowing.max_span_deviation_pct,
        frame_rate_hz=settings.windowing.frame_rate_hz,
        trigger_mode=settings.windowing.trigger_mode,
    )

    scheme = "wss" if settings.engine_link.use_tls else "ws"
    engine_url = f"{scheme}://{settings.engine_link.host}:{settings.engine_link.port}/relay/{settings.player_id}"

    session: Session | None = None

    async def on_combat_feedback(feedback: CombatFeedback) -> None:
        if session is not None:
            await session.send_combat_feedback(feedback)

    engine_link = EngineLinkClient(
        url=engine_url,
        bearer_token=settings.engine_link.bearer_token,
        on_combat_feedback=on_combat_feedback,
        ca_cert_path=settings.engine_link.ca_cert_path,
    )
    await engine_link.connect()

    async def on_weapon_changed(msg: WeaponChanged) -> None:
        await engine_link.send_weapon_changed(msg)

    async def on_action_result(result: ActionResult) -> None:
        weapon = windower.current_weapon
        if weapon is None:
            return
        await engine_link.send_action(
            Action(
                timestamp=result.timestamp,
                player_id=result.player_id,
                weapon=weapon,  # type: ignore[arg-type]
                action=result.action,
                action_confidence=result.action_confidence,
                guard_state=result.guard_state,
                guard_confidence=result.guard_confidence,
            )
        )

    session_config = SessionConfig(
        player_id=settings.player_id,
        psk=settings.psk,
        handshake_samples=settings.timesync.handshake_samples,
        handshake_spacing_ms=settings.timesync.handshake_spacing_ms,
        ewma_alpha=settings.timesync.ewma_alpha,
        outlier_rtt_multiplier=settings.timesync.outlier_rtt_multiplier,
        divergence_warn_ms=settings.timesync.divergence_warn_ms,
        heartbeat_interval_ms=settings.heartbeat.interval_ms,
        heartbeat_liveness_factor=settings.heartbeat.liveness_timeout_factor,
        reliability_base_timeout_ms=settings.reliability.retry_timeout_ms,
        reliability_backoff_factor=settings.reliability.retry_backoff_factor,
        reliability_max_attempts=settings.reliability.retry_max_attempts,
        reliability_jitter_pct=settings.reliability.retry_jitter_pct,
        min_action_confidence=settings.ultra96.min_confidence,
    )

    backoff_ms = settings.heartbeat.reconnect_backoff_ms
    attempt = 0
    try:
        while True:
            transport = _build_transport(settings)
            session = Session(
                transport=transport,
                config=session_config,
                windower=windower,
                ultra96=ultra96,
                on_weapon_changed=on_weapon_changed,
                on_action_result=on_action_result,
            )
            try:
                await session.run()
                attempt = 0
            except Exception as exc:
                # Top-level supervisor boundary: a session must never take the relay process down.
                # Every specific failure mode below this point is already its own typed exception;
                # this is the "and reconnect anyway" backstop, logged loudly rather than silent.
                logger.error("session_failed", error=str(exc))
            delay = backoff_ms[min(attempt, len(backoff_ms) - 1)] / 1000
            attempt += 1
            logger.warning("reconnecting", delay_s=delay)
            await asyncio.sleep(delay)
    finally:
        await engine_link.disconnect()
        await ultra96.disconnect()
        if tunnel is not None:
            await tunnel.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="CG4002 relay process")
    parser.add_argument("--config", type=Path, required=True, help="Path to a relay YAML config")
    parser.add_argument("--no-tunnel", action="store_true", help="Skip starting the SSH tunnel supervisor")
    parser.add_argument("--log-level", default="INFO", help="Level for the JSONL log file")
    parser.add_argument(
        "--console-level",
        default=None,
        help="Level for the console mirror; defaults to --log-level. Use WARNING to keep the "
        "terminal readable during a live demo. In continuous mode (windowing.trigger_mode: false) "
        "window_ready/action_classified fire several times a second at 50 Hz; in trigger mode "
        "(comms-demo) they fire once per button press instead. The file keeps every event either "
        "way for cg-latency-report.",
    )
    args = parser.parse_args()

    settings = RelaySettings.from_yaml(args.config)
    logs_dir = settings.logs_dir if settings.logs_dir.is_absolute() else find_repo_root() / "comms" / settings.logs_dir
    log_path = configure_logging(
        f"relay-p{settings.player_id}",
        logs_dir,
        level=getattr(logging, args.log_level.upper()),
        console_level=(
            None if args.console_level is None else getattr(logging, args.console_level.upper())
        ),
    )
    logger.info(
        "relay_starting",
        player_id=settings.player_id,
        transport=settings.transport.kind,
        ultra96=f"{settings.ultra96.host}:{settings.ultra96.port}",
        engine=f"{settings.engine_link.host}:{settings.engine_link.port}",
        log_path=str(log_path),
    )

    asyncio.run(run_relay(settings, use_tunnel=not args.no_tunnel))


if __name__ == "__main__":
    main()
