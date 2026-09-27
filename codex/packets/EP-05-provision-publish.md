---
id: EP-05
status: NOT_STARTED
depends_on: ['EP-02', 'EP-03', 'EP-04']
---
# EP-05 — Complete one-shot workflow and GitHub automation

## Required inputs
`docs/GIT_AND_PUBLICATION.md`, `docs/RECOVERY.md`, `docs/SCENARIOS_AND_ACCEPTANCE.md`,
`config/project-policy.example.json`, `schemas/command.schema.json`.

## Outcome
Deliver the purpose of the product end to end. GitHub repository creation, commit/push and draft
PR creation/update are core acceptance criteria, not optional post-release embellishments.

## Implementation tasks
1. Implement finite action-specific workflows with durable checkpoints before/after external
   effects. New project: validate identity/template → create or attach private GitHub repo → owned
   clone → baseline worktree → approved environment → Drive docs/context → registry/state report.
2. Existing GitHub/local imports use the same domain path but preserve original ownership/data.
   Derive deterministic resource keys from installation/project/operation; name collisions do not
   authorize adoption. No automatic destructive rollback of partially created resources.
3. DELIVER_VERIFY_PUBLISH freezes input → validates base → stages changes → snapshots → executes
   profile once → collects evidence → commits/pushes exact snapshot → creates/updates draft PR →
   returns typed report. Failed tests follow publication policy, not a disguised runner exception.
4. Use GitHub authenticated owner/repo identity and least feasible repository permissions. Probe
   draft PR availability for the actual account. If unavailable, report that capability and get an
   owner decision; never replace it silently with a ready/public PR.
5. Query known branch head and existing PR by owner/head/base before create/update. A retry after
   response loss reconciles actual remote state; no duplicate PR, wrong account or forced overwrite.
6. Separate protected baseline and workspace publishing refs. Refuse default-branch push, deletion,
   force push, merge and public visibility unless an explicit separately approved policy grants it.
7. Implement source-run PUBLISH and PUBLISH_REPORT against immutable evidence; these must not
   re-execute tests. GitHub connector access for ChatGPT stays MANUAL_REQUIRED when not verifiable.
8. Add cancellation checkpoints: stop future side effects, retain already published effects and
   report them. Never claim a pushed commit vanished because the local operation was cancelled.

## Acceptance and evidence
- fake APIs with injected timeouts after resource creation and PR creation demonstrate reconciliation;
- real disposable local Git remote: publish tested snapshot despite concurrent user edits;
- gated private GitHub/Drive end-to-end new-project path, passing test and draft PR;
- failing-test path still publishes labelled draft according to policy and returns failure evidence;
- auth/quota/branch conflict/unsupported draft feature produce precise partial result and no auto-merge;
- cancel between push and report retains remote commit reference and accurate result;
- UI-initiated and Drive-initiated CREATE_PROJECT produce the same resource structure.

## Scope boundary
Do not create a production deployment feature. Here “provision” means development resources and
approved test environments. Do not implement automatic ChatGPT repository entitlement by scraping
or driving the consumer interface.

## Packet closing rule
Write `codex/checkpoints/EP-05.md` using the checkpoint template. Put machine evidence in
`verification/implementation/EP-05/`. Record actual commands, exit codes and environment identity.
Do not mark this packet VERIFIED if only mocks pass but the specified gate requires a real local
process. Live provider gates may be BLOCKED while offline implementation is tested, but the final
release remains not deployable until they pass.
