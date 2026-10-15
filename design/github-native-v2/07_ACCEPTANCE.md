# Scenarios and acceptance

**74 scenario families.** Every live provider/platform case is NOT_RUN in this design package. `model` means related behavior is represented in the offline model; it does not certify the installed implementation. `git-local` is a disposable local Git experiment, not a GitHub roundtrip. Exact executed counts are in verification/REPORT.md.

## Requirements trace

- **R01** — GitHub-only autonomous operation; scenarios: SC-01, SC-04, SC-05, SC-65
- **R02** — Optional existing Google Docs transport; scenarios: SC-02, SC-03, SC-05, SC-65
- **R03** — Remote-only DRAFT/READY/runner ACK cycle; scenarios: SC-01, SC-02, SC-03, SC-06, SC-07, SC-08, SC-09, SC-10, SC-11, SC-12, SC-17, SC-20, SC-26, SC-28, SC-65
- **R04** — Immutable accepted intent and no duplicate execution; scenarios: SC-08, SC-09, SC-10, SC-11, SC-13, SC-14, SC-15, SC-24, SC-25, SC-40, SC-41, SC-72
- **R05** — Single lane and live FIFO, no active BUSY rejection; scenarios: SC-16, SC-18, SC-19, SC-22, SC-23, SC-26
- **R06** — Current source at execution start then frozen snapshot; scenarios: SC-01, SC-29, SC-30, SC-31, SC-32, SC-33, SC-34, SC-63, SC-72
- **R07** — No offline replay or accumulated command-history queue; scenarios: SC-19, SC-20, SC-21, SC-22, SC-23, SC-24, SC-25
- **R08** — Concrete runner and repository-writer trust; scenarios: SC-37, SC-58, SC-64, SC-74
- **R09** — Real native Linux/Windows/macOS plus explicit container option; scenarios: SC-25, SC-27, SC-49, SC-50, SC-55, SC-56, SC-57, SC-59, SC-67
- **R10** — Same repository and tested branch result-only publication; scenarios: SC-34, SC-35, SC-36, SC-37, SC-38, SC-40, SC-72
- **R11** — Environment/source/evidence provenance; scenarios: SC-32, SC-59, SC-60, SC-61
- **R12** — Local sibling fallback; no unsolicited alternate upload/retry; scenarios: SC-38, SC-39, SC-41, SC-42, SC-43
- **R13** — Typed tests and program outputs with truthful quality gates; scenarios: SC-44, SC-45, SC-46, SC-47, SC-48, SC-60, SC-73
- **R14** — Five first languages with qualified profiles; scenarios: SC-49, SC-50, SC-51, SC-52, SC-53, SC-54
- **R15** — Per-item personal approval and reversible temporary option; scenarios: SC-54, SC-62, SC-63
- **R16** — Ordinary-user native shell as a supported option; scenarios: SC-58
- **R17** — Safe concurrent author/provider changes; scenarios: SC-17
- **R18** — Recoverable migration preserving existing setup/evidence; scenarios: SC-66
- **R19** — Finite Compose lifecycle after core; scenarios: SC-68, SC-69
- **R20** — Unity last, bounded Editor qualification; scenarios: SC-70, SC-71

## Concrete cases

### SC-01 · Live GitHub happy path
Start/event: READY written with current token while idle.

Expected observable result: ACK in same file; one run; current branch snapshot; report in tested branch.

Owner: GP-03; requirements: R01, R03, R06; design evidence: model+git-local; live: **NOT_RUN**.

### SC-02 · Docs human happy path
Start/event: Human edits DRAFT and sets READY last.

Expected observable result: Runner sets ACCEPTED without local CLI; human can reuse document.

Owner: GP-04; requirements: R02, R03; design evidence: model; live: **NOT_RUN**.

### SC-03 · Docs chat happy path
Start/event: A chat uses the same registered Doc and revision-aware tool.

Expected observable result: Same protocol, receipt and results as human; no hidden local send step.

