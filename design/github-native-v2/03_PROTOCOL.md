# Mailbox / queue protocol v2 — normative design

## 1. One reusable slot, not an offline request-file archive
For GitHub use `.runner/commands/<runner_id>.json` in one registered command branch. Each file is a reusable slot for its specific runner, **not** `<request_id>.json`. Default is one slot per runner/project; adding more named slots is not part of this stage. For Docs use the existing registered command document and exact tab; no folder crawl or new document for every command.

Only the latest content of each known slot is inspected. Never search historical commits/revisions for missed submissions. Git result commits do not change command blob identity. The watched branch and the tested branch may differ; the result always goes to the tested branch. Do not introduce a separate command repository requirement.

A single slot cannot safely retain several unaccepted commands while the runner is offline. The author must not overwrite READY until the runner marks it accepted/rejected/needs-resubmit. This limitation is intentional and matches the owner decision. Two writers editing the same slot still need provider optimistic-concurrency discipline; additional operator roles would not solve that race.

## 2. Envelope and division of writers
The proposed schema is `contracts/mailbox.schema.json`.
- `protocol`: `cdr-mailbox/2`.
- `runner_id`, `project_id`: enrolled exact IDs. `execution.expected_os` denotes the intended execution OS: native must match host; a Linux container on Windows still declares Linux here.
- `slot_token`: UUID issued/rotated by runner; author preserves it.
- `dispatch`: DRAFT or READY authored; ACCEPTED/REJECTED/NEEDS_RESUBMIT written by runner.
- `request`: task payload, optional explicit `request_id`; null permits human use without inventing UUIDs.
- `receipt`: runner-managed response `{request_id, accepted_slot_token, payload_sha256, status, queue_sequence}` or null.

For a human: change DRAFT payload, set READY last, observe the returned receipt. Runner generates the accepted request ID if absent. On ACK it rotates slot_token automatically, so editing the next command does not require manually inventing an ID. `receipt.request_id` identifies the previous accepted task even while the new slot_token is ready for later use. Automated authors may supply a fresh explicit request_id (including cross-transport deduplication); never reuse an explicit ID for changed intent.

Generated request identity = UUIDv5 over stable installation namespace + enrolled mailbox ID + consumed slot_token. Explicit request_id overrides that derivation, but is unique per runner. Digest the semantic request plus project/runner; exclude dispatch, receipt and slot_token. Use specified UTF-8 compact sorted JSON with no floats/NaN in contract fields. Newline/formatting outside literal script strings does not change the semantic digest. Do not normalize the literal script contents or filenames. The package's model uses the same digest rule but is not a production parser.

## 3. Session and no-offline-replay rules
A new service start creates a fresh live intake session. Current observed slot contents become a baseline, not executable events. An unaccepted READY present at that baseline is marked NEEDS_RESUBMIT using conditional update, preserving request text and issuing a fresh token. No local button is needed: rewrite DRAFT then READY in the same remote slot.

A valid DRAFT at baseline may simply receive a fresh current token and remain DRAFT. New READY using a current token after synchronization is admissible. Token/session metadata is prefilled by runner, not manually computed by the user. Repeated reads do not constitute fresh intent.

A material observation gap (process stopped, computer sleep detected, provider/auth/read failure breaking observation continuity) ends that mailbox's intake continuity. On restoration, baseline the latest slot rather than accepting an unknown backlog. Conservative default: require resubmission of an unaccepted READY visible at this boundary. This can reject a request written immediately around a short outage; report `NEEDS_RESUBMIT/OBSERVATION_GAP`, not a silent loss. No claim of perfectly classifying unobserved events by timestamps.

Already live-accepted entries do not become offline imports. They may continue during a provider outage if their selected source can actually be resolved and their backend remains usable. A current remote source that cannot be fetched fails explicitly; never use an old cache as 'current'. On controller restart, queued entries are SUSPENDED_RESTART; running/starting entries are INTERRUPTED or NEEDS_RECONCILIATION. They never auto-replay. Clean shutdown records queued cancellation/session-ended before exiting where possible. Reports remain queryable.

## 4. Acceptance is a durable conditional handoff
The word ACCEPTED must not precede durable storage. Conversely project code cannot start merely because a candidate was stored.

1. Read current exact provider revision. Validate full JSON, scope, runner, token/session, schema, request identity and envelope budget; freeze referenced Drive changesets if applicable. DRAFT/malformed text never runs.
2. Short SQLite transaction reserves candidate and receipt identity, stores immutable payload bytes/digest, source revision and ACK intent. State `ACK_PENDING`, not runnable. Do not reserve execution capacity or hold a network call inside the DB transaction.
3. Conditional provider write changes only dispatch/runner receipt/token. For Docs use `requiredRevisionId`, **not** merge-oriented `targetRevisionId`; exact tab and UTF-16 ranges must be correct. For GitHub make a result-independent commit based on the observed branch HEAD and update the branch without force; if branch advanced, re-read command. Retry against the new head only when the command blob is exactly unchanged. Do not overwrite changed payload even when JSON still parses.
4. The step-3 acknowledgement carries `receipt.status=ACCEPTED` and `queue_sequence=null`: the intent is durable but the sequence is not yet finalized. Once that write is acknowledged/proven, a short transaction stores ACK proof, assigns one increasing sequence and marks QUEUED. Expose that sequence in the per-request/status view; do not unconditionally rewrite the reusable COMMAND to fill it in. Any optional same-slot enrichment must match the same request, token and current revision, otherwise skip it. The author may reuse the slot as soon as ACCEPTED is visible. Queue sequence is an ordering fact, not a promise about finish time.
5. Worker atomically claims the oldest runnable QUEUED request in current session, only if no active lane. Claim must be durable before spawning anything.

