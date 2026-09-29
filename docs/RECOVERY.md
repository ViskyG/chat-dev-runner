---
id: CDR-RECOVERY
status: implemented-core; EP-07-host-recovery-pending
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
| Preparation or test container creation/removal uncertain | phase run UUID, durable pre-create intent (daemon, image, profile, command hash, exact private source mount, nonce), cleanup code, retained lock | FAULT / reconciliation required | inspect the original rootless daemon; local `reconcile_run_attempt` releases the lock only after exact removal or explicitly requested aged absence proof for every phase |
| Bad optional JUnit/JSONL | raw output + parsing finding | output unparsed | inspect/project fix |
| Drive upload failed after tests | local immutable output | delivery FAILED/PENDING/PARTIAL | reconcile uncertain writes, then new PUBLISH_REPORT; no test rerun |
| Git push divergence | local snapshot + remote refs | publication BLOCKED | explicit branch resolution |
| Timeout after GitHub repo/PR creation | external intent/resource IDs | UNKNOWN / reconciliation needed | read remote state before repeating |
| FETCH transfer ambiguous after durable intent | exact private repo ID, fetch refspec, tracking-ref digest and repo lock | UNKNOWN / reconciliation needed; no successful fetch timestamp | owner-local read-only tracking-ref reconciliation; never replay the uncertain network fetch |
| Partial project creation | every created resource identity | PROVISIONING_PARTIAL | explicit resume from verified state |
| Host restarted while executing | durable operation journal | INTERRUPTED | terminate/reconcile survivors, don't rerun |
| Evidence ENOSPC/EDQUOT while SQLite remains writable | retained bytes and durable phase attempts/intents where available | committed minimal FAULT / `EVIDENCE_STORAGE_FULL`, execution UNKNOWN; no complete run manifest | free/archive space; inspect evidence and start a new request only after recovery; no silent deletion or replay |
| SQLite commit or journal write fails or is ambiguous | last confirmed receipt/attempt/intents and in-memory admission fence | `STORAGE_UNAVAILABLE`; terminal result is unknown until database readback | stop new admissions, signal live handlers, restore storage, restart and reconcile; never invent or retry an ambiguous receipt |
| Preparation capture tombstoned but unlink interrupted | immutable preparation receipt and output tombstone | `EVIDENCE_EXPIRED` on owner-local read; file may still occupy disk | trusted local owner retries `PreparationEvidenceStore.expire_output` to verify and purge the exact file; never restore download from an old path |
| Run output retention requested too early, while pinned, referenced, or busy | immutable manifest, output and append-only pin/expiry record | specific refusal code or durable tombstone, never a success guess | review exact run/output and age; owner-local retry only when every gate passes; do not delete the path manually |
| Cloud authorization revoked | local data, sanitized error | integration unavailable | local reauthorization |
| Project archived with retained runs | manifests + tombstones | read-only project history | deliberate restore/unarchive |

The broker records a phase-specific `CONTAINER_INTENDED` before each Docker create.
That append atomically checks the running, armed operation, exact lock, reserved phase
attempt and, for TEST after PREPARATION, the completed handoff. The trusted local
`reconcile_run_attempt()` route inventories the original rootless daemon for each unproved
intent. A candidate must match the recorded run name and label, private nonce, image and
profile revisions, command hash, exact source mount, and isolation/mount policy. Older
schema-6 TEST intents retain their original run-ID source-name check. A collision or changed
daemon fails closed. The route removes only an exact owned container by ID and journals
its `REMOVED` proof. If and only if removal finds no owned container, an owner may
explicitly request `--allow-absent` after the durable intent is at least two minutes old.
Two complete negative inventories on the original daemon then allow a distinct `ABSENT`
proof; any daemon change, collision, incomplete inventory, or removal failure remains
fenced. `confirm_stopped` releases the lock only after every phase intent has normal
CLEANED, recovered REMOVED, or aged ABSENT proof. A Docker create error with an empty
response never yields immediate absence: the daemon request may still complete later.
This recovery route is local and explicit, never a remote command or automatic startup action.