Owner: GP-04; requirements: R02, R03; design evidence: unrun-live; live: **NOT_RUN**.

### SC-04 · GitHub-only startup
Start/event: No Google account/SDK/configuration is present.

Expected observable result: Healthy GitHub intake and native tasks; zero required Google calls.

Owner: GP-02; requirements: R01; design evidence: unrun-live; live: **NOT_RUN**.

### SC-05 · Drive outage with healthy GitHub
Start/event: Enabled Drive provider fails independently.

Expected observable result: GitHub remains usable; Drive shows its own stale/failed state.

Owner: GP-02; requirements: R01, R02; design evidence: unrun-live; live: **NOT_RUN**.

### SC-06 · DRAFT or incomplete JSON
Start/event: Author pauses mid-edit.

Expected observable result: No candidate/running process; readable non-destructive intake diagnosis.

Owner: GP-04; requirements: R03; design evidence: model; live: **NOT_RUN**.

### SC-07 · Malformed READY
Start/event: Syntactically invalid command text.

Expected observable result: No execution and no guessed request ID; keep user text intact.

Owner: GP-04; requirements: R03; design evidence: unrun-live; live: **NOT_RUN**.

### SC-08 · READY edited before ACK
Start/event: Correct payload changes between read and conditional write.

Expected observable result: INPUT_CHANGED; no execution; changed draft not overwritten.

Owner: GP-03; requirements: R03, R04; design evidence: model; live: **NOT_RUN**.

### SC-09 · ACK lost response
Start/event: Provider applies ACK but response is lost.

Expected observable result: ACK_UNKNOWN; no spawn until exact proof; never guess.

Owner: GP-03; requirements: R03, R04; design evidence: model; live: **NOT_RUN**.

### SC-10 · ACK lost and proof retained
Start/event: Readback contains exact accepted token/request/hash.

Expected observable result: Reconcile within same session, at most one queue entry.

Owner: GP-04; requirements: R03, R04; design evidence: model; live: **NOT_RUN**.

### SC-11 · ACK lost and overwritten
Start/event: Newer command replaced old response; no exact proof remains.

Expected observable result: Explicit unresolved acceptance; no automatic run from guessed old intent.

Owner: GP-04; requirements: R03, R04; design evidence: model; live: **NOT_RUN**.

### SC-12 · Human identifiers
Start/event: Human repeats same task after prior ACCEPTED.

Expected observable result: Runner-generated ID/token advances; no UUID calculation required.

Owner: GP-04; requirements: R03; design evidence: model; live: **NOT_RUN**.

### SC-13 · Repeated observation
Start/event: Identical accepted file observed repeatedly.

Expected observable result: No second queue entry/run, even after result commit.

Owner: GP-03; requirements: R04; design evidence: model; live: **NOT_RUN**.

### SC-14 · Same ID through two transports
Start/event: Identical explicit request ID and payload in Docs/GitHub.

Expected observable result: Return same identity, no second execution.

Owner: GP-04; requirements: R04; design evidence: model; live: **NOT_RUN**.

### SC-15 · ID reused with changed payload
Start/event: New command changes intent under old explicit ID.

Expected observable result: REQUEST_ID_REUSED; frozen original unchanged.

Owner: GP-02; requirements: R04; design evidence: model; live: **NOT_RUN**.

### SC-16 · Live queue while busy
Start/event: One run active; second and third submissions ACKed.

Expected observable result: Single execution lane; others FIFO in acceptance order.

Owner: GP-02; requirements: R05; design evidence: model; live: **NOT_RUN**.

### SC-17 · Concurrent authors same slot
Start/event: Two agents read same file revision then publish different payloads.

Expected observable result: Second conditional writer detects conflict; no silent merge of intents.

Owner: GP-03; requirements: R03, R17; design evidence: unrun-live; live: **NOT_RUN**.

### SC-18 · Queue capacity
Start/event: Bound reached during continuous submission.

