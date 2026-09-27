---
id: CDR-ARCH
status: accepted-design
supersedes: earlier-chat-sketches
---
# Architecture / target v1

## 1. Purpose and boundaries

A single-host, human-directed development bridge. Inputs arrive through a loopback UI or
runner-owned Google Docs. Outputs return through Drive and optionally the local UI.
GitHub is the source history and publication destination. No ChatGPT API/client is part of
this application. The user starts the next development iteration.

Core: multi-project provisioning, per-project workspaces, explicit source transport,
profile-based environment provisioning, finite verification/build operations, Git commit/push,
PR create/update, typed evidence collection and publication. A prototype without these live
capabilities may be useful, but MUST NOT be described as a completed v1.

## 2. Intended stack

Python 3.10+ on Ubuntu; FastAPI + one Uvicorn worker; SQLite WAL; standard Git executable;
Google Drive/Docs APIs; GitHub REST; rootless container executor; static frontend served from
the same loopback origin. Choose a currently supported Python version at installation; retain
3.10 compatibility only while dependencies permit. Pin and test dependency versions in EP-00.

Keep the vanilla UI initially. No framework rewrite is needed to make the first version real.
No Celery, Redis, Kubernetes, distributed scheduler or external service database. One backend
process owns admission and the local worker lifecycle. Multiple workspaces can execute under
a finite resource limit without a waiting queue.

## 3. Components and ownership

- `runner_app/api/`: local authentication, CSRF/Host checks, query views and operation submission.
- `runner_app/domain/`: Command, Project, Workspace, Snapshot, Operation, Run, Output, Receipt.
- `runner_app/engine/`: validation, admission, scoped locks, finite plans, cancellation and recovery.
- `runner_app/adapters/drive/`: known Docs polling, revision guards, snapshots and delivery.
- `runner_app/git/`: safe local Git operations and repository/worktree metadata.
- `runner_app/adapters/github/`: repository provisioning, normal push authorization, draft PRs.
- `runner_app/executors/`: no-op/files-only and rootless container profiles; no remote shell DSL.
- `runner_app/outputs/`: raw capture, parsers, redaction, policy-controlled publication.
- `runner_app/storage/`: SQLite migrations, object storage, durable step journal and manifests.
- `prototype/`: initial visual system; replace demo state source with typed API adapter in EP-06.

`bridge_reference/` demonstrates selected invariants. It is not secretly the implementation
of the above daemon and must not be wired to arbitrary user repositories before hardening.

## 4. Authoritative state

Git stores source history. SQLite stores local identities, immutable accepted requests,
resource ownership, locks, operations and output manifests. Local content-addressed evidence
is authoritative until delivery succeeds. Drive is command input plus published views, not
an authoritative mutable database for worker states. Chat/UI never overwrites receipts.

Projects have stable IDs. Workspaces have stable IDs independent of their current branch.
Branches are mutable Git refs. Full object IDs are stored; abbreviations are UI-only. A run
records tested commit/tree, dirty-at-capture status and a source-file hash manifest. Results
can become stale without changing their historical truth.

## 5. Admission without a queue

A command is validated against its addressed runner, epoch, mailbox scope, local policy,
schema, expiry, expected Git state and payload revisions. In a single SQLite transaction,
reserve the appropriate scope and capacity or store a terminal REJECTED/BUSY receipt.
Never create `QUEUED`, `WAITING_FOR_SLOT`, a deferred executor callback or a retry-after scheduler.

A scoped lock is an internal correctness mechanism, not a product queue. Workspace mutation
is exclusive for that workspace. Repository metadata changes use a short repository lock;
unavailable locks reject or block the current operation explicitly rather than silently wait
for another user's operation to complete. Cross-workspace tests use separate snapshots and
resource namespaces. The process-level singleton lock prevents two local daemons.

Mailbox requests written while the computer is off are not guaranteed future jobs. They are
accepted only if still READY, unprocessed, unexpired and addressed to the current epoch at
observation. Startup must not run a historical backlog; after uncertain shutdown, re-arm
explicitly and publish a new epoch. BUSY remains terminal even after the slot becomes free.

## 6. One command, fixed finite plan

`DELIVER_VERIFY_PUBLISH` expands to a known plan:
validate → freeze input → stage explicit changes → create immutable snapshot → provision
approved environment → execute once → capture outputs → publish according to requested policy
→ deliver receipt/report → release workspace. No model-generated next step or dynamic repair.

The frontend's prototype timeline is illustrative. In the live UI, display the actual plan,
including skipped stages and publication-not-requested. Do not show push/PR completed on a
VERIFY-only operation or tests passed on CREATE_PROJECT.

The snapshot is frozen BEFORE verification, as shown in the prototype route.
Implementation order must follow this document. Publish the same snapshot,
never whatever happens to be in the editor after tests finish.

## 7. Crash and external-side-effect semantics

A unique request ID provides deduplication, not magical distributed exactly-once execution.
Persist intent before external writes and persist observed resource IDs afterward. A network
failure between an external write and acknowledgement yields an UNKNOWN/NEEDS_RECONCILIATION
step. Read current remote state before any repeat. Do not blindly create a second repo/PR,
push a different branch, delete a resource or rerun project code.

An interrupted operation stays interrupted. Recovery first ensures worker process groups are
gone and reconciles external effects. Safe delivery can be retried under an explicit
PUBLISH_REPORT request without a new test run. A creation workflow resumes only with a new
explicit request referencing the previous operation and only at verified unperformed stages.
Include RECONCILE/RESUME_PROVISION in the production API extension described in RECOVERY.md;
prototype schema v1 intentionally does not pretend to implement those handlers yet.

## 8. Layout on disk

```text
~/.config/chat-dev-runner/     # policy and credential references; not in Git or Drive
~/.local/share/chat-dev-runner/
  state.sqlite
  projects/<project_id>/
    repository/               # runner-owned central clone
    workspaces/<workspace_id>/
  snapshots/<run_id>/          # frozen inputs; no cloud credentials
  outputs/<run_id>/            # typed artifacts + raw local capture
  objects/sha256/<digest>
  operation-journal/<request_id>/
```

Do not adopt the user's existing development checkout as the runner-owned central clone.
Local import is metadata inspection + a separate clone/worktree arrangement unless explicitly
approved. No `.git`, dependency cache, secret file or entire HOME upload to Drive.

## 9. Security separation

The orchestrator holds OAuth/GitHub credentials. The executor gets only the frozen project
input, approved dependencies, scratch space and an output directory. It does not receive
cloud credentials, host HOME, SSH agent, Docker socket or unrelated projects. Worktree
separation is not process isolation [S1]; rootless operation reduces privilege but does not
make arbitrary untrusted code harmless [S7]. See SECURITY.md for release gates.

## 10. Scope deliberately not implemented in v1

Public hosting, automatic merge/force-push, cross-host state replication, arbitrary interactive
terminal sessions, fully automatic ChatGPT connector permissions and automatic LLM iteration.
Long-running local app previews require a later explicit START/STOP session contract with TTL,
port leases and shutdown behavior; do not smuggle them into a test job.

References: [S1], [S7] in SOURCES.md. All component boundaries above are project design decisions.
