# 2 players

Firmware for the six ESP32 boards (three per player), plus a laptop-side
listener for their debug and result output.

| Board               | Player 1                              | Player 2                              |
|---------------------|---------------------------------------|---------------------------------------|
| Right wrist (hub)   | `p1_right_wrist/p1_right_wrist.ino`   | `p2_right_wrist/p2_right_wrist.ino`   |
| Torso               | `p1_torso/p1_torso.ino`               | `p2_torso/p2_torso.ino`               |
| Left wrist          | `p1_left_wrist/p1_left_wrist.ino`     | `p2_left_wrist/p2_left_wrist.ino`     |

Open a board's folder in the Arduino IDE (board: ESP32 Dev Module) and upload.
The player 1 and player 2 sketches are identical except for `PLAYER_ID`, so any
change to one must be made to both. Label the boards, because a player 2 board
flashed with a player 1 sketch pairs with the wrong player.

## How it works

1. **Waiting.** Each board calibrates its gyro at boot, so keep it still.
   The LEDs blink amber until the hub hears fresh poses from both the torso
   and the left wrist.
2. **Weapon detection.** The hub (right wrist) compares both wrists' gravity
   directions against a reference stance for each weapon (sword & shield,
   spear, mace). Holding one stance steadily for 3 s locks that weapon. The
   LEDs are green while sampling and blue while a stance is being held.
