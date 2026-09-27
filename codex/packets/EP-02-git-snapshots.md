---
id: EP-02
status: VERIFIED
depends_on: ['EP-01']
---
# EP-02 — Projects, worktrees and immutable source snapshots

## Required inputs
`docs/GIT_AND_PUBLICATION.md`, `docs/SECURITY.md`, `docs/SCENARIOS_AND_ACCEPTANCE.md` scenarios
02–08, `bridge_reference/changes.py`, `bridge_reference/demo.py`, `config/project-policy.example.json`.

## Outcome
Implement local provisioning and source lifecycle on disposable repositories first. A project is
a repository identity, a workspace is an isolated checkout, a run tests an immutable snapshot.

## Implementation tasks
1. Provision under the runner's controlled root. Implement local-import opaque handles granted
   from the authenticated local UI; Drive cannot nominate arbitrary host paths.
2. Use runner-owned clones, disable hooks and unapproved filters/submodules/LFS; invoke Git via
   argv with sanitized environment and explicit cwd. Validate branch/ref/remote identities.
3. Implement create workspace and clean-idle retarget; reject branch already checked out elsewhere.
   Dirty/behind/diverged states must be visible; no auto-stash/reset/merge/rebase/force push.
4. Apply change sets to a disposable staging checkout after validating full base commit, expected
   file hashes, safe relative paths, allowed files and content limits. Defend symlink TOCTOU with
   safe fd-relative operations or a controlled staging tree; reference Path.resolve is not enough.
5. Produce a complete source snapshot before execution, including the declared dirty changes if
   VERIFY allows them. Store source generation, tree digest, base/target commits and provenance.
   Never update the human's moving checkout during a background snapshot run.
6. Publication selects the tested snapshot commit, not a fresh `git add .` after tests. Worktree
   environment/output directories never become part of source snapshots.
7. Implement fetch versus sync separately. Fetch changes remote-tracking metadata only. Sync is
   an explicit clean fast-forward/update or declared change-set import, never a bidirectional mirror.
8. Implement archive as explicit inactive/retained workspace; removal of nonempty or dirty source
   requires separate owner handling and must not discard uncommitted work.

## Acceptance and evidence
- real temporary Git repos: two branches/worktrees tested independently;
- clean switch works, dirty/busy/duplicate-branch switch refused;
- invalid path, symlink, partial stale change set and .git/.env/workflow mutation refused;
- editing original checkout during a run leaves snapshot unchanged and results marked stale;
- snapshot hash/commit used for publication matches hash/commit used by tests;
- local import does not overwrite, reset or add files to original repository;
- simulated crash between staging and promotion leaves recoverable evidence, no half-applied workspace.

## Scope boundary
Git worktree isolation is source organization, not execution containment. No tests from arbitrary
repositories may run until EP-03 has a verified sandbox profile.

## Packet closing rule
Write `codex/checkpoints/EP-02.md` using the checkpoint template. Put machine evidence in
`verification/implementation/EP-02/`. Record actual commands, exit codes and environment identity.
Do not mark this packet VERIFIED if only mocks pass but the specified gate requires a real local
process. Live provider gates may be BLOCKED while offline implementation is tested, but the final
release remains not deployable until they pass.
