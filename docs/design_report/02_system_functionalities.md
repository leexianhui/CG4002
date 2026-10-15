# Section 1: System Functionalities

## 1.1 Actors

- **Player 1 / Player 2** — human duelists, each equipped with a wearable sensor node and a physical weapon prop, wearing an AR headset (phone-in-headset mount).
- **Game System** — the combined backend (comms + AI + game logic) that observes both players and adjudicates the duel.

## 1.2 Feature List

1. **Weapon Selection** — a player picks up a physical weapon prop (Sword & Shield, Spear, or Mace); the system identifies the weapon via RFID and switches to the corresponding action classifier and move set.
2. **Real-Time Action Recognition** — the system classifies the player's physical motion into a weapon-appropriate action (e.g. horizontal swing, thrust, block) in real time as the player moves.
3. **Relative Positioning** — the system continuously tracks the distance (and approximate relative position) between the two players.
4. **Combat Resolution** — the system combines the attacking player's action + weapon with the defending player's position, orientation, and block/guard state to determine an outcome: **HIT**, **MISS**, **BLOCK**, or **PARRY**, with an associated damage value on a hit.
5. **Attack Cooldowns** — each weapon action has a predefined cooldown. After an action is successfully executed, that action cannot be used again until its cooldown expires.
6. **Health & Match State Tracking** — each player has a health pool that decreases on confirmed hits; the system tracks and broadcasts match state (ongoing/won/lost).
7. **AR Opponent Visualization** — each player sees, through their headset, a fully animated 3D avatar representing the opponent, positioned and animated consistently with the opponent's real actions and relative position.
8. **Combat Feedback Effects** — hit reactions, block/parry effects, weapon trails, and HP/UI overlays are rendered live to reinforce the outcome of each exchange.
9. **Mock/Simulated Play Mode** — the system can run entirely on simulated sensor/AI data (no physical hardware) for development, testing, and demoing individual subsystems in isolation.
