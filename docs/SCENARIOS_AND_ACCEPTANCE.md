---
id: CDR-SCENARIOS
status: acceptance-authority
---
# End-to-end user journeys and acceptance

These are externally observable stories, not hidden model reasoning. Each story identifies
the user decision, automatic work, final evidence and failure branch.

## SC-01 · First installation

User opens the local control center, reviews boundaries and approves owner-local installation.
Codex inventories OS, Python, Git, rootless container mode and existing projects without
changing them. Owner selects managed paths, local OAuth client, GitHub identity/owner,
private provisioning and approved templates. For this owner account the runner attaches the
already-provisioned `Chat-Dev Runner` Drive root and INSTALLATION INDEX by exact ID, validates
the indexed control documents and starts armed after clean recovery checks.
An explicit `--start-unarmed` mode remains available for maintenance. The completed disposable Doc
read/edit/readback handshake validates the chat connection. Owner arming rotates the epoch;
no sample command executes automatically.

Acceptance: only loopback listeners; secrets absent from Git/Drive; the existing Docs are
editable from chat and readable by the runner's owner-approved full Drive OAuth scope;
restarting does not duplicate the Drive installation; old READY examples do not execute;
failed authorization leaves meaningful setup progress. EP-07 has verified
the durable wheel 0.2.7 installation, loopback-only active/armed service and exact existing Drive attachment; a fresh separate-chat handoff remains open; a distinct overnight boot auto-started the preceding 0.2.6 service after one transient OAuth-refresh failure and systemd retry. See [release-upgrade-0.2.7.json](../verification/implementation/EP-07/release-upgrade-0.2.7.json).

## SC-02 · Create a brand-new project from chat

User asks for Notes Lab from approved python-basic template. Chat reads ROOT RECEIPT and
capabilities, writes one CREATE_PROJECT command and stops. Runner immediately admits or rejects,
then creates private GitHub repo, managed clone, initial branch/commit, first worktree,
environment binding, Drive docs and full project index. Initial setup is automatic.
The runner uses its configured owner-local GitHub grant. The owner reports
chat-side GitHub access configured. A current-chat Drive connector command created disposable private project
`cdr-ep07-drive-20260928` and received a matching COMPLETED root RECEIPT;
[the roundtrip evidence](../verification/implementation/EP-07/live-drive-chat-roundtrip-20260928.json)
does not yet prove a fresh separate chat can use the published instructions.

Acceptance: stable IDs across all resources; no empty “Drive-only project”; returned repo/Docs
IDs exist; private=true explicit; no unsolicited organization/public repo; no arbitrary host
paths; fault after repo creation is resumable without duplicate repo or data deletion.

## SC-03 · Import existing work

From GitHub: allowlisted owner/repo, metadata preflight, managed clone and profiles. From local:
UI grants an import_handle; chat may reuse the handle, not invent a path. Existing checkout,
index, remote configuration, branch and untracked files remain byte-for-byte unchanged.

Acceptance: dirty source import never overwrites edits; unknown import handle rejected;
submodules/LFS/hooks detected as requirements, not executed by surprise; multiple remotes
require explicit chosen source. No source credentials exported as context.

## SC-04 · Parallel developments in one project

Create rc-dashboard and airflow-refactor from explicit bases. Open each through VS Code menu.
Run two checks under global limit while editing a third area. Each run has its own environment,
snapshot, output path and cloud target. One workspace's paths/ports/volumes cannot collide with
another. Worktree metadata changes are scoped; branch binding remains unambiguous.

Acceptance: result links never cross workspace IDs; no shared venv/port conflict; same branch
cannot be forcibly checked out twice; third request beyond capacity gets a terminal BUSY,
not an automatically delayed run. Two projects work as well as two areas in one project.
EP-07's deployed UI created a second `parallel` workspace beside `primary` in
`cdr-ep07-ui-20260928`; independent
[readback](../verification/implementation/EP-07/live-multiworkspace-readback-20260928.json)
confirmed two active workspaces, distinct branches and separate Drive folders.
Both workspaces subsequently completed separate passing and failing contained
verification/publication routes on distinct branches with separate draft PRs
and Drive reports: [primary](../verification/implementation/EP-07/live-publish-primary-20260928.jsonl),
[parallel](../verification/implementation/EP-07/live-publish-parallel-20260928.jsonl).
The full parallel execution/isolation matrix remains open. A connected-chat project CREATE_WORKSPACE command on installed 0.2.6 completed with matching project RECEIPT and created a third independent local/Drive workspace from the declared full base commit: [live workspace evidence](../verification/implementation/EP-07/live-project-create-workspace-20260929.json).

## SC-05 · Deliver, verify and publish once

User asks chat to apply a reviewed change and run the whole route. Chat reads base hashes,
writes CHANGESET, obtains Docs revision, writes DRAFT and publishes READY last with a fresh
UUID/TTL/epoch. Runner freezes inputs, validates every path/hash, creates a snapshot, provisions
profile, runs once, captures outputs, commits/pushes the intended working branch, creates or
updates draft PR and returns report. Chat has already ended its turn. User later asks for results.