Expected observable result: Explicit QUEUE_FULL, never accepted-and-dropped.

Owner: GP-02; requirements: R05; design evidence: model; live: **NOT_RUN**.

### SC-19 · No offline backlog
Start/event: Runner starts with pre-existing READY.

Expected observable result: NEEDS_RESUBMIT; no history scan or automatic code execution.

Owner: GP-02; requirements: R05, R07; design evidence: model; live: **NOT_RUN**.

### SC-20 · Remote resubmission
Start/event: After startup baseline author resends via same slot.

Expected observable result: New live acceptance with no required local action.

Owner: GP-02; requirements: R03, R07; design evidence: model; live: **NOT_RUN**.

### SC-21 · Observation gap
Start/event: Provider unavailable/sleep breaks continuous observation.

Expected observable result: Baseline latest slot on recovery; unaccepted READY requires resubmission.

Owner: GP-02; requirements: R07; design evidence: model; live: **NOT_RUN**.

### SC-22 · Already accepted queue / provider outage
Start/event: Docs breaks but an accepted local-source run is ready.

Expected observable result: May execute accepted task within same session; do not discover old requests.

Owner: GP-02; requirements: R05, R07; design evidence: model; live: **NOT_RUN**.

### SC-23 · Restart with queued tasks
Start/event: Controller restarts before queued task started.

Expected observable result: SUSPENDED_RESTART; records retained, no automatic replay.

Owner: GP-02; requirements: R05, R07; design evidence: model; live: **NOT_RUN**.

### SC-24 · Crash after claim before spawn
Start/event: STARTING is durable; no process-start evidence.

Expected observable result: INTERRUPTED/uncertain; do not assume safe automatic rerun.

Owner: GP-05; requirements: R04, R07; design evidence: model; live: **NOT_RUN**.

### SC-25 · Crash while running
Start/event: Worker/process may survive controller.

Expected observable result: Reconcile process identity/cleanup, preserve partial evidence; no double run.

Owner: GP-05; requirements: R04, R07, R09; design evidence: model; live: **NOT_RUN**.

### SC-26 · Queue cancellation
Start/event: Task B waits behind long task A; cancel B via mailbox.

Expected observable result: Control bypasses FIFO; B never spawns.

Owner: GP-02; requirements: R05, R03; design evidence: model; live: **NOT_RUN**.

### SC-27 · Running cancellation
Start/event: Cancel active task with child/grandchild.

Expected observable result: Native lifecycle cleanup and partial output; prompt control response.

Owner: GP-05; requirements: R09; design evidence: model; live: **NOT_RUN**.

### SC-28 · Complete old / new draft
Start/event: A completes while author writes B in COMMAND.

Expected observable result: A result saved separately; B text and status unchanged.

Owner: GP-04; requirements: R03; design evidence: model; live: **NOT_RUN**.

### SC-29 · Current-at-start
Start/event: Branch changes while request waits.

Expected observable result: Run resolves latest fetched HEAD at start, not submission SHA.

Owner: GP-03; requirements: R06; design evidence: model; live: **NOT_RUN**.

### SC-30 · Source edit while running
Start/event: Author modifies workspace/branch after snapshot.

Expected observable result: Frozen input remains valid historically; no new-source pass claim.

Owner: GP-05; requirements: R06; design evidence: model; live: **NOT_RUN**.

### SC-31 · Remote unavailable at start
Start/event: Only stale remote-tracking cache exists.

Expected observable result: SOURCE_UNAVAILABLE; cached code not labelled current.

Owner: GP-03; requirements: R06; design evidence: model; live: **NOT_RUN**.

### SC-32 · Local dirty snapshot
Start/event: Explicit local_workspace includes dirty/untracked files.

Expected observable result: Record actual file snapshot and dirty provenance; never claim pure commit.

Owner: GP-05; requirements: R06, R11; design evidence: unrun-live; live: **NOT_RUN**.

