# Results, publication and native-platform delivery

## 1. Local evidence exists before any upload
Suggested managed layout (example, installation resolves OS-appropriate absolute paths):

```
<managed-project>/
  repository/                   # controller-managed Git clone, not user's active checkout
  workspaces/<workspace>/       # registered working areas
  runs/<request>/work/          # private per-run working copy
  runner-results/<runner>/<request>/
    result.json
    summary.md
    environment.json
    tests/                      # original report + normalized facts
    logs/                       # bounded raw/decoded streams
    outputs/                    # declared program/build results
    artifacts.json
    delivery.json               # local mutable/append-only delivery view, not hashed into immutable result
```

`runner-results` is **beside** the managed source and survives cleanup of run scratch. No silent `/tmp` retention for the user's only large result. For an enrolled existing checkout, choose a sibling result directory during itemized setup approval; don't add ignored directories to a user repo without approval. Paths in remote metadata are informative locators, not downloadable URLs.

Freeze/copy declared outputs and calculate hashes before publication; outputs must not continue changing while hashed/uploaded. Retain raw and redacted variants with separate hashes and provenance. The remote result set goes under `.runner/results/<runner_id>/<request_id>/` of the tested branch. This is a reserved result subtree, not a separate results branch.

## 2. What the result says
Result envelope (contracts/result.schema.json) contains identity, source, tasks/phases, execution, quality gate, cleanup, local availability and each delivery destination. `operation_status=COMPLETED` can coexist with `execution.outcome=FAILED` and delivered results. A process never started is NOT_RUN, not FAILED_TESTS. Exit zero with damaged/missing required test evidence is quality gate INSUFFICIENT/FAILED, not pass. Counts come from recognized parsers. Opaque formats are kept without fabricated statistics.

Short `summary.md` answers: which task/runner/branch; what source actually ran; native or container; phase exit and test findings; whether required program outputs exist; what is published vs local; what is needed next. Summaries need not include all passing case details; retain machine reports and original error traces within explicit limits.

Application outputs may be JSON/CSV/HTML/images/archives/executables. Required JSON can be checked with declared JSON Schema, CSV with explicit header/type checks, binary build by existence/size/hash plus a separately declared smoke test. Existence of an executable does not prove it runs correctly. Keep test outcomes and program semantic output validity separate.

## 3. Environment/provenance contract
Record host OS + version/build + arch; execution OS + arch; native/container/WSL/VM/emulation; runner version/source revision; task/profile/schema revisions; selected interpreter/compiler/SDK/test runner versions; image digest/daemon/Compose/Unity versions when applicable; source commit/tree and workload/overlay digests; lockfile/resolved dependency evidence; timezone/locale/encoding; UTC start/end and monotonic duration; sanitized command identity; cache status; requested/enforced/measured budgets; exit code/signal/timeout; parser version and evidence completeness.

Optional hardware fields such as available CPU/memory help explain variability. Do not publish full hostname, public IP, arbitrary environment dumps, tokens, usernames or HOME contents by default. `runner_id` is the normal machine alias. Per-output fallback metadata is the intentional exception for exact local paths, because the owner requested locators. Native trust means these records are diagnostic evidence, not tamper-proof attestation against malicious same-user code.

Every capability carries evidence status: DESIGNED, IMPLEMENTED_NOT_QUALIFIED, VERIFIED_ON_TARGET, UNAVAILABLE. Don't mark a platform verified from source compatibility or a Linux container. A WSL run reports host Windows and execution Linux, and is not counted toward native Windows acceptance.

## 4. Same-branch result publication
The task records `source.branch` at start. That exact branch is the result target even if the command was on a different branch. Never derive it from 'current checked-out branch' after the task. Branch deletion/protection/auth failures trigger local fallback; no automatic PR or new branch.

Publisher algorithm:
1. Seal eligible bundle locally; reserve immutable output path containing runner ID and request ID; use a unique publication marker in the journal.
2. Fetch target branch's current HEAD B. Tested commit may be A; B may include other developers' commits. Construct a tree **from B** with only the new result subtree inserted; preserve every other path. If path already exists, accept only an exact matching bundle digest, otherwise RESULT_PATH_CONFLICT.
3. Create a new commit whose parent is B. Record intended commit before push. Push without force. If ref advanced concurrently, read the new head, reconstruct the same result-only subtree atop it, and retry within a small finite budget (proposed three conflicts). Do not rebase/overwrite source or rerun tests.
4. On success, read remote branch/commit and verify the intended result content. On lost response, query exact commit/manifest to determine delivered vs unknown. If readback is unavailable, `DELIVERY_UNKNOWN` remains local; do not blindly push a differently identified duplicate.
5. Write final local delivery record; optional Docs receipt may include the publication commit. No infinite background retry queue. A later explicit REDELIVER through either mailbox reuses sealed evidence.