A minimal storage-fault receipt is durable only if its SQLite transaction commits. When
evidence bytes cannot be frozen but SQLite is healthy, the handler records a sanitized FAULT
with the known snapshot and an UNKNOWN execution outcome; it does not create a run manifest.
If SQLite cannot commit, no second receipt is fabricated: the Engine fences admissions for
its process lifetime, requests cancellation of live workers and attempts to disarm. A power
loss or truly full database volume cannot guarantee a terminal write. On restart, the last
confirmed RUNNING row becomes INTERRUPTED for explicit reconciliation; an ambiguous COMMIT
must be read back before any claim about the final state.

## Implemented explicit recovery routes

Schema v1 now includes owner-local `RECONCILE` and `RESUME_PROVISION`, each with a new
request ID and exact target request ID. They are explicit Engine handlers, not automatic
startup or retry behavior. The configured owner Runtime registers them; an unconfigured
Runtime does not expose provider handlers. `RECONCILE`
can positively classify a previously uncertain private GitHub repository, branch,
draft PR, scoped Drive resource, managed bare repository, or clean primary worktree.
The configured reader rechecks the original recorded identity without replaying a
provider write. A missing resource, unmatched registry row, dirty worktree, or
ambiguous negative search remains unresolved and retains the original operation's
lock until owner resolution.

`RESUME_PROVISION` rechecks the original GitHub marker and main commit,
registered local source and clean workspace, and every confirmed Drive ID/content
hash before atomically adopting each effect into the new request. A fully READY,
UI-originated new project completes without provider writes. A reconciled,
UI-originated new project whose confirmed effects form the original ordered prefix
may continue: the Drive adapter re-reads adopted IDs and creates only missing
project/workspace descendants and initial content under a fresh RESUME request.
Original creation metadata is preserved in those Docs. A negative search never
licenses adoption or replay of an uncertain effect. A Drive-root original can
follow the same owner-local RESUME path only after its terminal partial root
RECEIPT is journaled and re-read by exact document ID/hash while the frozen and
current READY command still match. That terminal marker is never adopted as a
project resource. An interrupted RESUME request is not automatically retried.
Neither action reruns a test or an ambiguous side effect.

`PUBLISH_REPORT` already appears in schema v1. It addresses immutable run evidence; it may retry
transport only. Durable delivery state is not a development-task queue and does not cause
execution. A retry is a new explicit PUBLISH_REPORT request after uncertain effects are
reconciled; provider writes are not automatically replayed.

EP-06's project-scoped `FETCH` journals a fixed no-prune tracking-ref update into the managed
bare repository. Local reconciliation can prove only the intended tracking-ref state on the
recorded repository; it never silently repeats a network fetch. A raced or partial state stays
UNKNOWN and retains the repository lock. An explicit read-only Git remote check does not
advance fetch freshness.

The owner-only retention API reports run pin state and output tombstones. Pin/unpin is
append-only. Per-output expiry has a default 14-day age gate and refuses pinned, referenced,
busy or uncertain evidence. A tombstone makes a missing payload explicit; retention never
rewrites the original manifest or a published hash. EP-07 host acceptance now records a successful same-path restore of 572 verified files,
an explicit owner-local retention trial with a deterministic future clock, rollback
from the unchanged backup, and managed-unit uninstall/reinstall in
`verification/implementation/EP-07/host-same-path-restore-20260928.json`,
`host-retention-trial-20260928.json` and `host-uninstall-reinstall-20260928.json`.
Retention remains an explicit owner decision through the authenticated UI/API;
there is no automatic retention scheduler. Real power-cycle and interruption
recovery gates remain open.

## Owner-local backup and restore

The production service uses a wheel-installed private environment at
`/home/visky/Work/GPTRunner/.runner-install/venv`, with dependencies installed
from hash-pinned `requirements.production.lock`, including the Google bootstrap
libraries. The selected durable state root is
`/home/visky/Work/GPTRunner/.runner-data/chat-dev-runner`; the default
`~/.local/share/chat-dev-runner` is not this service's state because it contains
old root-owned EP-03 volumes. Keep `XDG_DATA_HOME` set to the selected parent for
every backup, restore, status and service command. The installed user unit
retains the same resolved XDG paths when invoked by systemd.

