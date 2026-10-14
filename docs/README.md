# docs/ index

Start with the **Current** rows. The **Historical** docs are kept as design and investigation
records; their commands can be out of date. The comms individual-assessment docs (keyboard demo,
speed trials, video script, review runbook) were removed after the assessment; they are in git
history before the cleanup commit.

## Deployment checkout

Both imported projects are used directly: hardware source is under `cg4002-hw-main/`, and the
Unity project root is `CG4002-Viz-main/` (add this directory to Unity Hub). Firmware integration
builds from `firmware_gateway/`. There is no repeated inner project folder.

On a deployment laptop or Ultra96 checkout, install Git LFS and run from the repository root:

```sh
git lfs install
git pull --ff-only origin main
git lfs pull
```

Existing local changes should be committed or stashed before pulling. Python environments,
`comms/.env.relay`, `comms/.env.engine`, certificates/private keys are machine-local and are
not supplied by Git. Hardware Wi-Fi credentials remain in the teammate sketches as requested;
the relay PSK is supplied separately through `CG_GATEWAY_PSK_HEX` when building a hub.
Use the [hardware build instructions](../firmware_gateway/README.md) and
[integration runbook](INTEGRATION_RUNBOOK.md) for the actual startup commands.

## Current: run the demo

| Doc | Use it for |
|---|---|
| [INTEGRATION_RUNBOOK.md](INTEGRATION_RUNBOOK.md) | Imported visualizer online mode, mock stack, calibration, gateway handoff |
| [SUBSYSTEM_INTEGRATION_PLAN.md](SUBSYSTEM_INTEGRATION_PLAN.md) | Integration stages and ownership boundaries |
| [PRE_DEMO_ROBUSTNESS.md](PRE_DEMO_ROBUSTNESS.md) | Recovery contracts per link, restart/out-of-range verification table |
| [../comms/DEMO_RUNBOOK.md](../comms/DEMO_RUNBOOK.md) | Bringing the GPIO demo up; telling a real failure from a quiet rig |

## Current: design and contracts

| Doc | Use it for |
|---|---|
| [REPO_PLAN.md](REPO_PLAN.md) | Monorepo layout, ownership, where `firmware/`, `ai/`, `visualizer/` plug in |
| [COMMS_IMPLEMENTATION_PLAN.md](COMMS_IMPLEMENTATION_PLAN.md) | The comms design (relay, engine, wire protocol, Â§16 integration rules) |
| [../shared/README.md](../shared/README.md) | The cross-subsystem contract: wire protocol, JSON messages, schemas, golden vectors |

## Historical (superseded, kept for the record)

| Doc | Superseded by |
|---|---|
| [GUARD_LATENCY_FOLLOWUP.md](GUARD_LATENCY_FOLLOWUP.md) | Investigation that added the classifier latency diagnostics |
| [DEMO_TRIGGERED_ACTION_PLAN.md](DEMO_TRIGGERED_ACTION_PLAN.md) | Plan for GPIO-triggered action capture; now implemented (`trigger_mode`, GPIO25/26/27). The "planning only" banner is historical |