Acceptance: exact tested tree equals published tree; no automatic main merge; repeated READY
observations never rerun; no arbitrary command string; no user input required between routine
allowed stages; report includes workload outcome even on failure; next model iteration absent.
The installed `0.2.1` service completed a passing primary route with remote
draft PR and Drive report independently read back
([evidence](../verification/implementation/EP-07/live-publish-primary-20260928.jsonl));
exact tested-tree identity across all release scenarios remains a separate gate.

## SC-06 · Project tests fail normally

A deliberate bug gives two failed tests. Runner remains healthy and operation completes.
Full route with draft_always publishes a draft containing known findings. Console stdout/stderr,
JUnit, structured logs and optional artifacts remain available. Results show input revision,
not merely the current branch label.

Acceptance: no global “runner error: 2”; no success claim for tests; no guessed count when
JUnit missing; PUBLISH never labels failed tests passed; no automatic repair/retry.
The installed `0.2.1` parallel route had execution FAILED (exit 1) while the
runner operation COMPLETED and published a private `[FAILED]` draft PR with
Drive report ([evidence](../verification/implementation/EP-07/live-publish-parallel-20260928.jsonl)).
The installed 0.2.6 UI review confirmed separate operation/execution/publication/delivery axes and the exact failed JUnit case for this run, across dark/light/reduced-motion screenshots and three target viewports. Optional registered image and structured-log examples were absent, so that evidence-format portion remains open: [installed UI evidence](../verification/implementation/EP-07/ui-evidence-installed-20260928.md).

## SC-07 · Switch branches without losing work

From a clean idle area, select another existing unoccupied branch at a specified commit.
Keep workspace_id but update branch association and mark previous results historical. From
a dirty/busy area, refuse; offer new area. Switching a different area does not affect RC work.

Acceptance: no automatic stash/reset; no second “current project branch”; old results remain
retrievable; Drive workspace state gets the new association; base changes cannot occur between
preflight and mutation without conflict detection.

## SC-08 · Human/Codex edits during verification

A test runs on a frozen snapshot. User edits the live worktree in VS Code. Test result remains
valid for the snapshot but stale for current source. Publication only proceeds if it can safely
update the intended branch without overwriting user changes; otherwise preserve snapshot and
return WORKSPACE_CHANGED/publication blocked.

Acceptance: no erasure of new edits; no “current code passed” label; report identifies original
and current generation; resolve conflict through a new explicit action.

## SC-09 · Delivery fails after a useful run

Drive becomes unavailable after code executed. Local run and outputs remain intact. UI separates
execution outcome from delivery failure. User reauthorizes/checks connection and requests
PUBLISH_REPORT against existing run ID. It copies evidence only.

Acceptance: executor invocation count remains one; uploaded files have stable IDs/checksums;
no duplicate commits/PRs; partial output list is honest; secrets redacted before retry as well.

## SC-10 · Busy, duplicate, invalid and stale requests

Poll same READY ten times; submit same ID with changed body; submit expired/old epoch; submit
another request while area busy. Observe existing receipt, changed-ID rejection, expiry rejection,
and BUSY respectively. No test code runs for any refused request. Busy stays terminal later.

Acceptance: unique DB claim; concurrency tests; never rearm by remote field; no queue/status
label suggesting future execution. A malicious `intent` string cannot alter behavior.

## SC-11 · Partial setup, crash and cancellation

Interrupt after repo creation, during code execution, after push and before PR receipt. On
restart show interrupted/partial status. Read remote identities before resume, do not execute
ambiguous effects again. Cancel a long test and preserve partial outputs; completed push stays
visible instead of pretending to roll back the external world.

Acceptance: state machine and process-group tests; no duplicate new repo/PR; no background task
continues unnoticed; local singleton prevents a second controller; new explicit recovery request.

## SC-12 · Navigate evidence and reclaim storage

Open run summary, choose stderr, filter logs by ERROR, open a failed case, preview a safe image,
download binary build, open redacted chat report. A large log shows retained/observed size and
truncation. Pin important runs. Cleanup expires ordinary outputs and leaves tombstones.

Acceptance: text escaped; HTML/SVG cannot invoke local API; no XML external entities; unknown
formats preserved; UI never freezes on large logs; stale results and missing outputs distinct;
raw local file and redacted upload hashes are not confused.

## SC-13 · Restore context in a new chat

A new chat opens START HERE and current project/workspace index. It learns supported actions,
IDs, allowed profiles, command procedure and next-step boundary without rereading conversation
history. It reads a targeted report and requests only needed evidence. No latent instructions
inside project logs can redirect it into publishing a new operation.

Acceptance: exact IDs resolve; version mismatch is explicit; recipes include create project,
create workspace, deliver/verify/publish, branch retarget, context export and delivery retry.
No undocumented remembered magic phrases.
Installed `0.2.2` accepted a current-chat `FETCH` through the exact existing
project COMMAND and produced a matching project RECEIPT
([evidence](../verification/implementation/EP-07/live-project-fetch-20260928.json));
this does not close the fresh separate-chat acceptance gate.

## SC-14 · Quiet local control center

Open desktop UI at 1600×1120, 1280×800 and 2560×1440. Read current project/workspace/branch
within seconds. Use keyboard and reduced-motion mode. Simulate runner connection failure and
failed tests separately. All routine actions use the same backend protocol as Drive.

Acceptance: no KPI cards removed by the owner; no queue; no ambiguous Active badge; VS Code
menu lists worktrees; no fake live status; no external assets; focus and contrast checks.
