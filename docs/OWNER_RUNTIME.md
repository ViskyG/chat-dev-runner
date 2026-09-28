# Owner-local Runtime attachment

`runner serve` reads `~/.config/chat-dev-runner/owner-runtime.json` when that
owner-owned file exists with mode `0600` under a `0700` application directory.
Copy [the non-secret example](../config/owner-runtime.example.json) to that
path and edit the account identity or approved policy there. The file contains
no OAuth token, GitHub token, or credential contents. The existing full-scope
Google OAuth token remains in the application's private `tokens/` directory;
GitHub uses the existing private `github-token` file. The pinned
`installation-legacy.json` is still required for the existing Drive workspace.

Startup validates the exact existing root and INSTALLATION INDEX IDs against
the pinned local legacy manifest and live Google metadata, then verifies the
GitHub login and numeric user ID. It does not create a second Drive workspace,
bootstrap from `drive-seed/`, create a repository, or arm the Ledger. Configured
`runner serve` publishes its current SYSTEM STATE and starts a finite root
mailbox bridge; while disarmed, that bridge never reads or freezes a READY
COMMAND. Once the local owner explicitly arms the runner, it polls only the
indexed root COMMAND and registered workspace COMMAND IDs. If attachment
fails, no EP-05 handler becomes available. Without the file, Runtime remains
at its prior NOT_CONFIGURED state.

The configured Runtime exposes CREATE_PROJECT, read-only RECONCILE, and the
RESUME_PROVISION handler through the authenticated loopback API. A fully
provisioned UI-origin create is completed by readback and adoption alone. A
reconciled UI-origin partial create with a confirmed resource prefix may adopt
those exact effects and create only the missing Drive descendants and content.
A Drive-originated CREATE_PROJECT is admitted only from the indexed root COMMAND
and publishes to the indexed root RECEIPT. `import_roots`
are owner-approved absolute directories for local Git import handles; an empty
list grants no source path. Each `github_imports` entry must identify an
approved private repository by `repository`, `github_identity`,
`authenticated_login`, `repository_id`, `owner_id`, and `node_id`. An empty list
grants no GitHub import. The `template_profiles` mapping binds command-selectable
templates to locally approved profile IDs; it does not by itself install a
container image or execution broker.

Completed local project provisions are scanned by exact local project and
workspace IDs. Their registered Drive COMMAND/CHANGESET IDs are retained in
`Runtime.workspace_mailboxes`; a new completed CREATE_PROJECT is discovered
without restarting the process. This registration alone is inert: it does not
poll or freeze workspace COMMAND and does not advertise
DELIVER_VERIFY_PUBLISH. To enable that route, the owner-side Runtime must
name `execution_profile_dir` in the private owner configuration. Runtime loads
the installed owner grant, checks the rootless broker and hard-quota helper,
and builds `ExecutionPorts` only when they are ready. Route assembly also
verifies the exact private GitHub repository binding and existing Drive
installation before starting a workspace poller. The command cannot supply
those trusted execution ports. The root-owned helper creates each bounded
run volume below `/var/lib/chat-dev-runner/volumes/<run UUID>`, a host path
visible to the rootless Docker daemon. Its installer refuses to replace the
previous `/run`-based helper while any old run volume remains mounted.

Runtime status reports the verified EP-05 attachment separately from the
remaining execution setup. The Ledger still starts disarmed. A local owner
must explicitly arm it through the authenticated loopback API before any
READY command can be admitted. Partial CREATE_PROJECT recovery never replays
an uncertain cloud effect. Use RECONCILE first, confirm that the original locks
are released, then submit a fresh owner-local RESUME_PROVISION. Drive-root
originals require the original terminal partial root RECEIPT to be journaled
and re-read by exact document ID/hash before owner-local RESUME can adopt their
confirmed resource effects.
