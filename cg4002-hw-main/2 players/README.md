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
   how far the arm turns, and how much the torso turns with it. The torso's
   beacons supply the torso data.

   A thrust barely rotates, so it's found as a push stroke instead: a burst
   of linear acceleration, with gravity tracked by the gyro. It's compared
   with a thrust template on three measures: push direction, how far the hand
   travels before it stops, and how little the wrist turns. A push is
   rejected if it is the lead-in to a swing in the same candidate.

   - **Accepted:** the stroke is close enough to its nearest template
     (`STRIKE_MAX_DIST`) and isn't the return to guard after the strike
     before it. It gives a short buzz and a green flash, and the hub asks the
     torso for the same time window of its samples.
   - **Rejected:** a red flash.

   None of this depends on how fast the swing is. Every stroke is logged as
   a `Stroke ...` debug line with its features and decision.

   The templates are fitted from recordings by `fit_strike_model.py`, which
   writes the `STRIKE_MODEL` table into both hub sketches. Refit after new
   recordings or after changing how a board is strapped on.
   `speed_study.py` checks a model offline before you flash it.

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