3. **Locked.** The weapon stays fixed until reboot, and the LEDs show its
   colour (cyan, magenta or orange). The hub splits its own motion into
   candidate actions, and each candidate into strokes. A stroke is one
   coherent rotation, measured relative to its own peak, so a slow swing is
   found the same way as a fast one.

   Each stroke is compared with a strike template for every action. A
   template records which way the arm rotates, which way the hand is pushed,
   and how much the torso turns with it. The torso's beacons supply the torso
   data. Recorded wrist rotation angles are kept for diagnostics and fitting
   metadata, but are not used as angle limits or matching penalties.

   A thrust barely rotates, so it's found as a push stroke instead: a burst
   of linear acceleration, with gravity tracked by the gyro. It's compared
   with a thrust template on two measures: push direction and how far the hand
   travels before it stops. A push is
   rejected if it is the lead-in to a swing in the same candidate.

   - **Accepted:** the stroke is close enough to its nearest template
     (the locked weapon's `ACTION_LIMITS` distance, with the sword/shield live-play
     tolerances below) and isn't the return to guard after the strike
     before it. It gives a short buzz and a green flash, and the hub asks the
     torso for the same time window of its samples.
   - **Rejected:** a red flash.

   None of this depends on how fast the swing is. Every stroke is logged as
   a `Stroke ...` debug line with its features and decision.

   The templates are fitted from recordings by `fit_strike_model.py`, which
   writes weapon-specific strike, thrust, and limit tables into both hub
   sketches. The current rows were fitted from the 76 labeled player 1 debug
   sessions, including five explicitly attested grip overrides.
   Refit after changing how a board is strapped on.
   `speed_study.py` checks a model offline before you flash it.

   The sword/shield hubs also apply narrow tolerances from the 2026-10-19
   labeled player 1 captures. Horizontal and diagonal strikes allow distance
   1.75 instead of 1.50. A missed uppercut may use a 35-degree axis spread
   and distance 2.25 only when it peaks at least 250 dps and has torso ratio
   at least 0.070. Thrust's distance limit is 1.50 for pushes peaking
   at least 1.50 g, and 1.10 for weaker pushes (the fitted limit is 1.00).
   These adjustments are outside the
   generated model block and are applied only to sword/shield. The fitter's
   printed limits describe the generated baseline; check these live-play
   allowances with fresh labeled captures after flashing.

   Player 2 also accepts a second sword/shield uppercut wrist orientation seen
   in the 2026-10-21 `uppercut` capture. It requires peak speed of at least
   350 dps, torso participation, and a close
   match to that capture's axis and push direction. The same conditional code
   is in both hub sketches, but activates only for `PLAYER_ID=2`. Recheck
   uppercuts and accidental accepts on the physical boards after flashing.

   Both hubs completely ignore wrist rotation angle when deciding validity.
   There is no minimum stroke angle, uppercut angle gate, spin angle gate,
   thrust rotation penalty, or extra reach requirement for a twisting thrust.
   The logged `rot` measurements remain available, and `dRot` is always zero.
   Uppercuts retain an axis spread of at least 35 degrees, including the
   alternate player 2 grip. Speed and torso gates still apply, as do direction
   matching, minimum thrust reach, and duplicate/return suppression.

   Spins have no minimum or maximum wrist rotation angle. Matching allows at least a
   45-degree axis spread, a 60-degree push-direction spread, and distance 2.25.
   A matched spin is exempt from return-to-guard suppression, including after
   a non-spin strike. Torso data is still required. These allowances favour
   fewer missed attacks and may admit more handling movements; validate with
   multiple players and the non-action recordings below. Wrist angular speed
   still opens candidates and locates rotation strokes; removing angle gates
   does not remove the existing motion-strength thresholds.

   To suppress flicks without restoring angle limits, both hubs also use the
   identical action quality checks embedded directly in each wrist sketch.
   Each sketch can be copied into the Arduino IDE without a separate quality
   header. These are conservative
   starting thresholds and need testing with several players:

   - Every rotation stroke must last at least 100 ms, measured between its
     extracted endpoints (not the candidate's pre-trigger or settling window).
     A thrust uses its push-to-stop duration, also at least 100 ms.
   - Gravity-removed linear acceleration must stay at or above 0.15 g for at
     least 40 ms continuously. Rotation strokes use the whole stroke; thrusts
     use the accelerating push portion, excluding the stopping tail.
   - A matched spin additionally needs torso beacon speed at or above 35 dps
     for at least 100 ms continuously inside the stroke itself. Ordinary cuts
     and thrusts do not acquire this new torso-motion requirement.
   - Below-threshold readings reset each active run. Wrist gaps over 30 ms
     and torso beacon gaps over 45 ms also reset it, so long missing intervals
     cannot be mistaken for sustained activity.

   Debug lines include `dur`, `linearMs`, and (for rotation strokes)
   `torsoActiveMs`. New rejection reasons are `too_short`, `weak_hand`, and
   `spin_torso_quiet`. Tune the constants in each sketch's `ActionQuality` block from valid
   attacks, flicks, and handling captures. Direction matching and the other
   existing rejection checks still apply; these checks cannot identify every
   handling movement. Synthetic boundary, spike, missing-data, and rollover
   checks live in [the C++ regression test](../tests/action_quality_compile_test.cpp).
   Run [its PowerShell runner](../tests/action_quality_compile_test.ps1) with
   `-Compiler <path-to-g++>` to test the actual inline checks from both sketches.

   Spin templates compete only when the stroke meets the spin-specific torso
   requirement. A quiet torso must not cause an uppercut to be rejected merely
   because the broad spin template had a smaller distance. Both sword/shield
   uppercut grips are compared on every stroke, even when the initial match is
   already within its limit; a closer eligible uppercut can replace that match.
   The 2026-11-01 player 2 uppercut capture exposed this selection interaction.
   No motion thresholds or wrist-angle limits were changed for this fix.

   These runtime allowances are outside the generated tables. Offline fit and
   replay results do not validate them unless the same policies are implemented
   in those tools; confirm behaviour on the boards after flashing.

## Recording and fitting weapon-specific actions

Use the same sensor mounting for both players. Record one player at a time so
each session has a unique timestamp. The fitter reads the hub's `Stroke` debug
lines, and checks each session's filename weapon against its periodic
`Active weapon=<name>` line. The lock line is now available with `RAW_STREAM 0`;
raw samples are optional diagnostics and are not used for fitting.

For each of `sword_shield`, `spear`, and `mace`, record at least eight clear
repetitions of each of the six actions at slow, normal, and fast speeds. Record one session
each of `fake_flick`, `fake_handling`, `fake_walking`,
`fake_body_turn`, and `guard_raise`. For example:

```
python listener_2p.py --players 1 --label vertical_slow_spear
python listener_2p.py --players 1 --label fake_handling_spear
```

The fitter reports usable counts and stops if any action and speed group has
fewer than eight. The debug log must contain all `Stroke` lines and the closing
`Candidate ENDED` line for a thrust candidate. Inspect the printed training
comparison, especially thrust misses and non-action accepts, before using
`--write`.

The 2026-10-13 training check found mace thrust at 5/10 slow, 6/10 normal,
and 13/22 fast with the selected limit. Raising that limit admitted more
recorded non-actions, so test mace thrust and handling carefully after flashing.
Some sword/shield diagonals were accepted as another strike type; verify action
identity as well as detection in live play.

The hub keeps its weapon lock until reboot. Reboot it before changing weapon,
hold the new weapon's ready stance until it locks, and confirm the matching
`Active weapon=<name>` line before starting each labeled capture. Five
2026-10-13 spear/mace top-ups logged `Active weapon=sword_shield`; the recorder
confirmed the physical spear/mace grips. `logs/p1/weapon_grip_overrides.csv`
records those specific attestations. The fitter checks both labels and the
logged lock, retains the logged lock for diagnostics, and uses the attested
physical grip for fitting. Any other mismatch still stops fitting.

From the repository root, fit and inspect the generated model before writing:

```
python fit_strike_model.py --logs "2 players/logs/p1"
python fit_strike_model.py --logs "2 players/logs/p1" --write
```

The fitter excludes older sessions with no weapon label. The optional `_train`
suffix is equivalent to omitting a split. You can add `_val` sessions later
for a separate offline check, but they are not needed to fit the model. It fails
if a labeled session lacks a locked-weapon debug line, if the labels conflict
without a specific attestation, or if any group lacks enough usable strokes.
When `_val` recordings are supplied, it reports held-out detection and
non-action accepts against the model currently in the hub sketch. Otherwise,
flash both hubs after fitting and check detection and accidental accepts in
live play.

The hub broadcasts the current phase every 100 ms, so a board that reboots
rejoins on its own.

## Network

All six boards join the laptop hotspot set by `ssid` / `password` at the top of
each sketch. Windows Mobile Hotspot allows at most 8 clients, which leaves 2
spare. Each player's boards use their own block of UDP broadcast ports, so the
two players never process each other's packets. The `playerId` in every packet
header is checked as a second guard.

| Port   | Player 1 | Player 2 | Carries                                          |
|--------|----------|----------|--------------------------------------------------|
| LINK   | 4210     | 4220     | torso beacons, segment requests/acks (after lock) |
| DEBUG  | 4211     | 4221     | debug text -> laptop                             |
| POSE   | 4212     | 4222     | wrist/torso poses, hub phase status              |
| RESULT | 4213     | 4223     | milestone lines -> laptop                        |

## Build options

These are set at the top of each sketch.

| Option                    | Default | Effect                                                                 |
|---------------------------|---------|------------------------------------------------------------------------|
| `PLAYER_ID`               | 1 / 2   | Which player the board belongs to                                      |
| `WIFI_SLEEP_DISABLED`     | 1       | Turns modem sleep off for lower, steadier latency                      |
| `LATENCY_TEST`            | 0       | Hub pings the torso and logs RTT and beacon inter-arrival times. Set it on both the hub and the torso |
| `LOCAL_FEEDBACK_TRIGGERS` | 1       | Hub drives its own LED/buzz feedback. Set to 0 when the laptop drives it |
| `RAW_STREAM`              | 0       | Hub and torso stream every IMU sample on port 4214/4224, for recordings. The listener writes it to `raw_<time>_<label>.csv`. Set it on both the hub and the torso |

## Listening on the laptop

```
python listener_2p.py --label <label>
```

This prints every line tagged `P1`/`P2`, with a `*` marking result lines, and
writes per-player CSVs:

```
logs/p1/debug_<time>_<label>.csv   logs/p1/result_<time>_<label>.csv
logs/p2/debug_<time>_<label>.csv   logs/p2/result_<time>_<label>.csv
logs/pN/raw_<time>_<label>.csv      (RAW_STREAM builds: one row per IMU sample)
```

For speed recordings, use labels like `vertical_slow`, `vertical_normal` and
`vertical_fast`. The `logs/` folder isn't in git, so back it up.

Use `--players 2` to listen to one player only. Use `--results-only` to hide
debug lines on screen; they are still logged.
