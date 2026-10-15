# docs/ index

Start with the **Current** rows. The **Historical** docs are kept as design and investigation
records; their commands can be out of date. The comms individual-assessment docs (keyboard demo,
speed trials, video script, review runbook) were removed after the assessment; they are in git
history before the cleanup commit.

## Start here

**[SETUP_GUIDE.md](SETUP_GUIDE.md)** is the step-by-step bring-up of the whole system: flashing the
six boards, AI + engine on the Ultra96, the two relays, the phones and calibration.

Repo layout: `ai/`, `comms/` (with `comms/firmware/`, the BLE stack), `hardware/` (with
`hardware/gateway/`, the six-board build), `visualizer/` (Unity project root), plus `shared/`. The
design report is in `docs/design_report/`. Machine-local secrets (`comms/.env.*`, `comms/certs/`, PSKs)
are not in git.

## Current: run the demo

| Doc | Use it for |
|---|---|
| [SETUP_GUIDE.md](SETUP_GUIDE.md) | Full-system bring-up, step by step, per machine |
| [INTEGRATION_RUNBOOK.md](INTEGRATION_RUNBOOK.md) | Imported visualizer online mode, mock stack, calibration, gateway handoff |
| [SUBSYSTEM_INTEGRATION_PLAN.md](SUBSYSTEM_INTEGRATION_PLAN.md) | Integration stages and ownership boundaries |
| [PRE_DEMO_ROBUSTNESS.md](PRE_DEMO_ROBUSTNESS.md) | Recovery contracts per link, restart/out-of-range verification table |
| [../comms/DEMO_RUNBOOK.md](../comms/DEMO_RUNBOOK.md) | The original comms demo (GPIO buttons); telling a real failure from a quiet rig |

## Current: design and contracts

| Doc | Use it for |
|---|---|
| [REPO_PLAN.md](REPO_PLAN.md) | Original monorepo plan and ownership (the layout above supersedes its folder names) |
| [COMMS_IMPLEMENTATION_PLAN.md](COMMS_IMPLEMENTATION_PLAN.md) | The comms design (relay, engine, wire protocol, §16 integration rules) |
| [../ai/README.md](../ai/README.md) | Comms ↔ AI contract, AI backends, known AI gaps |
| [design_report/](design_report/) | The design report sources |
| [../shared/README.md](../shared/README.md) | The cross-subsystem contract: wire protocol, JSON messages, schemas, golden vectors |

## Historical (superseded, kept for the record)

| Doc | Superseded by |
|---|---|
| [GUARD_LATENCY_FOLLOWUP.md](GUARD_LATENCY_FOLLOWUP.md) | Investigation that added the classifier latency diagnostics |
| [DEMO_TRIGGERED_ACTION_PLAN.md](DEMO_TRIGGERED_ACTION_PLAN.md) | Plan for GPIO-triggered action capture; now implemented (`trigger_mode`, GPIO25/26/27). The "planning only" banner is historical |
