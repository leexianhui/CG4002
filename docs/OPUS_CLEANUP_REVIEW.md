# Review of Opus's cleanup and fixes

2026-09-28. Reviewed the supplied Opus transcript against the actual working-tree changes on top of
`d3571f7`. The scope is Opus's cleanup, every changed runtime source file, and the affected protocol,
recovery, classifier and Unity paths. This is not a certification that every possible hardware or
network failure has been tested. The user's earlier physical-test write-up remains the hardware
baseline; no new physical pass is claimed here.

## Verdict

Most changes were appropriate, but a review found incomplete fixes that the original four new tests
did not cover. These have now been corrected locally:

1. **Firmware handshake race:** clearing `g_handshake_done` in `onConnect` was not enough. The main
   loop could evaluate a successful old handshake, a callback could clear the flag, and the loop
   could then store its stale `true`. The callback updates and the loop's generation-check/store now
   use the same short FreeRTOS critical section. BLE calls and logging stay outside the lock.
2. **Mock malformed-request retries:** Opus's new semantic checks rejected the first bad echo, but
   decoding had already cached it as a successful reliable packet. Its identical retry was then
   ACKed. Three new tests reproduced this for zero trace, invalid weapon and invalid flags. The mock
   now commits receive/replay state only after application acceptance, matching the firmware policy.
   Rejected weapon/file requests likewise cannot enter the successful-request cache.
3. **Reset command from the wrong directory:** the missing-token check did not cover a nonexistent
   relative config path. That failed before the check with `FileNotFoundError`. The tool now reports
   a concise CLI error for a missing config too; both failure cases have tests.
4. **WebSocket annotations:** switching to `ServerConnection` exposed two optional-request type
   errors. The handler now checks the request before accessing its path/headers. The closing-timeout
   comment also distinguishes a close-handshake budget from a whole reconnection deadline.

The documentation index claimed all historical documents had replacement banners when they did
not. Its wording now refers to the index's replacement table instead.

## Review of the individual changes

| Opus change | Review result |
|---|---|
| Group `ASSESS_WEAPON_REQ` with other assessment requests in `protocol/codec.py` | Correct: matches firmware's single assessment watermark and the relay's serialized operation lock. Feedback remains independent. |
| Accept unknown `ACTION_RESULT` keys | Correct for the documented integration contract. Known fields remain validated; other message models retain their strict extra-key policy. |
| Update the ACTION_RESULT JSON schema | Consistent with the new unknown-key policy. Required fields remain required. |
| Parse classifier lines as bytes | Invalid UTF-8 is handled as a dropped malformed record by the existing parser, instead of escaping as a decode exception. Relay and simulator tests cover continuing with the next valid line. |
| Log classifier correlation mismatch fields | Useful and correct. Added tests confirm player, timestamp and trace mismatches still reject the result even when extra AI fields are present. No correlation checks were loosened. |
| Require `websockets>=14` and use `ServerConnection` | Consistent with the new asyncio API used by the application. Installed 17.0.1 passes the suite. This review does not claim a separate test run on every permitted dependency version. Optional-request annotation issue corrected above. |
| Remove unused NumPy dependency | No NumPy imports were found in comms. This does not remove dependencies from a separate AI project. Installed-environment `pip check` passes. |
| Reduce server `close_timeout` to two seconds | Reasonable bounded close-handshake setting. It is not a guarantee of complete reconnect within two seconds; failure detection, task cleanup, scheduling and backoff also contribute. |
| Normalize Unity heading 360 to zero | Consistent with the engine's required `[0, 360)` heading. Preserves the existing Unity-to-engine coordinate conversion. |
| Catch typed JSON parsing failures in Unity dispatch | Correct: a deserialization `JsonException` no longer aborts that update's remaining message dispatch. It does not claim to catch every possible subscriber/application exception. |
| Restore weapon label from ACTION_RECOGNIZED | Correct fallback after reset. The label recovers on the next recognized action; this does not invent an immediate weapon snapshot at reset. |
| Clear handshake state on connect | Direction correct; required the synchronization correction described above. |
| Withhold ACK if cached assessment response is refused by BLE stack | Correct: retains the already-applied operation and cached reply while allowing the request retry to recover the response. Does not cycle a weapon or append a file chunk again. Stack acceptance is still not proof of radio delivery. |
| Reject malformed mock echoes | Direction correct; required deferred replay-state commit described above. |
| Add Caps Lock hint for A/B/C | Does not change the deliberate lowercase a/b/c/d versus uppercase D/F/L/Q controls. |
| Improve reset tool error | Missing-token branch correct; missing-file case completed in this review. |
| Remove old Unity scaffold | Four obsolete scaffold files were staged for deletion. The active `CG4002VisualizerDemo_UnityTemp/My project` and its script references remain. |
| Add docs index and refresh READMEs | Useful navigation and accurate current phone route. Historical plans remain historical. Index banner claim corrected. |
| Ignore generated files, certificate exports and root scratch log | Does not delete those local files. The remaining `*.log` rule still exists; removal of its duplicate does not unignore logs. `*.cer` is a broad ignore rule, not a secret rotation. |
| Android build profile | Inspected as an Android Unity build-profile asset. It inherits the global scene list/settings and does not by itself encode separate P1/P2 app identity or guarantee a reproducible APK. It remains a user-owned untracked asset. |