Stop the owner service and confirm it is inactive before a snapshot. The
destination must be a new absolute **directory name** under an already
existing owner-only 0700 parent. The command creates `manifest.json` and
`state/` there; it refuses an existing destination or a runner holding the
singleton lock:

```bash
export XDG_DATA_HOME=/home/visky/Work/GPTRunner/.runner-data
runner_python=/home/visky/Work/GPTRunner/.runner-install/venv/bin/python
"$runner_python" -m runner_app.cli stop
"$runner_python" -m runner_app.cli status
"$runner_python" -m runner_app.cli backup \
  --output /home/visky/Work/GPTRunner/.runner-backups/backup-YYYYMMDD
```

The snapshot uses SQLite's online backup API for a consistent database image,
then copies the known durable state trees with integrity hashes. It includes
managed Git source and immutable evidence, so retain the archive as private
project data. It does **not** contain local configuration, Google/GitHub
credentials or tokens, cache, or runtime sockets. The verified pre-service
archive is
`/home/visky/Work/GPTRunner/.runner-backups/pre-ep07-service-20260928`
(schema 9, 181 files); its manifest hashes and SQLite integrity passed
inspection. After `runner stop`, the installed CLI also created
`/home/visky/Work/GPTRunner/.runner-backups/post-stop-service-20260928`
with schema 9 and 181 files. A backup attempted while the service was active
returned `RUNNER_RUNNING` and created no archive; a restore attempted while
the state root existed returned `RESTORE_TARGET_EXISTS`. These refusal and
backup results are recorded in `verification/implementation/EP-07/release-install.json`.
A successful restore to the absent original state path is recorded in `verification/implementation/EP-07/host-same-path-restore-20260928.json`; the preserved original directory and verified backup remain available for rollback.

For recovery, stop the service, keep it disarmed, and preserve the damaged state
directory under a separate private name after recording its condition. The
original `/home/visky/Work/GPTRunner/.runner-data/chat-dev-runner` path
**must be absent**. `restore --from` checks that the archive's recorded
source path is exactly that target, validates its manifest, file hashes and
SQLite image, and atomically creates the target. It refuses overwrite or a
different destination because database and managed Git records can contain
absolute paths. Config and tokens remain in their separate owner-private
location and are not recreated by restore:

```bash
export XDG_DATA_HOME=/home/visky/Work/GPTRunner/.runner-data
runner_python=/home/visky/Work/GPTRunner/.runner-install/venv/bin/python
"$runner_python" -m runner_app.cli stop
# After inspection, preserve the damaged state under an unused private name:
mv -T --no-clobber /home/visky/Work/GPTRunner/.runner-data/chat-dev-runner \
  /home/visky/Work/GPTRunner/.runner-data/chat-dev-runner.damaged-YYYYMMDD
"$runner_python" -m runner_app.cli restore \
  --from /home/visky/Work/GPTRunner/.runner-backups/pre-ep07-service-20260928
"$runner_python" -m runner_app.cli status
```

Do not start a restored service until its SQLite schema, state ownership,
archived project identities and unresolved external effects have been
reviewed. A restored database does not prove that an interrupted Docker,
Drive or GitHub operation completed; use the explicit reconciliation routes
above. EP-07 proved same-path restore on the owner host; interrupted-operation
recovery remains open.

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

## EP-07 operational gate

The owner-user service is installed from the private `0.2.6` wheel, active
and enabled after a validated stopped-service upgrade. The 572-file pre-upgrade
backup passed independent hashes and SQLite integrity; the exact existing
Drive mailbox preflight found no unexpired READY without terminal receipt.
After startup, local and Drive SYSTEM STATE epochs matched, and the listener
remained loopback-only. Same-path restore, explicit retention trial/rollback,
and managed-unit uninstall/reinstall are recorded under
`verification/implementation/EP-07/`. Interruption/power-loss recovery and
fresh-chat end-to-end access remain open. A distinct real host boot and automatic
service restart were observed, with one transient OAuth-refresh failure before systemd retry.
EP-07 is not yet a signed daily-use release; see `release-upgrade-0.2.6.json` for current evidence.
