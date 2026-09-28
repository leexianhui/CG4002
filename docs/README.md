# docs/ index

Start with the **Current** rows. The **Historical** docs are kept as design and investigation
records; the table below identifies what replaced them. Their commands can be out of date.

## Current: run the demo and assessment

| Doc | Use it for |
|---|---|
| [DETERMINISTIC_KEYBOARD_DEMO.md](DETERMINISTIC_KEYBOARD_DEMO.md) | Deployment and the keyboard map: `a` weapon, `b/c/d` editable IMU, live B := C edit, the `--assessment-deterministic` classifier |
| [COMMS_LIVE_AND_VIDEO_GUIDE.md](COMMS_LIVE_AND_VIDEO_GUIDE.md) | Speed trials (50/100/200 Hz), the professor-facing live script, video plan |
| [PRE_DEMO_ROBUSTNESS.md](PRE_DEMO_ROBUSTNESS.md) | Recovery contracts per link, restart/out-of-range verification table |
| [../comms/DEMO_RUNBOOK.md](../comms/DEMO_RUNBOOK.md) | Bringing the GPIO demo up; telling a real failure from a quiet rig |

## Current: design and contracts

| Doc | Use it for |
|---|---|
| [REPO_PLAN.md](REPO_PLAN.md) | Monorepo layout, ownership, where `firmware/`, `ai/`, `visualizer/` plug in |
| [COMMS_IMPLEMENTATION_PLAN.md](COMMS_IMPLEMENTATION_PLAN.md) | The comms design (relay, engine, wire protocol, §16 integration rules) |
| [../shared/README.md](../shared/README.md) | The cross-subsystem contract: wire protocol, JSON messages, schemas, golden vectors |

## Historical (superseded, kept for the record)

| Doc | Superseded by |
|---|---|
| [COMMS_ASSESSMENT_CHECKLIST.md](COMMS_ASSESSMENT_CHECKLIST.md) | Pre-implementation gap analysis; see the review/runbook, then the keyboard and live guides |
| [COMMS_ASSESSMENT_IMPLEMENTATION_PLAN.md](COMMS_ASSESSMENT_IMPLEMENTATION_PLAN.md) | Keyboard sections replaced by DETERMINISTIC_KEYBOARD_DEMO.md |
| [COMMS_ASSESSMENT_REVIEW_AND_RUNBOOK.md](COMMS_ASSESSMENT_REVIEW_AND_RUNBOOK.md) | Keyboard sections replaced by DETERMINISTIC_KEYBOARD_DEMO.md; the dashboard section still applies |
| [ASSESSMENT_KEYBOARD_TIMEOUT_FOLLOWUP.md](ASSESSMENT_KEYBOARD_TIMEOUT_FOLLOWUP.md) | Investigation that produced the 500 ms / 600 ms assessment timing profile |
| [GUARD_LATENCY_FOLLOWUP.md](GUARD_LATENCY_FOLLOWUP.md) | Investigation that added the classifier latency diagnostics |
| [DEMO_TRIGGERED_ACTION_PLAN.md](DEMO_TRIGGERED_ACTION_PLAN.md) | Plan for GPIO-triggered action capture; now implemented (`trigger_mode`, GPIO25/26/27). The "planning only" banner is historical |
