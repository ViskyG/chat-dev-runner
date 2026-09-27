---
id: CDR-RECOVERY
status: required-design
---
# Failure and recovery matrix

| Event | Preserve | State shown | Next authorized action |
|---|---|---|---|
| Duplicate READY observation | original receipt and digest | same existing operation | none; no replay |
| Same request ID, changed content | both observed digests | REQUEST_ID_REUSED | review, new request |
| Occupied workspace/global cap | rejected request receipt | REJECTED / BUSY | later new request, never deferred |
| Old epoch / expired command | rejection evidence | REJECTED | fresh state + new request |
| Source HEAD or byte hash mismatch | local edits + submitted payload | BLOCKED / CONFLICT | inspect diff; no overwrite |
| Docs changes after submission | captured revision IDs | INPUT_CHANGED | new frozen submission |
| Test exit code nonzero | every retained output | COMPLETED + execution FAILED | human analyses result |
| Timeout/cancel | partial output, signal history | timed out/cancelled execution | human decides next step |
| Process spawn/runtime missing | input + infrastructure trace | operation FAULT | repair runtime; new request |
| Container creation/removal uncertain | run UUID, durable pre-create intent (daemon, image, profile, command hash, private nonce), cleanup code, retained lock | FAULT / reconciliation required | inspect the original rootless daemon; local `reconcile_run_attempt` releases the lock only after exact owned-container removal is verified |
| Bad optional JUnit/JSONL | raw output + parsing finding | output unparsed | inspect/project fix |
| Drive upload failed after tests | local immutable output | delivery FAILED/PARTIAL | PUBLISH_REPORT, no test rerun |
| Git push divergence | local snapshot + remote refs | publication BLOCKED | explicit branch resolution |
| Timeout after GitHub repo/PR creation | external intent/resource IDs | UNKNOWN / reconciliation needed | read remote state before repeating |
| Partial project creation | every created resource identity | PROVISIONING_PARTIAL | explicit resume from verified state |
| Host restarted while executing | durable operation journal | INTERRUPTED | terminate/reconcile survivors, don't rerun |
| Disk space below reserve or evidence write returns ENOSPC | retained bytes and durable attempt where available | storage FAULT / `EVIDENCE_STORAGE_FULL`; no complete run manifest is claimed after hard ENOSPC | free/archive space; inspect the attempt and start a new request only after recovery; no silent deletion or replay |
| Cloud authorization revoked | local data, sanitized error | integration unavailable | local reauthorization |
| Project archived with retained runs | manifests + tombstones | read-only project history | deliberate restore/unarchive |

The broker records `CONTAINER_INTENDED` in the append-only journal before Docker create.
That write atomically checks the still-running, armed operation, exact lock and reserved
run attempt, preventing a paused worker from creating after startup releases its lock.
The trusted local `reconcile_run_attempt()` route reads that intent and inventories the
original rootless daemon. A candidate must match the recorded run name and label, private
nonce, image identity, profile revision, command hash, and container security and mount
policy. A collision or changed daemon fails closed. The route removes only the verified
container by its exact ID, verifies it is gone on the same daemon, then journals a `REMOVED`
proof before `confirm_stopped` releases the lock. An empty inventory is not proof of
absence after an ambiguous create: the Docker request may still complete later, so the
lock remains reserved for local investigation. This route is not exposed as a remote
command and is never run automatically at startup. A missing intent or unavailable
original daemon also leaves the lock reserved.

## Recovery commands beyond the small prototype schema

Production must add a versioned, narrow `RECONCILE` action that only reads and classifies external
state for a prior operation, and `RESUME_PROVISION` for a verified partial create workflow.
Both require new request IDs. They must not rerun a test or ambiguous completed side effect.
Add schema/tests in EP-02/EP-05; do not overload VERIFY or mutate an old request body.

`PUBLISH_REPORT` already appears in schema v1. It addresses immutable run evidence; it may retry
transport only. Durable delivery state is not a development-task queue and does not cause
execution. Default live policy: manual delivery retry after bounded immediate transport retries.

## Shutdown and cancellation

Stop admission first. Report intentional unavailability. Signal owned execution process groups
or containers and wait a bounded grace period, then kill remaining owned processes. Freeze
available outputs and record unknown external steps. Release locks only after executor state
is known. Do not kill unrelated user's programs or clean their branches.

## Partial provisioning and credentials

Do not roll back by deleting repositories or folders automatically: the user may already have
interacted with them. Read recorded numeric IDs and verify ownership. A same-name repo created
outside the operation is a conflict, not a resource to adopt silently. Revoked credentials may
leave a complete local project with unavailable cloud channels; preserve that useful state.

## Observability

Every operation event has request_id, step, attempt identity, timestamp, severity, code and
sanitized details. Heartbeat belongs to runner liveness; delivery/fetch freshness belong to
connections; workload failure belongs to runs. The global red indicator should mean there is
an actual runner/integration problem requiring local attention, not a failing unit test.
