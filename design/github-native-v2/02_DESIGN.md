# System design — narrow extension, not a rewrite

## 1. Layers and invariants
Four independent dimensions: command transport (GitHub/Docs/local UI), source selection (GitHub branch/registered local workspace), execution backend (native/container; later Compose/Unity), delivery destination (same-branch GitHub and optional Drive). The local engine/SQLite owns accepted intent and run evidence. Providers are mailboxes and published views, not remote worker databases.

One controller process per runner installation, one active execution lane, bounded FIFO for live-accepted requests. Polling and cancellation run independently of the execution lane. No Redis, Celery, Kubernetes, distributed work stealing, model API, UI automation or webhook endpoint is needed. Different named runners have independent queues and evidence namespaces. Do not install the same runner ID on two machines; detect a conflicting live instance during enrolment. Deliberate ID spoofing is out of scope of the trusted-collaborator model.

Core invariants:
- Every runnable task has immutable accepted payload, successful exact-version acknowledgement and one durable sequence number.
- At most one execution per accepted request; uncertain side effects require reconciliation, not blind rerun.
- No intake by replaying Git history or old Docs revisions. No unobserved offline backlog.
- FIFO applies to admitted task work, not to cancel/status control messages.
- Current source resolves exactly once at execution start, before dependency installation or project code; the run then remains tied to that snapshot.
- Result publication changes only the reserved result subtree, never replaces source with an old tested tree.
- A red status in project tests is not a runner infrastructure failure; delivery and cleanup remain separate axes.
- No function silently upgrades native execution into a sandbox claim or container execution into a native-OS claim.

## 2. Runtime composition
Keep existing `Engine`, `Ledger`, source registry, evidence stores and provider credentials where useful, but separate general runtime from Drive provisioning. `Runtime.arm_on_startup` currently requires Drive routes. Replace this with readiness of at least one explicitly configured transport plus a valid local registry and chosen executor. Disabled Drive must cause zero OAuth, import-time platform-side effects, ID validation or network requests. A failing optional provider must not disable a healthy other provider. Import checks must run on Windows/macOS too; several existing modules contain POSIX-only flags and renameat2.

Proposed seams (names below are target design, not claims of existing classes):
- `MailboxPort.read_current()` -> exact provider revision, bytes and observed branch head; `ack_if_current()` -> APPLIED / CONFLICT / UNKNOWN plus immutable proof when available.
- `IntakeCoordinator` -> schema validation, session continuity, frozen payload, reserve/ack/finalize.
- `QueueCoordinator` -> atomic sequence assignment, claim-next, control cancellation, session end; no provider network calls in SQLite transactions.
- `SourceResolver.resolve_current()` -> SourceSnapshot; source method records github_branch vs local_workspace.
- `TaskPlanner` -> finite phase plan from selected project manifest/task; no LLM evaluation.
- `ExecutorPort` -> NativePosix, NativeWindows, existing RootlessContainer; standard lifecycle/provenance, different declared guarantees.
- `ResultCollector` -> freeze selected files, parser facts, quality gate, artifact manifest.
- `SameBranchPublisher` -> safe result-only Git commit and bounded conflict handling.
- `PlatformServices` -> user paths/permissions, atomic files, singleton, process supervision, service installation. Avoid full abstraction of every OS API.

Place new modules beside existing flat adapters; do not reorganize the whole repository merely to match a diagram. Suggested anchors: `runner_app/engine/intake.py`, `queue.py`; `adapters/github_mailbox.py`, `github_results.py`; `executors/native_posix.py`, `native_windows.py`; `platform/`; `outputs/environment.py`. Existing names and source links are in SOURCE_AUDIT.json.

## 3. Logical records and ownership
Extend the existing database rather than introducing a second operational database. Use a new schema migration after inspecting current max migration. Logical tables/columns:

| Record | Required identity/state | Writer |
|---|---|---|
| mailbox binding | runner, project, provider, exact repo/branch/path or document+tab ID, current slot token, continuity/session, observed revision | controller |
| intake candidate | request ID, slot token, payload digest+bytes, provider observation, ACK state+proof | intake |
| queue entry | request ID unique, session ID, increasing sequence, QUEUED/CLAIMED/SUSPENDED/CANCELLED | coordinator |
| run | request ID, actual source commit/tree/manifest, phases, platform, outcomes | executor coordinator |
| delivery attempt | frozen bundle hash, target branch, parent/commit identity, result status | publisher |
| journal | append-only accepted/start/effect/cleanup/recovery facts | existing ledger |

Use SQLite immediate short writer transactions for reserve, ACK finalization and queue claims. Schema constraints must prohibit two active execution claims on one installation; the existing in-process lock is not sufficient for crash correctness. Pollers may acquire short local locks; they must not hold the execution lane while waiting for remote I/O.

## 4. End-to-end normal route
An enrolled file/document is already prefilled with an inert payload and runner-managed slot token. Author edits DRAFT, sets READY last. Intake validates the exact observed version and durably reserves intent. Controller conditionally writes ACCEPTED and receipt information, rotates the token for the next submission, then finalizes the local queue entry. Author can now reuse the slot. Worker claims the next entry, resolves current code, prepares a private run workspace, performs approved finite tasks, freezes output, attempts GitHub publication, records local fallback if needed and finishes. Completion never overwrites a later command in the slot.

## 5. Existing operations and v1 coexistence
Preserve historical v1 schemas and evidence readers. Initially keep v1 Drive mutation routes behind their existing explicitly selected protocol binding; route them through the same single global execution lane so v1/v2 cannot run concurrently by accident. Do not reinterpret old READY as v2. GitHub-first RUN_TASK must not call a route that implicitly creates Drive documents or draft PRs. Existing DELIVER_VERIFY_PUBLISH has different semantics (deliver code, verify, publish source/PR); keep that distinction.

If integrating v1 queueing requires changing its strict expected-head semantics, do not silently change it. The first v2 release supports current-at-start RUN_TASK and existing v1 routes with their original explicit base checks. A later v2 source-change command must revalidate approved changes at start and reject a changed base; it is not needed for GitHub-first execution.

## 6. Scope of automation and trust
Repository write access is enough to send tasks to the enrolled runner. Ordinary-user native execution can read/modify what that user can; a separate run folder prevents accidental checkout pollution, not intentional host access. Standard task scripts and arbitrary shell therefore share a trust boundary. Do not add mandatory operator ACLs, a special user or per-command consent prompts.

Keep predictable bookkeeping: no automatic sudo/elevation, no orchestrator tokens injected into subprocess env, no unbounded interactive terminals, no public listener, no automatic source reset/stash/force-push. A shell request is deliberately executable text, while console/report text is inert data and never a follow-up command.

## 7. Operational UX
Report mode, current session, active request, FIFO position and transport last-success time. Slots show DRAFT, READY, ACCEPTED, REJECTED or NEEDS_RESUBMIT, not a fake progress animation. Durable request records show QUEUED, RUNNING and terminal phase/evidence/delivery states. Publish state on change plus a modest optional heartbeat, not every poll as a Git commit. Fresh chats learn protocol, active capabilities, exact slots and result locations from registered START HERE/index files. No automatic AI response or automatic next development iteration.
