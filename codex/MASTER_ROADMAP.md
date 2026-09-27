# Implementation roadmap / v1

Core delivery is complete only after **all eight packets**. These are dependency boundaries,
not promises about elapsed time or token consumption. Work serially until the domain contracts
stabilize. EP-03 and EP-04 can later be developed independently only with fixed shared contracts.

| Packet | Outcome | Depends on | Live account needed for implementation? |
|---|---|---|---|
| [EP-00](packets/EP-00-foundation.md) | Inventory, gates, package scaffold, locked environment | — | No |
| [EP-01](packets/EP-01-domain-api.md) | Durable commands, immediate admission, local authenticated API | EP-00 | No |
| [EP-02](packets/EP-02-git-snapshots.md) | Multi-project registry, worktrees, immutable snapshots | EP-01 | No |
| [EP-03](packets/EP-03-execution-outputs.md) | Contained execution and typed output contracts | EP-02 | No |
| [EP-04](packets/EP-04-drive.md) | OAuth, known-ID Docs transport, evidence publication | EP-01 | Only for a gated smoke test |
| [EP-05](packets/EP-05-provision-publish.md) | Full finite provisioning/verify/publish/report workflow | EP-02,03,04 | Only for a gated smoke test |
| [EP-06](packets/EP-06-ui.md) | Beautiful UI bound to actual read models and commands | EP-05 | No; deterministic fake integration allowed |
| [EP-07](packets/EP-07-release.md) | Host acceptance, deployment, recovery, evidence release | EP-06 | Yes, explicit owner-controlled gate |

## Working source layout to create
```text
runner_app/
  api/             # loopback authentication, validation, REST, SSE
  domain/          # projects, workspaces, command/operation/run/output contracts
  engine/          # admission, finite workflows, step journal, reconciliation
  adapters/        # Google Docs/Drive and GitHub, no project-code execution
  git/             # safe Git argv, worktrees, snapshots, publication refs
  executors/       # rootless execution broker, approved environment profiles
  outputs/         # byte capture, safe parsers, typed index, privacy filtering
  storage/         # SQLite migrations, event journal, immutable evidence storage
  web/             # static assets + real-state frontend
  cli.py
```
Keep `bridge_reference/` as reference and regression tests. Reuse verified logic after adding
packaging, races, permissions, migrations and production recovery behavior; copying a small
reference function is not evidence that all of these concerns are solved.

## Deliverables by release
- working installed `runner` CLI and user service, configurable localhost port;
- UI with workspaces, operations, evidence inspector, integration state and local setup;
- versioned schema registry and migration tests;
- Drive seed/bootstrap plus per-project and per-workspace provisioning;
- core GitHub creation/push/PR with idempotent recovery;
- signed-off local policy and approved environment templates; no arbitrary remote shell;
- verified private disposable end-to-end run including a **failing test** and an intact runner;
- evidence bundle and operator recovery procedures.

## Contract decisions that must remain explicit
No queues means no terminal BUSY request can be silently retried when capacity later frees.
Transient upload retries for an already admitted operation are bounded transport steps, not a
queue of future executions. Cancellation and status reads must not wait for an execution slot.
A crash leaves uncertain side effects for reconciliation; do not claim distributed exactly-once.
Do not silently reinterpret `SYNC_WORKSPACE` as a two-way directory mirror or destructive pull.
Publishing after failed tests follows the chosen project policy and labels the draft PR correctly.
A run's pass/fail is separate from operation, publication, delivery and infrastructure health.

## Dependency and permissions gate
Pin application libraries and sandbox image digests at implementation time and commit a lock.
Verify current official API docs; source research in docs/SOURCES.md is a starting point, not a
permanent provider contract. Record the host versions without upgrading Kubernetes or Docker.
Do not infer GitHub owner from example strings, and do not change the user's existing repositories.
