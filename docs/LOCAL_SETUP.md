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
structure and contracts; they do not authorize replacement of existing documents. EP-04 must
attach by known ID, read the Installation Index, validate its root/control-document mapping,
and retain existing document identities. No live access was performed in EP-00.

`runner configure --show-required` displays the gate without writing configuration or secrets.
Local setup must eventually obtain, locally:

- Google desktop OAuth app in the owner-private local file
  `$XDG_CONFIG_HOME/chat-dev-runner/google-oauth-client.json` (default
  `~/.config/chat-dev-runner/google-oauth-client.json`). For this dedicated Google account,
  the owner explicitly selected only the full Drive OAuth scope
  `https://www.googleapis.com/auth/drive`. The browser authorization stores token fields only
  in the owner-private `$XDG_CONFIG_HOME/chat-dev-runner/tokens/google-drive.json`; it does
  not copy the Desktop client ID or secret into that token file. A prior `drive.file` token must
  be revoked and discarded before reauthorization. Confirm read access to the exact existing
  root and Installation Index IDs before any Drive write; do not create replacement infrastructure.
- An explicit GitHub owner/organization, private disposable smoke-test namespace, credential
  reference, actual identity/capability probe, and permission for draft PRs and normal pushes.
- Approved local import roots, environment templates, pinned images and dependency build
  policy. Remote fields cannot expand local policy or supply shell commands/host paths.
  The exact host prerequisites and safe verification order are in `docs/EP03_HOST_GATE.md`.
- The owner permits clearly labelled test files created by this runner inside the existing
  shared experiments installation, provided no pre-existing resources or other experiments
  are overwritten or deleted. Other live external effects still require the relevant
  configured policy or explicit owner authorization. Never ask for credentials in chat;
  use a local secret store. Configured production project policy
  will authorize ordinary finite publication without repetitive push confirmation dialogs.

Until these probes and later packet gates pass: NOT_CONFIGURED, armed=false, no READY
commands, no approved execution profiles. The health server is not an execution daemon.

# Filesystem and database

App paths are `$XDG_CONFIG_HOME/chat-dev-runner` (default `~/.config/chat-dev-runner`),
`$XDG_DATA_HOME/chat-dev-runner` (default `~/.local/share/chat-dev-runner`, containing durable
state per the architecture), and `$XDG_CACHE_HOME/chat-dev-runner` (default
`~/.cache/chat-dev-runner`). Optional runtime files use `$XDG_RUNTIME_DIR/chat-dev-runner`.
`XDG_STATE_HOME` does not relocate the architecture's data/state root.
App-owned roots and token/evidence directories are 0700; DB and secret files are 0600.
Reject unsafe existing permissions, symlinks and foreign ownership without modifying
unrelated ancestor directories. Inspection and installation dry-run create no paths.

SQLite uses foreign keys, WAL, explicit migrations and `PRAGMA user_version`. Migration 1
creates only metadata; EP-01 adds domain tables as a subsequent migration. A future schema
version is refused; migration failure rolls back. Take a consistent SQLite backup before
future upgrades. EP-07 must verify recovery/backup procedures before deployment.