### SC-33 · File changes during capture
Start/event: Editor writes while local snapshot copied.

Expected observable result: Retry bounded or INPUT_CHANGED; no mixed snapshot advertised.

Owner: GP-05; requirements: R06; design evidence: unrun-live; live: **NOT_RUN**.

### SC-34 · Branch disappeared
Start/event: Selected source/result branch is deleted.

Expected observable result: Source or delivery blocker; do not guess main or recreate branch.

Owner: GP-03; requirements: R06, R10; design evidence: model; live: **NOT_RUN**.

### SC-35 · Results while branch advances
Start/event: Test A, author pushes B during run.

Expected observable result: Append results to latest branch preserving B; report says tested A.

Owner: GP-03; requirements: R10; design evidence: git-local; live: **NOT_RUN**.

### SC-36 · Publication race
Start/event: Another commit wins after result parent was selected.

Expected observable result: Reject stale non-force push, rebuild only result patch; bounded attempts.

Owner: GP-03; requirements: R10; design evidence: git-local; live: **NOT_RUN**.

### SC-37 · Two runners one branch
Start/event: Different concrete runner IDs publish simultaneously.

Expected observable result: Distinct result subtrees retained; additive commit races handled.

Owner: GP-03; requirements: R08, R10; design evidence: git-local; live: **NOT_RUN**.

### SC-38 · Protected branch
Start/event: Result direct push denied.

Expected observable result: Keep complete local bundle beside source; no unsolicited PR/other branch.

Owner: GP-03; requirements: R10, R12; design evidence: unrun-live; live: **NOT_RUN**.

### SC-39 · GitHub down at finish
Start/event: Task completes, upload unavailable.

Expected observable result: Local result is truthful; no claim even pointer reached GitHub.

Owner: GP-03; requirements: R12; design evidence: model; live: **NOT_RUN**.

### SC-40 · Delivery response lost
Start/event: Push may have succeeded, confirmation missing.

Expected observable result: Exact commit/digest readback or UNKNOWN; no rerun.

Owner: GP-03; requirements: R04, R10; design evidence: unrun-live; live: **NOT_RUN**.

### SC-41 · Explicit redelivery
Start/event: Author requests existing local result again.

Expected observable result: Delivery only, same bytes/source, invocation count unchanged.

Owner: GP-03; requirements: R12, R04; design evidence: model; live: **NOT_RUN**.

### SC-42 · Oversized output
Start/event: Program produces result exceeding Git/provider budget.

Expected observable result: Keep original locally; report exact path/machine/hash/size/reason.

Owner: GP-03; requirements: R12; design evidence: model; live: **NOT_RUN**.

### SC-43 · Disk full
Start/event: Even minimal local write may fail.

Expected observable result: LOCAL_PERSISTENCE_FAILED, no false saved assertion; do not overwrite old evidence.

Owner: GP-05; requirements: R12; design evidence: model; live: **NOT_RUN**.

### SC-44 · Normal test failure
Start/event: Tests fail in a healthy execution route.

Expected observable result: Operation completes; test FAILED distinct from infra/delivery.

Owner: GP-06; requirements: R13; design evidence: model; live: **NOT_RUN**.

### SC-45 · Zero/all-skipped tests
Start/event: Exit 0 but no test executed.

Expected observable result: INSUFFICIENT by default, not PASSED.

Owner: GP-06; requirements: R13; design evidence: model; live: **NOT_RUN**.

### SC-46 · Damaged report
Start/event: Exit 0 with invalid required XML.

Expected observable result: Keep raw output, parse warning and insufficient gate; no invented counts.

Owner: GP-06; requirements: R13; design evidence: model; live: **NOT_RUN**.

### SC-47 · Missing program artifact
Start/event: Tests pass but required JSON/CSV/build is absent.

Expected observable result: Required-output gate fails independently of process code.

Owner: GP-06; requirements: R13; design evidence: model; live: **NOT_RUN**.

