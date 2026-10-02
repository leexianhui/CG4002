# Comms demo runbook

How to bring the demo up on one laptop, what to look at while it runs, and how to tell a real
failure from a healthy-but-quiet rig. Scope is the 3-week Communications demo: `firmware_demo/`
(ESP32 over BLE) + `comms/` (relay, engine, mocks) + `visualizer_demo/` (Unity).

Bring it up in three stages, each of which narrows down where a failure can be:

| Stage | What runs where | Command | Proves |
|---|---|---|---|
| 1. mock | everything on the laptop, sim device | `.\scripts\run_demo.ps1 mock` | the whole chain and all the application logic |
| 2. ble | everything on the laptop, real FireBeetle | `.\scripts\run_demo.ps1 ble` | the radio, the firmware, the PSK (§6) |
| 3. split | classifier + engine on the Ultra96 | by hand, see §7 | the network, the TLS SANs, the timeouts |

Stages 1 and 2 both pass on real hardware. Stage 2's last clean run: `timesync_seeded`, 609 windows
→ 609 classifications 1:1, HP 100→0, `phase=ENDED winner=1`, zero warnings or errors across 1220 log
events. Only go to stage 3 after stage 2 does, so that anything that breaks there can only be the
network.

Stage 3 cannot be driven from this laptop -- the company network policy blocks the board, its hostname
and the `stujump` jump host alike. It runs from the second laptop; §7.0 covers moving the code there.

`comms/README.md` covers install and the raw CLI flags. This file covers the order to do things in
and how to read the result.

## 0. One-time check

```bash
cd comms
.venv/Scripts/python.exe -m pytest -q       # 165 passed
```

If this fails, fix it before touching hardware: `tests/test_e2e_mock.py` drives the same relay,
engine, and resolver code the demo does, over an in-process transport. A red suite here means the
problem is in the stack, not in your cabling.

## 1. Bring the rig up

```powershell
.\scripts\run_demo.ps1 mock         # software device, no ESP32 needed
.\scripts\run_demo.ps1 ble          # real ESP32 over BLE
.\scripts\run_demo.ps1 mock full    # every log line on the console, for debugging
```

(From Git Bash it is `scripts/run_demo.sh mock` / `... mock full`. PowerShell has no handler for
`.sh` and running one produces no output, no error and no exit code, which is why the `.ps1` wrapper
exists.)

**The console is filtered by default**, because the unfiltered rig emits ~40 lines a second: at 50 Hz
with `stride_frames: 5` roughly ten windows complete per second, and each one logs `window_ready` +
`ultra96_sim_classified` + `action_classified` + `action_not_resolved`. The two lines that matter --
the hit and the HP the phones received -- scroll past unread. Default (`demo`) keeps startup,
`timesync_seeded`, every `combat_resolved`, every `visualizer_sim_match_state`, and everything at
WARNING or above; a full match is about 50 lines you can actually read. `full` restores the rest.

This filters the console **only**. `logs/*.jsonl` always records every event at INFO -- a filtered
49-line run still wrote all 63 `action_not_resolved` entries to disk. Use §4 to read them back.

One terminal, every component prefixed (`[engine]`, `[relay]`, `[viz2]`, ...), Ctrl-C stops all of
it. Prefer this to five hand-started terminals, because **start order is load-bearing**: `cg-relay`
opens its Ultra96 and engine connections during startup, before it ever talks to the device. Start
it first and it dies with `[WinError 1225] The remote computer refused the network connection`, then
retries on a backoff that grows to 10 s -- which at demo time reads as "the system is hung" rather
than "I started things in the wrong order". The script encodes engine -> ultra96 -> device -> relay
-> visualizers.

To run the pieces by hand anyway, use that order, and see `comms/README.md` for the flags.

## 2. What a healthy run looks like

**This is the demo rig (`trigger_mode: true`, `configs/relay.p1.yaml`), not the mock/e2e-test rig.**
The button wiring is:

| Button | GPIO | Trigger id | Effect |
|---|---|---|---|
| Weapon | 4 | — | Cycles SWORD → SPEAR → MACE. Confirm in the *relay* log (`weapon_confirmed`), not on the phone — nothing is broadcast to a visualizer for this press |
| Action 1 | 25 | 1 | One tagged 40-frame capture → one `SENSOR_WINDOW` → one classification |
| Action 2 | 26 | 2 | Same, trigger id 2 |
| Action 3 (guard) | 27 | 3 | Same, trigger id 3. Still a plain button in firmware — guard is a classifier output, not a firmware decision |

