# Deployment and local setup

## Current deployment state

EP-00–06 are verified. EP-07 has an installed, enabled owner-user service
running the tested `0.2.5` wheel from the private environment. A clean configured
start arms command intake by default; the service listens only on
`127.0.0.1:8765`. The initial host installation and prior upgrades are recorded in
[release-install.json](../verification/implementation/EP-07/release-install.json),
[release-upgrade-0.2.1.json](../verification/implementation/EP-07/release-upgrade-0.2.1.json) and
[release-upgrade-0.2.2.json](../verification/implementation/EP-07/release-upgrade-0.2.2.json) and
[drive-create-performance-upgrade-0.2.5.json](../verification/implementation/EP-07/drive-create-performance-upgrade-0.2.5.json).
The installed UI created one disposable private project; a command from this
chat's Drive connector created a second, and the local UI added a second
workspace to the first. The two workspaces completed separate passing and
failing contained verification/publication routes with private draft PRs and
Drive reports. Installed `0.2.2` also completed a project-level `FETCH` command
from this chat connector session with a matching project RECEIPT. Fresh
separate-chat and broader host/recovery acceptance remain open, so EP-07 is
not yet a signed daily-use release. The `0.2.5` update overlaps independent
Drive provisioning and validation requests; its new live CREATE_PROJECT duration
will be measured on the next real project request.

The original `prototype/` remains a separate offline simulation. To inspect
that prototype, stop the production service before using the same port:

```bash
./start-prototype.sh
# Open http://127.0.0.1:8765; stop with Ctrl+C.
```

Its static server serves only `prototype/`. It does not call Drive, GitHub or
the execution broker. Do not confuse it with the packaged runner UI.

## Existing installation and local configuration

The owner already provisioned the `Chat-Dev Runner` Drive root
`177IEHeBVezHIlbIijZsIvRDSbTJ42kxe` and INSTALLATION INDEX
`1x2KIBZCZfHs2w-s_Ah3yMW80elgG4KseTe7OZq7L_hA`. The runner attaches by these
exact IDs and a private legacy manifest; `drive-seed/` is reference material.
**Do not run `scripts/bootstrap_drive.py --apply` against this account.** The approved
OAuth scope is exactly `https://www.googleapis.com/auth/drive`. Desktop OAuth client,
refresh token, GitHub token, owner policy, database and evidence are private local data,
not service-unit environment variables or repository files. See `docs/LOCAL_SETUP.md`
and `docs/OWNER_RUNTIME.md` for the current paths and policy.

The EP-05 root-owned quota helper and narrow sudo rule are installed and probed READY;
owner-approved `python-basic` profiles use an exact image digest. The deployed
service reports execution readiness READY on a read-only check. The worker
uses the explicit owner rootless Docker socket;
project containers receive neither that socket nor Google/GitHub credentials. No public
listener, proxy, tunnel, ingress or firewall exception belongs to this installation.

## Commands available in the implementation checkout

The current `runner` CLI supports `doctor --offline`, `configure --show-required`,
`install --dry-run`, `install --user`, `status`, `stop`, `uninstall --user`,
`backup --output`, `restore --from`, `drive-check --live` and `serve`. The offline
doctor and setup report are foundation inspection commands; they do not
replace current runtime SYSTEM STATE or explicit connection checks. `install --dry-run` renders and inspects the proposed owner unit
without writing or starting anything. `drive-check --live` performs read-only
exact-ID Drive checks using the configured local OAuth token. `serve` has been exercised as
a transient process and binds to 127.0.0.1; its private owner configuration determines
which handlers are available. Configured `serve` arms by default after clean
recovery checks; `--start-unarmed` explicitly keeps
that process disarmed. `NEEDS_RECONCILIATION` leaves intake off until resolved.
With configured integrations, `serve` starts the Drive bridge and may publish
SYSTEM STATE to the existing exact control Doc at startup; it is not an offline
inspection command. The bridge does not freeze a READY command while disarmed.
A manual disarm of the running process lasts only until its next restart.

```bash
.venv/bin/python -m runner_app.cli configure --show-required
.venv/bin/python -m runner_app.cli drive-check --live
```

These examples are documented interfaces, not EP-07 installation evidence. The authenticated
UI starts from an owner-only bootstrap handoff; do not copy bootstrap or token values into
shell history, logs, Drive documents or this repository.

## EP-07 owner-user service

The production target for this host is a private, **wheel-installed** virtual environment
at `/home/visky/Work/GPTRunner/.runner-install/venv`. Dependencies come from
`requirements.production.lock`, the hash-pinned production and Google bootstrap lock.
The checkout's `.venv` is an editable development install; `install --user` rejects it
with `WHEEL_INSTALL_REQUIRED`. Build and record the wheel artifact, then install that
exact wheel into the dedicated environment. Stop the service and take a
private backup before an in-place upgrade. For the source version in this
checkout:

