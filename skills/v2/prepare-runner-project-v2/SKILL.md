---
name: prepare-runner-project-v2
description: Register an existing folder or repository with Runner v2 and its requested chat channels, or prepare its native/rootless tasks and reports.
metadata:
  version: "2.1.0-source-preview"
  state: source-preview
---
# Register or prepare an existing project for Runner v2 — source preview

Use for project registration or preparation, not routine task submission. Read its
AGENTS, manifests/tests/CI and the actual enrolled capabilities. Consult
[V2 operations](../../../docs/V2_OPERATIONS.md), “Project preparation” and the
owner configuration fields; [Russian text](SKILL.ru.md) is the companion.
This source does not establish a live enrollment or install a global skill.

For registration, follow [Project enrollment](../../../docs/PROJECT_ENROLLMENT.md#english)
through verification of the requested route. A local workspace and GitHub repo
alone do not make a project discoverable or reachable from chat. Infer required
channels from the owner's request and current workflow; GitHub-only needs no
Drive. Register the requested existing folder itself. Report runtime binding,
chat enrollment and task qualification separately; registration does not require
inventing a task recipe or sending READY. Continue below when task preparation
is also requested.

Identify project/runner, branch or registered local workspace, language/package
manager, exact tool paths, target OS/backend, real report format and required
program outputs. Missing dependencies/reporters/SDKs are concrete preparation
items. Preserve the framework, branch, unrelated dirty/untracked files and CI.
Do not silently stash/reset or manufacture passing tests.

Use existing authorization for the intended source/config and ordinary finite
recipe. Present concrete new changes with purpose, exact paths, source baseline,
rollback and a reversible per-run alternative where useful. Resolve only a new
consequential permission; do not require fresh approval for each scratch file or
for a previously enrolled native trust decision. System SDK installation,
credential grants and canonical skill registration are separate scopes.

Create `.runner/project.json` with `cdr-project/2`, the exact enrolled
`project_id` and task `profile_id`, whole-token argv, relative cwd, finite phases,
output `source_root`/path/format/required fields and a meaningful quality gate.
Use [real calculator examples](../../../examples/v2/README.md) or the actual
`standard_project` planner; neither installs tools. Native runs as the ordinary
user, with host access. Select `SHELL` only for an enrolled dialect/profile.
For containers select an actually enrolled backend/profile: `rootless-docker`
supports the fixed `python-quick` route; `rootless-compose` supports a finite
stack with health readiness, optional ordered migrations and one test client.
Both require exact one-step test argv/cwd/timeout, approved outputs, private
matching grants, pinned cached images and hard quotas. Read
[Compose tasks](../../../docs/COMPOSE_TASKS.md) for its restricted input and total
request budget. The [Control Center adapter](../../../examples/v2/analytics-control-center-compose/README.md)
prepares a released app/browser test from an external release manifest; it does
not enroll itself or deploy the analytics platform. A command-level aggregate
JUnit result must stay labelled as an aggregate, never invented assertion counts.
Use `publish: git_if_fits` for approved reports needed by the chat; `local_only`
does not deliver their contents through GitHub. General Compose YAML is not supported.

Choose source explicitly. A local workspace can include dirty/untracked paths
only under owner rules; remote source fetches current branch at execution start.
An overlay gets its own digest and never masquerades as an unchanged commit.
Qualify useful authorized behavior and original JUnit/TRX reports on the actual
target; no-tests, all-skipped and invalid/missing required evidence cannot pass.
Version probes are finite captured observations, not guessed tool versions.

Return applied scope, exact tested source/environment, enrolled task/profile,
concrete reusable slot, output locations and remaining gates. A manifest alone
is not deployment acceptance or permission to send a sample READY command.

Provenance: [unchanged design](../../../design/github-native-v2/skills/prepare-runner-project/SKILL.md)
and [hash index](../PROVENANCE.json).
