# Handoff integration roadmap — additive to current v2

Baseline `d1e930a5ebdf87f5ab64fb70914420b6d18db03b`, not the old ac7d55dd kit.
Do not apply an old v1 epoch/TTL command shape to the installed v2 reusable slot.
Read current AGENTS/STATUS and current v2 protocol before coding. Do not rewrite the existing service.

## H0 — Bind authority and expose the action (no live dispatch)

Read `config_v2.py`, `domain/contracts_v2.py`, `schemas/v2-mailbox.schema.json`, `storage/intake_v2.py`,
`engine/intake_v2.py`, `engine/runtime_v2.py`, `engine/queue_v2.py`, and project source resolution.
The first three v2 composition paths were read directly for this kit; other related files must be opened,
not assumed to have the exact older shape.

Add explicit owner-local project→handoff target enrollment; bind to registered stable workspace or approved
project inbox, with source intake folder IDs and budgets. Never use temporary run sources as editor targets.
Separate HANDOFF permission from execution profiles and .runner/project.json readiness. Registration creates
inbox only once and refuses existing tracked/conflicting/symlink paths. Exclude the inbox from context/snapshot
capture. Preserve all existing v2 queue, shell/profile, auth and v1 compatibility behavior.

Add `DELIVER_HANDOFF` to a version-negotiated compatible command schema; port `cdr-handoff/1` checks. Add
project-scoped dispatch in the existing V2Assembly handler; do not send it into NativeTaskRunner's RUN_TASK path.
Keep binding, actual request ID and authorization in trusted assembly. The example manifest is data, not authority.

Proof: focused schema/config/provenance/intake tests; unknown action on old versions, unregistered target,
project mismatch, absent local path and source-only GitHub registration all refuse without provider effects.
No full provider smoke here. Acceptance: a fake-provider accepted request reaches the delivery handler with
only locally resolved authority; no source executable/task is invoked.

## H1 — Transfer core, durable outcome, receipt

Use `handoff_reference/contracts.py` and `delivery.py` as tested starting code, not a new production service.
Move names/typing/error conventions into the current Runner architecture. Reuse existing private-file, effect,
backup and provider infrastructure where it gives the same or stronger guarantees. Do not retain duplicate
security utility implementations just to match this kit.

Implement Google blob metadata + bounded media streaming using existing OAuth transport; no arbitrary URLs,
no native-document export on this action, no new scopes silently. Register the allowed intake folder.
Wire cancellation and deadlines into each SDK request; streamed byte limits alone do not bound a blocked call.
The reference callbacks check time between chunks only; production HTTP timeouts are still required.

Journal exact handoff intent before I/O and local final-directory proof before publishing terminal status.
Extend current result/receipt handling for a non-execution action: no fabricated run ID, tests, quality gate,
source commit or PR. Prefer a typed handoff result inside TaskOutcome; examine current publish_state and schemas
rather than forcing it through test-report code. Register exact Drive output Doc(s) and publish compact receipt
there for a Docs-origin handoff, with current local/remote outcomes kept separate. GitHub-only command transport
can use its current state receipt when configured; never put a private absolute host path in public metadata.

Port path/ZIP/size/hash tests. Add delivery/restart boundaries: failure before rename; rename succeeded but DB
ack lost; duplicate request; same handoff via a fresh accepted slot turn; modified installed file; receipt upload
failure; cancellation before/after local commit. Preserve installed data on uncertainty. No queue redesign.

Proof: focused IO/provider fakes plus real disposable Git workspace preserving dirty, staged, index and HEAD.
At stable source run the relevant v2 integration suite once. Reuse applicable earlier evidence.
Acceptance: the complete handoff is visible in its unique directory with a verified receipt and no source mutation.

## H2 — Chat skill, local UI and one live qualification

Wire project inbox read model into existing localhost UI: list received packages, exact target path, copy Codex
prompt, safe folder/editor link. Do not render arbitrary package HTML as same-origin active content. The prompt
points to trusted START_HERE, not a shell command. No automatic Codex run.

Distribute the candidate `deliver-handoff-to-runner` skill through the repo's current usage-skill convention,
with provenance. Do not overwrite the owner's canonical library without an explicit separate publishing request.
Reuse current operate-runner skill for v2 slot behavior. Do not duplicate a stale protocol.

Live gate must prove the *actual chat tool surface* can upload one small inert file/ZIP and conditionally update
the exact registered Docs tab. This turn's discovery exposed read-only Drive actions, so that capability is NOT
proved here. Never substitute browser scraping or quietly enqueue SHELL to download a chat attachment.
A user-selected manual upload is a permitted fallback, but mark the first leg MANUAL, not automatic.

Use a single disposable enrolled target with an existing dirty sentinel. Upload a ZIP containing a tiny roadmap
and one text file, record bytes+hash, submit a single new action, read matching ACK then terminal handoff receipt,
independently inspect target bytes and unchanged tracked/index/HEAD state. Repeat *readback* or explicit same-ID
status to prove no duplicate; do not relaunch tests just for another green result. Prove a malformed archive with
fakes unless a release gate specifically needs live staging evidence. Test returned prompt against the actual path.

Before deployment use existing backup/upgrade/rollback process; no duplicate daemon or Drive root. Mark source,
fake-provider, live provider, installed service and chat-authoring qualification separately. Finish with exact
changes, focused tests, reused evidence, one live gate result or explicit blocked capability.

## Work boundaries / stop conditions

No run-on-host fallback is needed: this operation only transfers inert data. Archive decoding is still untrusted
input handling; enforce memory/disk/time budgets. Preserve current v2 lane behavior, even though older v1 documents
in this conversation prohibited queues. Do not retrofit v1 restrictions into v2.

Do not finish unrelated old EP/NX/GP acceptance just because this is a new feature. Map the needed existing
capabilities and reuse valid evidence. Do not claim full project readiness from the standalone tests in this kit.
