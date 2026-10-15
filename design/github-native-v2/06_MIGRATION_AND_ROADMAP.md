# Integration and roadmap

## Baseline / delta
Read SOURCE_AUDIT.json for exact commit/blob anchors. Relevant observed seams:
- `engine/runtime.py`: arm_on_startup and construction are coupled to Drive; optional transport composition is a real refactor.
- `engine/service.py` and `storage/ledger.py`: immediate admission/no queue, parallel worker map; add live-session FIFO and a single lane, not just a new enum.
- `adapters/drive_poller.py`: immutable slot store, Linux renameat2 and direct Engine admission; add guarded ACK plus cross-platform storage seam.
- `adapters/github.py`: current provisioning/PR transport, GET/POST/PATCH only, small request/response budgets and limited headers; add mailbox/result needs deliberately rather than assuming it is already a general file transport.
- `executors/profiles.py`/`broker.py`: rootless Linux-only contract; keep it and add native executors, do not relax containment checks to claim portability.
- `docs/OUTPUTS.md`: useful independent outcome axes and evidence rules to reuse, not regenerate under incompatible names without a migration.

## Approval proposal for the implementing agent
Design itself is delivered. Before modifying the project, expand the following into exact patch-level item descriptions and ask the owner to approve each chosen item (a response listing IDs is enough). The same procedure applies to any user's project preparation.

| Item | Change / reason | Reversible alternative |
|---|---|---|
| AP-01 | Update AGENTS/docs/STATUS intent to final v2 decisions, explicitly superseding no-queue/Drive-only/rootless-only assumptions | Keep design sidecar only until approved |
| AP-02 | Add versioned v2 contracts and compatibility readers; retain v1 | Isolated branch with schema tests |
| AP-03 | Add DB migration for session/candidate/queue state and one execution lane | Backup DB, test copied DB before live migration |
| AP-04 | Decouple runtime from mandatory Drive; register GitHub-only bindings | Test configuration in separate disposable runtime |
| AP-05 | Add GitHub mailbox and exact ACK, same-branch result publication | Disposable repo/branch; no real project binding yet |
| AP-06 | Upgrade existing Docs mailbox protocol with runner-written ACK/status | Existing canary doc first; backup exact text/revision; don't duplicate installation |
| AP-07 | Add native executors/platform helpers and explicit shell mode | Opt-in native profile in disposable working copy |
| AP-08 | Add first five-language profiles and parser contracts | Per-run overlays/temporary configs rather than source edits where workable |
| AP-09 | Update user UI/status and bilingual skills/navigation registration | Package skill drafts only; install after approval |
| AP-10 | Install/upgrade user service on each selected native OS | Foreground qualification first, backup/rollback plan |
| AP-11 | Add finite Compose managed task | Temporary project/DB/volumes, never production stack |
| AP-12 | Add Unity qualification profile | Disposable Editor project, no edits to game originals |

Each actual approval record includes baseline file hash, exact proposed diff (or stable digest), reason, without-change behavior, temporary option, reversal, owner decision and timestamp/source. Approval is not inferred from a model-written phrase. Changed scope invalidates the corresponding approval; don't re-ask unchanged approved items. New env/config/CI/reporting changes in user projects get their own numbered proposal.

## Packet graph and release boundaries
GP-00 -> GP-01 -> GP-02 -> GP-03; GP-02 also enables GP-04.
GP-01 + GP-02 -> GP-05 (native platform executors); GP-03 + GP-05 -> GP-06 (language qualification).
GP-03 + GP-04 + GP-05 + GP-06 -> GP-07 (skills, release integration and target acceptance).
GP-07 -> GP-08 (Compose) -> GP-09 (Unity spike).

| Packet | User-visible result | Release gate |
|---|---|---|
| GP-00 | Exact source map, approved scope, backup/recovery baseline | No changes without approved items; old evidence preserved |
| GP-01 | v2 contracts, lifecycle and migration plan wired into tests | Protocol tests + old manifest readers; no source/runtime claim from schemas alone |
| GP-02 | One live FIFO and optional provider runtime | no offline replay, one lane, cancel bypass, no mandatory Drive |
| GP-03 | GitHub command -> existing qualified Python/container run -> same-branch report | actual GitHub ACK/readback, result concurrency and local fallback |
| GP-04 | Docs command status flips automatically; two transports share engine | human/chat DRAFT/READY/ACK and stale-revision/crash tests |
| GP-05 | Native Linux/Windows/macOS plus explicit container mode | real platform process lifecycle, filesystem, restart and shell fixtures |
| GP-06 | Python/JS/TS/Java/C# tests, builds, finite app outputs | genuine framework reports, required artifacts, cold/warm setup |
| GP-07 | Complete install/use/prepare skills and release | native/platform/transport matrix, permissions, upgrade/rollback |
| GP-08 | Finite Compose stack test and cleanup | real unhealthy-service/cancel/isolation/local fallback scenarios |
| GP-09 | Unity EditMode proof and bounded expansion plan | Editor/license/module/error/cleanup evidence, no full Unity promise |

GP-03 deliberately proves GitHub transport using the existing executor first; GP-05 does not require inventing all toolchains before native lifecycle tests. Windows/macOS platform probes begin within GP-05 before GP-06 is declared complete; they are not replaced by a late WSL fallback. Missing target hardware leaves that capability NOT_QUALIFIED, not a fake green check. It need not stop independent approved GitHub/Linux work.

Parallel work: after GP-01 freeze, provider adapters and platform backend modules can be developed independently. One integration owner changes Engine/Ledger/runtime; other workers do not edit the same dispatcher concurrently. GP-03 publisher owns results-only Git writes; GP-04 owns Docs field edits. Shared schemas change only through a versioned integration decision.

## Tests and evidence economy
Reuse unmodified applicable evidence. Run focused parser/profile tests after local edits; run broader regression at protocol/ledger/runtime and release boundaries. Per-language success is not evidence for native OS service recovery. Existing old no-queue tests must be replaced with explicit new live-queue tests, not silently deleted. Keep anti-replay and immutable evidence tests.

Minimum first-language/native matrix: 5 languages x 3 native OS targets x 2 transports x pass/fail = 60 end-to-end cases. Container/Compose/Unity, special failure cases and Git conflict/restart tests are additional. It is an acceptance target, not work performed in this design session. Every result stores actual runtime versions/architecture; one run may satisfy multiple requirements only where it directly demonstrates them.

## Old EP-07 and previous next-stage material
Do not force implementation to finish unrelated old packets first merely because old START_HERE says first incomplete packet. GP-00 reconciles the actual EP-07 release gaps with this stage: reuse installed evidence, carry forward relevant unfinished recovery routes, and mark old claims obsolete when behavior changes. Prior feasibility/model artifacts with NO QUEUE, results branch, offline backlog or isolated-user assumptions are superseded by this package. Do not import them as authoritative tests.

## Handoff discipline
Only the selected packet's reading set is mandatory. Checkpoint includes observed checkout SHA, approved item IDs, changed files, tests really run, platform/provider blockers, remaining work and safe next packet. No auto scheduler/model loop. Final publication must verify remote commit/content after any subsequently authorized push. No capsule registry/Udot changes were requested or performed in this design handoff; packet graph is one local plan, not a second external execution queue.