```bash
install -d -m 0700 /home/visky/Work/GPTRunner/.runner-install
cdr_wheel_version="$(.venv/bin/python -m runner_app.cli --version)"
cdr_wheel_path="/home/visky/Work/GPTRunner/.runner-install/wheels/chat_dev_runner-${cdr_wheel_version}-py3-none-any.whl"
uv build --wheel --out-dir /home/visky/Work/GPTRunner/.runner-install/wheels
test -f "$cdr_wheel_path"
sha256sum "$cdr_wheel_path"
uv venv /home/visky/Work/GPTRunner/.runner-install/venv --python /usr/bin/python3.10
uv pip sync --python /home/visky/Work/GPTRunner/.runner-install/venv/bin/python \
  --require-hashes requirements.production.lock
uv pip install --python /home/visky/Work/GPTRunner/.runner-install/venv/bin/python \
  --no-deps "$cdr_wheel_path"
```

Record the final installed wheel SHA-256 in release evidence; the production
lock SHA-256 is
`79036fafb676757f4109d02c9a85910ac0a9f607582cc6151339e7d9c91e2cea`.
Retain both hashes with the release evidence. The initial `0.2.0` install
and in-place `0.2.1` and `0.2.2` upgrades are recorded separately in
[release-install.json](../verification/implementation/EP-07/release-install.json),
[release-upgrade-0.2.1.json](../verification/implementation/EP-07/release-upgrade-0.2.1.json) and
[release-upgrade-0.2.2.json](../verification/implementation/EP-07/release-upgrade-0.2.2.json).

The durable state root selected for this host is
`/home/visky/Work/GPTRunner/.runner-data/chat-dev-runner`: the default
`~/.local/share/chat-dev-runner` contains old root-owned EP-03 volumes and is not
the service state. Set `XDG_DATA_HOME` to the parent of the selected state root
for **every** operator command below. The existing private config and token files
still come from `~/.config/chat-dev-runner`; the unit carries the exact resolved
XDG paths itself after installation.

The packaged `runner_app/production.service.in` template is rendered with the
installed Python and exact XDG paths into
`$XDG_CONFIG_HOME/systemd/user/chat-dev-runner.service` (default
`~/.config/systemd/user/chat-dev-runner.service`). The owner-user commands are:

```bash
export XDG_DATA_HOME=/home/visky/Work/GPTRunner/.runner-data
runner_python=/home/visky/Work/GPTRunner/.runner-install/venv/bin/python
"$runner_python" -m runner_app.cli install --dry-run
"$runner_python" -m runner_app.cli install --user
"$runner_python" -m runner_app.cli status
"$runner_python" -m runner_app.cli stop
"$runner_python" -m runner_app.cli uninstall --user
```

`install --user` creates only the managed user unit, reloads systemd, enables and
starts it; `uninstall --user` stops/disables and removes only that unit. Neither
deletes local state, source repositories nor cloud evidence. The unit runs from
the private state root, binds `127.0.0.1`, restarts on failure and uses the
wheel-installed virtualenv Python. Its orchestrator must retain the ability to
invoke the installed narrow sudo quota helper; contained project processes
separately enforce `no-new-privileges` and have no token or Docker-socket mount.
The installer refuses an unmanaged unit with the same name.

On this host the user unit is already enabled for `default.target`, and
`loginctl show-user visky -p Linger` reports `Linger=yes`. This starts the user
manager and enabled service at computer boot even before an interactive login;
it does not require a separate system unit. The installed wheel and managed
unit enable command intake after a clean boot/restart.
An explicit `--start-unarmed` process or unresolved
`NEEDS_RECONCILIATION` starts with intake off. Stopping intake in the UI lasts
only until the next service restart; check the live UI or SYSTEM STATE after a
reboot. The installed managed unit omits `--start-unarmed`. A real reboot
check remains to be recorded.

