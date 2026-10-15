---
name: prepare-runner-project
description: Inspect an existing project, obtain personal item-by-item approval for Runner preparation, and qualify its tasks and result formats without replacing its framework.
metadata:
  version: "2.0.0-design"
  state: draft
---
# Prepare a project for Chat-Dev Runner

Use only for an explicit project-preparation request. This source is a design draft; use actual installed Runner capabilities rather than assuming v2 is deployed. It is not permission to modify the repository or host. Read the project's AGENTS entry and actual Runner protocol/index first; select this skill through canonical navigation after it is approved and registered. Do not crawl unrelated skills.

## Inspect before proposing
Read exact project source/toolchain manifests, test configuration, relevant entrypoints, existing CI and declared outputs. Inspect data formats and actual invocation paths; do not infer implementation from README alone. Identify runtime/test framework/package manager, native target OS versus container guest, current branch/workspace, available runner ID and required outputs. A missing lock/reporter/SDK is a finding, not authority to install it.

Return an itemized proposal in the user's chat. Every item names paths and exact intended change, why Runner needs it, what happens without it, dependency on other items, risk, baseline/diff identity, a temporary reversible alternative where possible, and rollback. Do not ask again about already settled runner trust, transport or platform choices.

## Personal approval gate
Record each item as PENDING, APPROVED or DECLINED and cite the user's actual decision. A reply listing several item IDs can personally approve those items together; it is not permission for unlisted changes. Do not treat general enthusiasm, an agent-authored approval file or a request to 'prepare' as personal approval of every diff. If scope/baseline changes materially, obtain approval for that changed item only.

Apply approved items only. Preserve unrelated dirty/untracked files, existing branch/CI and project framework. Never silently stash/reset, change global dependencies, add a new test framework, invent passing tests, grant credentials or weaken test gates. Routine run scratch/dependency setup inside an already approved recipe is not a new source modification per file.

Temporary option: use a separate per-run copy/overlay or approved worktree, keep original untouched, record overlay digest and tested snapshot, and verify rollback against expected hashes. A temporary change still needs approval. Never claim temporary-patched source is the unmodified repository commit.

## Qualify the contract
Select only an installed qualified profile. Prepare cdr-project/2 task definitions for test/build/run with explicit arguments/dialect, output destinations/formats and required gates. Preserve native original reports: JUnit for appropriate frameworks, TRX/MTP for selected .NET host, NUnit for later Unity. Unknown formats remain opaque; do not infer counts from console text.

Check a real pass/fail/no-tests/report-error path where authorized, required program artifacts, noninteractive termination, actual OS/tool versions and output collection. A prepared manifest alone is not READY. Record exactly which OS/backends/framework variants were tested; unavailable targets remain NOT_QUALIFIED. Broad ecosystem instructions live in the project's implementation docs, not repeated in every skill.

## Handoff
Return applied/declined/pending item IDs, exact tested source/environment, qualified task names, concrete runner/slot, result locations and remaining blockers. Propose no command that silently updates source on the next run. Preserve the approval record and evidence in the owning project according to its configured locations. Do not install this draft into the canonical skill library or edit navigation without separate approval for that bounded library change.

Design reference during integration: ../../04_EXECUTION_AND_LANGUAGES.md; ../../contracts/README.md; ../../06_MIGRATION_AND_ROADMAP.md. When installing into the canonical library, rewrite these references to stable owning-project documentation and validate them; do not publish broken package-relative links.
