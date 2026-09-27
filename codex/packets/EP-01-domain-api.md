---
id: EP-01
status: VERIFIED
depends_on: ['EP-00']
---
# EP-01 — Durable domain model and local API

## Required inputs
`docs/API_CONTRACT.md`, `docs/DRIVE_PROTOCOL.md`, `docs/RECOVERY.md`, all `schemas/`,
`bridge_reference/protocol.py`, `bridge_reference/ledger.py`, relevant `tests/test_reference.py`.

## Outcome
Implement the authority boundary shared by UI and Drive. An operation starts immediately if
admitted; otherwise it is terminally rejected. A mailbox is not a job queue.

## Implementation tasks
1. Model projects, workspaces, operations, steps, runs, outputs, remote resources and faults with
   durable IDs. Migrate a versioned SQLite schema with foreign keys and transactions enabled.
2. Validate JSON strictly: unknown keys, timezone-aware expiry, current arming epoch, payload
   limits, action-specific parameters and source mailbox identity. Freeze canonical payload and
   content digest. Do not normalize source-code newlines/Unicode while hashing bytes.
3. Implement atomic deduplication/admission with workspace, repository and global resource locks.
   Use nonblocking acquisition of the complete lock set in deterministic order; no hidden waiter.
   The single-workspace reference lock is not sufficient for project-wide git mutations.
4. Exempt authenticated read/status and targeted cancellation from execution slot requirements.
   Expired/stale commands receive terminal receipts; unfinished commands do not auto-resume.
5. Add API endpoints and a local session: validated Host/Origin, CSRF for mutations, no wildcard
   CORS, GET without side effects, no remote bind option. Browser origin policy is not enough.
6. Add an append-only step/effect journal before side-effect adapters. Receipt persistence and
   delivery are separate; keep a terminal local truth even when Drive is unavailable.
7. Implement reconciliation state for crashes with versioned schemas/read models. Avoid adding
   an unrecognized enum to v1 strict schemas without version/test/example updates.

## Acceptance and evidence
- concurrent identical requests: at most one admitted execution; duplicate returns same receipt;
- same ID / different content rejects without replacing original evidence;
- busy workspace/global/repository locks reject and NEVER launch later;
- cancellation reaches an executing task while all regular execution slots are occupied;
- expired/epoch-stale/unarmed/source-spoofed/malformed commands reject;
- forged Host/Origin/CSRF, GET mutation and path traversal all fail;
- DB restart preserves terminal receipts and interrupted jobs are not re-run.

## Scope boundary
Never promise distributed exactly-once behavior. Acceptance proves local admission and conservative
reconciliation, not atomic transactions across SQLite, GitHub and Google Drive.

## Packet closing rule
Write `codex/checkpoints/EP-01.md` using the checkpoint template. Put machine evidence in
`verification/implementation/EP-01/`. Record actual commands, exit codes and environment identity.
Do not mark this packet VERIFIED if only mocks pass but the specified gate requires a real local
process. Live provider gates may be BLOCKED while offline implementation is tested, but the final
release remains not deployable until they pass.
