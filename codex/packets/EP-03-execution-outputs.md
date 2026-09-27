---
id: EP-03
status: BLOCKED
depends_on: ['EP-02']
---
# EP-03 — Contained execution and typed results

## Required inputs
`docs/OUTPUTS.md`, `docs/ENVIRONMENTS.md`, `docs/SECURITY.md`, `docs/RECOVERY.md`, `schemas/run-manifest.schema.json`, `schemas/operation-manifest.schema.json`, `schemas/output.schema.json`,
`bridge_reference/execution.py`, `config/profile.python-quick.json`.

## Outcome
Create a real execution broker with bounded resources and stable evidence. Project failures must
not turn the runner status red; infrastructure errors and output collection failures are explicit.

## Implementation tasks
1. Build an approved profile registry. Profiles are locally granted capabilities with pinned image
   digest, argv, cwd, mounts, user, memory/pid/cpu/deadline and network policy. A repository's
   suggested profile does not gain authority automatically. Templates may be granted once so
   future new projects need no repetitive configuration.
2. Use a rootless executor, no privileged mode/host PID/host network, no Docker socket, no runner
   DB, no Google/GitHub tokens, no SSH agent or home mount. Read-only source snapshot; dedicated
   writable build/output/tmp paths. Separate trusted dependency-preparation from test execution.
3. Capture exact raw stdout/stderr bytes, plus decoded safe views, stream identifiers and monotonic
   sequence/time. Do not fabricate a total inter-stream ordering. Drain output after a storage cap
   and record truncation; terminate/kill process groups/containers after a bounded timeout.
4. Collect outputs by approved relative paths/globs under run output root; reject escape/symlink,
   giant or malformed outputs. Store SHA-256, size, media type, schema, provenance and completeness.
5. Implement safe JUnit/JSONL/Cobertura parsers with size/depth limits and disabled external XML
   entities. Preserve parser failures/raw output. Never infer passed tests simply from exit code 0.
6. Add Markdown/text reports, screenshots, binaries and bundle manifests. Escape text in UI;
   serve generated HTML/SVG as untrusted content, never same-origin active code.
7. Privacy review/redaction precedes outbound upload. Local originals and sanitized exports have
   different hashes/IDs. Redaction is best effort, not proof that secrets cannot appear.
8. Separate operation/execution/publication/delivery status fields in versioned domain contracts.
   Implement output retention without deleting referenced or pending-publication evidence.

## Acceptance and evidence
- real rootless toy passing/failing/timeout/cancel runs on the target host;
- hostile toy cannot read credentials/home, access network, host socket or another workspace;
- fork/memory/log-volume limits are enforced, not merely declared;
- invalid UTF-8, ANSI, huge logs and malformed XML do not crash runner/UI;
- test failure still yields complete manifest/logs and healthy runner;
- disk-full/capture truncation/delivery-redaction cases have correct completeness metadata;
- cancelling cleans child processes without silently starting another operation.

## Scope boundary
If rootless execution is unavailable, stop the real-project execution gate. Do not quietly fall
back to unsandboxed `subprocess`. The included host capturer is only for trusted generated fixtures.

## Packet closing rule
Write `codex/checkpoints/EP-03.md` using the checkpoint template. Put machine evidence in
`verification/implementation/EP-03/`. Record actual commands, exit codes and environment identity.
Do not mark this packet VERIFIED if only mocks pass but the specified gate requires a real local
process. Live provider gates may be BLOCKED while offline implementation is tested, but the final
release remains not deployable until they pass.
