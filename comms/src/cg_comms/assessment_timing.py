"""Explicit paired latency budget for the communications assessment over the SSH route.

The hardware log proves that 120 ms discards replies; it does not establish a tail bound.
500 ms is a bounded demonstration budget, with 100 ms reserved for other arrival delays.
Keep the ordinary gameplay configuration unchanged unless this profile is selected.
"""

from cg_comms.config import RelaySettings, RulesConfig

CLASSIFIER_TIMEOUT_MS = 500
REORDER_DELAY_MS = 600  # Base delay; the existing guard grace is added by GameStateTask.


def relay_settings_for_profile(settings: RelaySettings) -> RelaySettings:
    if settings.latency_profile != "assessment":
        return settings
    if not settings.assessment.enabled:
        raise ValueError("--latency-profile assessment requires --assessment on the relay")
    return settings.model_copy(
        update={
            "ultra96": settings.ultra96.model_copy(update={"request_timeout_ms": CLASSIFIER_TIMEOUT_MS})
        }
    )


def rules_for_profile(config: RulesConfig, profile: str) -> RulesConfig:
    if profile != "assessment":
        return config
    return config.model_copy(update={"reorder_delay_ms": REORDER_DELAY_MS})
