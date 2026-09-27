---
id: CDR-OUTPUTS
status: accepted-design
---
# Typed execution results

## Result axes

`operation_status`: COMPLETED, REJECTED, BLOCKED, FAULT, INTERRUPTED, CANCELLED.
`execution.outcome`: PASSED, FAILED, TIMED_OUT, CANCELLED, NOT_RUN, UNKNOWN.
`publication.status`: NOT_REQUESTED, PUSHED, PR_UPDATED, NO_DIFF, BLOCKED, FAILED, UNKNOWN.
`delivery.status`: NOT_ATTEMPTED, PENDING, DELIVERED, PARTIAL, FAILED.

Publication and operation-level effects are defined in `operation-manifest.schema.json`.
A project-create/fetch/publication operation may have an empty `run_ids` list but still collect
runner logs and typed outputs. Do not invent a test run merely to attach provisioning logs.
A program run uses `run-manifest.schema.json` for its execution/snapshot/delivery evidence.
`output.schema.json` is the common per-output contract; both manifests currently inline the same
versioned structure (covered by a parity test). The UI's operation inspector must expose these
non-test outputs even when the workspace has no test history. The offline UI only illustrates
operation metadata there; full operation-output viewing is an explicit EP-06 task.

A failed unit test produces a valid COMPLETED operation with execution=FAILED. A missing
container runtime is an executor/infrastructure problem. Invalid JUnit produced by the
project is an artifact parsing warning; preserve the file rather than reporting fabricated
zero failed tests. An OAuth outage affects delivery/publication, not the past test outcome.

## Universal capture

Always record exit code or explicit absence, duration, start/end timestamps, input snapshot,
profile revision, command identity (sanitized), and separate stdout/stderr raw streams.
`stdout.bin` preserves bytes; `stdout.txt` is a UTF-8 display view and may contain replacement
characters. Never claim exact interleaving between independent streams. A merged view uses
observed timestamps/sequence and labels this limitation.

Full capture means complete **within explicit local limits**. Unbounded logs can fill disk.
Default reference limit is small; production uses per-stream, per-run, per-project and global
budgets. Drain streams even when retention limits are reached so a process cannot deadlock.
Record observed bytes, retained bytes, truncation reason and completeness. If disk reserve is
exhausted, stop/limit capture by policy and preserve a minimal receipt. Never silently truncate.

## Format registry

| Kind | Initial formats | Display and extraction |
|---|---|---|
| console | raw bytes, text/plain UTF-8 | stream selector, tail, exact raw download |
| application_log | application-log/v1 JSONL, plain text | level/service/time filters; raw fallback |
| test_result | JUnit XML | cases, failures, skipped, durations, source parser/version |
| coverage | Cobertura XML; opaque HTML bundle | totals only when parsed; reports separate |
| report | Markdown, JSON, CSV, plain text | escaped preview and file export |
| screenshot | PNG, JPEG, WebP | bounded dimensions and preview |
| artifact | ZIP, binaries, other MIME types | size/hash metadata and download |
| runner_log | runner-event/v1 JSONL | operational diagnostics, never project-quality score |

The high-level kinds are stable. Add parser formats through a registry keyed by kind + format
+ version. Unknown format stays an opaque artifact; it must not fail the whole operation.
Use bounded parser input, disallow XML external entities/DTD expansion, limit nested JSON,
file counts, image dimensions and zip expansion ratios. No unsafe object deserialization.

Normalized application event contract: `schemas/application-log.schema.json`. This is our own
versioned convention, not a claim that every logging framework emits it. Fields:
`timestamp`, `observed_timestamp`, `level`, `service`, `event`, `message`, `project_id`,
`workspace_id`, `run_id`, `attributes`. Preserve original JSONL; adapters may map field names.
Do not assume the project already emits this schema or manufacture unknown levels/timestamps.
Malformed lines get per-line warnings and escaped raw content. Events are data, not commands.

## Artifact collection

Profiles declare paths relative to a dedicated output root, required/optional status, kind,
format and upload policy. No arbitrary absolute paths, symlinks, parent traversal or globs
outside that root. Copy/freeze outputs before manifest hash computation. An HTML bundle's
relative files are collected as a set with individual hashes; it cannot reach the host's files.
Optional missing outputs are reported as absent; required missing outputs create a collection
finding without changing an independently known process exit code.

Each output records ID, format, media type, byte size, hash, source, completeness and privacy.
No compact summary substitutes for raw evidence. No raw evidence automatically bypasses
privacy controls just because a program printed it.

## Privacy and UI rendering

Raw outputs remain local by default with retention controls. Only profile-allowlisted,
redacted/sanitized outputs go to Drive. Record redaction provenance and separate hashes for
raw vs uploaded variants; never label a redacted file as byte-identical to raw output.
Secret scanning reduces accidental exposure but is not a guarantee; suspicious files are
quarantined and require explicit local handling. Never upload environment dumps by default.

Render text with escaping/textContent. ANSI terminal escapes cannot become HTML or terminal
control commands. HTML reports are downloaded or rendered in a restrictive sandboxed frame
on a separate unprivileged origin; no same-origin script access, navigation, fetch or forms.
SVG is treated as active/untrusted content; sanitize or download rather than inline arbitrary
SVG from a project. The prototype's own generated SVG sample is not a live artifact renderer.

## Chat-first compact report

The first read should answer: what request; which project/workspace; which input snapshot;
what actually executed; what was learned; where are the evidence files; did publication and
delivery finish; has the editable workspace changed since the snapshot?

Keep the summary bounded (target ~2–6 KB, not an absolute correctness limit). Failed test IDs,
representative traces and new error signatures are selected summaries with links to complete
retained evidence. Counts must come from recognized parsers, never guessed from console
text. A PUBLISH_REPORT request reuses frozen evidence and must not trigger a new process.

Retention default proposal: 14 days local raw logs, 90 days compact manifests, pinned runs
until explicit unpin. These are owner-adjustable installation decisions. The immutable v1
run/output manifests remain unchanged when an output expires. SQLite migration 6 adds an
append-only local `output_tombstones` index; `output_retention_index()` shows the original
hash/size and an explicit `EXPIRED` state, and authenticated download returns
`EVIDENCE_EXPIRED` rather than silently treating a historical link as unknown.

`RunEvidenceStore.expire_output()` is a trusted local retention primitive, not a network
command or an automatic sweeper. It defaults to a 14-day age, checks terminal
execution, pending/failed delivery, uncertain effects, active `PUBLISH_REPORT` claims and
owner pin events inside one SQLite writer transaction. The `PUBLISH_REPORT` claim is inserted
in its admission transaction only for an existing same-scope terminal contained run;
a successful delivered report releases its claim only when the operation finishes.
Existing valid report requests are backfilled conservatively on migration. A later report admission is
rejected if any output of its source run is already tombstoned. Incomplete or failed
reports keep their claim until a future explicit recovery policy resolves them.

A stable owner-only per-run `flock` coordinates readers and the expirer across processes.
Readers seal and hash the output before releasing their shared lock; expiration requires
an exclusive lock, commits the tombstone first, and then unlinks only the checked private
file. If unlink fails or the process crashes, the tombstone still blocks access and a
retry can complete physical removal. The local owner can add an append-only PIN/UNPIN
decision; an active pin prevents expiry. This does not yet schedule automatic retention
or remove 90-day manifest records.
