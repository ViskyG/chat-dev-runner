---
id: EP-00
status: VERIFIED
depends_on: []
---
# EP-00 — Foundation and host inventory

## Required inputs
`AGENTS.md`, `STATUS.json`, `docs/DECISIONS.md`, `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, `deploy/README.md`.

## Outcome
Scaffold the production package and developer environment without cloud writes or user-project
execution. Inventory the installed Python/Git/Node, OS, cgroups and rootless container support;
record facts, not inferred preferences. Existing Docker/Kubernetes on the host are not ours to
upgrade, reconfigure, or expose. Establish the owner-visible installation gate for cloud credentials.

## Implementation tasks
1. Run the reference tests and prototype smoke test; preserve their baseline reports.
2. Create `runner_app` modules and CLI entry points; add dependency lock, type/lint tools and a
   tiny health-only loopback server. Source the chosen Python version from the host inventory.
3. Define XDG config/state/cache roots and versioned migration strategy. Enforce private modes
   on state and token directories. No secrets in repo, Drive, manifests or CLI history.
4. Design local setup for GitHub identity/capabilities, Google OAuth desktop app, permitted local
   import roots and environment templates. Show NOT_CONFIGURED until each live probe is real.
5. Add `runner doctor --offline` and `runner install --dry-run`. Installation must report missing
   prerequisites instead of attempting sudo or upgrading system components.
6. Finalize the sandbox image selection/digest and dependency lock in a reproducible setup record;
   do not ship a floating `latest` image as the final verified environment.

## Acceptance and evidence
- clean venv editable and installed-package import checks; schemas/resources accessible;
- package CLI help and offline doctor without cloud credentials;
- install dry-run creates no service/cloud side effects;
- secrets/config permissions tests and a forbidden-0.0.0.0 configuration test;
- baseline reference suite remains green; no user git status changed.

## Scope boundary
Do not add a real runner loop, OAuth login or GitHub repository creation yet. A packaging failure
must be fixed here rather than worked around with undocumented PYTHONPATH behavior.

## Packet closing rule
Write `codex/checkpoints/EP-00.md` using the checkpoint template. Put machine evidence in
`verification/implementation/EP-00/`. Record actual commands, exit codes and environment identity.
Do not mark this packet VERIFIED if only mocks pass but the specified gate requires a real local
process. Live provider gates may be BLOCKED while offline implementation is tested, but the final
release remains not deployable until they pass.
