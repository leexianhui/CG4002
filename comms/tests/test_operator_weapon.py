"""Operator weapon set and the phones' weapon lock-in countdown (2026-11-13).

- tools/set_weapon.py sends SET_WEAPON on /control; the engine sends that player's relay
  COMBAT_FEEDBACK WEAPON_SET_* (codes 8-10) and the hub locks it at once, from any stage.
- The hub's WEAPON_CANDIDATE (a weapon pose started / stopped counting toward the lock) goes relay ->
  engine -> phones as the snapshot's weaponLock {weapon, heldMs, totalMs}.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import websockets
from test_online_integration import receive_until
from test_pre_demo_recovery import free_port
from test_segments import frame, session

from cg_comms.clock import monotonic_ms
from cg_comms.config import EngineSettings
from cg_comms.engine.app import run_engine
from cg_comms.engine.game_state_task import GameStateTask
from cg_comms.engine.inbound import InboundWeaponCandidate, InboundWeaponChanged
from cg_comms.engine.rules import Rules
from cg_comms.messages.enums import FeedbackCode, Weapon
from cg_comms.messages.models import WeaponCandidate, WeaponChanged
from cg_comms.protocol.constants import PacketType


def engine(presence):
    sent = []

    async def feedback(player_id, message):
        sent.append((player_id, message.code))

    noop = AsyncMock()
    task = GameStateTask(Rules.load(), 0, 10000, noop, noop, feedback, noop, lambda: presence,
                         require_visualizer_ready=True)
    task._clock_anchors = {(pid, ch): 0 for pid in (1, 2) for ch in ("action", "position")}
    return task, sent


async def test_set_weapon_reaches_the_hub_once_its_relay_is_connected():
    links = {"1": True, "2": False}
    task, sent = engine(links)
    await task._update_presentation()
    sent.clear()
    task._weapon_set_pending[2] = Weapon.SPEAR  # as the run loop does for SetWeapon(2, SPEAR)
    task._weapon_set_pending[1] = Weapon.MACE
    await task._update_presentation()
    assert (1, FeedbackCode.WEAPON_SET_MACE) in sent and all(pid != 2 for pid, _ in sent)
    links["2"] = True  # P2's relay comes back
    await task._update_presentation()
    assert (2, FeedbackCode.WEAPON_SET_SPEAR) in sent


async def test_a_reset_supersedes_an_undelivered_set_weapon():
    links = {"1": False, "2": False}
    task, sent = engine(links)
    task._weapon_set_pending[1] = Weapon.SWORD
    await task.reset_match()
    links["1"] = True
    await task._update_presentation()
    assert (1, FeedbackCode.WEAPON_UNLOCK) in sent
    assert all(code is not FeedbackCode.WEAPON_SET_SWORD for _, code in sent)


async def test_snapshot_shows_the_lock_in_until_the_weapon_locks():
    task, _ = engine({"1": True, "2": True})
    now = monotonic_ms()
    task._absorb(InboundWeaponCandidate(1, WeaponCandidate(
        timestamp=now - 1200, player_id=1, weapon="SPEAR", lock_ms=3000)))
    snap = task.current_state_snapshot()
    lock = next(p.weapon_lock for p in snap.players if p.player_id == 1)
    assert (lock.weapon, lock.total_ms) == (Weapon.SPEAR, 3000) and 1150 <= lock.held_ms <= 1400
    assert next(p.weapon_lock for p in snap.players if p.player_id == 2) is None
    wire = json.loads(snap.model_dump_json(by_alias=True))
    assert wire["players"][0]["weaponLock"]["weapon"] == "SPEAR"
    # The hub locks it: WEAPON_CHANGED clears the countdown.
    task._absorb(InboundWeaponChanged(1, WeaponChanged(timestamp=monotonic_ms(), player_id=1,
                                                       weapon="SPEAR")))
    await asyncio.sleep(0.15)  # past the reorder delay
    await task._drain_ready()
    assert task.state.players[1].weapon_candidate is None
    # A lost pose (weapon null) clears it too.
    task._absorb(InboundWeaponCandidate(2, WeaponCandidate(
        timestamp=now, player_id=2, weapon="MACE", lock_ms=3000)))
    task._absorb(InboundWeaponCandidate(2, WeaponCandidate(
        timestamp=now, player_id=2, weapon=None, lock_ms=3000)))
    assert task.state.players[2].weapon_candidate is None


async def test_relay_forwards_the_hubs_weapon_candidate_edges():
    subject = session()
    got = []

    async def forward(candidate):
        got.append(candidate)

    subject.on_weapon_candidate = forward
    for seq, (weapon_id, ts) in enumerate([(3, 4000), (0, 4900)], start=1):
        await subject._handle_frame(frame({"device_timestamp_ms": ts, "weapon_id": weapon_id,
                                           "reserved": 0, "lock_ms": 3000}, seq,
                                          kind=PacketType.WEAPON_CANDIDATE))
    assert subject._send_ack.await_count == 2
    assert [(c.weapon, c.lock_ms) for c in got] == [(Weapon.MACE, 3000), (None, 3000)]


async def test_set_weapon_operator_command_end_to_end_through_the_engine():
    port = free_port()
    token = "operator-weapon-test"
    config = EngineSettings(_env_file=None, host="127.0.0.1", port=port, use_tls=False,
                            bearer_token=token, require_visualizer_ready=True,
                            publish_state_snapshots=True)
    task = asyncio.create_task(run_engine(config))

    async def connect(path):
        for _ in range(100):
            try:
                return await websockets.connect(f"ws://127.0.0.1:{port}{path}",
                                                additional_headers={"Authorization": f"Bearer {token}"})
            except OSError:
                await asyncio.sleep(.01)
        raise AssertionError("engine did not start")

    connections = []
    try:
        relay = await connect("/relay/2")
        control = await connect("/control")
        connections += [relay, control]
        await control.send(json.dumps({"type": "SET_WEAPON", "version": 1, "playerId": 2,
                                       "weapon": "SPEAR"}))
        command = await receive_until(relay, lambda m: m.get("code") == "WEAPON_SET_SPEAR")
        assert (command["type"], command["playerId"], command["intensityPct"]) == ("COMBAT_FEEDBACK", 2, 0)
        # Malformed variants are rejected (no command reaches the relay).
        await control.send(json.dumps({"type": "SET_WEAPON", "version": 1, "playerId": 3,
                                       "weapon": "SPEAR"}))
        await control.send(json.dumps({"type": "SET_WEAPON", "version": 1, "playerId": 2,
                                       "weapon": "BOW"}))
        try:
            extra = await receive_until(relay, lambda m: str(m.get("code", "")).startswith("WEAPON_SET"),
                                        timeout=0.5)
        except asyncio.TimeoutError:
            extra = None
        assert extra is None
    finally:
        for ws in connections:
            await ws.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
