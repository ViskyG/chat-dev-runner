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
indexed root COMMAND and registered project/workspace COMMAND IDs. If attachment
fails, no EP-05 handler becomes available. Without the file, Runtime remains
at its prior NOT_CONFIGURED state.

The configured Runtime exposes CREATE_PROJECT, CREATE_WORKSPACE, read-only
RECONCILE, RESUME_PROVISION and bounded project FETCH handlers through the
authenticated loopback API. FETCH checks the exact registered private GitHub
identity, journals the network effect and only updates managed bare-repository tracking refs; an
ambiguous transfer stays locked for local reconciliation. Workspace
DELIVER_VERIFY_PUBLISH appears only after the installed owner execution profile,
quota helper and exact workspace route all pass readiness checks. A fully
provisioned UI-origin create is completed by readback and adoption alone. A
reconciled UI-origin partial create with a confirmed resource prefix may adopt
those exact effects and create only the missing Drive descendants and content.
A Drive-originated CREATE_PROJECT is admitted only from the indexed root COMMAND
and publishes to the indexed root RECEIPT. The owner-local UI has created a second workspace in an existing project.
Installed `0.2.2` also registers each verified project COMMAND/RECEIPT for
`CREATE_WORKSPACE` and `FETCH`. A current-chat project FETCH passed end to end;
a live Drive-origin CREATE_WORKSPACE project command remains unverified.

## First project from chat on the installed service

1. On this computer, open `http://127.0.0.1:8765`. Sign in with the current
   one-time code from the private `tokens/session-bootstrap.json` file described
   in [Local authority and API](DOMAIN_API.md). In **Система**, select
   **Включить приём**. One arm enables the current epoch until the owner stops
   intake or the service restarts; it is not a separate step for every command.
   Read the fresh `SYSTEM STATE` in Drive and confirm `ARMED`, its epoch, the
   approved template and `CREATE_PROJECT` in `available_actions`.
2. In chat, open the existing [INSTALLATION INDEX](https://docs.google.com/document/d/1x2KIBZCZfHs2w-s_Ah3yMW80elgG4KseTe7OZq7L_hA/edit)
   and its `CHAT PROTOCOL`. Ask for one new project from an approved template.
   The chat must use the indexed **root** `COMMAND` and `RECEIPT`, follow the
   protocol's revision-guarded `DRAFT` → `READY` procedure, and end its turn
   after submission. On a later request, read the matching root `RECEIPT` and
   the new `PROJECT STATE`; these identify the managed local project, private
   GitHub repository and registered `primary` workspace.
3. For a code change, select that exact project and workspace. Read its
   `SOURCE CONTEXT` and current Git HEAD, then use its registered workspace
   `CHANGESET` and `COMMAND` for one `DELIVER_VERIFY_PUBLISH` request. The chat
   records per-file base hashes and the actual Docs revision, writes `DRAFT`,
   and publishes `READY` last. The installed workspace route can then apply
   the change, run the approved contained profile and publish according to the
   chosen policy. Check `DELIVER_VERIFY_PUBLISH` in the fresh `SYSTEM STATE`
   before submitting. The installed `0.2.1` service completed separate passing
   and failing contained routes with private draft PRs and delivered Drive
   reports; broader EP-07 release acceptance remains open.
4. In a later chat turn, read the matching workspace `RECEIPT` and `LATEST
   RESULT`, follow their Drive report/evidence links and inspect the draft PR.
   Read the project test result separately from operation, delivery and
   publication status. A private repository newly created by the runner may
   also need to be included in ChatGPT's GitHub connector grant before the
   chat can browse its code; the runner's GitHub publication does not depend on
   that connector grant.

For a project refresh from chat, read the fresh `SYSTEM STATE` and the exact
registered **project** `COMMAND`/`RECEIPT` from the existing project index.
Submit one `FETCH` request with the project ID and `workspace_id: null` using
the same revision-guarded `DRAFT` → `READY` procedure, then end the chat turn.
Later, read its matching project `RECEIPT`. FETCH updates managed bare-repo
tracking refs without modifying an occupied worktree. The installed `0.2.2`
service completed this path for `cdr-ep07-drive-20260928` in the current chat
connector session: [project FETCH evidence](../verification/implementation/EP-07/live-project-fetch-20260928.json).
`CREATE_WORKSPACE` is configured and advertised on the project mailbox, but a
live Drive-origin command/RECEIPT rehearsal for it remains open. The complete
EP-07 release checklist also remains open, including a fresh separate chat.

`import_roots` are owner-approved absolute directories for local Git import handles; an empty
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

EP-06 also added authenticated exact workspace source reads, separate on-demand
Drive/GitHub/Git remote checks, exact Drive document links, VS Code launch by registered
workspace ID, and owner-only run evidence retention controls. Explicit `FETCH` is the
only operation that advances the durable per-project fetch timestamp; an `ls-remote`
connection check is read-only and does not imply a fetch. The packaged UI
reflects these backend values. EP-07 has installed and upgraded the durable
owner-user service to wheel `0.2.2`; it is active, enabled and starts disarmed
after a service restart. Read the live UI or `SYSTEM STATE` for
its current intake state. Exact existing Drive root/Index and provider reads, one
UI-origin project, one current-chat Drive-origin project and a second local-UI
workspace have passed deployed checks. See
[the current service upgrade](../verification/implementation/EP-07/release-upgrade-0.2.2.json),
[UI create](../verification/implementation/EP-07/live-ui-acceptance-20260928T105918Z.json),
[Drive roundtrip](../verification/implementation/EP-07/live-drive-chat-roundtrip-20260928.json) and
[multiworkspace readback](../verification/implementation/EP-07/live-multiworkspace-readback-20260928.json).
The separate [passing primary](../verification/implementation/EP-07/live-publish-primary-20260928.jsonl)
and [failing parallel](../verification/implementation/EP-07/live-publish-parallel-20260928.jsonl)
workspace routes each completed with a private draft PR and Drive report while
keeping test outcome separate from operation completion. Same-path restore,
uninstall, broader safety/recovery acceptance, a live project-level Drive
CREATE_WORKSPACE command, and a command from a fresh separate chat remain
release gates.
