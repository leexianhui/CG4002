"""Single writer: all combat inputs share event-time order, with explicit guard-only lookahead."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import structlog

from cg_comms.clock import monotonic_ms
from cg_comms.engine.cooldowns import add_stagger, start_cooldown
from cg_comms.engine.inbound import (
    InboundAction,
    InboundEvent,
    InboundPosition,
    InboundWeaponChanged,
    LinkReset,
    ResetMatch,
)
from cg_comms.engine.reorder import ReorderBuffer
from cg_comms.engine.resolver import ActionEvent, Decision, _angle_diff_deg, _bearing_deg, resolve
from cg_comms.engine.rules import Rules
from cg_comms.engine.state import MatchState as EngineMatchState
from cg_comms.engine.state import PlayerState, Vec2
from cg_comms.messages.enums import (
    WIRE_COMBAT_OUTCOMES,
    ActionClass,
    CombatOutcome,
    FeedbackCode,
    GuardState,
    MatchPhase,
)
from cg_comms.messages.models import (
    Action,
    ActionRecognized,
    CombatEvent,
    CombatFeedback,
    Position,
    WeaponChanged,
)
from cg_comms.messages.models import MatchState as WireMatchState

logger = structlog.get_logger(__name__)

_FEEDBACK_FOR_OUTCOME: dict[CombatOutcome, tuple[FeedbackCode, FeedbackCode | None]] = {
    CombatOutcome.HIT: (FeedbackCode.HIT_LANDED, FeedbackCode.HIT_TAKEN),
    CombatOutcome.BLOCK: (FeedbackCode.HIT_LANDED, FeedbackCode.BLOCKED),
    CombatOutcome.PARRY: (FeedbackCode.HIT_LANDED, FeedbackCode.PARRIED),
}

# ACTION/WEAPON_CHANGED arrive over the relay link; POSITION arrives over the visualizer link — a
# different sender that may be running on yet another machine with its own unrelated clock offset.
# Anchored independently per (player_id, channel) so one channel's offset never leaks into the other.
_CHANNEL_ACTION = "action"
_CHANNEL_POSITION = "position"


@dataclass(frozen=True)
class TimedInput:
    timestamp: int
    event: InboundEvent


@dataclass
class GameStateTask:
    rules: Rules
    reorder_delay_ms: int
    late_drop_ms: int
    on_combat_event: Callable[[CombatEvent], Awaitable[None]]
    on_match_state: Callable[[WireMatchState], Awaitable[None]]
    on_combat_feedback: Callable[[int, CombatFeedback], Awaitable[None]]
    on_action_recognized: Callable[[ActionRecognized], Awaitable[None]]
    presence: Callable[[], dict[str, bool]]
    on_weapon_changed: Callable[[WeaponChanged], Awaitable[None]] | None = None

    state: EngineMatchState = field(default_factory=EngineMatchState, init=False)
    _reorder: ReorderBuffer = field(init=False)
    _inbox: "asyncio.Queue[InboundEvent]" = field(default_factory=lambda: asyncio.Queue(512), init=False)
    # (player_id, channel) -> engine_local_monotonic_ms - remote_ts, seeded from the first message on
    # that channel. InboundAction/InboundPosition.timestamp are each some *other* machine's
    # time.monotonic()-based "engine time" (relay laptop for actions, visualizer host for positions) —
    # meaningless outside the process that produced it (clock.py's own monotonic_ms docstring). This
    # engine process's monotonic_ms() runs on yet another clock (a third machine, the Ultra96).
    # Comparing them raw (ReorderBuffer's now_engine_ms, or resolver.py's ev.timestamp vs
    # position_ts/guard_entered_ts across players) silently does the wrong thing instead of raising:
    # a laptop with days of uptime makes every incoming timestamp look "already released" or
    # "impossibly old", so events sit in the reorder heap forever and nothing ever logs why. Anchoring
    # once per channel — the same seed-once-trust-the-offset approach relay/timesync.py already uses
    # for device->relay time — makes every anchored timestamp comparable in this process's own frame.
    _clock_anchors: dict[tuple[int, str], int] = field(default_factory=dict, init=False)
    # Latches the one-time "match is over" warning, so a rig left streaming after the match ends
    # says so once instead of on every window.
    _logged_post_match: bool = field(default=False, init=False)

    _reset_floor: int = field(default=-1, init=False)

    def __post_init__(self) -> None:
        self._reorder = ReorderBuffer(
            reorder_delay_ms=self.reorder_delay_ms + self.rules.config.guard_grace_ms,
            late_drop_ms=self.late_drop_ms,
        )
        for player_id in (1, 2):
            self.state.players[player_id] = PlayerState(player_id=player_id, hp=self.rules.starting_hp)

    async def submit(self, event: InboundEvent) -> None:
        if isinstance(event, (ResetMatch, LinkReset)):
            await self._inbox.put(event)
            return
        try:
            self._inbox.put_nowait(event)
        except asyncio.QueueFull:
            logger.error("engine_inbox_full", event_type=type(event).__name__)

    async def start_match(self) -> None:
        if self.state.phase == MatchPhase.WAITING:
            self.state.phase = MatchPhase.IN_PROGRESS
            self.state.started_ts = monotonic_ms()
            await self._broadcast_match_state()

    async def run(self, stop_event: asyncio.Event, drain_interval_ms: int = 20) -> None:
        while not stop_event.is_set():
            try:
                event = await asyncio.wait_for(self._inbox.get(), timeout=drain_interval_ms / 1000)
            except asyncio.TimeoutError:
                event = None
            if event is not None:
                if isinstance(event, ResetMatch):
                    await self.reset_match()
                elif isinstance(event, LinkReset):
                    self._clock_anchors.pop((event.player_id, event.channel), None)
                    # Already normalized events and gameplay state belong to the match, not the
                    # replacement socket. Clearing guard here would let a later reconnect alter
                    # an earlier queued attack. New input updates state in event-time order.
                    logger.info("engine_channel_reset", player_id=event.player_id, channel=event.channel)
                else:
                    self._absorb(event)
            await self._drain_ready()

    def _anchor(self, player_id: int, channel: str, remote_ts: int, now: int) -> int:
        """Converts a remote-clock timestamp into this engine process's own monotonic_ms() frame.

        Seeded from the first message on each (player_id, channel): the offset from that single
        sample is trusted for the rest of the match (same seed-once approach as
        relay/timesync.py's TimeSyncEstimator.seed — there's no cheap way to do better without a
        real NTP-style exchange with the relay/visualizer processes, and a constant offset is all
        that's needed to make ReorderBuffer/resolver comparisons meaningful again)."""
        key = (player_id, channel)
        offset = self._clock_anchors.get(key)
        if offset is None:
            offset = now - remote_ts
            self._clock_anchors[key] = offset
            logger.info("clock_anchor_seeded", player_id=player_id, channel=channel, offset_ms=offset,
                        accuracy="arrival_biased_not_measured")
        return remote_ts + offset

    def _absorb(self, event: InboundEvent) -> None:
        now = monotonic_ms()
        message: Action | Position | WeaponChanged
        if isinstance(event, InboundAction):
            message, channel = event.action, _CHANNEL_ACTION
        elif isinstance(event, InboundWeaponChanged):
            message, channel = event.weapon_changed, _CHANNEL_ACTION
        elif isinstance(event, InboundPosition):
            message, channel = event.position, _CHANNEL_POSITION
        else:
            return
        remote_ts = message.timestamp
        timestamp = self._anchor(event.player_id, channel, remote_ts, now)
        timing = dict(player_id=event.player_id, message_type=message.type, channel=channel,
                      source_timestamp=remote_ts, engine_timestamp_ms=timestamp,
                      arrival_engine_timestamp_ms=now, event_age_ms=now - timestamp,
                      released_timestamp_ms=self._reorder.released_timestamp,
                      release_delay_ms=self._reorder.reorder_delay_ms)
        if isinstance(event, InboundAction):
            timing["demo_trace_id"] = event.action.demo_trace_id
            logger.info("action_received", **timing, weapon=event.action.weapon.value,
                        action=event.action.action.value, guard_state=event.action.guard_state.value)
        if timestamp < self._reset_floor:
            logger.warning("event_before_match_reset", **timing, reason="before_match_reset")
            return
        reason = self._reorder.push(timestamp, TimedInput(timestamp, event), now,
                                    priority=1 if isinstance(event, InboundAction) else 0)
        if reason is not None:
            logger.warning(
                "action_dropped_late" if isinstance(event, InboundAction) else "event_dropped",
                **timing, reason=reason, queue_depth=len(self._reorder),
                total_dropped_late=self._reorder.dropped_late)

    async def reset_match(self) -> None:
        self.state = EngineMatchState()
        for pid in (1, 2):
            self.state.players[pid] = PlayerState(player_id=pid, hp=self.rules.starting_hp)
        self._reorder = ReorderBuffer(
            reorder_delay_ms=self.reorder_delay_ms + self.rules.config.guard_grace_ms,
            late_drop_ms=self.late_drop_ms)
        self._reset_floor = monotonic_ms()
        self._logged_post_match = False
        self.state.phase = MatchPhase.IN_PROGRESS
        self.state.started_ts = self._reset_floor
        logger.info("match_reset")
        snapshot = self.current_match_state().model_copy(update={"reason": "reset"})
        await self.on_match_state(snapshot)

    async def _drain_ready(self) -> None:
        ready = self._reorder.drain_ready(monotonic_ms())
        for index, item in enumerate(ready):
            event, timestamp = item.event, item.timestamp
            player = self.state.players.get(event.player_id)
            if player is None:
                continue
            if isinstance(event, InboundWeaponChanged):
                player.weapon = event.weapon_changed.weapon
                logger.info("weapon_selected", player_id=event.player_id, weapon=player.weapon.value)
                if self.on_weapon_changed is not None:
                    await self.on_weapon_changed(event.weapon_changed)
            elif isinstance(event, InboundPosition):
                player.position = Vec2(event.position.x, event.position.y)
                player.heading_deg = event.position.heading
                player.position_ts = timestamp
            elif isinstance(event, InboundAction):
                action = event.action
                if player.weapon is None:
                    player.weapon = action.weapon
                if player.guard_state != action.guard_state:
                    player.guard_state = action.guard_state
                    player.guard_entered_ts = timestamp
                # ACTION carries its own weapon so fresh engine/reset state recovers on the next action.
                ev = ActionEvent(event.player_id, action.action, action.action_confidence, timestamp,
                                 action.guard_state, action.guard_confidence, action.weapon, action.demo_trace_id)
                snapshot = self.state.clone()
                # Only guard activation may look forward. Later geometry/weapon/guard release never
                # mutates this snapshot. A late activation can BLOCK but cannot retroactively PARRY.
                future = ready[index + 1:] + self._reorder.pending()
                for candidate in future:
                    if candidate.timestamp > timestamp + self.rules.config.guard_grace_ms:
                        break
                    guard = candidate.event
                    if (isinstance(guard, InboundAction) and guard.player_id != event.player_id
                            and guard.action.guard_state == GuardState.GUARDING):
                        defender = snapshot.players[guard.player_id]
                        if defender.guard_state != GuardState.GUARDING:
                            defender.guard_state = GuardState.GUARDING
                            defender.guard_entered_ts = candidate.timestamp
                await self._resolve_one(ev, snapshot)

    async def _resolve_one(self, ev: ActionEvent, snapshot: EngineMatchState | None = None) -> None:
        resolution_state = snapshot if snapshot is not None else self.state
        decision = resolve(resolution_state, ev, self.rules)
        attacker = self.state.players.get(decision.attacker_id)
        weapon = ev.weapon if ev.weapon is not None else (attacker.weapon if attacker else None)
        if attacker is not None:
            attacker.last_action = ev.action

        if (
            attacker is not None
            and weapon is not None
            and (ev.action is not ActionClass.IDLE or ev.guard_state is not GuardState.IDLE)
        ):
            target_id = (
                decision.defender_id
                if decision.defender_id is not None and self.presence().get(str(decision.defender_id), False)
                else None
            )
            await self.on_action_recognized(
                ActionRecognized(
                    timestamp=ev.timestamp,
                    player_id=ev.player_id,  # type: ignore[arg-type]
                    weapon=weapon,  # type: ignore[arg-type]
                    action=ev.action,
                    action_confidence=ev.confidence,
                    guard_state=ev.guard_state,
                    guard_confidence=ev.guard_confidence,
                    outcome=decision.outcome,
                    demo_trace_id=ev.demo_trace_id,
                    target_id=target_id,  # type: ignore[arg-type]
                )
            )

        if decision.outcome in (CombatOutcome.IGNORED, CombatOutcome.STATE_ONLY, CombatOutcome.ON_COOLDOWN):
            if self.state.phase is not MatchPhase.IN_PROGRESS:
                if not self._logged_post_match:
                    self._logged_post_match = True
                    logger.warning(
                        "match_over_actions_ignored",
                        phase=self.state.phase.value,
                        winner=self.state.winner_id,
                        hint="use the authenticated reset_match operator command to play again",
                    )
                return

            # These three outcomes emit no COMBAT_EVENT, which is exactly why they need logging:
            # "the swing did nothing" is otherwise indistinguishable from "the swing never
            # arrived". IGNORED in particular covers a stale/missing POSITION (resolver step 7) —
            # the single most likely reason a demo looks dead while every link is healthy.
            logger.info(
                "action_not_resolved",
                engine_timestamp_ms=ev.timestamp,
                outcome=decision.outcome.value,
                attacker=decision.attacker_id,
                action=ev.action.value,
                confidence=round(ev.confidence, 3),
                weapon=weapon.value if weapon else None,
            )
            return

        if attacker is not None and weapon is not None:
            move = self.rules.move_for(weapon, ev.action)
            if move is not None:
                start_cooldown(attacker, ev.action, ev.timestamp, move.cooldown_ms)

        if decision.outcome == CombatOutcome.PARRY and attacker is not None:
            add_stagger(attacker, self.rules.stagger_ms, ev.timestamp)

        defender = self.state.players.get(decision.defender_id) if decision.defender_id is not None else None
        if defender is not None and decision.damage > 0:
            defender.hp = max(0, defender.hp - decision.damage)

        if (
            decision.outcome in WIRE_COMBAT_OUTCOMES
            and attacker is not None
            and weapon is not None
            and defender is not None
        ):
            # Inspect the exact event-time snapshot used by resolve, including guard-only grace.
            resolved_attacker = resolution_state.players[decision.attacker_id]
            resolved_defender = resolution_state.players[defender.player_id]
            assert resolved_attacker.position is not None and resolved_defender.position is not None
            bearing = _bearing_deg(resolved_defender.position, resolved_attacker.position)
            facing_error = _angle_diff_deg(resolved_defender.heading_deg, bearing)
            logger.info(
                "combat_resolved",
                engine_timestamp_ms=ev.timestamp,
                outcome=decision.outcome.value,
                attacker=decision.attacker_id,
                defender=decision.defender_id,
                weapon=weapon.value,
                action=ev.action.value,
                damage=decision.damage,
                attacker_hp=attacker.hp,
                defender_hp=defender.hp,
                range_m=round(decision.range_m, 2),
                defender_guard_state=resolved_defender.guard_state.value,
                guard_entered_engine_timestamp_ms=resolved_defender.guard_entered_ts,
                guard_age_ms=ev.timestamp - resolved_defender.guard_entered_ts,
                defender_heading_deg=resolved_defender.heading_deg,
                bearing_to_attacker_deg=bearing,
                facing_error_deg=facing_error,
                facing_attacker=facing_error <= self.rules.guard_arc_deg / 2,
            )
            await self.on_combat_event(
                CombatEvent(
                    timestamp=ev.timestamp,
                    attacker_id=decision.attacker_id,  # type: ignore[arg-type]
                    defender_id=decision.defender_id,  # type: ignore[arg-type]
                    weapon=weapon,  # type: ignore[arg-type]
                    action=ev.action,  # type: ignore[arg-type]
                    outcome=decision.outcome,  # type: ignore[arg-type]
                    damage=decision.damage,
                    attacker_hp=attacker.hp,
                    defender_hp=defender.hp,
                    range_m=decision.range_m,
                )
            )
            await self._send_feedback(decision, attacker.player_id, defender)

        if defender is not None and defender.hp <= 0 and self.state.phase == MatchPhase.IN_PROGRESS:
            self.state.phase = MatchPhase.ENDED
            self.state.winner_id = attacker.player_id if attacker is not None else None

        await self._broadcast_match_state()

    async def _send_feedback(self, decision: Decision, attacker_id: int, defender: PlayerState) -> None:
        codes = _FEEDBACK_FOR_OUTCOME.get(decision.outcome)
        if codes is None:
            return
        attacker_code, defender_code = codes
        await self.on_combat_feedback(
            attacker_id,
            CombatFeedback(
                timestamp=monotonic_ms(),
                player_id=attacker_id,  # type: ignore[arg-type]
                code=attacker_code,
                intensity_pct=100,
                duration_ms=200,
                repeat_count=1,
            ),
        )
        if defender_code is not None:
            await self.on_combat_feedback(
                defender.player_id,
                CombatFeedback(
                    timestamp=monotonic_ms(),
                    player_id=defender.player_id,  # type: ignore[arg-type]
                    code=defender_code,
                    intensity_pct=100,
                    duration_ms=200,
                    repeat_count=1,
                ),
            )

    def current_match_state(self) -> WireMatchState:
        """Builds the current `MATCH_STATE` snapshot without sending it — used by engine/app.py to
        answer a fresh `/visualizer/*` connection immediately (docs/DEMO_TRIGGERED_ACTION_PLAN.md
        §7.4), and internally by `_broadcast_match_state` so both paths construct the same shape."""
        return WireMatchState(
            timestamp=monotonic_ms(),
            phase=self.state.phase,
            hp={"1": self.state.players[1].hp, "2": self.state.players[2].hp},
            winner_id=self.state.winner_id,  # type: ignore[arg-type]
            connected=self.presence(),
        )

    async def _broadcast_match_state(self) -> None:
        await self.on_match_state(self.current_match_state())