### SC-48 · Build-only project
Start/event: Task requests build without tests.

Expected observable result: Build result valid without invented test run/cases.

Owner: GP-06; requirements: R13; design evidence: model; live: **NOT_RUN**.

### SC-49 · Python native profiles
Start/event: Prepared pytest CLI/package on each OS.

Expected observable result: Real report, environment and declared program output.

Owner: GP-06; requirements: R14, R09; design evidence: unrun-live; live: **NOT_RUN**.

### SC-50 · JavaScript native profiles
Start/event: npm/Vitest project, cold and warm cache.

Expected observable result: Genuine reporter output and bundle; failed tests remain failed.

Owner: GP-06; requirements: R14, R09; design evidence: unrun-live; live: **NOT_RUN**.

### SC-51 · TypeScript typecheck failure
Start/event: Type errors but runtime tests could pass.

Expected observable result: Separate typecheck FAILED; do not hide behind test success.

Owner: GP-06; requirements: R14; design evidence: unrun-live; live: **NOT_RUN**.

### SC-52 · Java compile/dependency failure
Start/event: Maven fails before Surefire ran.

Expected observable result: Tests NOT_RUN; resolved deps/compile logs show actual phase.

Owner: GP-06; requirements: R14; design evidence: unrun-live; live: **NOT_RUN**.

### SC-53 · C# test host distinction
Start/event: VSTest vs MTP/target frameworks.

Expected observable result: Correct recipe and native reports; no silent logger assumption.

Owner: GP-06; requirements: R14; design evidence: unrun-live; live: **NOT_RUN**.

### SC-54 · Unknown framework/manager
Start/event: Jest/Gradle/pnpm or missing lock not qualified.

Expected observable result: Specific preparation proposal, not automatic framework migration.

Owner: GP-06; requirements: R14, R15; design evidence: unrun-live; live: **NOT_RUN**.

### SC-55 · Native Windows process tree
Start/event: Child/grandchild + cancel/crash/quoted Unicode paths.

Expected observable result: Job lifecycle tested; no POSIX imports, truthful outputs.

Owner: GP-05; requirements: R09; design evidence: unrun-live; live: **NOT_RUN**.

### SC-56 · Native macOS process tree
Start/event: Paths/locale/service+sleep/cancel fixture.

Expected observable result: Actual macOS execution, cleanup and LaunchAgent evidence.

Owner: GP-05; requirements: R09; design evidence: unrun-live; live: **NOT_RUN**.

### SC-57 · Native Linux process tree
Start/event: Existing service + ordinary-user tasks.

Expected observable result: Cancellation/process identity/storage evidence; existing rootless unaffected.

Owner: GP-05; requirements: R09; design evidence: unrun-live; live: **NOT_RUN**.

### SC-58 · Native shell
Start/event: Any repository writer selects shell with enrolled ordinary-user mode.

Expected observable result: No forced container/special account/per-command operator approval.

Owner: GP-05; requirements: R08, R16; design evidence: unrun-live; live: **NOT_RUN**.

### SC-59 · Container on different host
Start/event: Linux container on Windows/macOS.

Expected observable result: Host and guest recorded; not counted as native Windows/macOS.

Owner: GP-05; requirements: R09, R11; design evidence: unrun-live; live: **NOT_RUN**.

### SC-60 · Native output leakage check
Start/event: Program emits token-like value/ANSI/HTML.

Expected observable result: Raw local vs sanitized published variant distinguished; inert display.

Owner: GP-06; requirements: R11, R13; design evidence: unrun-live; live: **NOT_RUN**.

### SC-61 · Environment explanation
Start/event: Two runs differ in runtime/arch/locks/profile.

Expected observable result: Manifest captures differences, unknown facts remain null/unknown.

Owner: GP-06; requirements: R11; design evidence: unrun-live; live: **NOT_RUN**.

### SC-62 · Approval per item
Start/event: Owner approves only some proposed preparation changes.

