# Evidence and primary sources

Checked 2026-10-15. Design choices are proposals governed by the owner, not statements copied from vendor documentation. No model-facing secret or live command is embedded.

## Repository

Exact source anchors/read coverage are in `SOURCE_AUDIT.json`. All observations are pinned to `ac7d55ddce58bcb422bae821b2550483592f7975`. The owner explicitly confirms local checkout parity. No target service was called. The canonical skills navigator and design/scenario/roadmap skills in `ViskyG/agent-skills` were used as workflow references; new skills here remain drafts, not installed canonical entries.

## Public primary sources

### E01 — Google Docs batchUpdate write controls
https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate

requiredRevisionId rejects writes against a changed revision; targetRevisionId merges collaborator changes and is not the selected ACK mechanism.

### E02 — GitHub Git references REST
https://docs.github.com/en/rest/git/refs

Non-forced ref updates preserve fast-forward behavior. No invented expected_sha REST parameter.

### E03 — GitHub large files
https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github

Ordinary Git files above 100 MiB blocked; package intentionally chooses local fallback, not LFS.

### E04 — SQLite transactions
https://www.sqlite.org/lang_transaction.html

Short BEGIN IMMEDIATE writer transactions are the selected local coordination primitive, not a cross-provider transaction.

### E05 — pytest output
https://docs.pytest.org/en/stable/how-to/output.html

Recognized test-report generation, including JUnit. Does not validate our new runner.

### E06 — Vitest reporters
https://vitest.dev/guide/reporters

Native reporters for JS/TS profiles. Exact project version must be qualified.

### E07 — npm ci
https://docs.npmjs.com/cli/v11/commands/npm-ci/

Clean dependency installation for lockfile-based npm profile; not equivalent to support for all package managers.

### E08 — TypeScript noEmit
https://www.typescriptlang.org/tsconfig/noEmit.html

Separate typecheck phase without build artifact emission.

### E09 — Maven Surefire
https://maven.apache.org/surefire/maven-surefire-plugin/

Maven test execution/reporting; resolved dependencies and version details still need capture.

### E10 — dotnet test
https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet-test

Test-host-specific options; qualify VSTest/MTP separately.

### E11 — Windows Job Objects
https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects

Process-group lifecycle and kill-on-job-close; not an application sandbox.

### E12 — Python subprocess
https://docs.python.org/3/library/subprocess.html

Platform process launching, pipes and explicit shell semantics.

### E13 — Apple launchd
https://support.apple.com/en-il/guide/terminal/apdc6c1077b-5d5d-4d35-9c19-60f2397b2369/mac

Per-user launchd-based startup design; actual install must be qualified on target.

### E14 — Compose config
https://docs.docker.com/reference/cli/docker/compose/config/

Resolved model merges files and expands variables; inspect the actual configuration.

### E15 — Compose readiness
https://docs.docker.com/compose/how-tos/startup-order/

Running is not ready; healthcheck conditions and finite startup are explicit.

### E16 — Unity Test Framework command line
https://docs.unity3d.com/Packages/com.unity.test-framework@1.4/manual/reference-command-line.html

Version-specific Editor test CLI and XML results; not universal licensing/build support.

### E17 — Codex AGENTS.md
https://developers.openai.com/codex/guides/agents-md

Entry instructions for a Codex handoff; product/version behavior must be checked when installed.

### E18 — Claude Code memory
https://code.claude.com/docs/en/memory

Thin Claude-specific entry may reference common project instructions, not a second diverging design.

## Verification hierarchy

Model/structural checks < disposable real local Git operations < live provider roundtrips < installed target-platform lifecycle. This package reports only the first two levels. The old feasibility archive is historical context, superseded where owner decisions changed.
