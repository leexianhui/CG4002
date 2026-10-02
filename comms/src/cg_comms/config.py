"""Config loading: YAML files under comms/configs/ and shared/config/, overridable by environment
variables (pydantic-settings), plus the repo-root locator every shared/config/*.yaml reader needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def find_repo_root() -> Path:
    """Walks up from this file until a directory containing both `comms/` and `shared/` is found."""
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "shared").is_dir() and (candidate / "comms").is_dir():
            return candidate
    raise FileNotFoundError("could not locate repo root (expected shared/ and comms/ as siblings)")


def shared_config_dir() -> Path:
    return find_repo_root() / "shared" / "config"


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TransportConfig(_Model):
    kind: Literal["ble", "tcp", "loopback"] = "ble"
    ble_address: str | None = None
    ble_notify_char_uuid: str | None = None
    ble_write_char_uuid: str | None = None
    tcp_host: str = "127.0.0.1"
    tcp_port: int = 9000


class ReliabilityConfig(_Model):
    retry_timeout_ms: int = 200
    retry_backoff_factor: float = 2.0
    retry_max_attempts: int = 5
    retry_jitter_pct: float = 0.2


class HeartbeatConfig(_Model):
    interval_ms: int = 1000
    liveness_timeout_factor: int = 3
    reconnect_backoff_ms: list[int] = Field(default_factory=lambda: [1000, 2000, 4000, 8000, 10000])


class BackpressureConfig(_Model):
    raw_ingest_depth: int = 256
    windower_depth: int = 100
    ultra96_depth: int = 4
    engine_event_depth: int = 512


class WindowingConfig(_Model):
    window_samples: int = 40
    stride_frames: int = 5
    max_stale_pct: float = 20.0
    max_span_deviation_pct: float = 15.0
    frame_rate_hz: float = 50.0
    # comms-demo only (docs/DEMO_TRIGGERED_ACTION_PLAN.md §3.2): one SensorWindow per tagged button
    # capture instead of continuous stride emission. False (the real game's mode) is unchanged behaviour.
    trigger_mode: bool = False


class Ultra96Config(_Model):
    use_mock: bool = False
    host: str = "127.0.0.1"
    port: int = 8888
    request_timeout_ms: int = 120
    max_in_flight: int = 2
    min_confidence: float = 0.6
    tunnel_enabled: bool = False
    tunnel_ssh_jump_user: str | None = None
    tunnel_ssh_jump_host: str = "stfjump.comp.nus.edu.sg"
    tunnel_ssh_target_user: str = "xilinx"
    tunnel_ssh_target_host: str | None = None
    tunnel_local_port: int = 8888
    tunnel_remote_port: int = 8888
    tunnel_connect_timeout_s: float = 30.0


class EngineLinkConfig(_Model):
    host: str = "127.0.0.1"
    port: int = 8443
    use_tls: bool = True
    ca_cert_path: str | None = None
    bearer_token: str = ""


class TimeSyncConfig(_Model):
    handshake_samples: int = 8
    handshake_spacing_ms: int = 100
    resync_interval_s: int = 30
    ewma_alpha: float = 0.1
    outlier_rtt_multiplier: float = 2.0
    divergence_warn_ms: float = 20.0
    hello_nonce_replay_window_s: int = 60


class RelaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CG_RELAY_", env_nested_delimiter="__", env_file=".env.relay", extra="forbid"
    )

    player_id: Literal[1, 2] = 1
    psk_hex: str = ""
    logs_dir: Path = Path("logs")

    transport: TransportConfig = Field(default_factory=TransportConfig)
    reliability: ReliabilityConfig = Field(default_factory=ReliabilityConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    backpressure: BackpressureConfig = Field(default_factory=BackpressureConfig)
    windowing: WindowingConfig = Field(default_factory=WindowingConfig)
    timesync: TimeSyncConfig = Field(default_factory=TimeSyncConfig)
    ultra96: Ultra96Config = Field(default_factory=Ultra96Config)
    engine_link: EngineLinkConfig = Field(default_factory=EngineLinkConfig)

    @field_validator("player_id", mode="before")
    @classmethod
    def parse_player_id(cls, value: object) -> object:
        # Environment values are strings; keep the Literal validation for every other value.
        return int(value) if isinstance(value, str) and value in ("1", "2") else value

    @property
    def psk(self) -> bytes:
        return bytes.fromhex(self.psk_hex)

    @classmethod
    def from_yaml(cls, path: Path) -> "RelaySettings":
        return cls(**load_yaml(path))


class EngineSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CG_ENGINE_", env_nested_delimiter="__", env_file=".env.engine", extra="forbid"
    )

    host: str = "0.0.0.0"
    port: int = 8443
    use_tls: bool = True
    cert_path: str = "certs/engine.crt"
    key_path: str = "certs/engine.key"
    bearer_token: str = ""
    logs_dir: Path = Path("logs")

    backpressure: BackpressureConfig = Field(default_factory=BackpressureConfig)

    reorder_delay_ms: int = 60
    late_drop_ms: int = 500
    send_queue_depth: int = 32

    @classmethod
    def from_yaml(cls, path: Path) -> "EngineSettings":
        return cls(**load_yaml(path))


class WeaponMove(_Model):
    damage: int
    range_m: float
    cooldown_ms: int


class RulesConfig(_Model):
    """Loaded from shared/config/weapons.yaml + match.yaml (COMMS_IMPLEMENTATION_PLAN.md §10.4).
    §16.3 rule 1: the resolver looks moves up here; it never branches on a literal weapon/action."""

    moves: dict[str, dict[str, WeaponMove]]  # weapon name -> action name -> move
    starting_hp: int
    parry_window_ms: int
    block_multiplier: float
    stagger_ms: int
    guard_arc_deg: float
    min_confidence: float
    position_max_age_ms: int
    reorder_delay_ms: int
    guard_grace_ms: int = Field(default=0, ge=0, le=500)

    @classmethod
    def load(cls, shared_dir: Path | None = None) -> "RulesConfig":
        shared_dir = shared_dir or shared_config_dir()
        weapons_raw = load_yaml(shared_dir / "weapons.yaml")["weapons"]
        moves = {weapon: data["moves"] for weapon, data in weapons_raw.items()}
        match_raw = load_yaml(shared_dir / "match.yaml")
        return cls(moves=moves, **match_raw)
