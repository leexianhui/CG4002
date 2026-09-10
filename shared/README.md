# `shared/` — the cross-subsystem contract

**Everything in this directory is jointly owned. Change it only via a PR that at least one other
affected owner approves.**

Every other directory in this repo is owned by one person and needs no review. This one is different:
a change here breaks somebody else's build by definition. That visibility is the entire reason the four
subsystems live in one repository (`docs/REPO_PLAN.md` §1, §3).

## What is in here

| Path | Normative for | Read by |
|---|---|---|
| `protocol/VERSION` | protocol version integer; bump on any breaking change | all |
| `protocol/wire_protocol.md` | the binary FireBeetle ↔ laptop protocol | firmware, comms |
| `protocol/cg_protocol.h` | the same, as C — packet types, structs, CRC16 | firmware, comms |
| `protocol/app_messages.md` | the JSON messages between relay, engine, Ultra96, visualizer | all |
| `schemas/*.schema.json` | machine-checkable form of the above | comms, AI, visualizer |
| `enums/enums.yaml` | Weapon, ActionClass, GuardState, CombatOutcome, MatchPhase, FeedbackCode | all |
| `config/weapons.yaml` | per-weapon move set, damage, range, cooldowns | comms (engine) |
| `config/match.yaml` | HP, parry window, block multiplier, arena tuning | comms (engine) |
| `config/sensors.yaml` | IMU fixed-point scaling and windowing parameters | firmware, comms, AI |
| `generated/` | codegen output — **never hand-edit**, run `tools/codegen` | all |

## Rules

1. **`wire_protocol.md` and `cg_protocol.h` must agree exactly.** If they disagree, that is a bug, not
   a choice — fix both in the same PR. Firmware reads the header, the Python codec mirrors it, and the
   golden vectors are what prove they match.
2. **Never renumber a shipped enum `wire` value.** Append new members. A renumber makes an old firmware
   and a new laptop silently read a byte as the wrong member — no error, just wrong behaviour.
3. **Bump `protocol/VERSION` on any breaking change**, and let receivers drop unknown versions loudly.
4. **`generated/` is output, not source.** Edit `enums.yaml` or the schemas and regenerate.
5. **No secrets here.** The AES-128 PSK lives in an environment variable or a file outside the repo,
   never in this directory.

## Expected to change

`config/weapons.yaml` and `enums.yaml`'s `ActionClass` will almost certainly change once the AI
dataset's final class set is known — the report's move set is a starting point, not a guarantee. That
is planned for; see `docs/COMMS_IMPLEMENTATION_PLAN.md` §16.2 for what moves with it.
