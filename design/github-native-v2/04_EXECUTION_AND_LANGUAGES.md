# Execution and language profiles

## Current source at task start
A request says what to do and where to get current source. `github_branch` means fetch the selected ref from the enrolled repository at task start, not cached local origin state or writer timestamp. `local_workspace` means a registered local workspace's current files, including intended dirty/untracked files under its approved inclusion rules. Neither mode overwrites an existing user checkout. GitHub commands default to github_branch; existing Drive workspace workflows keep an explicit local-workspace selection when needed.

Create a dedicated per-run working copy in the managed project area. Freeze source before any dependency hooks or compiler/test code. Store source_commit, tree where meaningful, per-file digest, exclusions, and dirty/untracked/overlay metadata. If capturing a live local workspace, verify files did not change during capture; bounded retry or INPUT_CHANGED rather than a mixed snapshot. A source branch must be explicit if results must return to that branch. Detached commits have no 'tested branch': block with BRANCH_REQUIRED, do not guess main.

Exclude controller-owned command/state/results directories and .git/credential material from workload input by explicit versioned rules, retaining `.runner/project.json` and approved task scripts. Preserve an audit index of excluded paths. Some projects need Git metadata; qualify an explicit sanitized metadata variant, never silently mount the user's credential-bearing .git. These exclusions mean workload_snapshot_sha256 and source_commit are distinct, both reported. Hash the actual prepared/overlaid source as well. Never advertise an overlaid project as an unmodified commit.

Source remains fixed during preparation, test, build and program run. Result publication does not change the tested snapshot. When a later task starts it resolves the latest branch, possibly including earlier result commits; reserved result paths remain excluded from its workload.

## Task contract
`contracts/project.schema.json` defines versioned task declarations, not a grant of host privilege. Keep one manifest per project and explicit module roots for monorepos. A task is a finite list of phases with explicit `argv` token arrays and relative cwd, or a deliberately selected shell script with named dialect. Standard phases: prepare, typecheck, test, build, run. `RUN_TASK` selects an existing task; `SHELL` carries an explicit dialect/script and expected artifacts.

Controller substitutes only documented whole argument tokens `{python}`, `{venv_python}`, `{node}`, `{npm}`, `{maven}`, `{dotnet}`, `{workspace}`, `{out}`, `{vitest}`, `{tsc}`, and declared `{output:<id>}` after resolving toolchains. It does not parse shell expansion in argv. For interpolated paths inside arguments, implement explicit typed token segments or generate approved scripts; never naive text replacement in arbitrary shell source. Store resolved sanitized argv, executable version/path and task digest. Unknown placeholders fail before spawn.

Environment variables `CDR_OUTPUT_DIR`, `CDR_RUN_ID`, `CDR_SOURCE_SHA` provide output routing; don't require shell interpolation for basic tasks. Native scripts on POSIX may use `$CDR_OUTPUT_DIR`; PowerShell uses `$env:CDR_OUTPUT_DIR`. Do not run a POSIX script through cmd.exe or translate it heuristically.

Process outcome, quality gate, artifact collection, cleanup and delivery are separate. Collect outputs even on nonzero exit/timeout when safe. Default test gate requires at least one actually executed test, a recognized complete report and no unexpected failures. Explicitly expected failures/skips preserve their semantics. All skipped or no-tests is INSUFFICIENT, not PASS. Build-only/run-only tasks may have `test_required=false`. Required result absence/schema violation fails the task quality gate even when exit=0. Inconsistent report counters, exit code and cases produce a warning/UNKNOWN, never a fabricated zero failures.

## First qualified profiles (five languages, four toolchains)
The list is the agreed initial scope, not a claim to support every framework or package manager.

| Language | First supported family | Phase design | Native artifacts |
|---|---|---|---|
| Python | pytest-based package/CLI, unittest through pytest when compatible | per-run venv; approved dependency install; pytest; optional wheel/sdist; finite app | JUnit XML, package files, declared CSV/JSON/other outputs |
| JavaScript | Node + npm lockfile + Vitest | npm ci in run workspace; vitest run; optional project build/run scripts | JUnit + optional JSON, JS bundle and app results |
| TypeScript | same Node profile plus project-local TypeScript compiler | npm ci; tsc --noEmit; vitest run; build/run | typecheck exit/diagnostics separate from tests; JUnit and bundle |
| Java | Maven wrapper or resolved Maven + selected JDK, Surefire/Failsafe | dependency resolution; test or verify/package; finite app | original Surefire/Failsafe XML, JAR and declared outputs |
| C# | .NET SDK-pinned project/solution with explicit test host | restore; build; selected VSTest or Microsoft.Testing.Platform test recipe; publish/run | original TRX/MTP report and binary outputs |

Sources: pytest output [E05], Vitest reporters [E06], npm ci [E07], TS noEmit [E08], Maven Surefire [E09], .NET CLI [E10]. Exact qualified versions are discovered/pinned by the implementation on the target, not invented in this package.

### Python details
Preserve installed python-basic/rootless profile and wheelhouse as a regression fixture; add native profile without rewriting old evidence. Use interpreter from selected environment, not `pip` from PATH or the owner's global Python. Windows venv executable differs from POSIX; resolver returns absolute executable. pytest can emit `--junitxml` directly. Any reporter/config addition is an approval item; avoid new tests written solely to manufacture a green count. A project using plain unittest may be run via compatible pytest or an explicitly approved adapter; unittest itself is not assumed to emit JUnit XML.

