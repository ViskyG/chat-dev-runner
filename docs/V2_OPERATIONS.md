# Runner v2 operations — Linux preview / предварительные инструкции

**Linux preview, installed package 0.3.0.dev6.** The [runtime audit](AUDIT_2026-10-22.md)
adds independent mailbox polling and the finite Compose backend. Handoff delivery
passed a connector-only ZIP upload, guarded Docs DRAFT/READY and terminal readback
with no sender `drive_version` or SDK metadata helper. Runner pins the API version
before download and checks it afterward; see [H3](../codex/checkpoints/handoff/H3.md).
The earlier assisted ZIP+text transfer remains historical evidence in
[H2](../codex/checkpoints/handoff/H2.md).
Earlier installed-wheel upgrade/rollback and existing v1 readback are recorded
under `verification/implementation/GP-07/release`.
Provider and target qualification remains specific to its evidence. On 2026-10-16,
Docs v2 passed its live canary and installed STATUS route; connected-chat read
and guarded DRAFT write also passed. Other native OS targets are not qualified.
The dedicated slot recorded for test project `gp03-github-pass`, runner
`local-runner`, tab `t.0`, is
[DOCS V2 COMMAND](https://docs.google.com/document/d/1soKWAI1yyesh05xABAi78bb61Rri2nTFEWcetYSsFyU/edit).
It was left as DRAFT at that checkpoint; read its live token/receipt before
preparing another intent.
Other project mailboxes retain their existing registrations. The bilingual entrypoints are in
[skills/v2](../skills/v2/README.md). Their verbatim design sources remain unchanged
under [design/github-native-v2/skills](../design/github-native-v2/skills/README.md).
Source-stage approval does not itself authorize live deployment, global skill
registration, credential grants or system SDK installation. Reuse authorization
already given; ask only about a new consequential action outside that scope.

## Owner configuration and service

The existing owner-user service `chat-dev-runner.service` reads
`<Paths.config_dir>/runtime-v2.json`. Default XDG locations are
`~/.config/chat-dev-runner` for configuration and
`~/.local/share/chat-dev-runner` for durable state; preserve the actual service's
XDG settings. Do not introduce a second daemon or change its loopback listener.

[config/runtime-v2.example.json](../config/runtime-v2.example.json) is an inert
format example. Its repository IDs, document/tab IDs and absolute paths are
illustrative, not registered resources. Do not install it unchanged. Replace
identities from the owner's enrollment record, remove unused channels/profiles,
and save only the reviewed owner-local configuration with mode **0600**, in a
**0700** configuration directory. It contains path references, never credential
values. Keep config, token files and rootless grants outside repositories.

- `runner_id` must match the existing durable ledger and every enrolled slot.
  The example uses the legacy default `local-runner`; read the actual identity
  instead of renaming a live runner. Mismatch is refused.
- `enable_v1: true` retains loading of existing v1 integration configuration.
  `false` explicitly disables those routes without deleting their resources.
  Keep existing v1 attachment IDs and history when retaining v1.
- GitHub enrollment pins `id`, `node_id`, `owner`, `owner_id` and `name` from the
  actual repository, plus each existing mailbox branch and path under
  `.runner/commands/`. No repository/branch/PR is provisioned by these fields.
  The current runtime binds private repositories. Mailbox ACK and result
  publication need the approved repository Contents read/write capability;
  read-only access does not prove either write path.
- `github_token_file` points to the existing approved single-token ASCII file,
  optionally newline-terminated, mode 0600 with immediate parent mode 0700.
  Keep the owner's **selected repositories** choice. Manually add only the
  intended repository when needed; “All repositories” is not a requirement.
- `google_config_file` is optional. GitHub-only operation needs no Google setup.
  For Docs, it references an owner-private provider JSON containing
  `oauth_client_file`, `token_file`, `scope`, `root_folder_id` and
  `installation_index_document_id`; optional `legacy_manifest_file` is an
  existing attachment reference. The supported credential layout remains
  `google-oauth-client.json` and `tokens/google-drive.json` in that same existing
  config directory. Scope is `https://www.googleapis.com/auth/drive`.
  Preserve root `177IEHeBVezHIlbIijZsIvRDSbTJ42kxe` and installation index
  `1x2KIBZCZfHs2w-s_Ah3yMW80elgG4KseTe7OZq7L_hA` exactly. No bootstrap,
  duplicate root/index, scope migration or replacement OAuth grant is implied.
  A v2 slot is an exact dedicated `document_id` **and** `tab_id`; protected v1
  installation/mailbox documents cannot be repurposed as v2 slots.
- `native_profiles` bind explicit installed tool paths and finite budgets.
  npm can be `[absolute_node, absolute_npm_cli_js]`; do not rely on a Windows
  `.cmd` shim. Vitest/tsc resolve from the selected project's `node_modules`
  after the explicit `npm ci` phase, not from global tools or `npx` auto-fetch.
  Missing tools/locks/reporters require a concrete preparation proposal.
- `workspaces` are approved local bindings; `include_dirty` and
  `include_untracked` are owner rules, not grants a mailbox can enlarge.
  The latter contains exact relative paths or directory prefixes ending `/`.
- Optional project `handoff_targets` maps each `target_id` to an existing
  `workspace_id`, one or more `allowed_drive_parents`, optional `limits`,
  `timeout_seconds` and `request_timeout_seconds`, and paired
  `result_document_id` + `result_tab_id`. The example's `editor-handoff` target
  selects its registered `editor` workspace; all Drive/Docs IDs are placeholders.
  A GitHub-only project has no local handoff target until the owner registers a
  persistent workspace and target. The chat cannot supply or alter a host path.
  Default target selection is allowed only when exactly one target is enrolled.
- Handoff limits are checked against the implementation bounds; unspecified
  values use [the handoff contract defaults](../runner_app/handoff/contracts.py).
  The optional overall deadline defaults to 120 seconds (allowed 1–600), and
  each blocking provider-call deadline to 15 seconds (allowed 1–30). These are
  delivery limits, not execution budgets. The owner-configured Drive folder
  controls raw-file inputs; public sharing, a second root or new OAuth scopes
  are unnecessary.
- Both result IDs must be supplied together for confirmed Docs-origin handoff
  feedback. Enroll a dedicated result document and exact tab, separate from all
  command documents and protected v1 resources. Start its selected tab empty or
  with `{"protocol":"cdr-handoff-results/1","results":{}}`. Runner publishes
  terminal results there using a revision guard; the existing command slot and
  its Runner-owned ACK receipt retain their normal roles. Do not enroll an
  unrelated existing tab or create these resources from an example implicitly.
- Single-container support uses backend `rootless-docker`, enrolled profile
  `python-quick`, and an existing approved `rootless_profile_dir` with matching
  `python-quick-grant.json`, profiles, pinned image and hard-quota helper.
  Its task must match the fixed one-step test argv/cwd/timeout and approved
  outputs; arbitrary container tasks and container SHELL are refused.
- Finite Linux stacks use backend `rootless-compose` and a separate private
  `compose_profile_dir` with hash-pinned profiles. The intended project must list
  the profile in `container_profiles`. See [Compose tasks](COMPOSE_TASKS.md) for
  supported input, readiness/migration/test ordering, output retention and cleanup.

Native execution runs as the chosen ordinary user and can access that user's
host files. A per-run copy and environment shaping provide repeatability, not a
sandbox. Shell execution requires an explicitly enrolled profile/dialect;
normal use within that existing capability needs no new per-command approval.
Linux evidence does not qualify Windows/macOS/WSL. Missing SDKs, unsupported
platforms or unavailable cleanup primitives remain explicit gates.

Use existing commands for owner-local service diagnostics:

```bash
runner --version
runner doctor --offline
runner status
```

These do not submit a task. Normal configured `runner serve` and the existing
user service arm intake on startup unless reconciliation blocks it.
`runner serve --start-unarmed` keeps intake off for that process. For a controlled
preview, stop the existing service first; never run two instances against the
same state. The authenticated UI's v2 view and `GET /api/v2/status`,
`GET /api/v2/requests`, `GET /api/v2/requests/<request_id>` provide readback through
the existing local session boundary. No v2 task-send or v2 arm API is added;
use the normal service startup path for configured intake. The UI is not a
required intermediary for remote submission.

## Project preparation

For new registrations or a project missing from chat, follow
[Project enrollment / Регистрация проекта](PROJECT_ENROLLMENT.md). It covers the
requested runtime, mailbox and discovery bindings and their independent readback.
Creating a repository or adding a local workspace is only part of a chat route.

Use the [prepare skill](../skills/v2/prepare-runner-project-v2/SKILL.md) when
preparation is requested. Preserve existing framework, CI, branch and dirty
files. `.runner/project.json` uses `cdr-project/2`; its `project_id`, selected
`task` and task `profile_id` must match enrollment. Tool/schema declarations do
not grant host authority. The four [calculator examples](../examples/v2/README.md)
show real Python, JS, TS and .NET/VSTest recipes. Java/Maven remains unavailable
where no installed binding exists; installing a system SDK is a separate item.

Select `github_branch` to fetch the exact selected branch at execution start,
or `local_workspace` with a registered `workspace_id` and its actual branch.
Local dirty/untracked inclusion follows the configured rules. Detached source
has no inferred result branch. Source is copied and frozen before preparation;
results/command/state/handoff subtrees and credential material are excluded while
`.runner/project.json` and approved task scripts remain. Report original commit
and actual workload/overlay digest separately.

## Fresh-chat remote submission

Start from the owner's enrollment handoff: runner/project identity, repository
and mailbox **branch + path**, or exact **document ID + tab ID**, allowed task,
source branch/workspace and backend/profile. Read that live slot and current
receipt. A design example is not a live mailbox. A source branch can differ
from the command-file branch; always state the tested source branch explicitly.

1. Read the exact slot's full JSON and GitHub blob SHA, or the selected Docs
   tab plus document revision. Do not overwrite an unacknowledged `READY`.
2. Reuse the acknowledged slot by conditionally writing `DRAFT` first. Preserve
   `protocol`, `runner_id`, `project_id`, the **current** `slot_token`, and the
   runner-managed `receipt`. Do not invent/rotate a token or carry one from an
   earlier session. Prepare the new `request`; authors may use
   `"request_id": null`, and the Runner derives identity from this slot turn.
3. As a final separate conditional update change only `dispatch` to `READY`.
   GitHub writes need the last-read blob SHA and explicit branch, or an
   equivalent guarded non-force Git update preserving unrelated paths. Docs
   writes need the selected tab and exact document revision guard
   (`requiredRevisionId` in the actual batch API). Reread conflicts; never
   blind-overwrite or invent connector write/conditional-update capability.
   If the current connector lacks that capability, retain the draft and state
   what is unavailable. No local send CLI, local button or local hash is needed.
4. Read back the matching Runner receipt: its `accepted_slot_token` refers to
   the token just submitted, and `request_id` identifies the durable request.
   ACK rotates/releases the reusable slot; another request may be drafted
   while the accepted one is queued/running. READY/write success/ACK is not a
   terminal execution result. Explicit cross-channel dedup uses one fresh UUID
   for the same intent; changed intent always gets a new UUID or null-ID turn.

For a native task, the `request` object is concretely:

```json
{
  "request_id": null,
  "action": "RUN_TASK",
  "source": {"kind": "github_branch", "branch": "example-work"},
  "task": "test",
  "execution": {"mode": "native", "expected_os": "linux", "profile_id": "python-native", "timeout_seconds": 900}
}
```

This is a **request fragment for an existing DRAFT**, not a complete mailbox or
a READY submission. Replace illustrative IDs/branch/profile with enrollment.
For local source use `{"kind":"local_workspace","branch":"example-work",
"workspace_id":"editor"}`. For the fixed container route select
`mode: "container"`, `backend_id: "rootless-docker"`, `profile_id: "python-quick"`
and the qualified guest `expected_os`; the budget must cover preparation+test.
For an enrolled finite stack select `backend_id: "rootless-compose"` and its
actual profile/task; the budget also covers readiness, migrations and cleanup.

One execution lane is shared by live-accepted v2 FIFO work and retained v1
execution. `CANCEL` and `STATUS` target an exact request and bypass execution
waiting; `REDELIVER` is a separately accepted finite delivery action. Each uses
only `request_id`, `action` and `target_request_id`, for example:

```json
{"request_id": null, "action": "REDELIVER", "target_request_id": "41e4b458-140e-4e39-a2ca-9afc84744aca"}
```

Use the current slot token and the same DRAFT/conditional-READY procedure for
these controls. No model loop or historical command scanning is part of intake.
Startup/observation gaps baseline the slot; old READY becomes `NEEDS_RESUBMIT`.
Read its fresh token and resubmit only on current user intent. Accepted queued
work suspended on restart is not automatically replayed. `ACK_UNKNOWN` and
uncertain cleanup require exact reconciliation, not a duplicate run.

## Handoff delivery to a registered local inbox

Use [deliver-handoff-to-runner](../skills/usage/chatgpt/deliver-handoff-to-runner/SKILL.md)
([Russian](../skills/usage/chatgpt/deliver-handoff-to-runner/SKILL.ru.md)) when
the owner explicitly asks ordinary ChatGPT to transfer specified design,
roadmap, implementation, asset or reference files. Require installed
`DELIVER_HANDOFF` support and the actual enrollment before dispatch. The
unmodified `d1e930a5` baseline did not accept this action. Existing v1 and v2
task/control routes keep their contracts.

This operation uses the existing v2 FIFO and one local Runner. It does not run
an archive or script, apply source, install dependencies, invoke Codex, create a
commit/PR, or grant native/container execution. The owner separately copies the
delivered prompt into Codex and authorizes subsequent work. `purpose` and a
recorded design `base_commit` describe the package; they are not execution
instructions or a source-selection grant.

1. Resolve the exact runner/project/target, allowed Drive folder, registered
   command Doc/tab and separately registered result Doc/tab. Confirm that the
   current chat exposes real raw-file upload, independent metadata reading and
   guarded native Docs tab updates. A connected app name is not capability proof.
2. Treat one ZIP or several files as one package. Preserve each original's
   exact bytes and compute size/SHA-256. Upload ordinary Drive files into the
   allowed folder without converting them to Docs or changing sharing. Make a
   separate metadata read of each returned Drive ID to confirm parent, ordinary
   MIME type, filename, size and non-trashed state. Native Docs and
   arbitrary download URLs are not handoff inputs. Freeze those observations
   in one manifest and do not modify the uploaded files after publication.
   From **0.3.0.dev6**, artifact `drive_version` is optional; the runtime advertises
   `handoff_optional_drive_version: true`. Include a genuinely observed version
   as a strict sender pin, or omit the field when the connector drops it. Null,
   a guessed `"1"` or a Docs revision is not an omitted version. Runner reads the
   exact Drive file's numeric version before download and rejects any version
   change afterward. All ID/parent/type/size/SHA-256, download and archive checks
   remain mandatory. The submitted manifest and its digest are never rewritten
   with the resolved version. Older installations still require the explicit pin.
3. Validate `request.handoff` against the canonical
   [handoff-manifest.schema.json](../schemas/handoff-manifest.schema.json),
   then the complete mailbox against
   [v2-mailbox.schema.json](../schemas/v2-mailbox.schema.json). Set
   `request.action` to `DELIVER_HANDOFF`, use one stable handoff UUID and a fresh
   request identity for the intent. Do not add source, task, execution, shell,
   absolute-path or URL fields. A package revision needs a new handoff ID.
   ZIP expansion is opt-in through `unpack: "zip"` under the enrolled bounds;
   otherwise preserve the archive with `unpack: "none"`.
4. Reuse the **Fresh-chat remote submission** procedure above: read the full
   live command tab/current token/revision, preserve the Runner-owned receipt,
   write and reread one guarded DRAFT, then separately change only dispatch to
   READY against the latest revision. The Google Drive connector's native batch
   argument is `write_control.requiredRevisionId`; scope content requests to
   the exact tab and use real native indexes. Never overwrite unacknowledged
   READY. A conflict or uncertain write requires reading the same identity and
   reconciling it, not a blind resend or another hidden queue.
5. Read the matching ACK, including `accepted_slot_token` and `request_id`.
   Then read the terminal result for that request as described below. A file
   upload, READY write or ACK alone does not prove local delivery.

If upload is unavailable, retain the prepared package and identify the missing
leg. The explicitly labelled **MANUAL_UPLOAD** fallback lets the owner upload
the same bytes into the exact allowed folder and supply real Drive file IDs;
the chat must still verify the metadata before dispatch. It does not establish
automatic end-to-end delivery. If guarded Docs writing is unavailable, retain
the prepared DRAFT and report **TRANSPORT_BLOCKED** without publishing READY.
Do not replace either missing leg with a hidden local sender, UI scraper,
arbitrary URL or credential grant.

Runner downloads only the manifest's exact raw Drive IDs, checks the enrolled
parent and observed version before and after the bounded download, and verifies
actual size/SHA-256. It stages originals byte-for-byte and optional safe ZIP
content, then publishes one new unique directory:

```text
<registered workspace>/.runner/handoffs/<handoff_id>/
  files/<artifact_id>/<filename>   # exact originals
  content/<artifact_id>/           # populated for requested ZIP expansion
  manifest.json
  delivery.json
  START_HERE.md
  CODEX_PROMPT.txt
```

There is no extraction over the project root. A matching handoff ID + manifest
digest reconciles the existing delivery; another manifest with that ID is a
conflict. An uncertain local commit is reconciled from receipt/inventory before
any further effect. Missing or changed delivered files are not permission to
overwrite the destination. Cancel before local commit can stop delivery; a
completed local commit is not reported as cancelled because feedback failed.

The inbox is excluded from source/context capture, including dirty/untracked
inclusion. Its creation can still be visible to Git as untracked content:
`handoff.inbox_visible_in_git` reports that fact. Runner preserves HEAD, index
and tracked files, refuses an already tracked handoff inbox, and does not edit
`.gitignore` or `.git/info/exclude`. Ignoring or removing received files is a
separate owner decision.

### Handoff terminal result and Codex prompt

The dedicated result tab contains `cdr-handoff-results/1`; read
`results[request_id]` for the accepted request. Its payload is
`cdr-handoff-state/1`, separate from the mailbox ACK. Successful local delivery
has `status: "DELIVERED"`, `operation_status: "COMPLETED"` and
`handoff.local_delivery_status: "DELIVERED"`. Match runner/project/request IDs,
`handoff_id`, `target_id`, `manifest_sha256`, `files_count` and the returned
safe project-relative paths: `safe_relative_handoff_path`, `codex_entrypoint`
and `delivery_receipt`. `codex_entrypoint` points to the trusted generated
`START_HERE.md`; it does not directly select an untrusted archive instruction.

The payload's top-level `codex_prompt` is the exact text of the generated,
verified local `CODEX_PROMPT.txt`, using project-relative paths. Return that text
for the owner to copy and the actual entrypoint path. Do not invent absolute
host paths or download links. These are non-executable delivery results; no
tested commit, test count, quality gate or PR is implied.

The authenticated local request record retains `result.receipt_publication`
independently of `result.handoff`: a successful verified remote readback is
`CONFIRMED / DELIVERY_RESULT_CONFIRMED`; a lost write/readback can remain
`UNCONFIRMED / DELIVERY_RESULT_UNCONFIRMED`. A definite publication rejection
may be `FAILED` with its observed code. Publication failure never deletes a
confirmed local package or turns local delivery into failed execution. If the
remote result is absent, chat must report delivery as unknown from that channel
unless an authenticated local readback supplies evidence. Do not infer success
from an ACK or infer failed local delivery from a missing cloud receipt.

Report `UPLOADED_TO_DRIVE`, `ACCEPTED_BY_RUNNER`, `DELIVERED_LOCAL` and
`REMOTE_RECEIPT_CONFIRMED` as separate observed facts. Use bounded readback;
later status requests reread the known request/handoff instead of reuploading or
sending another `DELIVER_HANDOFF`. Separately authorized STATUS/REDELIVER controls
use the current v2 slot and exact target request. For handoffs, these reconcile
the known delivery and refresh receipt publication without rerunning input or
redownloading a confirmed package. A restart never grants automatic replay.

## Results, availability and redelivery

The following execution/result-bundle rules apply to task operations. For
`DELIVER_HANDOFF`, use the separate terminal contract above.

For GitHub-origin commands, read `.runner/state/<runner_id>/<request_id>.json`
on the **mailbox branch** for terminal `status`, sanitized failure `code`,
execution and current bundle delivery. This receipt also covers errors before
execution and the outcome of `STATUS`/`CANCEL`; controls include a scoped
`target` view. It is separate from the runner-managed mailbox acknowledgement.
Docs-origin tasks use their selected source branch when a GitHub delivery
transport exists. A receipt is schema version 1 and can be updated; result
bundles stay immutable. Its `delivery` describes the bundle, not publication of
the receipt itself. `null` means no bundle delivery was attempted.

`result.json` records delivery at sealing time, so its historical
`LOCAL_ONLY / PUBLICATION_PENDING` does not describe subsequent publication.
Use the state receipt for the current delivery outcome and commit. An explicit
`STATUS` or `REDELIVER` also refreshes the target receipt without rerunning its
task. If GitHub is unavailable, receipt delivery is journaled locally and a
later explicit control can retry it; no automatic execution replay is added.

Read the terminal request by its receipt ID. Keep separate: queue/admission,
`operation_status`, `execution.outcome`, `execution.quality_gate`,
`execution.cleanup`, output availability and delivery. Exit 0 with no tests,
all-skipped tests or missing/invalid required reports is not a passing test gate.
A failed project test is not automatically a Runner infrastructure failure.

Local bundles are retained under
`<Paths.state_dir>/projects-v2/<project>/runner-results/<runner>/<request>/bundle`.
Eligible results are added under `.runner/results/<runner>/<request>/` in the
**tested source branch**, preserving later source commits. No automatic PR,
new result branch, forced push or large-file hosting is introduced. Large or
unpublishable outputs retain exact runner/path/size/SHA-256/reason locators;
a filesystem path is not a download URL. GitHub failure can prevent even a
remote locator from being delivered.

The sealed `result.json` is immutable historical evidence; its initial delivery
view does not become the latest by rewriting it. Local `delivery.json` and the
original request API record's `result.delivery` hold the current delivery view.
An explicit `REDELIVER` targets the exact original request in the same
runner/project, verifies its expected local bundle path and SHA-256, and
reuses those bytes and the recorded tested branch. It never re-runs code or
accepts a replacement branch/bundle/path from the new command.

## Upgrade and rollback boundary

Preview source is not an installed wheel. Candidate **0.3.0.dev3** migrates the
durable database to **schema 11** for additive handoff admission. A retained
0.3.0.dev2 installation normally uses schema 10; earlier v1 installations use
schema 9. Record the actual installed version/schema before any candidate
launch. The old binary rejects a newer schema; removing handoff configuration
does not reverse the migration.

The existing user installer requires
a wheel; an editable/source install is rejected with `WHEEL_INSTALL_REQUIRED`.
Keep the old binary/environment and its approved artifact identity intact.
Before the first schema-11 launch, use the **currently installed retained
binary**, with the existing service stopped, for the pre-upgrade backup. The
paths below are placeholders, not deployment instructions for a known host:

```bash
/absolute/retained-installed/bin/runner stop
/absolute/retained-installed/bin/runner backup --output /absolute/private-backups/pre-handoff-state
```

Record and verify the backup's schema/version/digest and restoration on a
disposable copy. Config and credential values are excluded from `runner backup`;
retain owner-local config references and protected configuration separately.
Handoff inboxes live in enrolled workspaces outside the Runner state directory;
preserve those artifacts separately when needed. A state backup or restore does
not itself copy, delete or undo a previously delivered workspace package.
Candidate backup revision 2 includes v2 source/execution/result directories and
preserves sealed files and executable modes; revision-1 archives remain readable.
The earlier schema-9/schema-10 scratch round trip is recorded in
`verification/implementation/GP-07/v2-backup/result.json`.
It does not qualify the candidate schema-11 upgrade or rollback. The same
matching-binary/pre-upgrade-backup rule applies to a first schema-9 → schema-10
v2 upgrade; do not treat the handoff example as an existing deployment.
After approved wheel/config changes, `runner install --dry-run` inspects the
existing user-service plan; apply only the authorized deployment and then
qualify real mailbox/provider/result readback before claiming deployed support.

Rollback requires a stopped service, the matching retained old binary **and**
its matching pre-upgrade state/config binding. Never open schema 11 with the
schema-10 or schema-9 binary, or schema 10 with a schema-9 binary. Do not treat
removing `runtime-v2.json` as database rollback. Use the retained binary's
`restore --from /absolute/private-backups/pre-handoff-state` only after
preserving/quarantining the newer state and satisfying the restore command's
absent-target requirement. Preserve uncertain process/provider effects for
reconciliation; restoring a backup is not permission to replay old READY work.
