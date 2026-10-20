# Runner v2 operations — Linux preview / предварительные инструкции

**Linux preview, package 0.3.0.dev2.** Installed-wheel upgrade/rollback and the
existing v1 readback are recorded under `verification/implementation/GP-07/release`.
Provider and target qualification remains specific to its evidence. On 2026-10-16,
Docs v2 passed its live canary and installed STATUS route; connected-chat read
and guarded DRAFT write also passed. Other native OS targets are not qualified.
The current dedicated slot for test project `gp03-github-pass`, runner
`local-runner`, tab `t.0`, is
[DOCS V2 COMMAND](https://docs.google.com/document/d/1soKWAI1yyesh05xABAi78bb61Rri2nTFEWcetYSsFyU/edit).
It is left as DRAFT; read its live token/receipt before preparing another intent.
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
- Container support is only backend `rootless-docker`, enrolled profile
  `python-quick`, and an existing approved `rootless_profile_dir` with matching
  `python-quick-grant.json`, profiles, pinned image and hard-quota helper.
  Its task must match the fixed one-step test argv/cwd/timeout and approved
  outputs; arbitrary container tasks and container SHELL are refused.

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
results/command/state subtrees and credential material are excluded while
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

## Results, availability and redelivery

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

Preview source is not an installed wheel. The existing user installer requires
a wheel; an editable/source install is rejected with `WHEEL_INSTALL_REQUIRED`.
Keep the old binary/environment and its approved artifact identity intact.
Before the first schema-10 launch, use the **retained schema-9 binary**, with the
existing service stopped, for the pre-upgrade backup:

```bash
/absolute/retained-v1/bin/runner stop
/absolute/retained-v1/bin/runner backup --output /absolute/private-backups/pre-v2-schema9
```

Record and verify the backup's schema/version/digest and restoration on a
disposable copy. Config and credential values are excluded from `runner backup`;
retain owner-local config references and protected configuration separately.
Candidate backup revision 2 includes v2 source/execution/result directories and
preserves sealed files and executable modes; revision-1 archives remain readable.
The actual scratch round trip is recorded in
`verification/implementation/GP-07/v2-backup/result.json`.
After approved wheel/config changes, `runner install --dry-run` inspects the
existing user-service plan; apply only the authorized deployment and then
qualify real mailbox/provider/result readback before claiming deployed support.

Rollback requires a stopped service, the matching retained old binary **and**
the pre-upgrade schema-9 state/config binding. Never open schema 10 with the old
binary or treat removing `runtime-v2.json` as database rollback. Use the retained
binary's `restore --from /absolute/private-backups/pre-v2-schema9` only after
preserving/quarantining the newer state and satisfying the restore command's
absent-target requirement. Preserve uncertain process/provider effects for
reconciliation; restoring a backup is not permission to replay old READY work.
