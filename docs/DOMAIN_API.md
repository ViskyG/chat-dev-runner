# Local authority and API

The loopback API uses durable admission and the owner-configured EP-05 Drive,
GitHub and contained execution routes when those integrations are present.
`runner serve` binds only `127.0.0.1` and takes an exclusive owner-only state
lock. Startup starts a fresh epoch and marks unfinished operations
`INTERRUPTED`. A configured normal `serve` arms command intake after its
handlers and mailbox bridge are ready, unless `--start-unarmed` was selected or
the Ledger reports `NEEDS_RECONCILIATION`. Unconfigured processes remain
disarmed. An interrupted operation retains its reserved capacity and resource
locks until the broker proves its worker stopped. A provider effect in
`UNKNOWN` state requires exact remote read/reconciliation before retry. The
API never resumes a previous request automatically.

The server writes a short-lived one-time bootstrap value to
`$XDG_CONFIG_HOME/chat-dev-runner/tokens/session-bootstrap.json` (0600).
The owner reads it locally and POSTs it as JSON to `/api/v1/local/session`
from the same loopback origin. The response sets an HttpOnly, SameSite=Strict
session cookie and returns a per-session CSRF value. The cookie is never placed
in a URL or persistent browser storage. Sessions live only in the server
process and expire; a restart invalidates them. EP-06 adds public fixed-package
`GET /` and `/assets/{name}` routes for the browser login shell. Once the cookie
exists, `GET /api/v1/local/session` returns its CSRF token to a reloaded tab;
it never returns the session cookie or the bootstrap value. The running
server atomically rotates the 0600 owner-only bootstrap file before expiry and
after an exchange. Rotation does not revoke existing browser sessions. A
server restart still invalidates in-memory sessions and issues a new bootstrap.

Every request checks the exact Host and supplied Origin. Mutations additionally
require Origin, `application/json`, a valid session, CSRF and bounded body.
GET routes have no mutation side effects. EP-06 adds explicit owner-authenticated
Drive, GitHub API and selected Git remote-head checks. Each provider read is timestamped
separately and keeps the last success visible after a later failure. Git remote-head
knowledge is `ls-remote`, not a fetch; the UI must not show a fetch timestamp from it.
A project-scoped `FETCH` reads the exact registered private GitHub identity, journals
an effect intent, and uses a fixed no-prune Git refspec to update only local bare
`origin/*` tracking refs. It does not alter worktrees, run tests or upload files.
Its last confirmed result provides the durable fetch freshness projection; an
ambiguous transfer retains the repository lock until explicit local reconciliation.
The workspace Drive Docs links are loaded only on demand through exact locally
registered IDs and revalidated remote metadata, with no title search or creation.
The authenticated `/source` read inspects the current registered Git worktree and
reports observed HEAD/branch/dirty separately from durable registry identity.
The owner-only editor route accepts project and workspace IDs, then launches a
fixed VS Code argv using the checked managed path; no client path or command is accepted.
The authenticated system projection exposes sorted nonsecret approved template,
profile, GitHub identity and imported-repository IDs so forms can present actual
owner choices; it does not expose credential material or expand authority.
Production `arm` is available only when a locally configured production handler
is registered; an unconfigured process refuses with `ACTION_UNAVAILABLE`.
An owner can disarm the running process through the authenticated local UI/API;
that choice lasts until restart. A clean configured restart arms by default,
and startup always creates a fresh epoch so older READY commands cannot inherit
the previous epoch.
`/api/v1/health` is public but returns only minimal local capability/arming state. `/api/v1/system`, operations,
projects, runs and event journal require the session. `GET /api/v1/events`
returns a finite ordered SSE batch; Last-Event-ID resumes from durable SQLite
history without taking an execution slot. Output bytes stream only by registered
run/output ID from EP-03's immutable evidence store, as an attachment with a
sandboxed content policy.

The authenticated retention view reports an owner pin and registered output
availability beside immutable run evidence. A local owner can pin or unpin a run
and request expiration of one named output. The evidence store enforces age,
completed execution and delivery, reference, uncertain-effect and pin gates,
then writes an append-only tombstone before unlinking the checked private file.
A rejected or failed purge does not silently rewrite the original run manifest.
The browser confirms the exact run/output before an expire request.

The same `runner_app.domain.commands` validation and `runner_app.storage.ledger`
admission serve both the local UI and Drive transports. A transport supplies trusted
`Provenance` from local selection or a registered Drive document ID; JSON
command fields cannot grant source authority. A command freezes canonical JSON
and a SHA-256 digest, validates v1 schema, canonical IDs, RFC3339 times, local
profile/template/account policy, current epoch, and registered scope. Database
admission binds ID, digest and source, reserves a complete sorted lock set and
capacity, or saves terminal refusal. A duplicate identical ID is a lookup;
changed content is `REQUEST_ID_REUSED`. `BUSY` never waits or retries. Cancellation
is an authenticated operation exempt from execution capacity and bound to the
same workspace as its target.

SQLite migrations through schema version 9 add projects, workspaces,
operations, steps, runs, outputs, resources, faults, locks, effect intents,
append-only journal and later source, execution, delivery and retention records
on top of EP-00 migration metadata. Intent is durable before provider writes.
The existing wire schemas remain v1.0; `INTERRUPTED` and `UNKNOWN` were already
available, and no new wire enum was invented. EP-02 through EP-05 now populate
source, project, operation and run records, with passing and failing live EP-05
disposable routes recorded in their checkpoints.

The existing Drive installation is pinned by exact root and Index IDs in
`config/installation.existing.json`. EP-04 attached and validated it, and EP-05
used it for real disposable command, result and report delivery. `drive-seed/`
remains a reference, never a request to bootstrap a second workspace.