### Node / TypeScript details
Pick one package manager per profile; initial npm uses its lockfile and `npm ci`. Missing/ambiguous lockfile or different manager gets a preparation proposal, not a silent lock rewrite. npm package lifecycle scripts are project code. Resolve local installed Vitest/tsc, no `npx` auto-fetch on an unqualified path. On Windows npm commonly uses a .cmd shim: prefer the resolved node executable + npm CLI JS entry or an explicit tested shim wrapper; do not rely on POSIX quoting. Test files/code may be shared between JS and TS profiles, but typecheck is a separate phase with its own errors. Existing Jest/node:test projects aren't automatically migrated; classify as unqualified, offer their specific reporter adapter as a separately approved extension.

### Java details
Keep Maven and Java versions and wrapper hashes. Surefire unit and Failsafe integration outcomes are separate; parse module-scoped reports without double counting. Compile failure before tests means test NOT_RUN. Maven does not have a universal lockfile equivalent to npm; do not require a nonexistent one. Record resolved dependency graph, POM and plugin versions, repository sources without credentials, and cache hit/miss. SNAPSHOT/dynamic deps lower reproducibility classification. Project-local caches avoid modifying the owner's shared Maven settings. Gradle is a later profile with its own wrapper/dependency-lock qualification, not a hidden rewrite.

### .NET details
Read global.json, target frameworks, runtime identifiers, package lock strategy and test platform. TRX remains original evidence; normalize to common test facts rather than requiring a third-party JUnit logger. VSTest and Microsoft.Testing.Platform use different option/report conventions; select recipe from actual project and SDK. Version mismatch returns TOOLCHAIN_UNAVAILABLE. Multi-target test runs have separate framework dimensions; don't overwrite one TRX with another or sum duplicates. Never call a Linux .NET run a Windows desktop test. WPF/WinForms, platform APIs and Unity are outside the generic cross-platform project profile.

## Dependency preparation and approval
Preparation may use the network with the ordinary user's existing access when explicitly part of the enrolled native recipe. Do not claim reproducibility from just a version string: record locks/resolution and a reproducibility_level (`locked`, `resolved_only`, `unknown`). Dependencies and compiler plugins execute code. Remove cloud controller token variables from inherited env; this reduces accidental leakage but does not isolate native execution from the same user's files.

Cold-cache and warm-cache runs are both acceptance cases. Caches are scoped by OS, architecture, runtime, manifest/lock and profile revision; never share a writable executable cache across unrelated runs by accident. Missing tools report concrete prerequisites and an itemized preparation proposal. Routine RUN_TASK must not silently download/install system SDKs, change global PATH, edit project configuration or grant permissions.

## Native backends
Run as the user's current account; no mandatory separate account or sandbox. Per-run work directory, env shaping, budgets and process supervision provide predictable behavior, not protection against a trusted collaborator intentionally accessing the host. `SHELL` is allowed natively without a per-command confirmation once this capability is enrolled. Fixed semantic fields select mode; no silent container fallback or elevation.

POSIX: start a new process session/group, close stdin, collect byte stdout/stderr concurrently, TERM then KILL on timeout/cancel. Record process start identity to avoid killing a reused PID after restart. On Linux optionally use the existing per-user service/cgroup supervision if available. On macOS use process groups plus tracked descendants; intentional daemon escape is not a sandbox promise. When cleanup cannot be proved, flag CLEANUP_UNCERTAIN and do not start another local execution lane until bounded reconciliation or a new explicit reset decision.

Windows: create process suspended, attach a Job Object configured to terminate owned descendants on handle close, then resume; manage handles so a child doesn't keep the job alive. Resolve and report any inherited-job limitations. PowerShell/cmd dialects are explicit; do not run arbitrary names through a hidden shell. Probe cancellation of a child/grandchild and service crash, not just a single sleep process. Sources [E11–E13] document the underlying primitives; platform tests remain required.

All platforms: don't wait forever for inherited pipe handles; timeout applies through cleanup/capture. Raw bytes remain authoritative, decoded views state encoding/replacement. Do not equate memory sampling with enforced hard limits; record requested/enforced/observed separately. Log/disk budgets continue draining to avoid deadlock. Reserve enough disk for a minimal failure record; disk exhaustion can make saving impossible and must never be presented as successful preservation.

## Containers / Compose / Unity
Container mode explicitly names a configured backend and compatible pinned image. Host OS, guest OS/arch and emulation are separate provenance. Preserve the existing rootless contract; Docker Desktop is a separate capability probe, not the same daemon assumption. Node/Java/.NET need larger, measured preparation/output budgets than the installed toy Python limits; approve increases per profile, don't remove all bounds.

Compose is GP-08 after core: finite service stack + test client; resolved configuration via `docker compose config` [E14], per-run project name, temporary DB/volumes, explicit healthcheck readiness [E15], migration/test exit, collect artifacts/logs before bounded `down` and exact resource readback. Different compose projects never share implicit fixed host ports/volumes. No production data, global prune or always-running preview. Any requested host mounts, Docker socket, privileges or external volumes must be clearly shown as part of the approved Compose preparation; don't silently insert them. An arbitrary native shell can invoke tools separately, but that doesn't make an unqualified Compose task managed/cleanup-certified.

Unity is GP-09, last: inspect installed Editor, version/target modules, license availability and Test Framework version. First fixture is a small native EditMode pass/fail project with NUnit report [E16]; then limited PlayMode/build. Preserve originals and use a per-run copy, preserving .meta; avoid uploading Library/Temp. Missing license/module is an infrastructure blocker. No claim that headless means rendered graphics/device behavior was tested. License automation/distribution is not part of the package.
