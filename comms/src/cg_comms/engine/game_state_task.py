"""Single-writer coroutine owning MatchState (COMMS_IMPLEMENTATION_PLAN.md §10, §16.5).

Only this task ever mutates a live PlayerState/MatchState; everything else only ever sees
`.clone()`d snapshots or receives messages describing what happened, which is the entire
justification for running lock-free. Combat ACTION events pass through a ReorderBuffer first
(parry timing must be judged in engine-time order, not network-arrival order); POSITION and
WEAPON_CHANGED updates apply immediately since nothing downstream depends on their strict
ordering relative to combat resolution.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import structlog

from cg_comms.clock import monotonic_ms
from cg_comms.engine.cooldowns import add_stagger, start_cooldown
from cg_comms.engine.inbound import InboundAction, InboundEvent, InboundPosition, InboundWeaponChanged
from cg_comms.engine.reorder import ReorderBuffer
from cg_comms.engine.resolver import ActionEvent, Decision, resolve
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
from cg_comms.messages.models import ActionRecognized, CombatEvent, CombatFeedback
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

    state: EngineMatchState = field(default_factory=EngineMatchState, init=False)
    _reorder: ReorderBuffer = field(init=False)
    _inbox: "asyncio.Queue[InboundEvent]" = field(default_factory=asyncio.Queue, init=False)
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

    def __post_init__(self) -> None:
        self._reorder = ReorderBuffer(
            reorder_delay_ms=self.reorder_delay_ms, late_drop_ms=self.late_drop_ms
        )
        for player_id in (1, 2):
            self.state.players[player_id] = PlayerState(player_id=player_id, hp=self.rules.starting_hp)

    async def submit(self, event: InboundEvent) -> None:
        await self._inbox.put(event)

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
        return remote_ts + offset

    def _absorb(self, event: InboundEvent) -> None:
        now = monotonic_ms()
        if isinstance(event, InboundAction):
            anchored_ts = self._anchor(event.player_id, _CHANNEL_ACTION, event.action.timestamp, now)
            ev = ActionEvent(
                player_id=event.player_id,
                action=event.action.action,
                confidence=event.action.action_confidence,
                timestamp=anchored_ts,
                guard_state=event.action.guard_state,
                guard_confidence=event.action.guard_confidence,
            )
            self._apply_guard_state(event, anchored_ts)
            dropped_before = self._reorder.dropped_late
            self._reorder.push(ev.timestamp, ev, now)
            if self._reorder.dropped_late != dropped_before:
                # dropped_late used to be incremented and never read anywhere — a silent counter.
                # It only ever fires on a real "too late" arrival now that timestamps are anchored,
                # but a rig that never logs this could still go quietly dead the same way, so surface it.
                logger.warning(
                    "action_dropped_late",
                    player_id=event.player_id,
                    action=event.action.action.value,
                    total_dropped_late=self._reorder.dropped_late,
                )
        elif isinstance(event, InboundWeaponChanged):
            player = self.state.players.get(event.player_id)
            if player is not None:
                player.weapon = event.weapon_changed.weapon
        elif isinstance(event, InboundPosition):
            player = self.state.players.get(event.player_id)
            if player is not None:
                anchored_ts = self._anchor(event.player_id, _CHANNEL_POSITION, event.position.timestamp, now)
                player.position = Vec2(event.position.x, event.position.y)
                player.heading_deg = event.position.heading
                player.position_ts = anchored_ts

    def _apply_guard_state(self, event: InboundAction, anchored_ts: int) -> None:
        player = self.state.players.get(event.player_id)
        if player is None:
            return
        new_guard = event.action.guard_state
        if new_guard != player.guard_state:
            player.guard_state = new_guard
            player.guard_entered_ts = anchored_ts

    async def _drain_ready(self) -> None:
        now = monotonic_ms()
        for ev in self._reorder.drain_ready(now):
            await self._resolve_one(ev)

    async def _resolve_one(self, ev: ActionEvent) -> None:
        decision = resolve(self.state, ev, self.rules)
        attacker = self.state.players.get(decision.attacker_id)
        if attacker is not None:
            attacker.last_action = ev.action

        if (
            attacker is not None
            and attacker.weapon is not None
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
                    weapon=attacker.weapon,  # type: ignore[arg-type]
                    action=ev.action,
                    action_confidence=ev.confidence,
                    guard_state=ev.guard_state,
                    guard_confidence=ev.guard_confidence,
                    outcome=decision.outcome,
                    target_id=target_id,  # type: ignore[arg-type]
                )
            )

        if decision.outcome in (CombatOutcome.IGNORED, CombatOutcome.STATE_ONLY, CombatOutcome.ON_COOLDOWN):
            # Once the match is ENDED, resolve() refuses every action (its phase gate) and there is no
            # reset path, so a rig left streaming logs this same line forever. Say so once, at WARNING,
            # instead of emitting thousands of indistinguishable INFO lines: without it the console
            # looks like a busy, healthy system that has silently stopped doing anything, and the
            # actual instruction ("restart the engine") appears nowhere in the output.
            if self.state.phase is not MatchPhase.IN_PROGRESS:
                if not self._logged_post_match:
                    self._logged_post_match = True
                    logger.warning(
                        "match_over_actions_ignored",
                        phase=self.state.phase.value,
                        winner=self.state.winner_id,
                        hint="match is over; restart the engine to play again (no reset path)",
                    )
                return

            # These three outcomes emit no COMBAT_EVENT, which is exactly why they need logging:
            # "the swing did nothing" is otherwise indistinguishable from "the swing never
            # arrived". IGNORED in particular covers a stale/missing POSITION (resolver step 7) —
            # the single most likely reason a demo looks dead while every link is healthy.
            logger.info(
                "action_not_resolved",
                outcome=decision.outcome.value,
                attacker=decision.attacker_id,
                action=ev.action.value,
                confidence=round(ev.confidence, 3),
                weapon=attacker.weapon.value if attacker is not None and attacker.weapon else None,
            )
            return

        if attacker is not None and attacker.weapon is not None:
            move = self.rules.move_for(attacker.weapon, ev.action)
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
            and attacker.weapon is not None
            and defender is not None
        ):
            logger.info(
                "combat_resolved",
                outcome=decision.outcome.value,
                attacker=decision.attacker_id,
                defender=decision.defender_id,
                weapon=attacker.weapon.value,
                action=ev.action.value,
                damage=decision.damage,
                attacker_hp=attacker.hp,
                defender_hp=defender.hp,
                range_m=round(decision.range_m, 2),
            )
            await self.on_combat_event(
                CombatEvent(
                    timestamp=ev.timestamp,
                    attacker_id=decision.attacker_id,  # type: ignore[arg-type]
                    defender_id=decision.defender_id,  # type: ignore[arg-type]
                    weapon=attacker.weapon,  # type: ignore[arg-type]
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
