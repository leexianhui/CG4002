# docs/ index

Start with the **Current** rows. The **Historical** docs are kept as design and investigation
records; their commands can be out of date. The comms individual-assessment docs (keyboard demo,
speed trials, video script, review runbook) were removed after the assessment; they are in git
history before the cleanup commit.

## Current: run the demo

| Doc | Use it for |
|---|---|
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
| [GUARD_LATENCY_FOLLOWUP.md](GUARD_LATENCY_FOLLOWUP.md) | Investigation that added the classifier latency diagnostics |
| [DEMO_TRIGGERED_ACTION_PLAN.md](DEMO_TRIGGERED_ACTION_PLAN.md) | Plan for GPIO-triggered action capture; now implemented (`trigger_mode`, GPIO25/26/27). The "planning only" banner is historical |