Expected observable result: Only named approved diffs applied; no other file/CI/SDK changes.

Owner: GP-07; requirements: R15; design evidence: unrun-live; live: **NOT_RUN**.

### SC-63 · Temporary preparation
Start/event: Owner selects reversible per-run overlay.

Expected observable result: Original unchanged; overlay and tested source both recorded; checked rollback.

Owner: GP-07; requirements: R15, R06; design evidence: unrun-live; live: **NOT_RUN**.

### SC-64 · Permission model
Start/event: Trusted repository collaborator sends task.

Expected observable result: Accepted without additional operator ACL; scope remains enrolled repo/runner.

Owner: GP-03; requirements: R08; design evidence: unrun-live; live: **NOT_RUN**.

### SC-65 · Fresh chat without memory
Start/event: Read enrolled START HERE then send task.

Expected observable result: All IDs/contracts discoverable; no hidden workflow state or local send command.

Owner: GP-07; requirements: R01, R02, R03; design evidence: unrun-live; live: **NOT_RUN**.

### SC-66 · Upgrade rollback
Start/event: Migrate existing DB/service and roll back controlled copy.

Expected observable result: Historical evidence/Drive IDs preserved; old binary never opens new schema.

Owner: GP-07; requirements: R18; design evidence: unrun-live; live: **NOT_RUN**.

### SC-67 · Optional runtime unavailable
Start/event: Docker absent but native configured.

Expected observable result: Native works; unavailable container reports exact blocker.

Owner: GP-05; requirements: R09; design evidence: unrun-live; live: **NOT_RUN**.

### SC-68 · Compose readiness
Start/event: Container starts before DB is healthy.

Expected observable result: Explicit health readiness before test, not a fixed sleep.

Owner: GP-08; requirements: R19; design evidence: unrun-live; live: **NOT_RUN**.

### SC-69 · Compose cleanup failure
Start/event: Timeout/cancel and uncertain volumes/processes.

Expected observable result: Collect partial results; mark cleanup uncertain and block lane if necessary.

Owner: GP-08; requirements: R19; design evidence: unrun-live; live: **NOT_RUN**.

### SC-70 · Unity EditMode
Start/event: Installed compatible Editor/license and tiny pass/fail fixture.

Expected observable result: Real NUnit result, environment; no claim of visual/device coverage.

Owner: GP-09; requirements: R20; design evidence: unrun-live; live: **NOT_RUN**.

### SC-71 · Unity missing capability
Start/event: License/module/Test Framework unavailable.

Expected observable result: Specific blocker and retained logs, no fabricated failure/pass count.

Owner: GP-09; requirements: R20; design evidence: unrun-live; live: **NOT_RUN**.

### SC-72 · No results recursion
Start/event: Result/ACK commits become branch HEAD.

Expected observable result: No new submission; prior results excluded from next workload snapshot.

Owner: GP-03; requirements: R04, R06, R10; design evidence: unrun-live; live: **NOT_RUN**.

### SC-73 · Path/reference safety
Start/event: Output symlink/reparse/traversal or result-path collision.

Expected observable result: Do not collect unrelated path/overwrite other result; explicit error.

Owner: GP-06; requirements: R13; design evidence: unrun-live; live: **NOT_RUN**.

### SC-74 · Queue IDs on two installations
Start/event: Same runner ID reused accidentally.

Expected observable result: Enrolment conflict warning; no claim of distributed exactly-once.

Owner: GP-07; requirements: R08; design evidence: unrun-live; live: **NOT_RUN**.

## Qualification matrix

For initial language release execute 5 languages × 3 native OS targets × 2 transports × pass/fail = 60 real routes. Add cold/warm caches, missing tools, malformed reports, cancellation, restart, same-branch races and program-artifact gates from the cases above. Container checks report actual guest OS; Compose/Unity are additional. A fixture may cover multiple requirements only with explicit evidence links. Never count placeholder/no-op tests as project readiness.