In sequence, per button press, within a couple seconds of the press:

| Where | Line | Means |
|---|---|---|
| `[engine]` | `ws_server_listening` | engine is accepting connections (once, at startup) |
| `[engine]` | `ws_client_connected path=/relay/1` | relay authenticated (bearer token matches, once at startup) |
| `[relay]` | `timesync_seeded offset_ms=...` | **the protocol works** -- HELLO/HELLO_ACK + 8 TIME_SYNC exchanges all decoded, so framing, AES-GCM, and CRC are all correct end to end (once, at startup) |
| `[relay]` | `window_ready request_id=N weapon=SWORD trigger_id=1` | the one tagged capture from this press passed the staleness/span checks |
| `[ultra96]` | `ultra96_sim_classified action=...` | classifier stand-in answered that window, from `demo_trigger_map.yaml`'s `(weapon, trigger_id)` row |
| `[relay]` | `action_classified action=...` | result cleared the confidence gate, forwarded to the engine |
| `[engine]` | `ACTION_RECOGNIZED` broadcast | engine reports what was recognised, regardless of outcome -- this is the message Unity's feed prints, and it fires even with no P2 and even for IDLE+GUARDING |
| phone HUD | `> P1 used HORIZONTAL_SWING` (or `> P1 used GUARD` for trigger id 3) | the demo's actual proof: one press produced exactly one line |

**One of each per press; silence between presses is healthy.** This replaces the old continuous-mode
expectation below the fold -- under `trigger_mode` there is no windowing at all while idle, so a
quiet relay log between button presses is the *designed* state, not a stall. (The old "several times
a second" description applied to `stride_frames`-driven continuous windowing, which is still the
real-game default but is not how the demo runs.)

`timesync_seeded` is still the single most informative line for protocol-layer health: everything
before it is protocol correctness, everything after is application logic. It fires once at startup,
not per press.

`combat_resolved` / HP movement only happens with a second player position flowing (M4/M5, not in
NOW/NEXT scope) -- a P1-only run never produces it, and that is a PASS, not a failure. See §3 and
`docs/DEMO_TRIGGERED_ACTION_PLAN.md` §15 row 6.

## 3. Failure modes, and what each one actually means

**Nothing after `timesync_seeded`, no `window_ready`.** The stream is flowing but every window is
being discarded. `relay/windowing.py` drops a window silently (returns `None`, no log) in four
cases, and they are the hardest class of bug in this system to see:

- No weapon confirmed. `_current_weapon is None` until an RFID_EVENT arrives, and no window is ever
  emitted before that. **On the ESP32 this means: press the button.** Nothing else produces an
  RFID_EVENT, so an untouched board streams sensor frames forever and produces zero actions -- the
  rig looks perfectly healthy and does nothing. Same trap applies to soak runs.
- Frame rate off nominal. A 40-sample window has a nominal 780 ms span and is dropped if the real
  span deviates >15% (`max_span_deviation_pct`). At 20 ms/frame you have ~117 ms of slack across the
  whole window, so a device streaming at 40 Hz instead of 50 produces nothing at all.
- Too many stale frames (`max_stale_pct`, 20%).
- Fewer than `window_samples` frames buffered -- normal for the first ~0.8 s only.

**`window_ready` but no `action_classified`.** Either `ultra96_timeout` (the sim is wedged or the
120 ms `request_timeout_ms` is too tight) or `action_below_confidence_gate` (below `min_confidence`,
0.6). Both are logged; check which.

**`action_classified` but no `combat_resolved`.** Look for `action_not_resolved` in `[engine]` and
read its `outcome`:

- `IGNORED` **after** a `match_over_actions_ignored` warning -- the match is over, not broken. Every
  action from that point on is refused by the resolver's phase gate. See "Restarting a match".
