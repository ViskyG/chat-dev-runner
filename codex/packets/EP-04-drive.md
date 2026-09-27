---
id: EP-04
status: IN_PROGRESS
depends_on: ['EP-01']
---
# EP-04 — Native Docs transport and result delivery

## Required inputs
`docs/DRIVE_PROTOCOL.md`, `drive-seed/README.md`, `drive-seed/00-control/CHAT_PROTOCOL.md`,
`scripts/bootstrap_drive.py`, `docs/SOURCES.md`, `schemas/command.schema.json`.

## Outcome
Implement the existing-ID Docs mailbox transport. It neither drives ChatGPT nor grants authority
because an instruction happens to exist in an arbitrary Drive file.

## Implementation tasks
1. Extend the supplied bootstrap with bounded retry/rate-limit handling, installed configuration
   and structured diagnostics. Preserve per-installation resource identity, ID mapping and no-overwrite
   behavior. Leave control unarmed and all templates DRAFT.
2. Keep OAuth tokens local. For this dedicated account, use the owner-authorized full
   Drive OAuth scope only. Validate the existing installation by exact IDs and preserve the
   provisioning path; scope breadth alone does not prove cross-app edit compatibility.
3. Register one installation command Doc plus per-workspace COMMAND/CHANGESET and runner-owned
   RECEIPT/LATEST RESULT/STATE Docs. Store exact IDs; never route by ambiguous filenames or shared
   folder contents alone. Include complete tab text or reject unsupported tabs explicitly.
4. Poll known command IDs with bounded backoff/jitter. Detect READY payload, revision and epoch;
   read/freeze the referenced change set, re-read revisions, reject concurrent changes. Final READY
   write uses Docs requiredRevisionId; a Doc update is not a transaction across several Docs.
5. Call the same admission service as local UI. No auto-execute after BUSY, expiry or machine
   wake-up in a new epoch. Do not overwrite a newer command while publishing an older receipt.
6. Publish an immutable run folder/index, sanitized artifacts, concise latest report and receipt.
   Publication may finish in bounded retries within the current operation deadline; on failure
   leave DELIVERY_PENDING and support explicit PUBLISH_REPORT without rerunning tests.
7. Export read-only project context for chat: file inventory, content hashes, base commit, active
   workspace, policy and relevant source; large/binary files are indexed rather than blindly mirrored.
8. Capability probe: chat reads/edits an inert canary Doc created by local OAuth, and local adapter
   observes the change. This gate is required before claiming cross-app write compatibility.

## Acceptance and evidence
- fake provider covers pagination, duplicate names, moved files, stale revision and partial writes;
- READY set before a changing change set never executes an incoherent snapshot;
- two identical polls generate one execution; two different commands within same mailbox scope
  cannot overwrite the active frozen input;
- old-epoch command after restart rejected; default bootstrap does no network calls;
- explicit authorized live canary read/write across both apps; record IDs, never token values;
- output delivery failure is recoverable without project rerun;
- no ChatGPT API/browser calls anywhere in the adapter.

## Scope boundary
A successful OAuth bootstrap alone does not prove ChatGPT can edit the created Docs. Mark the
canary gate unverified until both directions are observed through their actual interfaces.

## Packet closing rule
Write `codex/checkpoints/EP-04.md` using the checkpoint template. Put machine evidence in
`verification/implementation/EP-04/`. Record actual commands, exit codes and environment identity.
Do not mark this packet VERIFIED if only mocks pass but the specified gate requires a real local
process. Live provider gates may be BLOCKED while offline implementation is tested, but the final
release remains not deployable until they pass.