GitHub ref `force=false` enforces fast-forward behavior [E02], but is not a universal REST compare-and-swap of an arbitrary expected_sha field. The same-parent competing-commit design and refresh-after-rejection supply the necessary optimistic protocol. The adapter must not assume a parameter absent from GitHub's API.

A result commit cannot include its own final SHA in its contents. The immutable result manifest stores tested source SHA and bundle hashes, not its own Git commit hash. Publication SHA lives in local/outside receipt or is derived when reading Git history. The artifact index doesn't hash itself. Avoid self-referential checksum loops.

Result/ACK/state commits never trigger task execution: only a fresh valid slot submission can. Other existing CI workflows may trigger on result commits; propose path filters as a separately approved source change, never modify CI silently. Branch moving from A to B does not invalidate the historical result for A, but the summary must not assert B passed. Differences only in reserved runner paths can be labelled control-only via a workload digest without hiding full commit identities.

## 5. Large files and failed publication
Ordinary GitHub Git files above 100 MiB are blocked [E03]. Use an installation-configured lower per-file/bundle budget (proposal 10 MiB per file and 25 MiB total, adjustable; policy limits are not provider limits). Keep large originals locally and put an artifacts.json entry with `availability=LOCAL_ONLY`, reason (`PROVIDER_SIZE_LIMIT`, `POLICY_SIZE_LIMIT`, `PUBLICATION_FAILED`, `SENSITIVE`), bytes, SHA-256, runner ID, exact local path and retention/pin state.

No automatic Releases, LFS, Drive offload or compression to evade the user's choice. Optional Drive delivery only occurs if explicitly configured/requested; GitHub-only users are fully supported. A failed overall publication keeps the entire bundle beside source; a GitHub outage means even the pointer cannot be posted there. Return it locally and through any actually available configured channel, don't invent remote visibility.

First stage performs no automatic deletion of unpublished/large fallback outputs. Display retention and disk usage; require explicit deletion/policy approval. Ordinary terminal scratch can be cleaned after evidence is frozen. When filesystem access fails, report `LOCAL_PERSISTENCE_FAILED`; do not claim 'saved next to the project' unless the exact bytes/path were verified.

## 6. Platform installation and recovery
Linux: extend existing user service, not a second daemon. Windows: ordinary user process plus approved per-user startup/Task Scheduler integration; no LocalSystem service by default. macOS: per-user launchd/LaunchAgent after approval [E13]. Foreground mode remains useful for qualification. Installation preflight reports permissions, executable discovery, credentials, filesystem and backend readiness; missing system tools require explicit item approval, no unattended elevation.

Platform seam covers private path creation, atomic replacement, singleton and process supervision. Current `drive_poller` uses Linux `renameat2`, existing runtime uses POSIX assumptions. Do not import unavailable `fcntl`, os.O_DIRECTORY/O_NOFOLLOW, UID helpers or signal APIs unconditionally on Windows. Use platform-specific secure primitives or explicit unsupported blocker, never substitute an unchecked copy while claiming the old invariant. Windows reparse points/case-insensitive collisions/locked files and macOS path Unicode/case behavior are test cases. Lock/rename behavior on network shares is not assumed equivalent; first qualify local filesystems.

Bootstrap/update: stop intake; finish/cancel active work explicitly; back up DB and credential references with service stopped; migrate atomically; install; start unarmed for controlled qualification; establish new slot sessions without replay; verify configured transports. Preserve exact existing Drive root/index and old evidence. Rollback means restore matching old binary+schema backup under stopped service; old code must not open a new unknown DB schema. Restart smoke tests must include a prior queued request and an ACK-unknown candidate.

A broken optional Drive config cannot prevent GitHub-only startup, and missing Docker cannot block an enrolled native task. OAuth/secret values stay local. Neither Codex nor Claude receives tokens embedded in the package. Each target OS requires real pass/fail/cancel/restart/capture/delivery evidence before it is advertised as qualified.
