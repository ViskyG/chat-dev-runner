# Finite Compose tasks (Linux preview)

`rootless-compose` runs a temporary stack, waits for service health, executes
ordered migrations and a finite test client, retains outputs/logs, then removes
the exact resources of that run. It uses the existing rootless Docker daemon and
hard-quota helper. It does not deploy a production stack or expose host ports.

The initial supported input is a small **JSON Compose subset**, not an arbitrary
Compose YAML project. The top level contains `services`; each service has an
approved digest-pinned `image` and explicit array `command`. Readiness services
also declare an exec-form `healthcheck`. Runner supplies the network, mounts,
resource limits, labels and cleanup policy. Builds, pulls, implicit `.env`,
external volumes, socket mounts and project-selected host paths are unavailable.
The reference fixture is [compose-v2](../tests/fixtures/compose-v2/).

For an existing Analytics Control Center release, the
[project adapter](../examples/v2/analytics-control-center-compose/README.md)
generates the pinned stack, task and private profile from its release manifest.
It runs the released `npm run test:ui` against the bundled example catalog and
temporary state. Its JUnit entry represents the complete command; original test
diagnostics remain in the log. This route does not start the analytics server stack.

## Enroll a project once

1. Prepare the project's `compose.json` and finite client that writes reports
   under `/out`. All services see frozen source at `/src` (read-only), bounded
   shared data at `/data`, and their own bounded `/tmp`. A private internal network
   provides service-name discovery. Migration services run after readiness and
   before the test client. Dependencies must already exist in approved local images.
2. Store a `cdr-compose-profile/1` file outside the repository with owner-only
   `0600` permissions. It pins `compose_file`, the exact `compose_sha256`, service
   roles, test argv/budgets, platform, resource limits and collected outputs.
   [The qualification fixture](../tests/fixtures/compose-v2/qualify.py) contains a
   complete profile example; its temporary paths are not installation instructions.
3. In the same private directory, `compose-grant.json` uses this structure:

   ```json
   {"schema_version":"cdr-compose-grant/1",
    "profile_revision_sha256":{"my-stack":"SHA256_OF_EXACT_PROFILE_BYTES"}}
   ```

   Set `compose_profile_dir` in the existing owner `runtime-v2.json` and add
   `my-stack` to the intended project's `container_profiles`. The exact local
   profile and grant are checked again for every run. A request cannot enroll itself.
4. Declare one test step in `.runner/project.json` with the same profile ID,
   argv, `cwd: "."`, test timeout and approved `out` output declarations. The
   request budget must cover readiness, all migrations, test, twice cleanup and
   120 seconds of bounded controller operations. Use the normal test quality gate.
5. Restart the existing idle service after owner configuration changes. Submit a
   normal v2 `RUN_TASK` for that enrolled task with
   `execution.mode: "container"`, `backend_id: "rootless-compose"`,
   `expected_os: "linux"` and the approved profile. Normal GitHub/Docs admission,
   source-at-start, result publication and terminal state receipts still apply.

`image` pins the test client's image. Optional `allowed_images` pins up to eight
cached service images; it defaults to `[image]`. An image declaring implicit
anonymous volumes is rejected. Actual initial host qualification uses one Python
image for HTTP, SQLite migration and the finite client; it does not qualify every
database image or a Windows/macOS Docker host.

## Outcomes and recovery

Test failure is a project outcome. Readiness/migration errors remain visible in
the terminal code and backend phase evidence. Timeout and cancellation preserve
available partial outputs. Docker logs have bounded tail retention; original
history completeness after rotation is not claimed.

Before teardown Runner checks the original daemon, recorded resource IDs, run
labels and ownership nonce. It confirms absence of those exact IDs afterwards
and releases the run's quota mount. Missing images are preflight failures and do
not retain the execution lane. Uncertain mutations, changed resource identities
or incomplete evidence retain the lane for reconciliation; they never cause an
automatic rerun or global prune. Preserve the local journal, resolved config and
exact IDs when investigating an interrupted run.
