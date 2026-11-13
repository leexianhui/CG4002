"""Routes one raw inbound WebSocket text frame to a typed inbound event, or logs+drops it
(COMMS_IMPLEMENTATION_PLAN.md §11.1).

Kept separate from ws_server.py so socket/connection lifecycle stays independent of "what do we do
with a parsed message."
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from cg_comms.engine.ws_server import RELAY_PATHS, VISUALIZER_PATHS
from cg_comms.messages.enums import Weapon
from cg_comms.messages.models import (
    Action,
    Position,
    StateSnapshot,
    VisualizerReady,
    WeaponCandidate,
    WeaponChanged,
)
from cg_comms.messages.validate import MessageDropped, parse_app_message

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class InboundAction:
    player_id: int
    action: Action


@dataclass(frozen=True)
class InboundWeaponChanged:
    player_id: int
    weapon_changed: WeaponChanged


@dataclass(frozen=True)
class InboundPosition:
    player_id: int
    position: Position


@dataclass(frozen=True)
class ResetMatch:
    """Local operator command delivered by the authenticated /control connection."""


@dataclass(frozen=True)
class SetGodMode:
    """Local operator command delivered by the authenticated /control connection (a phone's
    GOD MODE button): while enabled, hits still resolve and report their damage, but nobody's HP drops."""

    enabled: bool


@dataclass(frozen=True)
class SetWeapon:
    """Operator command on /control (tools/set_weapon.py): lock this weapon on that player's hub now,
    from any stage. The hub then reports it as a normal lock (WEAPON_CHANGED)."""

    player_id: int
    weapon: Weapon


@dataclass(frozen=True)
class InboundWeaponCandidate:
    """Relay: the hub's weapon pose started (or stopped) counting toward its lock."""

    player_id: int
    candidate: WeaponCandidate


@dataclass(frozen=True)
class SetTutorial:
    """Local operator command delivered by the authenticated /control connection (a phone's
    TUTORIAL button): `player_id` practises alone against a dummy the engine plays as the other
    player (None ends the tutorial). `dummy_guards` makes the dummy raise its guard now and then."""

    player_id: int | None
    dummy_guards: bool


@dataclass(frozen=True)
class LinkReset:
    player_id: int
    channel: str


@dataclass(frozen=True)
class InboundVisualizerReady:
    player_id: int
    ready: bool


@dataclass(frozen=True)
class SnapshotRequest:
    send: Callable[[StateSnapshot], Awaitable[None]]


InboundEvent = (InboundAction | InboundWeaponChanged | InboundPosition | ResetMatch | SetGodMode
                | SetTutorial | SetWeapon | InboundWeaponCandidate | LinkReset | InboundVisualizerReady
                | SnapshotRequest)


def route_inbound(path: str, raw: str) -> InboundEvent | None:
    parsed = parse_app_message(raw)
    if isinstance(parsed, MessageDropped):
        logger.warning(
            "inbound_message_dropped", path=path, reason=parsed.reason.value, detail=parsed.detail
        )
        return None

    if path in RELAY_PATHS:
        expected_player_id = RELAY_PATHS[path]
        if isinstance(parsed, Action):
            if parsed.player_id != expected_player_id:
                logger.warning("inbound_player_id_mismatch", path=path, actual=parsed.player_id)
                return None
            return InboundAction(player_id=expected_player_id, action=parsed)
        if isinstance(parsed, WeaponChanged):
            if parsed.player_id != expected_player_id:
                logger.warning("inbound_player_id_mismatch", path=path, actual=parsed.player_id)
                return None
            return InboundWeaponChanged(player_id=expected_player_id, weapon_changed=parsed)
        if isinstance(parsed, WeaponCandidate):
            if parsed.player_id != expected_player_id:
                logger.warning("inbound_player_id_mismatch", path=path, actual=parsed.player_id)
                return None
            return InboundWeaponCandidate(player_id=expected_player_id, candidate=parsed)
        logger.warning("inbound_unexpected_type", path=path, msg_type=type(parsed).__name__)
        return None

    if path in VISUALIZER_PATHS:
        expected_player_id = VISUALIZER_PATHS[path]
        if isinstance(parsed, VisualizerReady):
            if parsed.player_id != expected_player_id:
                logger.warning("inbound_player_id_mismatch", path=path, actual=parsed.player_id)
                return None
            return InboundVisualizerReady(expected_player_id, parsed.ready)
        if isinstance(parsed, Position):
            if parsed.player_id != expected_player_id:
                logger.warning("inbound_player_id_mismatch", path=path, actual=parsed.player_id)
                return None
            return InboundPosition(player_id=expected_player_id, position=parsed)
        logger.warning("inbound_unexpected_type", path=path, msg_type=type(parsed).__name__)
        return None

    logger.warning("inbound_unknown_path", path=path)
    return None