Known revision conflict: abort candidate with INPUT_CHANGED; no code runs; do not overwrite the changed document. Provider write error with known no effect: ACK_FAILED, no execution. Lost response: ACK_UNKNOWN, no execution until readback proves the exact ACK. If current slot still contains the accepted receipt/token, reconcile. Git may also prove the ack via the exact intended commit; this is effect reconciliation, not history-based intake. Google Docs does not promise that old revision text can be retrieved. If the author replaced an ACK before lost-response recovery and no proof survives, retain explicit ambiguity and require a new submission; never infer success from a newer unrelated document.

On crash after ACK and before local finalization: recover candidate evidence, but under the session rule do not launch it automatically. Show suspended/uncertain status. This small gap is real; SQLite and Google/Git are not one atomic transaction. A receipt identifies accepted intent, not a guarantee of execution across power loss.

## 5. Repeats, edits, and malformed input
Same explicit request ID + same semantic digest => return existing receipt; no new sequence or process, even through the other provider. For an already finalized request, conditionally clear the new slot's READY and attach a duplicate-of receipt/rotate its token without creating another operation. A duplicate while original ACK is still pending/unknown cannot falsely report completed acceptance; expose that pending state until proof exists. Changed payload under same ID => REQUEST_ID_REUSED. A deliberate rerun needs new identity; generated identities advance when token rotates.

Never consume another request from an already reserved token. Editing a READY request before ACK risks INPUT_CHANGED; author must switch to DRAFT first. Editing payload after ACCEPTED cannot affect frozen intent. Successful completion publishes a per-request result and optional status view, never rewrites a newer COMMAND payload/status.

Invalid JSON with no reliable token/ID: write a bounded separate intake status, do not rewrite the body or invent a receipt ID. Valid envelope with invalid request: conditional REJECTED, error details, fresh slot token so it is editable again. It is possible for a complete-looking READY command to express the author's wrong intent; no parser can detect that. No debounce timer is treated as proof of completion.

## 6. Queue and controls
FIFO is the order of durable ACK finalization. Sender clocks, commit timestamps and request names do not set priority. Poll bindings in stable round-robin order; commands first observed in one sweep are serialized by acceptance, not a purported global author time.

One lane across v1/v2 tasks, projects and native/container modes. Queue bound is an installation budget (proposed 100 entries, adjustable); full => explicit QUEUE_FULL with no acceptance, not data loss. No priorities, batch arrays or cross-runner work stealing in this stage.

`CANCEL` and `STATUS` are control messages handled outside the execution FIFO, otherwise a long-running task could make cancellation impossible. Cancellation is still addressed by exact request ID and goes through the same remote slot. QUEUED -> CANCELLED before spawn; RUNNING -> cancellation signal, bounded cleanup, partial evidence. A cancel may also retire an ACK_PENDING/ACK_UNKNOWN candidate that is provably never runnable; preserve the acknowledgement uncertainty as history. Cancel never changes already published facts. `REDELIVER` is a new finite queued delivery-only task reusing immutable evidence. No automatic infinite retry and no rerun of project code.

Multiple runners: every submission has a concrete runner_id. Different IDs can run simultaneously; their result paths include runner_id + request_id. Same source branch publishing races are resolved as additive result commits. The system is not claiming distributed exactly-once execution across cloned runner databases.

## 7. Source binding while waiting
Request references source selector and task name, not frozen branch HEAD. At dequeue: fetch/read the selected current source, pin it, load that version of project task contract, and record task+lock+profile hashes. These may differ from what the author saw at submission; that is current-at-start by design. Planned source modifications remain separately subject to approved patches/base checks. A user edit during execution has no effect on the frozen run.

## 8. Transport operations
GitHub read: branch HEAD -> exact command blob at that commit. Conditional polling can use ETag/Last-Modified, retaining response headers and treating 304 as successful observation. Current adapter needs new allowed operations/headers or a separate mailbox transport; do not shoehorn binary artifacts into its 128 KiB JSON body cap.

GitHub ACK and result publishing may use local Git object construction and non-forced push from a dedicated managed clone. Pin safe Git config, disable hooks for controller plumbing, no credentials in URLs. Per-binding relative paths are validated and branches selected explicitly. A returned 'push succeeded' is followed by remote readback of the intended commit/tree.

Docs uses registered file/tab ID and revision-guarded edit. Rich-text stylistic changes can conflict but cannot broaden authority. Keep full command as one JSON object and instructions in a different document. The schema allows the prefilled runner-managed fields; chat tools that cannot perform a conditional write must report the transport limitation, not switch to a mandatory local CLI.