- `IGNORED` -- most often a **missing or stale POSITION**. The resolver refuses to adjudicate if
  either player's position is absent or older than `position_max_age_ms` (1000 ms). This is why the
  launcher starts *both* visualizer sims: with only player 1 connected, every single attack is
  IGNORED and the demo silently does nothing. It also covers no weapon, an invalid weapon/action
  pair (`shared/config/weapons.yaml`), and confidence below the floor.
- `STATE_ONLY` -- an IDLE classification. Expected; most windows are IDLE.
- `ON_COOLDOWN` -- fired inside the weapon's cooldown. Expected during rapid swings.

**Repeated `event_dropped` for `POSITION`, while Unity shows attacks without damage.**
Read `player_id`, `event_age_ms`, and `reason`. With the default `shared/config/match.yaml` the release hold is
110 ms (`reorder_delay_ms` 60 + `guard_grace_ms` 50); the absolute age limit is 610 ms (110 +
`late_drop_ms` 500). Events can also be rejected
earlier if another event has already advanced the release watermark. Rejected positions stop
refreshing geometry, so attacks become `IGNORED`. `action_not_resolved.reason` now distinguishes
`missing_position` / `stale_position` from other causes, and `position_age_ms` gives both players'
ages at attack time (`null` means no accepted position). Unity can still show ACTION_RECOGNIZED.

The old Unity sender used UTC milliseconds with a fixed first-arrival engine offset. A phone
clock adjustment could therefore cause persistent drops until reconnect. Rebuild both Unity
clients with the monotonic Stopwatch sender and keep both visualizers connected and foregrounded.
To recover an existing session, reconnect the affected visualizer to reseed its position offset;
a match reset alone deliberately preserves clock offsets. If drops return with the updated
clients, investigate sustained Wi-Fi/tunnel delay or clock drift. Sporadic drops can be jitter;
a sustained offset produces a warning on every 0.5-second position send in older engines.
Updated engines log position drops at most once per player per five seconds, with suppressed and
total drop counts. Timing rejection and geometry freshness limits remain enforced.

**Relay reconnect loop with `WinError 1225`.** Something it dials isn't up: engine (8443) or Ultra96
(8888). Check start order.

**`ws_auth_rejected`.** Bearer token mismatch between `.env.engine`'s `CG_ENGINE_BEARER_TOKEN` and
`.env.relay`'s `CG_RELAY_ENGINE_LINK__BEARER_TOKEN` (or the Unity client's field).

**`stream_decode_drop` / `handshake_decode_drop`.** The PSK differs between the two ends, or the
firmware's protocol port has drifted. `cg_config.h`'s `CG_DEMO_PSK_HEX` must equal `.env.relay`'s
`CG_RELAY_PSK_HEX`. On BLE this is the first thing to check.

**`heartbeat_stale` then `link_lost`.** No packet from the device for 3 s. On BLE: board reset, out
of range, or the connection dropped. The relay reconnects on its own.

**Demo-specific (trigger mode) failures** -- see `docs/DEMO_TRIGGERED_ACTION_PLAN.md` §15 for the
full decoder; the ones most likely to bite on demo day:

- **Weapon press prints nothing on the phone.** Correct behaviour, not a bug -- `WEAPON_CHANGED` is
  relay→engine only. Confirm `weapon_confirmed` in the *relay* log instead. The phone's feed only
  shows the weapon on the *next* action, as `> P1 weapon = SWORD` printed immediately before that
  action's line.
- **Every button works except guard (trigger id 3).** If `ACTION_RECOGNIZED` never follows a
  `guard`-classified window, the engine's emit rule was implemented as `action != IDLE` instead of
  `action != IDLE OR guard_state != IDLE` -- guard is `action: IDLE` + `guard_state: GUARDING`, so the
  narrower rule silently drops it while every log upstream looks perfect.
- **One press produces multiple actions.** (a) `trigger_mode: false` on the laptop → continuous
  windowing, `window_ready` every ~100 ms; (b) no firmware debounce → two capture lines on the serial
  monitor for one press, raise `CG_DEMO_BUTTON_DEBOUNCE_MS`; (c) back-to-back windows exactly
  `window_samples` apart → the refractory reset is missing, the capture re-armed while still tagged.
- **`targetId` is non-null with no P2 connected.** Wrong: that field must be gated on relay presence
  (`/relay/<id>`), never copied from the resolver's `defender_id` (which is populated even with no
  P2). Expect `targetId: null` whenever `MATCH_STATE.connected["2"] == false`.
