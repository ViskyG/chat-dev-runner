---
id: CDR-ENV
status: target-design
---
# Automatic environments without repetitive plumbing

Environment setup is part of project/workspace provisioning, not a manual task repeated for every
new repo. The owner grants **templates** once in the local setup UI. A Drive command chooses an
existing template ID; it cannot create a new privileged execution capability.

## Initial template families

| Template | Source detection | Preparation | Execution | Unsupported condition |
|---|---|---|---|---|
| python-basic | known pyproject/requirements/lockfiles, or supplied toy starter | pinned Python image, isolated env, approved locked dependency install | python-quick profile with JUnit output | ambiguous package manager, missing lock or disallowed dependency source |
| node-basic | package.json plus approved lockfile | pinned Node image, deterministic installation in contained build phase | approved node-test/node-build profiles | unknown scripts/profile, missing lock, incompatible runtime |
| files-only | any approved source types | clone/worktree + Drive documents, no runtime | none; source/context/publish actions only | an execution command returns PROFILE_UNAVAILABLE |

`python-basic` with `python-prepare` and `python-quick` is installed and was exercised on
the owner host in EP-05. `node-basic` and `files-only` remain design/template choices until
locally approved and actually installed; the live UI advertises only policy-approved choices.
Existing projects are not assumed compatible. Image digests, interpreter versions and
lockfiles must be checked against the installed profile; do not use a floating runtime or
install project packages into the owner's Python.

## Implemented `python-basic` route

The new-project starter now carries a five-wheel, hash-pinned Python 3.12 test
wheelhouse and `requirements.lock`. The approved `python-prepare` profile runs
`pip install --no-index --require-hashes --no-deps` in a rootless container,
writing only to `/build`. The distinct `python-quick` profile imports pytest
from that staged build, runs project tests with the container network disabled,
and requires `/out/junit.xml`. Both profiles pin the same installed Python image
by registry digest. The wheelhouse is source data in the frozen Git snapshot;
changing a wheel or lockfile changes the snapshot and preparation identity.

`.venv/bin/python scripts/install_python_quick_profiles.py --install` validates the
packaged wheel hashes, the explicit user-owned rootless Docker daemon and its
installed image digest, then writes owner-private `0600` profiles and a
preparation grant under an owner-only profile directory. `--profile-dir` may
select another owner-private directory. Installation alone does not arm the
runner or bypass the broker's separate hard-quota PREPARATION and TEST mounts.
A full disposable Engine PREPARATION→TEST run using the real rootless broker,
separate hard-quota mounts and required JUnit passed; evidence is under
`verification/implementation/EP-05/live-python-quick/`. The permanent
`HardQuotaVolumeProvider` allocates per-run mounts only after admission through a
fixed root-owned helper and retains uncertain mounts for explicit reconciliation.
The reviewed root-owned helper and narrow sudo rule were installed on the owner host in
EP-05; its probe returned `READY` and exact disposable volume allocation/removal passed.
EP-07 now runs an installed, enabled owner-user service from wheel `0.2.2`,
currently disarmed. Two contained disposable workspace routes completed through
the earlier installed `0.2.1` wheel: primary tests PASSED and parallel tests
FAILED while each operation completed with a private draft PR and Drive report.
See [the current service upgrade](../verification/implementation/EP-07/release-upgrade-0.2.2.json),
[passing route](../verification/implementation/EP-07/live-publish-primary-20260928.jsonl) and
[failing route](../verification/implementation/EP-07/live-publish-parallel-20260928.jsonl).
Broader EP-07 recovery, isolation and fresh-chat release acceptance remains open.

The helper accepts only 64 MiB PREPARATION and 256 MiB TEST allocations, with
at most eight mounted phase slots. The owner releases a settled run with:

```bash
.venv/bin/python scripts/release_run_volumes.py \
  --profile-dir /absolute/owner-private/profiles \
  --request-id CANONICAL-REQUEST-UUID
```

This command checks the terminal ledger state, exact container removal proofs,
immutable preparation receipt and capture, and immutable test outputs before it
clears a mount. It never follows project symlinks. A missing proof, changed
output, or active/reconciling operation retains the mount for investigation.
The command may be repeated after an interrupted cleanup; it never allocates
or executes a project. Run it after reviewing each finite operation so the
bounded slots are reused. It does not remove frozen evidence or project Git.

## Finite setup route
Inspect metadata without importing project Python/JS → choose approved matching template → verify
lockfiles/source policy → prepare dependencies in a credential-free contained build environment →
key cache by image digest + platform + profile revision + lockfile hashes → create workspace-private
writable directories → execute a smoke check → record a concrete environment manifest.

Package install/build hooks are **project code** and need containment too. A separate preparation
phase may have narrowly approved network access; the test phase starts with networking disabled.
Do not pass GitHub/Google tokens to npm/pip, even for a private dependency. Private package access
needs an explicit separate credential mechanism and policy, not implicit runner-token inheritance.

The approved profile may run a project-declared test script inside the sandbox, but that declaration
cannot change mounts, network, host identity, timeouts, output scope or secret access. A changed
lockfile changes the environment cache identity; a changed profile needs local approval/versioning.

## Multi-workspace resource isolation
Each workspace has its own writable build/cache overlay/output directories and, for future service
profiles, unique port/database namespaces. Shared caches are immutable or controlled by the broker;
never let one project overwrite executables that another project later trusts. Git worktrees share
repository objects, not virtualenv state by necessity. Test snapshots and workspaces are separate.

An unsupported DWH/Unity/GPU/integration-test environment returns a precise setup blocker with the
missing capability. It does not silently run on the host or claim project readiness. Additional
profiles are a planned registry extension, not a rewrite of command transport or the UI.

No production app deployment or unbounded development server is included. A future preview session
requires its own start/stop/TTL/port-leasing contract, distinct from a finite test operation.