The owner-host service passed its first gate, a second gate after
stop/restart, and gates after the `0.2.1` and `0.2.2` upgrades.
`runner install --dry-run` reported an absent unit and wheel package; `install --user` started and enabled the 0600 user unit.
`runner stop` closed the listener, and a repeat `install --user` restarted the
same unit without changing it. The deployed gate observed only
`127.0.0.1:8765`, an unarmed health response, a packaged UI with CSP, and
403 responses for unauthenticated, spoofed-Host and cross-origin access.
On-demand Drive and GitHub read checks returned OK under the `0.2.1` wheel,
and execution readiness returned READY. The current unit is active and enabled
with clean-start intake armed by default. The project-level FETCH
completed through the existing Drive mailbox under `0.2.2`. The configured service may publish SYSTEM
STATE to the existing Drive control Doc at startup; it creates no second
Drive installation. See [release-install.json](../verification/implementation/EP-07/release-install.json),
[release-upgrade-0.2.1.json](../verification/implementation/EP-07/release-upgrade-0.2.1.json),
[release-upgrade-0.2.2.json](../verification/implementation/EP-07/release-upgrade-0.2.2.json),
[live-project-fetch-20260928.json](../verification/implementation/EP-07/live-project-fetch-20260928.json),
[service-gate.json](../verification/implementation/EP-07/service-gate.json) and
[installed-runtime-read-after-upgrade.json](../verification/implementation/EP-07/installed-runtime-read-after-upgrade.json)
for exact host evidence. Uninstall, a successful same-path live restore, a
fresh separate-chat command, live project-level Drive CREATE_WORKSPACE
command and broader recovery/security acceptance are still open.

## EP-07 backup and recovery gates

EP-07 has verified private backup creation and refusal gates. A successful
same-path restore, migration recovery, interrupted operation reconciliation,
disk-cap handling and retention still need host acceptance. Wheel upgrades to
`0.2.1` and `0.2.2` passed their recorded service gates; the latter followed a
private schema-9 backup whose manifest hash, all 416 file hashes and SQLite
integrity were independently verified, plus a successful `uv pip check`.
The pre-service snapshot exists at
`/home/visky/Work/GPTRunner/.runner-backups/pre-ep07-service-20260928`: its
manifest records schema 9 and 181 files, and its hashes and SQLite integrity
were verified. It excludes config, credentials and tokens; it can still contain
private managed project source and run evidence. The archive is an owner-only
**directory** with `manifest.json` and `state/`, not a tarball. A second
post-stop snapshot at
`/home/visky/Work/GPTRunner/.runner-backups/post-stop-service-20260928`
completed with schema 9 and 181 files.

To make a new snapshot, stop the service and choose a new, absent archive
directory below an existing owner-only 0700 parent:

```bash
export XDG_DATA_HOME=/home/visky/Work/GPTRunner/.runner-data
runner_python=/home/visky/Work/GPTRunner/.runner-install/venv/bin/python
"$runner_python" -m runner_app.cli stop
"$runner_python" -m runner_app.cli backup \
  --output /home/visky/Work/GPTRunner/.runner-backups/backup-YYYYMMDD
```

The backup command refuses a running singleton, unknown or changing state
entries, and an existing output path. On this host, a backup while active
returned `RUNNER_RUNNING` without creating an archive, and restore while the
state path existed returned `RESTORE_TARGET_EXISTS`. `restore --from` validates
every hash and SQLite integrity, then restores **only** to the same absolute
state path recorded in the archive, when that target does not exist; it never
overwrites current state. See `docs/RECOVERY.md` before moving a damaged state directory
aside. A live restore test remains an EP-07 gate.

The explicit `scripts/reconcile_run_attempt.py` and
`scripts/release_run_volumes.py` tools were used for EP-05 container and volume
recovery; they do not replace a complete backup/restore procedure. See
`docs/RECOVERY.md` and `codex/RELEASE_ACCEPTANCE.md` for the remaining gates.
Uninstall must stop only the app-owned service/hooks. Managed source
repositories and cloud evidence remain unless the owner separately authorizes
deletion. OAuth/GitHub revocation must target this runner's actual grants, not
the owner's unrelated integrations.

EP-07 has completed the local-UI and current-chat Drive-origin disposable
project gates: [UI create](../verification/implementation/EP-07/live-ui-acceptance-20260928T105918Z.json),
[Drive command and receipt](../verification/implementation/EP-07/live-drive-chat-roundtrip-20260928.json).
The local UI then created an independent `parallel` workspace beside `primary`
in the first project. The original browser wait timed out after backend
completion; independent [readback](../verification/implementation/EP-07/live-multiworkspace-readback-20260928.json)
confirmed both workspaces active, distinct branches and registered Drive
folders. The separate [passing primary](../verification/implementation/EP-07/live-publish-primary-20260928.jsonl)
and [failing parallel](../verification/implementation/EP-07/live-publish-parallel-20260928.jsonl)
routes each completed with a private draft PR and Drive report. The
[current-chat project FETCH](../verification/implementation/EP-07/live-project-fetch-20260928.json)
also completed through the installed `0.2.2` project mailbox and matching
RECEIPT. Interruption/recovery evidence, a live project-level Drive
CREATE_WORKSPACE command, and a command from a fresh separate chat still need
acceptance. Prior EP-05/06 smoke evidence does not substitute for these
release gates. Those bounded `0.2.1`/`0.2.2` live gates ended disarmed;
the current service arms after a clean restart.
