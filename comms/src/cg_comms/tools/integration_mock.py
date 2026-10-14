"""One-process mock upstream for two real Unity phones, using production relay sessions."""

from __future__ import annotations

import argparse
import asyncio
import secrets
from pathlib import Path

from cg_comms.config import EngineSettings, RelaySettings
from cg_comms.engine.app import run_engine
from cg_comms.logging_setup import configure_logging
from cg_comms.mock.firebeetle_sim import FireBeetleSim
from cg_comms.mock.scenario import ScenarioStep
from cg_comms.mock.ultra96_sim import _handle_client
from cg_comms.relay.engine_link import EngineLinkClient
from cg_comms.relay.session import Session, SessionConfig
from cg_comms.relay.ultra96 import Ultra96Client
from cg_comms.relay.windowing import PlayerWindower
from cg_comms.timer_resolution import high_resolution_timers
from cg_comms.transport.loopback import LoopbackTransport


def device_steps() -> list[ScenarioStep]:
    # One second at 50 Hz; control packets have zero added delay, avoiding artificial gaps.
    return [ScenarioStep(0, "rfid_event", {"weapon": "SWORD"})] + [
        ScenarioStep(20, "sensor_frame", {"dominant": [0, 0, 2048, 0, 0, 0],
                                         "offhand": [0, 0, 2048, 0, 0, 0]})
        for _ in range(50)
    ] + [ScenarioStep(0, "heartbeat", {})]


async def run_stack(
    settings: EngineSettings, engine_host: str = "localhost", *, combat_enabled: bool = True,
) -> None:
    """Does not own either visualizer path. Phones provide real calibrated positions/readiness."""
    if not settings.bearer_token:
        raise ValueError("Set CG_ENGINE_BEARER_TOKEN in .env.engine or the environment")
    settings = settings.model_copy(update={"require_visualizer_ready": True,
                                           "publish_state_snapshots": True})
    steps = [ScenarioStep(0, "action_result", {"action": "IDLE"}) for _ in range(19)]
    if combat_enabled:
        steps += [ScenarioStep(0, "action_result", {"action": "HORIZONTAL_SWING"})]

    async def classify(reader, writer):
        await _handle_client(reader, writer, steps, None)

    classifier_server = await asyncio.start_server(classify, "127.0.0.1", 0)
    classifier_port = classifier_server.sockets[0].getsockname()[1]
    engine = asyncio.create_task(run_engine(settings))
    clients = []
    links = []
    workers = [engine]
    try:
        for pid in (1, 2):
            relay_transport, device_transport = LoopbackTransport.pair()
            psk = secrets.token_bytes(16)
            classifier = Ultra96Client("127.0.0.1", classifier_port, 1000, 2)
            clients.append(classifier)
            classifier.start()
            windower = PlayerWindower(pid, 40, 5, 20, 15, 50)
            scheme = "wss" if settings.use_tls else "ws"
            # Feedback only queues to the matching player's production Session.
            session_box = []

            async def feedback(message, box=session_box):
                box[0].queue_combat_feedback(message)

            link = EngineLinkClient(f"{scheme}://{engine_host}:{settings.port}/relay/{pid}",
                settings.bearer_token, feedback,
                ca_cert_path=settings.cert_path if settings.use_tls else None)
            links.append(link)
            link.start()
            defaults = RelaySettings(_env_file=None)
            session_config = SessionConfig(
                player_id=pid, psk=psk,
                handshake_samples=defaults.timesync.handshake_samples,
                handshake_spacing_ms=defaults.timesync.handshake_spacing_ms,
                ewma_alpha=defaults.timesync.ewma_alpha,
                outlier_rtt_multiplier=defaults.timesync.outlier_rtt_multiplier,
                divergence_warn_ms=defaults.timesync.divergence_warn_ms,
                heartbeat_interval_ms=defaults.heartbeat.interval_ms,
                heartbeat_liveness_factor=defaults.heartbeat.liveness_timeout_factor,
                reliability_base_timeout_ms=defaults.reliability.retry_timeout_ms,
                reliability_backoff_factor=defaults.reliability.retry_backoff_factor,
                reliability_max_attempts=defaults.reliability.retry_max_attempts,
                reliability_jitter_pct=defaults.reliability.retry_jitter_pct,
                min_action_confidence=defaults.ultra96.min_confidence,
            )
            session = Session(relay_transport, session_config,
                              windower, classifier, link.send_weapon_changed, link.send_action)
            session_box.append(session)
            sim = FireBeetleSim(device_transport, psk, pid)
            workers.extend((asyncio.create_task(session.run()),
                            asyncio.create_task(sim.run(device_steps(), loop_scenario=True))))
        done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        raise RuntimeError("A mock stack component stopped")
    finally:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        for link in links:
            await link.disconnect()
        for client in clients:
            await client.disconnect()
        classifier_server.close()
        await classifier_server.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/engine.integration.yaml"))
    parser.add_argument("--engine-host", default="localhost",
                        help="Host matching engine TLS certificate")
    parser.add_argument("--plain-local", action="store_true",
                        help="Loopback-only WS for editor/SSH forwarding")
    parser.add_argument("--calibration-only", action="store_true",
                        help="Keep the mock classifier idle while checking phone geometry")
    args = parser.parse_args()
    settings = EngineSettings.from_yaml(args.config)
    if args.plain_local:
        settings = settings.model_copy(update={"host": "127.0.0.1", "use_tls": False})
    if not settings.bearer_token:
        parser.error("Set CG_ENGINE_BEARER_TOKEN; the token is never printed by this launcher")
    configure_logging("integration-mock", settings.logs_dir)
    try:
        with high_resolution_timers():
            asyncio.run(run_stack(settings, args.engine_host, combat_enabled=not args.calibration_only))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
