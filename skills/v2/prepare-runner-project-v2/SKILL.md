---
name: prepare-runner-project-v2
description: Prepare this project's existing codebase for enrolled Runner v2 tasks, explicit native or fixed Python rootless profiles, recognized outputs and truthful target qualification.
metadata:
  version: "2.0.1-source-preview"
  state: source-preview
---
# Prepare an existing project for Runner v2 — source preview

Use for a project-preparation request, not routine task submission. Read its
AGENTS, manifests/tests/CI and the actual enrolled capabilities. Consult
[V2 operations](../../../docs/V2_OPERATIONS.md), “Project preparation” and the
owner configuration fields; [Russian text](SKILL.ru.md) is the companion.
This source does not establish a live enrollment or install a global skill.

On first project enrollment or adoption, inspect the existing
`.project/passport.json` identity. If absent, follow the
[shared project-passport skill](https://github.com/ViskyG/agent-skills/blob/main/skills/shared/project-passport/SKILL.md)
within the authorized project scope; preserve the real Runner `project_id`
under `bindings.runner` without renaming it. The passport supplies the human
title, not Runner execution authority. Do not insert `name`, `title` or
`display_name` into `cdr-runtime/2`: this runtime parser does not accept them.
A pending passport does not block an otherwise approved finite test.

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
The container route is only fixed `rootless-docker` / `python-quick`: exact
one-step test argv/cwd/timeout and approved outputs, existing matching grants,
pinned image, preparation handoff and hard quotas. No generic container task.

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
