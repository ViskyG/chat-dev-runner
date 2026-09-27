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

These are design IDs, not preinstalled images or a promise that every existing project is compatible.
UI fixtures may list them; live setup must only advertise locally approved, actually installed ones.
Image digests, interpreter versions and lockfiles are selected and verified in EP-00/03. Do not pin
a guessed digest, download a floating runtime on every operation, or install into the owner's Python.

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
