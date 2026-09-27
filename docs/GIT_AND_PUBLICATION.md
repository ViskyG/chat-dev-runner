---
id: CDR-GIT
status: accepted-design
---
# Git, provisioning and publication

## Source model

One project → one runner-owned repository → several workspaces. A workspace has a worktree,
branch association, environment profile and Drive documents. Git supports multiple linked
working trees; branches already checked out elsewhere require care [S1]. This project never
uses `--force` to check out the same branch in two active worktrees. Use a distinct branch
or an explicitly detached read-only inspection snapshot.

“Current branch of project” is removed. Retargeting belongs to a selected workspace only.
Show branch, HEAD, modified files, last fetched remote state, upstream relation and the tested
snapshot separately. Fetch does not imply that a worktree's files changed or are up to date.

## Creation paths

**NEW:** local owner approves identity, owner/organization and templates once. A CREATE_PROJECT
request selects a template; default private repository creation is explicit (`private=true`,
never rely on API defaults). Allocate stable project ID, record provisioning intent, create
repo, initialize local Git and initial commit, normal push, first workspace, Drive mailboxes,
profile bindings and project manifest. Resource creation is journaled step-by-step.

**GITHUB IMPORT:** validate owner/repo against policy and credentials, inspect metadata, clone
into the managed root, create chosen workspaces, configure Drive and profiles. Do not clone
from arbitrary network protocols. No hooks, submodules, LFS/smudge or dependency installation
may run unexpectedly during import. Record unsupported repository requirements explicitly.

**LOCAL IMPORT:** only a locally granted `import_handle` resolves to a directory. Inspect Git
and dirty state. Create a separate managed clone/snapshot; do not change the existing checkout,
remotes, branch, index or unstaged/untracked files. Offer an explicit later migration plan,
never auto-adopt by basename. An import may be read-only until the owner selects a profile.

Repository creation and pull-request operations have separate GitHub permissions [S5,S6].
Verify actual account type, scope, organization restrictions and selected-repository access.
A token configured for selected old repos may not authorize a newly created repo. Do not
assume private draft PRs are available on every GitHub plan; preflight capability and return
PUBLICATION_CAPABILITY_MISSING rather than silently make a regular/public PR.

## File and test snapshot rules

Freeze accepted source inputs before tests. The usual automated route starts from a clean
workspace at `expected_head`; stage changes in a disposable worktree, run all path/hash checks,
write the batch, and create an internal snapshot commit/tree. The user's editable directory
is not the test scratch directory. Fixtures and outputs use separate paths.

For a dirty workspace, reject automatic delivery. A local “verify current changes” action may
explicitly capture exactly the displayed dirty tree into a private snapshot without committing
or overwriting the working copy. Its manifest records dirty-at-capture and included paths.
Never silently include untracked secrets in a commit or context export.

After tests, recheck the target branch HEAD and relevant working-tree generation before
updating it. If a user/Codex made edits meanwhile, preserve them and report WORKSPACE_CHANGED;
keep the tested snapshot and outputs separately. Do not point the existing workspace branch
at a new commit while leaving mismatching editor files behind. Use compare-and-set ref logic,
clean-worktree validation and a safe checkout/update under the workspace lock.

## Automatic publication is core

A full operation can create the commit, push its working branch, create/update its PR and
return links without manual copy/paste. Configure authorization once; don't ask the human to
reapprove each routine step inside an already-approved operation. Provider-required
confirmation remains intact.

Default `draft_always`: publish the exact tested snapshot as a draft, including when tests
have findings; attach honest outcome, full tested SHA, profile and report link. This records
work-in-progress, not endorsement. `on_pass` skips publication after project failure.
`never` is a supported explicit operation policy, not a global prohibition.

Commit scope = the reviewed input change. Do not add logs, build artifacts, dependencies or
all files with `git add .`. An unchanged tree does not need a duplicate commit. An existing
matching branch PR is updated, not duplicated. If branch vs base has no diff, report NO_DIFF.
A VERIFY-only request never pushes. A PUBLISH request points to an existing run/snapshot and
its current branch expectations, not an arbitrary working-directory state.

Push is normal fast-forward only; branch protection is honored. On divergence, retain local
commit, set publication=BLOCKED, return remote/local refs and stop. No force-push/rebase/merge
as an unsolicited fix. No merge into main in v1. Branch deletion is a capability with explicit
scope; protected, unmerged, current, dirty or uncertain branches are refused. Never delete a
remote repository as rollback for partial provisioning.

The current offline EP-02 source APIs, durable records and explicit later-packet boundaries are documented in [SOURCE_LIFECYCLE.md](SOURCE_LIFECYCLE.md).

## Implementation mechanics

Use argv arrays and validated refs. Pass `--` where the Git subcommand supports it; do not
invent unsupported option delimiters. Disable hooks and unexpected global Git configuration.
Use a dedicated credential helper/isolated environment for GitHub access. Redact command
errors so tokens are not written to logs. Credentials never enter the executor's environment.

Store GitHub numeric/node resource IDs and returned URLs. Treat timeout after create/push/PR
as ambiguous: inspect by recorded resource identity and branch/commit before resuming.
A local DB UUID alone cannot prove remote exactly-once behavior.

References: [S1], [S5], [S6]. Concrete local policies above are design decisions.