- **P2 shows HP 100 instead of `OFFLINE`.** The engine always carries both HP keys by design (no P2
  connection needed for `MATCH_STATE.hp` to exist) -- the HUD must read `connected`, not infer
  presence from HP.
- **Phone shows `engine: connect_failed: ...`.** Tunnel or TLS. Check the phone's own SSH tunnel
  first (§7/§8 below), then a cert error specifically (§8's socat fallback), then the bearer token in
  the Unity Inspector against `.env.engine`.

## 4. Reading the logs

Console output is a human-readable mirror; the full record is JSONL in `comms/logs/`, one file per
process per run (`relay-p1-<epoch>.jsonl`). Both carry the same events.

```bash
# newest relay log, event histogram -- the fastest way to see what a run actually did
ls -t logs/relay-p1-*.jsonl | head -1 | xargs -I{} python -c "
import json,collections
c=collections.Counter(json.loads(l).get('event') for l in open('{}',encoding='utf-8') if l.strip())
print(*sorted(c.items(), key=lambda kv:-kv[1]), sep='\n')"

# built-in summary (drop reasons, heartbeat gaps)
.venv/Scripts/cg-latency-report.exe logs/relay-p1-<epoch>.jsonl
```

At 50 Hz the relay console scrolls several lines a second. To keep a demo screen readable while
keeping full detail on disk:

```bash
.venv/Scripts/cg-relay.exe --config configs/relay.p1.yaml --no-tunnel --console-level WARNING
```

## 5. Restarting a match

Engine restart creates fresh in-memory gameplay state. Relays and phones reconnect automatically;
presence rebuilds from registrations and subsequent weapon/action/position input populates the match.

To reset without restarting processes or dropping clients, run from `comms/` on the engine host:

```sh
python -m cg_comms.tools.reset_match --config configs/engine.yaml --host localhost
```

Use the existing certificate hostname instead of localhost if necessary; retain the existing
environment/bearer-token/TLS configuration. This uses an authenticated `/control` WebSocket, not an
unauthenticated HTTP endpoint. Expect `match_reset_requested`, then `match_reset`, and a printed
MATCH_STATE with `reason:"reset"`, both HP values restored, and live presence. Both phones receive it.
`match_over_actions_ignored` now points to this command.

See [pre-demo verification and exact recovery logs](../docs/PRE_DEMO_ROBUSTNESS.md#h-physical-demo-verification)
before deploying the paired relay/firmware changes.

## 6. Switching to the real ESP32

1. Confirm `CG_DEMO_PSK_HEX` in `include/cg_config.h` matches `.env.relay`'s `CG_RELAY_PSK_HEX`.
   Flashing with the wrong PSK is the single most common BLE-mode failure and shows up only as
   `handshake_decode_drop`.
2. Pick the env for the board you actually have: `firebeetle32` for a FireBeetle, `esp32dev` for a
   generic DevKitC. They build the same sketch (the demo only uses a plain GPIO button, no
   board-specific peripherals), but flashing with the wrong env can pick wrong flash/PSRAM settings.

   `pio` lives in a project-local venv at `CG4002/pio-venv/` and is **not** on PATH in a fresh
   PowerShell window (`pio : The term 'pio' is not recognized`). Put it there once per window:

   ```powershell
   $env:PATH = "C:\Users\T0332075\Desktop\CG4002\pio-venv\Scripts;$env:PATH"
   ```

   Then from `firmware_demo/` (these are yours to run, not the script's):

   ```bash
   pio device list                                              # confirm the port FIRST
   pio run -e firebeetle32                                      # build only
   pio run -e firebeetle32 -t upload --upload-port COM8         # flash
   pio device monitor -p COM8 -b 115200                         # serial log
   ```

   Run `pio device list` before assuming a port. The FireBeetle appears as `USB-SERIAL CH340`; COM5
   and COM6 on this laptop are Bluetooth serial and are not the board. Naming a port that does not
   exist fails with `could not open port`, which reads like a broken board rather than a typo.

   Keep the monitor open in its own terminal during the demo: it is the only view of the device
   side. `[tx] sensor_frames=50/s` confirms the 50 Hz stream, `[weapon] now 1` confirms the button.
   Note the monitor holds COM8 exclusively -- close it before re-flashing or the upload fails on a
   busy port.
3. Wire the weapon button: one leg to **GPIO 4** (`CG_DEMO_WEAPON_BUTTON_PIN`), the other to GND.
   `INPUT_PULLUP` means a press reads LOW, so no external resistor is needed. Without this button
   there is no way to produce an RFID_EVENT, and with no weapon confirmed the relay emits zero
   windows -- momentarily shorting GPIO 4 to GND with a jumper works if you have no button.
4. Find the board's BLE MAC and set `configs/relay.p1.yaml`'s `transport.ble_address` (the MAC is
   not the COM port and is not printed on the board):

   ```bash
   cd comms && .venv/Scripts/python.exe scripts/ble_scan.py
   ```

   If the scan finds nothing while the board is clearly running, something is still connected to it
   (a previous `cg-relay`, or Windows' own Bluetooth settings page) -- a connected ESP32 stops
   advertising and becomes invisible to a scan.
5. `scripts/run_demo.sh ble`, then **press the weapon button** -- no button press, no RFID_EVENT, no
   windows, no combat.
6. Watch for `timesync_seeded`. If it doesn't appear, the problem is PSK or protocol, not BLE.

The firmware ACKs COMBAT_FEEDBACK (`drain_incoming_non_blocking` in `src/main.cpp`), which it must:
that packet is reliable, so a device that only logs it makes the relay retransmit each hit five
times and then log `link_degraded` + `combat_feedback_undelivered` at ERROR while the link is
actually fine. If you ever see that pair in BLE mode, check that handler first -- it is a device
bug, not a radio problem.

`firmware_demo/` has no equivalent of the Windows timer-resolution problem
(`cg_comms/timer_resolution.py`): FreeRTOS ticks at 1 ms, so the ESP32's 50 Hz loop is accurate and
the windowing span check passes. That failure mode is specific to the laptop-side sims.

`configs/relay.p1.mock.yaml` is the same config with `transport.kind: tcp`, used by `mock` mode.

## 7. Moving the classifier and engine onto the Ultra96

Nothing in the relay changes for this -- it dials whatever `ultra96.host` and `engine_link.host` say,
so the port is a config edit plus a cert regen. Do it only after `ble` mode passes on the laptop, so
that a failure here can only be the network.

The board, as provisioned: `172.26.191.151` (eth0, `/23`), hostname
`makerslab-fpga-53.ddns.comp.nus.edu.sg`, user `xilinx`, reached via
`-J e1138443@stujump.comp.nus.edu.sg`.

**This laptop cannot reach it, and there are two independent reasons.** Re-check any time the network
changes:

```powershell
.\scripts\check_ultra96_reach.ps1
```

That walks the whole ladder -- Wi-Fi address, internet, DNS, board SSH, jump host SSH, and a
`github.com:22` control -- and names the first rung that fails, because "timeout" has four causes here
with four different fixes.

Current result, VPN off, on school Wi-Fi (`172.25.99.112`):

| Check | Result |
|---|---|
| NUS Wi-Fi address, internet, DNS | pass -- `172.26.191.151` resolves fine |
| `172.26.191.151:22` | fail |
| `stujump.comp.nus.edu.sg:22` | fail |
| `github.com:22` (control) | **fail** |

The control is the informative one. Port 22 is blocked to *every* host, including GitHub, so this is
**Zscaler on this laptop** (`ZSATunnel` runs a PAC server on `127.0.0.1:9000`), not an NUS policy.
Zscaler intercepts below the VPN, which is why turning the VPN off changed nothing -- and its PAC
sends all RFC1918 destinations `DIRECT`, so it is not even proxying the board, just dropping non-web
ports. Only IT can exempt a destination.

Independently, NUS does not route the Wi-Fi subnet to the lab subnet: `tracert` to the board dies two
hops in with `172.18.176.126 reports: Destination net unreachable`. The board's DDNS name resolves to
a **private** address, so it is not reachable from the internet either. Both problems would have to be
solved to work from here, and one of them needs corporate IT.

So: development and stages 1--2 on this laptop, deployment from the second laptop. §7.0 moves the code.

### 7.0 Getting the code onto the deployment laptop

**Preferred: GitHub.** This laptop develops and pushes; the laptop that can reach the Ultra96 pulls.

Set it up once. **From PowerShell use the `.ps1`**, not the `.sh`:

```powershell
.\scripts\git_bootstrap.ps1 "Your Name" "you@personal.com" your-github-username
.\scripts\git_precommit_check.ps1
```

```bash
# from Git Bash instead:
scripts/git_bootstrap.sh "Your Name" "you@personal.com" your-github-username
scripts/git_precommit_check.sh
```

Typing `scripts/git_bootstrap.sh` at a **PowerShell** prompt does nothing whatsoever -- no output, no
error, no exit code, and no repo. PowerShell has no `.sh` handler, so it hands the file to the Windows
file association, which silently does nothing and returns success. For the secret check that silence is
the dangerous case: it looks exactly like "clean, nothing found" when in fact nothing ran. Verify with
`git config --local --list` (or `ls .git`) if unsure -- if that errors, the bootstrap did not run.

Then the loop is:

```powershell
# here, after editing
.\scripts\git_precommit_check.ps1     # stop if this exits non-zero
git add -A; git commit -m "..."; git push
```

```bash
# on the other laptop / on the board
git pull
```

Three things about this machine make `git init` insufficient on its own, all handled by the bootstrap
script:

- The **global git identity here is the work Thales address.** `git init` inherits it, so the first
  commit would be authored by an employer address on a personal public repo -- and commit authorship
  is permanent in the object history. The script sets `user.email` with `--local` only, and refuses a
  `@thalesgroup.com` / `@gemalto.com` address outright.
- **Zscaler intercepts TLS**, so git's bundled OpenSSL rejects GitHub with `SSL certificate problem:
  unable to get local issuer certificate`. The fix is `http.sslBackend=schannel`, which uses the
  Windows cert store -- Windows *does* trust Zscaler's root CA. Never "fix" it with
  `http.sslVerify=false`; that turns off certificate checking for every fetch and push.
- **SSH remotes cannot work here** -- port 22 is blocked outbound to every host (§7.0 above). So the
  remote is HTTPS and the credential is a fine-grained personal access token, not a key.

Run `scripts/git_precommit_check.sh` before the first commit especially. Pushing a secret to GitHub is
not undoable: deleting the file later leaves the value in the history, and force-pushing leaves it
reachable through the reflog and any fork or cached view GitHub already made. The only real remedy is
rotating the PSK -- which also means re-flashing the FireBeetle. The check inspects what git *would*
commit (tracked plus untracked-not-ignored), because `.gitignore` only governs files git does not
already track: anything added before a rule existed stays tracked and keeps being committed silently.

`.gitignore` was verified rule-by-rule for this: `pio-venv/`, `lecture_slides/`, `comms/.venv/`,
`logs/`, `certs/`, `.env`, `.env.relay`, `.env.engine`, `firmware_demo/.pio/` and the caches are all
excluded, while `src/`, `shared/config/`, `.env.example` and the docs are all included. That is what
takes ~440 MB on disk down to a small repo.

**Fallback: a tarball**, if GitHub is unavailable or you want an offline copy:

```bash
comms/scripts/make_transfer_bundle.sh          # -> ../cg4002-bundle-<date>.tar.gz, ~170 KB
```

Send it however is convenient and follow the instructions it prints. Do not zip the folder by hand:
`comms/.env*` and `comms/certs/*.key` sit inside the tree that a right-click "Compressed folder" would
swallow, and a chat app keeps a copy of whatever you attach regardless of what you delete afterwards.
The script excludes them, then greps the finished archive to prove they are absent.

Either way it is the **repo root** that travels, not `comms/`. `cg_comms` finds its config by walking
up for a directory containing both `comms/` and `shared/` (`config.py:15`), and the engine loads
`shared/config/weapons.yaml` and `match.yaml` at startup, so a `comms/`-only copy dies with
`could not locate repo root` -- an error a long way from its cause.

Secrets never travel by either route; they are recreated on the far side. The cert has to be
regenerated with the board's IP in its SANs anyway (step 2), and `.env.engine` is a single line you
type into the SSH session. Read the token out of this laptop's existing `.env.relay` rather than
generating a new one -- the relay and engine must agree.

1. **Check the interpreter first.** Over SSH:

   ```bash
   python3 --version        # must be >= 3.10; the PYNQ image ships 3.10.4
   ```

   `requires-python` is `>=3.10` specifically so this step passes on the stock board image. Do not
   try to install a newer CPython on the Ultra96 to match your laptop: `pynq` itself is bound to the
   system 3.10 site-packages, and on aarch64 a from-source build of `numpy`/`cryptography` for a
   non-standard interpreter is a long, failure-prone compile. The interpreter versions on the two
   ends do not need to match -- the contract between them is the wire protocol, not the runtime.

   `cg_comms` does use PEP 604 unions (`int | None`) in pydantic model fields, which are evaluated at
   runtime when the model is built rather than only by the type checker. Those are fine on 3.10; it
   is 3.9 and older where the package would import and then die at *startup* with a `TypeError`
   inside pydantic -- a failure that looks like a broken install rather than a wrong Python.

2. **Regenerate the cert with the Ultra96's IP in the SANs.** This is the step people lose an hour
   to. The current cert has SANs `127.0.0.1`, `localhost`, `cg4002-engine` only -- fine while
   everything was loopback. The moment the relay dials the engine at, say, `192.168.1.50`, the TLS
   handshake fails with `certificate is not valid for '192.168.1.50'`, because modern clients ignore
   CN and require a matching SAN.

   Generate it **on the board**, so the private key never travels:

   ```bash
   cd comms && scripts/gen_certs.sh ./certs 365 172.26.191.151
   ```

   Copy only the new `certs/engine.crt` back to each laptop as its `engine_link.ca_cert_path`. The
   engine keeps `engine.key` and it stays on the board -- never in an archive, a chat app, or git.

3. **Point the relay at the board.** In `configs/relay.p1.yaml`, replace both loopback addresses --
   they are commented `LOCAL LOOPBACK TEST ONLY` precisely so this step is findable:

   ```yaml
   ultra96:
     host: "172.26.191.151"   # was 127.0.0.1
     use_mock: false          # only gates the SSH tunnel, not the protocol
   engine_link:
     host: "172.26.191.151"   # was 127.0.0.1
   ```

   `use_mock` gates one thing only (`relay/app.py:49`, whether to open the SSH tunnel). The wire
   protocol, framing, and timeouts are identical either way, which is why the laptop-only run is a
   real test of the same code rather than a special mock path.

4. **Install on the board**, once, in a venv beside the extracted tree:

   ```bash
   cd ~/cg4002/comms
   python3 -m venv .venv
   .venv/bin/pip install -e .
   ```

   `bleak` will be installed and never imported -- BLE is the laptop-side device transport only
   (`relay/app.py` -> `transport/ble.py`; the engine and the classifier sim import neither). It is a
   hard dependency because the relay always needs it. If the install fights you on the board, `pip
   install -e . --no-deps` plus the other six by hand is a valid escape hatch.

5. **Start the two remote components on the board, engine first**, with `.env.engine` present there:

   ```bash
   .venv/bin/cg-engine --config configs/engine.yaml
   .venv/bin/cg-ultra96-sim --port 8888 --scenario configs/scenarios/demo_scenario.yaml
   ```

   `engine.yaml` already binds `0.0.0.0`, so it accepts remote connections as-is. Note the paths are
   `.venv/bin/`, not `.venv/Scripts/` -- that is the Linux venv layout. Both processes need to outlive
   your SSH session, so start each under `tmux` (or `nohup ... &`); an SSH drop otherwise kills the
   engine mid-match and the relay reports a link failure that is really your laptop's WiFi.

6. **On the laptop, run the device and relay only.** `run_demo.sh` launches all six locally, so it is
   the wrong tool once two of them are remote; start these by hand:

   ```bash
   .venv/Scripts/cg-relay.exe --config configs/relay.p1.yaml --no-tunnel
   ```

   Plus the two `cg-visualizer-sim` calls from `run_demo.sh` with
   `--url wss://172.26.191.151:8443/...`. Both visualizers, not one: the resolver returns `IGNORED`
   for every attack while either player's POSITION is missing.

7. **Expect `request_timeout_ms` to matter now.** It is `120` ms, chosen against a loopback
   classifier where round trips are sub-millisecond. Over real WiFi to the board that budget is no
   longer generous. If you see `ultra96_timeout` in the relay log while the classifier is plainly
   answering, raise it -- that is a tuning value, not a failure.

Ports to open on the Ultra96: **8443** (engine WSS) and **8888** (classifier). Venue WiFi isolates
clients from each other but whitelists the Ultra96, which is why both services live there rather than
one on each laptop.

### 7.8 Why the Ultra96 can't just expose a public server for this laptop

A reasonable thought, but it does not work, for a reason that is not about effort:

`makerslab-fpga-53.ddns.comp.nus.edu.sg` resolves to `172.26.191.151`, which is **RFC1918 private
space**. There is no public IP anywhere on that board, so there is nothing to point a client at from
outside NUS. "Hosting a server on it" would still be a server on a private address -- the listening
port is not what is missing.

Getting around that would need a **reverse** tunnel: the board dials outward to a public rendezvous
service (ngrok, Cloudflare Tunnel, a Tailscale mesh) and that service accepts the laptop's connection.
Note the direction -- outbound from the board, which is why it sidesteps the missing inbound route.
Don't do it for this project:

- It punches a hole from the public internet into an NUS lab machine you share with the rest of the
  team. Whether that breaches the Makerslab acceptable-use terms is not a question worth discovering
  during a graded demo.
- The demo is graded in the venue, on venue WiFi, where the board *is* whitelisted and directly
  reachable. A tunnel would be scaffolding for a problem that does not exist at demo time -- and one
  more thing to fail in front of your prof.
- Zscaler blocks non-web outbound ports from this laptop anyway, so several of those tunnels would not
  connect from this end even if the board's end were fine.

The actual constraint is narrower than "this laptop can't reach the board": it is that *this laptop
cannot reach it during development*. Deployment is a handful of commands run once from the second
laptop, which has a working path. Optimising that away is not worth the risk.

## 8. Adding the Unity phone

**Superseded 2026-09-19: no laptop relay hop, no LAN IP, no cert regeneration.** An earlier version
of this section said to point `EngineClient.Host` at the laptop's LAN IP and regenerate
`certs/engine.crt` with that IP in its SANs, because the existing cert only covers `127.0.0.1` /
`localhost` / `cg4002-engine`. That guidance is obsolete now that the phone opens its **own** SSH `-J`
tunnel straight to the Ultra96 (the same jump-host path the laptop uses, §7), forwarding the engine's
port to the phone's own loopback:

```bash
# on the PHONE (Termux)
ssh -J <user>@stujump.comp.nus.edu.sg -L 127.0.0.1:8443:127.0.0.1:8443 xilinx@172.26.191.151 -N
```

With that tunnel open, `EngineClient.Host = "127.0.0.1"` is correct, exactly as it is for anything
else running on the same machine as the tunnel's local end. The **existing** cert SANs already cover
this -- no LAN IP is ever involved, and there is no laptop relay hop for the phone to go through.
Never regenerate the cert for this step.

Use the same bearer token as everything else (`.env.engine`'s `CG_ENGINE_BEARER_TOKEN`, pasted into
the Unity Inspector's `BearerToken` field), and copy the engine cert's SHA-256 fingerprint into
`PinnedCertSha256` the first time (`openssl x509 -noout -fingerprint -sha256 -in comms/certs/engine.crt`).

**If Unity/IL2CPP rejects the self-signed cert (TLS handshake fails, not a tunnel problem)** --
terminate TLS on the phone instead of touching the engine or relay's TLS config:

```bash
# on the PHONE, a second Termux session
socat TCP-LISTEN:8080,reuseaddr,fork OPENSSL:127.0.0.1:8443,verify=0
```

Then set `UseTls = false` and `Port = 8080` in the Inspector so Unity talks to the socat sidecar over
plain `ws://127.0.0.1:8080` instead of `wss://127.0.0.1:8443` directly. The engine's listener and the
relay→engine leg are never reconfigured for this -- `use_tls: false` in `engine.yaml` would break the
already-working relay leg, so the fallback only ever happens on the phone.

Run `cg-visualizer-sim` against the engine first (from wherever the tunnel already terminates) to
confirm the engine side is fine before debugging Unity -- it exercises the identical path.
