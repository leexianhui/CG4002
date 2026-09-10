# CG4002 Application-Layer Messages

**Normative.** This is the source for `shared/schemas/*.schema.json`. It covers the JSON messages
exchanged over the WebSocket links (relay ↔ engine ↔ visualizer) and the line-delimited JSON exchanged
between the laptop relay and the Ultra96 classifier. It does not cover the binary FireBeetle↔laptop
wire protocol — see `wire_protocol.md` for that.

See `../../docs/COMMS_IMPLEMENTATION_PLAN.md` §9 (engine/visualizer messages) and §8 (Ultra96 protocol)
for design rationale.

---

## 1. Envelope

Every message in this document carries:

```jsonc
{ "type": "...", "version": 1, "timestamp": 0, /* message-specific fields */ }
```

- `type` — the message name, exactly as titled in each section below.
- `version` — `shared/protocol/VERSION` at the time the sender was built. A receiver that sees a
  version it does not know **logs and drops** the message rather than guessing at compatibility.
- `timestamp` — `int64` milliseconds, **already normalized to the Game Engine clock**. The relay does
  this normalization on ingest (using the offset from `wire_protocol.md` §9); no other subsystem does
  clock arithmetic (REPO_PLAN.md §7).

Most messages additionally carry `playerId` (`1` or `2` — never `0`, never a string; `0` is reserved on
the binary wire as "unset"). Two kinds of message deliberately do not:

- **`MATCH_STATE`** is global — it describes both players at once, so it has no single `playerId`.
- **`COMBAT_EVENT`** concerns two players by construction, so it carries `attackerId` and `defenderId`
  instead of a single `playerId`.

Enum-valued fields are **UPPER_SNAKE strings** in JSON (e.g. `"HORIZONTAL_SWING"`) and `uint8` in the
binary protocol. `shared/enums/enums.yaml` is the single source for both mappings.

---

## 2. Engine-facing messages (relay ↔ engine ↔ visualizer)

### 2.1 `ACTION` — relay → engine

```jsonc
{ "type": "ACTION", "version": 1, "timestamp": 0, "playerId": 1,
  "weapon": "SWORD", "action": "HORIZONTAL_SWING", "actionConfidence": 0.94,
  "guardState": "IDLE", "guardConfidence": 0.88 }
```

Built by the relay from the Ultra96's `ACTION_RESULT` (§4.2) plus the relay's own tracked weapon state.
`action`/`actionConfidence` is a **discrete event** (what move just happened); `guardState`/
`guardConfidence` is a **continuous state update** (is the player currently guarding). They travel in
one message because `ACTION_RESULT` produces both classifier outputs together, but the engine must
treat them as two different kinds of update — conflating them is the easiest way to break parry timing
(COMMS_IMPLEMENTATION_PLAN.md §9).

`action: "IDLE"` means "no discrete action this tick" — the engine resolver treats it as a state-only
update (`last_action` changes, no `COMBAT_EVENT` is emitted).

### 2.2 `WEAPON_CHANGED` — relay → engine

```jsonc
{ "type": "WEAPON_CHANGED", "version": 1, "timestamp": 0, "playerId": 1, "weapon": "SPEAR" }
```

Sent once per confirmed RFID tag change (binary `RFID_EVENT`, debounced by firmware). The engine
updates `PlayerState.weapon` and resets nothing else — cooldowns and HP are independent of weapon.

### 2.3 `POSITION` — visualizer → engine, echoed engine → visualizer

```jsonc
{ "type": "POSITION", "version": 1, "timestamp": 0, "playerId": 2, "x": 1.2, "y": 0.4, "heading": 87.5 }
```

- `x`, `y` — arena-relative metres. Origin and axis convention: TBD per decision #7 in the comms plan
  (default: midpoint of the two spawn marks, +Y toward Player 2).
- `heading` — degrees, `[0, 360)`, arena-relative (not device-relative).

**Position is echoed, not just consumed.** The engine forwards each player's `POSITION` to the *other*
player's visualizer, using its own copy — the visualizer needs the opponent's position to place the
avatar, and both clients must agree on the engine's version of it, not derive it independently.

### 2.4 `COMBAT_EVENT` — engine → both visualizers

```jsonc
{ "type": "COMBAT_EVENT", "version": 1, "timestamp": 0, "attackerId": 1, "defenderId": 2,
  "weapon": "SWORD", "action": "HORIZONTAL_SWING", "outcome": "BLOCK",
  "damage": 3, "attackerHp": 40, "defenderHp": 37, "rangeM": 1.4 }
```

