# Released Analytics Control Center browser contract

This adapter runs the **existing `npm run test:ui` in a selected released UI
image** against its matching released application image. It uses the application's
bundled example catalog and fresh temporary state. It does not copy private
application source or substitute a demonstration application.

The generated workspace is separate from the Analytics worktrees. Its source is
frozen at `/src`; the application writes under `/data/control-center`, browser
temporary files under `/tmp`, and reports under `/out`. Runner supplies a unique
internal network, read-only image roots, rootless container UID 0, CPU/memory/PID
limits, and a shared 1 GiB writable quota. No host port, real catalog, credential,
database, owner state, image build, or image pull is declared by this project.
The UI command uses the released image's installed dependencies, with `/work` as
its working directory and `http://control-center:8768` as its target.

## Prepare once

Use the project-owned release manifest selected by the operator. Preparation
checks immutable image references and writes **new** private directories; it does
not authenticate the manifest, initialize Git, enroll the live Runner, or execute
Docker. The chosen manifest is the owner's input authority. A different release
requires fresh preparation and qualification of its image layout and UI command.

```bash
python3 examples/v2/analytics-control-center-compose/prepare.py \
  --release-manifest /absolute/path/to/release-manifest.json \
  --project-dir /absolute/path/to/new-private-test-project \
  --profile-dir /absolute/path/to/new-private-compose-profiles \
  --project-id cdr-runner-test \
  --profile-id analytics-cc-ui
```

Both parent directories must exist. The two destination directories must not
exist, overlap, or be nested. A repeated invocation refuses to overwrite them.
Partial output after an I/O failure is retained for inspection; select fresh paths
after resolving the error. Profiles and their hash grant remain outside Git.

The project contains `compose.json`, `.runner/project.json`,
`run-ui-contract.cjs`, and `release-provenance.json`. Provenance records the
manifest hash, release version, source commit, both image digests, adapter hash,
and Compose hash. The profile directory contains the owner profile and
`compose-grant.json`; its generated files are mode `0600` in a `0700` directory.

## Run through Runner

1. Independently verify the chosen release and cache its two exact digest-pinned
   images in the enrolled rootless daemon. Check that the runtime has
   `/app/server.py` and `/app/examples/catalog`, and the UI image has `/work` with
   the intended `package.json` `test:ui` script. Neither image may declare an
   implicit Docker volume. Preparation deliberately does not perform these actions.
   The installed quota helper must support 1024 MiB. Older installations only
   allow 64/256 MiB; a project profile cannot extend that root-owned policy.
   Use the reviewed `scripts/install_volume_helper.sh` upgrade during an idle
   service stop, then restart intake. The current qualification checkpoint below
   distinguishes prepared source from the actually installed helper. Verify its
   installed hash against the reviewed source; `probe: READY` alone does not
   establish the supported sizes.
2. Publish the generated files on a separate branch of an enrolled GitHub project,
   or commit them in a registered local workspace. The generated `project_id`
   must match that enrollment. Configure `compose_profile_dir` to
   the generated private directory and enroll `analytics-cc-ui` in that project's
   `container_profiles`. Preserve other configured projects/profiles; merging an
   existing enrollment is an owner operation, not this generator's job.
   Reload configuration only while the execution lane and queue are idle and no
   configured mailbox contains READY intent. Reuse an existing command mailbox;
   its branch and the request's source branch can differ. Read the current slot
   token after restart before submitting new intent.
3. Submit one ordinary `RUN_TASK` request for `task: "test"` and the registered
   source, with execution `mode: "container"`, `backend_id: "rootless-compose"`,
   `profile_id: "analytics-cc-ui"`, `expected_os: "linux"`,
   `timeout_seconds: 800`. The test phase has 600 seconds, readiness 60 seconds,
   and cleanup 10 seconds per stop/teardown stage; the outer budget includes
   Runner's existing control allowance.
4. Read the terminal Runner result and immutable bundle. Success requires the
   released command's exit `0`, parsed JUnit, complete evidence, confirmed
   cleanup, released execution lane, and absent quota mount. A failed UI contract
   is a test failure. Do not replay an uncertain run; inspect its exact preserved
   resources and follow Runner's reconciliation procedure.

JUnit contains **one aggregate command-level contract**, not inferred counts of
the application's individual assertions. JSON records the actual package script,
exit code, signal, completeness, provenance, resource samples and log truncation.
The original combined stdout/stderr log is bounded to 4 MiB. Before completion,
JUnit has zero completed tests; cancellation cannot leave a premature green report.
JSON/text are declared as opaque artifacts, retaining their original bytes
without requesting unsupported semantic parsers. Project outputs use `git_if_fits`
so the ordinary Runner result publication can return synthetic test evidence
within its existing limits; oversized originals remain in the local bundle.

The default profile sets 2 CPUs, 2 GiB memory and 256 PIDs **per service**.
The writable quota is shared across both services and reports. Playwright's
released Chromium command uses `/tmp` with `--disable-dev-shm-usage`; Runner's
small `/dev/shm` is unchanged. Image resource assumptions must be checked for each
selected release. No latest unreleased application branch is tested by this route.

## Focused adapter checks

From the Runner repository with its existing development environment:

```bash
python -m pytest -q examples/v2/analytics-control-center-compose/test_prepare.py
node examples/v2/analytics-control-center-compose/test-run-ui-contract.cjs
```

The Node file uses `node:test` directly so its intentional `SIGTERM` exercise
does not trigger an outer `node --test` worker's own signal handler.
These checks use fake manifest metadata and local child processes. They verify
Runner schema/profile compatibility, immutable enrollment, refusal behavior,
reporting of pass/failure/missing executable, bounded logs and interruption.
They do not execute the application or prove live Docker qualification. The first
installed request stopped at the old helper's quota policy before creating any
container. Exact resource absence was confirmed and the lane released; successful
application qualification is still pending the helper upgrade and a fresh request.
See [the current checkpoint](../../../codex/checkpoints/GP-08.md) and
`verification/implementation/GP-08/analytics-20261023/` for evidence. No deployment
to a real Analytics environment or ongoing reliability claim follows from this adapter.
