# Deployment and local setup

## What is runnable now
The offline prototype and reference toy demo. No production daemon is installed by this kit.
The prototype has **no mutation API** and never invokes GitHub, Google Drive or project commands.

```bash
# Any location chosen for the unpacked kit:
cd /path/to/chat-dev-runner
./start-prototype.sh
# Open http://127.0.0.1:8765 ; stop with Ctrl+C.
```
The static server serves only `prototype/`, not configuration, source code or credentials.
The self-contained `runner-prototype.html` may also be opened directly; storage availability
in a file URL depends on browser policy. Prefer the loopback server when local file scripts are blocked.

## Optional prototype-only service
Only if you deliberately want it. The included `prototype.service` expects the kit at
`~/.local/share/chat-dev-runner-kit`. Copying a kit into that directory is an explicit owner action.
No installer in this delivery performs these commands automatically.

```bash
mkdir -p "$HOME/.config/systemd/user"
cp deploy/prototype.service "$HOME/.config/systemd/user/chat-dev-runner-prototype.service"
systemctl --user daemon-reload
systemctl --user start chat-dev-runner-prototype.service
systemctl --user status chat-dev-runner-prototype.service
# Stop/remove:
systemctl --user stop chat-dev-runner-prototype.service
rm "$HOME/.config/systemd/user/chat-dev-runner-prototype.service"
systemctl --user daemon-reload
```
Enabling at login is optional and not part of the default prototype. Do not enable linger or a
system/root service simply to show a mockup.

## Production installation — Codex must implement and test this
`production.service.in` is intentionally a **template**, not an installable completed service.
EP-07 renders exact paths and runs the real acceptance tests. The production CLI/interface should be:

```text
runner doctor --offline
runner configure                  # local credentials, granted templates, import roots
runner bootstrap-drive --plan
runner bootstrap-drive --apply    # explicit owner authorization
runner doctor --integrations      # actual OAuth/GitHub canaries
runner install --dry-run
runner install --user             # explicit owner authorization, starts unarmed
runner serve                      # only 127.0.0.1
runner status
runner stop
```
EP-00 implements `runner doctor --offline`, `runner configure --show-required`,
`runner install --dry-run`, and `runner serve --health-only`. EP-01 adds the authenticated
local `runner serve` API, which starts unarmed with no production action handlers. The other commands
remain future packet work. For this existing Drive installation, bootstrap apply is prohibited;
attach using `config/installation.existing.json` and `docs/LOCAL_SETUP.md`. Setup UI should provide the same workflow with secrets entered
locally and permissions scoped to this installation.

Production needs separate configuration/state/cache/evidence paths. Reserve a private state root,
version SQLite migrations, record profile/image digests and obtain actual GitHub identity. Check
Draft PR account support; do not silently substitute a ready PR. Keep ChatGPT repo entitlement
manual until an official supported mechanism is confirmed.

Rootless sandbox setup must be compatible with the service hardening. In particular, a worker
that creates user namespaces may not work under a blanket restriction; isolate the API/credential
broker from its approved execution broker and test the final unit. Never “fix” it by mounting the
host Docker socket into project containers, running root, or exposing the API.

No cloud webhook listener, public reverse proxy, Kubernetes ingress, router forwarding or tunnel
is needed. Drive/GitHub access is outbound. Existing Kubernetes/Docker configuration is not modified.

## Required operational procedures
EP-07 must fill in tested commands for backup/restore, database migration rollback, interrupted
operation reconciliation, OAuth revocation, disk-cap handling, retention, service upgrade and
uninstall. Uninstall must retain user sources and cloud evidence unless deletion is separately
requested. Revoke the actual local app credentials rather than all of the user's Google/GitHub access.