Emitted only for the four outcomes visible to players: `MISS`, `HIT`, `BLOCK`, `PARRY` (resolver steps
8–12 in `wire_protocol.md`'s companion, `COMMS_IMPLEMENTATION_PLAN.md` §10.3). `IGNORED`,
`STATE_ONLY`, and `ON_COOLDOWN` resolver results never reach the wire — they are logged internally only.
A `MISS` is still sent, with `damage: 0`, so the visualizer can animate a whiff (decision #9).

`damage`/`attackerHp`/`defenderHp` reflect state *after* this event's damage has been applied and HP
clamped at 0.

### 2.5 `MATCH_STATE` — engine → both visualizers

```jsonc
{ "type": "MATCH_STATE", "version": 1, "timestamp": 0, "phase": "IN_PROGRESS",
  "hp": { "1": 40, "2": 37 }, "winnerId": null, "reason": null }
```

- `phase` — one of `WAITING`, `ALIGNING`, `IN_PROGRESS`, `PAUSED`, `ENDED`.
- `winnerId` — `1`, `2`, or `null` while the match has no winner yet.
- `reason` — `null` in normal play; a short reason code when `phase` changes for a notable cause, e.g.
  `"LINK_LOST"` when a player's link drops (§6 of the comms plan) or `"HP_ZERO"` when a match ends.

Sent on every phase transition, on HP change, and as part of the full-state snapshot a visualizer
receives immediately on connect (so a reconnecting client is never left waiting for the next event to
know what's going on).

### 2.6 `COOLDOWN_STATE` — engine → both visualizers

```jsonc
{ "type": "COOLDOWN_STATE", "version": 1, "timestamp": 0, "playerId": 1,
  "cooldowns": [ { "action": "HORIZONTAL_SWING", "remainingMs": 420, "totalMs": 800 } ] }
```

One entry per action currently on cooldown for that player. An action absent from the array is off
cooldown. Cooldowns pause (do not tick through) while `MATCH_STATE.phase == PAUSED` (decision #10).

### 2.7 `COMBAT_FEEDBACK` — engine → relay → FireBeetle

```jsonc
{ "type": "COMBAT_FEEDBACK", "version": 1, "timestamp": 0, "playerId": 2,
  "code": "HIT_TAKEN", "intensityPct": 90, "durationMs": 180, "repeatCount": 1 }
```

This is the JSON form the engine sends to the relay over WSS. The relay translates it 1:1 into the
binary `CG_PKT_COMBAT_FEEDBACK` packet (`wire_protocol.md` §3) for the FireBeetle — `code` maps to
`feedbackCode` via `FeedbackCode` in `enums.yaml`. It is not part of `shared/schemas/` because it never
reaches the visualizer or Ultra96; only the relay's internal WSS client and `mock/visualizer_sim.py`'s
counterpart, `mock/firebeetle_sim.py`, need to agree on it, and they do so against this document plus
`enums.yaml`.

Emitted in pairs by the resolver: `HIT_LANDED` to the attacker's wrist, and exactly one of
`HIT_TAKEN` / `BLOCKED` / `PARRIED` to the defender's, matching the resolved outcome.

---

## 3. Ultra96 classifier protocol (relay ↔ Ultra96)

Line-delimited JSON (`\n`-terminated, UTF-8) over a single long-lived TCP connection held open for the
match (comms plan §8, decision #2 default). Every request and response carries a `requestId`
(monotonic `uint32`, assigned by the relay, echoed by the Ultra96 server) so a response can be
correlated to its request when one is dropped, and so per-request latency can be measured for report
§5.5.1 (decision #1 — raised with the AI side in Week 4; treated as required by default).

### 3.1 `SENSOR_WINDOW` — relay → Ultra96

```jsonc
{ "type": "SENSOR_WINDOW", "version": 1, "timestamp": 0, "playerId": 1, "requestId": 42,
  "weapon": "SWORD", "windowStartTs": 0, "windowEndTs": 800, "sampleRateHz": 50,
  "dominant": [ [0.01, 0.02, 0.98, 1.1, -0.4, 0.2], /* … windowSamples rows … */ ],
  "offhand":  [ [0.00, 0.01, 0.99, 0.0,  0.1, 0.0], /* … windowSamples rows … */ ],
  "stalePct": 0.0 }
```

- `windowStartTs`/`windowEndTs` — engine-time ms bounds of the window.
- `dominant`/`offhand` — each a list of `windowSamples` rows (relay `windowing.py`
  **[config]** `windowSamples: 40`), each row `[accelX, accelY, accelZ, gyroX, gyroY, gyroZ]` as
  floats, converted from the wire's fixed-point `int16` per `wire_protocol.md` §3.2. Row count is
  **not** fixed at the schema level because it is dictated by the trained model's input shape
  (COMMS_IMPLEMENTATION_PLAN.md §16.2) and may change without a protocol version bump.
- `weapon` — never `"NONE"`; the relay withholds windows until a weapon is confirmed (§7 of the comms
  plan).
- `stalePct` — percentage of samples in this window flagged `DOM_STALE`/`OFF_STALE` on the wire. The
  relay does not emit a window at all if this exceeds `maxStalePct` **[config]**; the field is
  included for the Ultra96's own logging, not as a signal it needs to act on.

### 3.2 `ACTION_RESULT` — Ultra96 → relay

```jsonc
{ "type": "ACTION_RESULT", "version": 1, "timestamp": 0, "playerId": 1, "requestId": 42,
  "action": "HORIZONTAL_SWING", "actionConfidence": 0.94,
  "guardState": "IDLE", "guardConfidence": 0.88 }
```

Both classifier outputs — action and guard — in one payload (report §4.5). The relay parses this
behind an adapter that tolerates both a single class and a probability vector, and ignores unknown
keys (§16.3 rule 3) — the AI side's real output shape is expected to change this message the most of
anything in `shared/` (§16.2).

The relay discards any `ACTION_RESULT` whose `actionConfidence < min_confidence` (`shared/config/
match.yaml`) before it reaches the engine, **except** `action: "IDLE"`, which always passes through
since it is what clears the avatar's animation state (comms plan §8).
