# Local setup and authorization gate

The existing Drive installation is authoritative. Its root is
`177IEHeBVezHIlbIijZsIvRDSbTJ42kxe`; its [INSTALLATION INDEX](https://docs.google.com/document/d/1x2KIBZCZfHs2w-s_Ah3yMW80elgG4KseTe7OZq7L_hA/edit)
is `1x2KIBZCZfHs2w-s_Ah3yMW80elgG4KseTe7OZq7L_hA`.
The owner-supplied IDs have now passed authenticated exact-ID reads. A private local
manifest at `~/.config/chat-dev-runner/installation-legacy.json` maps 13 existing
roles to provider IDs, each present in the human-readable Index; it is stored with
mode 0600 under a 0700 directory. The cross-app canary also passed using a new
labelled test Doc, without modifying pre-existing files.
Do not run `scripts/bootstrap_drive.py --apply` for this installation. The seed files specify
structure and contracts; they do not authorize replacement of existing documents. EP-04
attached by known ID, read the Installation Index, validated its root/control-document mapping
and retained existing document identities. EP-05 used that attachment for disposable private
project delivery; EP-06 checked the same installation from the real disarmed local process.

`runner configure --show-required` displays the gate without writing configuration or secrets.
Local setup uses the following owner-private inputs and policy:

- Google desktop OAuth app in the owner-private local file
  `$XDG_CONFIG_HOME/chat-dev-runner/google-oauth-client.json` (default
  `~/.config/chat-dev-runner/google-oauth-client.json`). For this dedicated Google account,
  the owner explicitly selected only the full Drive OAuth scope
  `https://www.googleapis.com/auth/drive`. The browser authorization stores token fields only
  in the owner-private `$XDG_CONFIG_HOME/chat-dev-runner/tokens/google-drive.json`; it does
  not copy the Desktop client ID or secret into that token file. The prior `drive.file`
  token was revoked and discarded before full-scope reauthorization. Exact-ID root and Index
  reads and the cross-app canary have passed; do not create replacement infrastructure.
- An explicit GitHub owner/organization, private disposable smoke-test namespace, credential
  reference, actual identity/capability probe, and permission for draft PRs and normal pushes.
- Approved local import roots, environment templates, pinned images and dependency build
  policy. Remote fields cannot expand local policy or supply shell commands/host paths.
  The exact host prerequisites and safe verification order are in `docs/EP03_HOST_GATE.md`.
- The owner permits clearly labelled test files created by this runner inside the existing
  shared experiments installation, provided no pre-existing resources or other experiments
  are overwritten or deleted. Other live external effects still require the relevant
  configured policy or explicit owner authorization. Never ask for credentials in chat;
  use a local secret store. The configured project policy authorizes ordinary finite
  publication without repetitive push confirmation dialogs.

The EP-04/05 integration and execution gates passed. The owner runtime is configured,
and the approved `python-basic` profile and quota helper are installed. EP-07
installed an enabled owner-user service from a locked wheel, bound only to
`127.0.0.1:8765`. Clean configured startup now arms command intake by default;
check live SYSTEM STATE for its current state. A completed command can
remain READY in its Drive document without being re-executed: admission is keyed
to its recorded request ID and receipt. One disposable private project was
created through the installed UI and another through the existing root Drive
COMMAND from this chat connector session. A second workspace was created from
the local UI. The two workspaces completed separate passing and failing
contained verification/publication routes with private draft PRs and Drive
reports. The current-chat Drive connector also submitted a project-level
FETCH through installed `0.2.2` and read its matching COMPLETED project
RECEIPT. See [the prior `0.2.2` release evidence](../verification/implementation/EP-07/release-upgrade-0.2.2.json),
[UI create](../verification/implementation/EP-07/live-ui-acceptance-20260928T105918Z.json),
[Drive create](../verification/implementation/EP-07/live-drive-chat-roundtrip-20260928.json),
[two-workspace readback](../verification/implementation/EP-07/live-multiworkspace-readback-20260928.json),
[passing route](../verification/implementation/EP-07/live-publish-primary-20260928.jsonl),
[failing route](../verification/implementation/EP-07/live-publish-parallel-20260928.jsonl) and
[project FETCH](../verification/implementation/EP-07/live-project-fetch-20260928.json).
The installed user unit is enabled for the owner's `default.target`, and this
host has `Linger=yes` for the owner; systemd can therefore start it when the
computer boots without waiting for an interactive login. A reboot acceptance
check has not yet been recorded. The configured `serve` arms automatically
after clean startup checks; `--start-unarmed` explicitly
keeps that process disarmed. A `NEEDS_RECONCILIATION` gate keeps intake off
until resolved. A manual disarm is temporary and does not survive a restart.

Same-path restore, uninstall, a live project-level Drive CREATE_WORKSPACE
command, broader recovery/security acceptance and a command from a fresh
separate chat remain open. The stateless
`serve --health-only` mode is a foundation probe, not the configured service.

# Filesystem and database

App paths are `$XDG_CONFIG_HOME/chat-dev-runner` (default `~/.config/chat-dev-runner`),
`$XDG_DATA_HOME/chat-dev-runner` (default `~/.local/share/chat-dev-runner`, containing durable
state per the architecture), and `$XDG_CACHE_HOME/chat-dev-runner` (default
`~/.cache/chat-dev-runner`). Optional runtime files use `$XDG_RUNTIME_DIR/chat-dev-runner`.
`XDG_STATE_HOME` does not relocate the architecture's data/state root. On this
host, the installed service uses `XDG_DATA_HOME=/home/visky/Work/GPTRunner/.runner-data`
and thus `/home/visky/Work/GPTRunner/.runner-data/chat-dev-runner` for state;
the default data path contains older root-owned test volumes. Use the same
`XDG_DATA_HOME` for owner CLI maintenance commands. App-owned roots and
token/evidence directories are 0700; DB and secret files are 0600.
Reject unsafe existing permissions, symlinks and foreign ownership without modifying
unrelated ancestor directories. Inspection and installation dry-run create no paths.

SQLite uses foreign keys, WAL, explicit migrations and `PRAGMA user_version`; the current
runtime schema is version 9. A future schema version is refused and migration failures roll
back. Take a consistent SQLite backup before upgrades. EP-07 verified a private
pre-service snapshot and stopped-service backups before both the `0.2.1`
and `0.2.2` upgrades, plus refusal to back up while running or restore over
existing state. The latter backup held 416 files with schema 9; its manifest
hash, all file hashes and SQLite integrity were independently verified. A successful same-path live
restore and migration recovery still require host acceptance; see
[release-install.json](../verification/implementation/EP-07/release-install.json) and
[release-upgrade-0.2.1.json](../verification/implementation/EP-07/release-upgrade-0.2.1.json) and
[release-upgrade-0.2.2.json](../verification/implementation/EP-07/release-upgrade-0.2.2.json).
