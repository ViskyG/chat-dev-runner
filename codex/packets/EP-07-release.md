---
id: EP-07
status: NOT_STARTED
depends_on: ['EP-06']
---
# EP-07 — Host acceptance, deployment and recovery

## Required inputs
`docs/SCENARIOS_AND_ACCEPTANCE.md`, `docs/SECURITY.md`, `docs/RECOVERY.md`,
`deploy/README.md`, `codex/RELEASE_ACCEPTANCE.md`, prior packet checkpoints.

## Outcome
Finish a reproducible owner-controlled installation with evidence. The machine's existing
repositories, Kubernetes and personal configuration must remain intact.

## Implementation tasks
1. Produce a locked installation and migration path. Render the production service template to
   actual paths; startup is unarmed until owner setup passes. Rootless sandbox execution broker
   separation and credentials handling must be documented and tested as deployed.
2. Bind only 127.0.0.1 (optional separately validated ::1). Check listeners and external reachability;
   do not create firewall/router exceptions, ingress, public tunnel, webhook listener or proxy.
3. Test stop/restart/power interruption around each durable boundary, OAuth expiry, GitHub outage,
   disk cap, network loss during upload, dirty worktree, conflicting branch, BUSY and cancellation.
4. Complete one private disposable project from local UI and one from Drive, with two workspaces,
   one failure, one successful verification, automatic branch/draft PR and report publication.
5. Capture evidence that no credentials enter sandbox/Drive/GitHub/artifacts; smoke-test malicious
   artifact/log text and localhost-CSRF. Do not print real token values as “proof”.
6. Create backup/restore/retention/uninstall docs. Uninstall stops the service and removes app-owned
   integration hooks only; source repos/cloud evidence are retained unless the owner separately
   requests deletion. Revocation instructions identify the actual OAuth app/GitHub grants.
7. Export versioned Drive instructions, schema registry and capability state. Verify a fresh chat
   can find the correct project/workspace and issue one valid command using the published instructions.
8. Update STATUS.json from implemented evidence, release notes, lockfiles, checksums and test report.
   Keep all blocked live gates visible; do not present the development prototype as deployed software.

## Acceptance and evidence
Use codex/RELEASE_ACCEPTANCE.md as a mandatory signed checklist. Pass the complete scenario
matrix and maintain evidence paths for each scenario. The release must include the exact launch,
status, stop, backup and recovery commands tested on the owner's host, plus actual installed versions.

## Scope boundary
Do not arm a local service or launch production user-code tests merely to complete an unchecked
release task. Ask only at genuine authorization/secret-entry boundaries; work offline on everything
else. A missing permission is a blocked integration gate, not an excuse to weaken the design.

## Packet closing rule
Write `codex/checkpoints/EP-07.md` using the checkpoint template. Put machine evidence in
`verification/implementation/EP-07/`. Record actual commands, exit codes and environment identity.
Do not mark this packet VERIFIED if only mocks pass but the specified gate requires a real local
process. Live provider gates may be BLOCKED while offline implementation is tested, but the final
release remains not deployable until they pass.
