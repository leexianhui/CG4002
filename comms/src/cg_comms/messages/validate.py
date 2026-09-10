"""Schema-version gate and unknown-version drop for app-layer JSON messages.

"A receiver that sees a version it does not know logs and drops rather than guessing"
(shared/protocol/app_messages.md §1). A malformed message is always a counted, logged drop
(`MessageDropped`), never a raised exception — callers log `MessageDropped` at the appropriate
level and increment a drop counter; they never need a try/except around this call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ValidationError

from cg_comms.messages.models import (
    Action,
    ActionResult,
    AppMessage,
    CombatEvent,
    CombatFeedback,
    CooldownState,
    MatchState,
    Position,
    SensorWindow,
    WeaponChanged,
)
from cg_comms.protocol.constants import PROTOCOL_VERSION

_TYPE_TO_MODEL: dict[str, type[BaseModel]] = {
    "ACTION": Action,
    "WEAPON_CHANGED": WeaponChanged,
    "POSITION": Position,
    "COMBAT_EVENT": CombatEvent,
    "MATCH_STATE": MatchState,
    "COOLDOWN_STATE": CooldownState,
    "COMBAT_FEEDBACK": CombatFeedback,
    "SENSOR_WINDOW": SensorWindow,
    "ACTION_RESULT": ActionResult,
}


class MessageDropReason(str, Enum):
    MALFORMED_JSON = "malformed_json"
    NOT_AN_OBJECT = "not_an_object"
    MISSING_TYPE = "missing_type"
    UNKNOWN_TYPE = "unknown_type"
    UNKNOWN_VERSION = "unknown_version"
    SCHEMA_INVALID = "schema_invalid"


@dataclass(frozen=True)
class MessageDropped:
    reason: MessageDropReason
    detail: str = ""


def parse_app_message(raw: bytes | str) -> AppMessage | MessageDropped:
    """Parses one line-delimited or WebSocket text-frame JSON message into its pydantic model, or
    a `MessageDropped` describing why it was rejected."""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return MessageDropped(MessageDropReason.MALFORMED_JSON, str(exc))

    if not isinstance(obj, dict):
        return MessageDropped(MessageDropReason.NOT_AN_OBJECT)

    msg_type = obj.get("type")
    if not isinstance(msg_type, str):
        return MessageDropped(MessageDropReason.MISSING_TYPE)

    model = _TYPE_TO_MODEL.get(msg_type)
    if model is None:
        return MessageDropped(MessageDropReason.UNKNOWN_TYPE, msg_type)

    version = obj.get("version")
    if version != PROTOCOL_VERSION:
        return MessageDropped(
            MessageDropReason.UNKNOWN_VERSION, f"type={msg_type} version={version!r}"
        )

    try:
        return model.model_validate(obj)  # type: ignore[return-value]
    except ValidationError as exc:
        return MessageDropped(MessageDropReason.SCHEMA_INVALID, str(exc))


def dump_app_message(message: AppMessage) -> str:
    """Serializes an app message back to its wire JSON form (camelCase aliases)."""
    return message.model_dump_json(by_alias=True)
