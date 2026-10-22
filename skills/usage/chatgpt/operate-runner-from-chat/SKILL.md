---
name: operate-runner-from-chat
description: "Submit, inspect, cancel, or redeliver one finite already-enrolled Chat-Dev Runner v2 operation through its registered GitHub command file or Docs tab from ordinary ChatGPT on explicit owner request."
metadata:
  version: "1.2.0-source-adaptation"
  maturity: "experimental"
  distribution: "public-copy"
---

# Direct ChatGPT → registered slot → Runner

> Public distribution copy of an owner-maintained canonical skill. Follow the actual installed Runner protocol and your enrolled project permissions. This file is not automatically installed or activated.

Use when a user explicitly requests a finite Runner run/test/status/control from an ordinary ChatGPT conversation. The Runner executes locally, and the chat only prepares a command and reads evidence. This is not Udot's multi-wave integration route or Scheduled Capsule Chat; merely reading a repository or a skill does not authorize a run.

For an explicitly requested package transfer, use [deliver-handoff-to-runner](../deliver-handoff-to-runner/SKILL.md) for upload, registered target and terminal delivery semantics, and reuse the slot procedure below. `DELIVER_HANDOFF` requires support in the selected installed Runner; its repository-local skill is not proof of deployment. It does not run code or require source/task/execution fields.

## Authoritative protocol
First read one coherent current revision of the Runner's [operational skill](../../../v2/operate-runner-v2/SKILL.md), [v2 operations](../../../../docs/V2_OPERATIONS.md) (submission/results sections) and [mailbox schema](../../../../schemas/v2-mailbox.schema.json). Check the installed version/qualification when material: a source preview, example IDs or merged code does not prove deployment. On a material mismatch, stop the dependent action rather than inventing a protocol. This skill is a ChatGPT entry point, not a competing copy of the Runner protocol.

## Finite slot submission
1. Resolve the real enrolled runner/project ID and exact command transport: GitHub **repository + mailbox branch + path**, or Docs **document ID + tab ID**. For task execution also resolve selected task, allowed OS/mode/profile/time budget and **source branch or registered workspace**. Source and command branches may differ. Handoff delivery instead resolves the registered target and Drive/result bindings through its dedicated skill. Read current project manifest/instructions if relevant; never infer enrollment from an example or name. Status inspection does not require launching a task.
2. Read the complete **live** mailbox and GitHub blob SHA, or full selected native Docs tab and document revision. Never overwrite an unacknowledged `READY`. Preserve the existing protocol, runner/project identities, current `slot_token` and runner-managed `receipt`. Reconcile `ACK_UNKNOWN`, `NEEDS_RESUBMIT` and uncertain interrupted work without automatic resubmission.
3. Build and validate a complete `DRAFT` against the live schema. Use `request_id: null` where supported, or a fresh UUID; changed intent requires a new explicit ID. Conditionally write DRAFT to the exact branch/path using the observed blob SHA, or exact Docs tab with `requiredRevisionId`, then reread it. For Docs use native tab indexes from the actual API; do not estimate them from rendered text length. The complete selected tab must parse as one JSON object, without code fences or trailing material.
4. As a **separate final guarded write**, change only `dispatch` to `READY` using the latest GitHub blob SHA or exact Docs document revision. With `batch_update_document`, the connector argument is `write_control.requiredRevisionId`; a merge-tolerant revision hint is insufficient. Scope each content request to the registered tab, preserving other tabs. On conflict reread and reconcile. Without actual conditional-write capability, retain the draft and report the blocked leg; do not claim submission or use an undisclosed local sender.
5. Read the matching Runner mailbox receipt and verify accepted slot token, request ID and status. `READY`, a successful remote write and `ACCEPTED` are **not** evidence of terminal execution, passed tests or delivered artifacts. ACK can release the slot while an accepted request remains in the existing v2 FIFO. Read the matching terminal result separately; missing/uncertain feedback calls for rereading the same identity, not resubmitting it.

For `STATUS`, `CANCEL` and `REDELIVER`, use a separately authorized new slot turn and exact target request ID, following the live action schema. Redelivery must reuse the original sealed bytes and source branch without rerunning code. Never add a model loop, API client, hidden Codex fallback, credentials or unenrolled shell command.

## Evidence and recovery
For `DELIVER_HANDOFF`, follow the [dedicated handoff skill](../deliver-handoff-to-runner/SKILL.md#confirm-and-hand-over): read its separately registered exact result Doc/tab and match terminal `cdr-handoff-state/1` to the request and handoff. Preserve the Runner-owned mailbox receipt. Report file delivery and receipt availability separately, return only the actual delivered Codex entrypoint/prompt, and do not apply task execution/quality or Git result assumptions to this action.

For GitHub-origin tasks on installed Runner **0.3.0.dev2 or later**, first read the matching **terminal state receipt** `.runner/state/<runner_id>/<request_id>.json` on the **mailbox branch**. It records final status, sanitized failure code, execution/quality, current bundle delivery and, for control actions, a scoped target view. This is separate from the initial mailbox ACK and can exist even when no test bundle was produced. Then read reports on the **tested source branch** under `.runner/results/<runner_id>/<request_id>/`, or an actually available authenticated local readback. Match `source.json` (source SHA, frozen workload), `result.json` (operation status, execution, quality gate, cleanup), `tests.json`, required outputs and relevant raw logs. Project test failure is not Runner infrastructure failure. Separate authoring, ACK, execution, quality, publication and availability; include exact IDs, commit and links, and mark unrun checks `NOT_RUN`.

The sealed `result.json` is an immutable **pre-delivery** snapshot: `LOCAL_ONLY / PUBLICATION_PENDING` may remain after results actually appeared in GitHub. Use the current terminal state receipt or actual remote files to verify delivery; a receipt’s `delivery` concerns the result bundle, not publication of the receipt itself. The earlier 0.3.0.dev1 canary showed `FORMAT_NOT_REGISTERED` warnings on ordinary raw stdout/stderr; 0.3.0.dev2 added opaque raw-log handling. Judge warnings from the **installed** version, not the historical result.

**Version/availability boundary (2026-10-20):** the old 0.3.0.dev1 preview sometimes exposed early failures and `STATUS` only locally. Installed 0.3.0.dev2 has real GitHub receipt proof for a successful task, an early pre-execution failure and `STATUS`, recorded in [live acceptance evidence](../../../../verification/implementation/GP-07/remote-state-20261020/live.json). An absent receipt on an older installation or during a GitHub outage still means **unknown from GitHub**, not success or ongoing execution. Inspect authenticated local status, reconcile before retrying, and never send a duplicate uncertain run.

An owner-private GitHub-only canary on 2026-10-20 tested pass → intentional failure → repaired pass; its private test repository is not publicly readable. Public [0.3.0.dev2 receipt acceptance evidence](../../../../verification/implementation/GP-07/remote-state-20261020/live.json) covers a passing task, early failure and STATUS reply. Neither qualifies queue stress, service restart, other OS profiles or every failure mode.

For Udot-accepted multi-wave implementation and synchronization, use [execute-roadmap-via-chat-runner](../../udot/execute-roadmap-via-chat-runner/SKILL.md). This skill does not permit scheduled chats to dispatch Runner work.