## What was preserved

No changes to the keyboard dummy table or hash-based classifier selection, physical GPIO trigger
map, file-transfer wire layout, 1037-byte fixture, throughput calculations, normal 50 Hz scheduling,
100/200 Hz build intervals, classifier/game timing profile, secrets or tunnel endpoints.

The new mock fix affects validation fidelity in software tests; it does not replace the physical
board or route keyboard actions directly to the engine. Existing stream, file, ACK-loss, ordering,
guard, reconnection and end-to-end simulated-pipeline tests remain in the full suite.

The user's existing Unity scene/render/project settings and local `.claude/settings.local.json`
were not rewritten. They must be considered separately when selecting files for a future commit.

## Validation

- Full Python suite after corrections: **398 passed in 58.57 seconds** (389 existing plus nine
  review regressions).
- The three malformed-echo retry tests failed against Opus's version, then passed after the fix.
- Both normal firmware targets and all four 100/200 Hz benchmark targets compile successfully after
  the critical-section correction. No board was flashed.
- Active Unity C# sources compiled successfully with Unity 6000.6.2f1's bundled Roslyn compiler and
  the project's Android player response-file references/defines. Output was redirected to
  `comms/logs/opus-code-review/Assembly-CSharp.dll`; the running Editor's assemblies were not replaced.
  This checks C# compilation against actual Unity assemblies, not APK packaging, IL2CPP execution,
  scene rendering or phone behavior.
- Ruff correctness checks (`E9,F63,F7,F82`) pass across Python source and tests; full Ruff checks pass
  on the mock/reset modules changed by this review and the new regression test module.
- Python 3.10 grammar parsing passes for all 62 source modules. Execution used the installed Python
  3.14 interpreter; this is not a Python 3.10 runtime test.
- `pip check`: no broken requirements in the installed environment.
- Configured mypy remains non-clean: **60 errors in 14 files**, predominantly existing import/type
  issues. Before this review's request guard it reported 62, not the unchanged 61 claimed in the
  transcript. Full Ruff also still contains existing findings; no broad style/type rewrite was made.

## Limits and deployment

The C++ callback interleaving correction is reviewed and compiled, not stress-tested on a real
dual-core ESP32. No native C++ test runtime, Android APK build/install, Ultra96 execution, physical
BLE benchmark or out-of-range test was completed during this review.

Existing limitations reported by Opus were not silently reclassified as fixed: keyboard operations
remain serialized through classification; Q can cancel the active keyboard operation; the HELLO
nonce cache is session-local; the mock is not a full radio/firmware emulator; cooldown-state output
and other engine extensions are separate work. File-read requests before upload completion fail
within bounded retries rather than resuming an incomplete transfer. Keys, certificates and
cross-player clock accuracy were not changed or newly certified.

For deployment, the cleanup and these review corrections must first be included in a commit on the
runtime machines' branch. A pull of `d3571f7` alone does not contain them. Then update Python on laptop
and Ultra96, flash both normal firmware targets, rebuild/install the active Unity APKs and rehearse:

1. Both devices streaming near 50 Hz on one laptop; uppercase D packet inspection.
2. a and GPIO4 share the existing weapon cycle; b/c/d and B := C retain trace/data evidence.
3. GPIO25/26/27 retain their old trigger-map actions at normal 50 Hz.
4. F and L+F complete actual board RAM readback and file verification.
5. Independent power reset and actual out-of-range recovery, followed by a fresh keyboard action.

Use the existing deterministic keyboard and live/video guides for commands. Physical retesting
remains necessary even though the local regression tests and compilation checks pass.
