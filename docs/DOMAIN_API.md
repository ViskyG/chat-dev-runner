# EP-01 local authority and API

EP-01 provides an unarmed local API and durable admission. It has no production
execution handler, GitHub adapter, Drive adapter, or project-code execution broker.
`runner serve` binds only `127.0.0.1` and takes an exclusive owner-only state
lock. The first process starts a new epoch, leaves the runner unarmed, and marks
unfinished operations `INTERRUPTED`. An interrupted operation retains its
reserved capacity and resource locks until the broker proves its worker stopped.
A provider effect in `UNKNOWN` state requires remote read/reconciliation before
any retry. The API does not automatically resume a previous request.

The server writes a short-lived one-time bootstrap value to
`$XDG_CONFIG_HOME/chat-dev-runner/tokens/session-bootstrap.json` (0600).
The owner reads it locally and POSTs it as JSON to `/api/v1/local/session`
from the same loopback origin. The response sets an HttpOnly, SameSite=Strict
session cookie and returns a per-session CSRF value. The cookie is never placed
in a URL or persistent browser storage. Sessions live only in the server
process and expire; a restart invalidates them. Browser setup and renewal
are EP-06 work; the current CLI requires a server restart to obtain a new
bootstrap after expiration. A running operation makes such a restart an
interruption, so this is an explicit remaining usability gap before release.

Every request checks the exact Host and supplied Origin. Mutations additionally
require Origin, `application/json`, a valid session, CSRF and bounded body.
GET routes have no mutation side effects. Production `arm` currently refuses
with `ACTION_UNAVAILABLE` because no production handlers are installed;
registered fake handlers exist only in tests. `/api/v1/health` is public but
returns only minimal local capability/arming state. `/api/v1/system`, operations,
projects, runs and event journal require the session. `GET /api/v1/events`
returns a finite ordered SSE batch; Last-Event-ID resumes from durable SQLite
history without taking an execution slot. Output bytes are not served until
EP-03 adds immutable registered storage.

The same `runner_app.domain.commands` validation and `runner_app.storage.ledger`
admission serve future local UI and Drive transports. A transport supplies trusted
`Provenance` from local selection or a registered Drive document ID; JSON
command fields cannot grant source authority. A command freezes canonical JSON
and a SHA-256 digest, validates v1 schema, canonical IDs, RFC3339 times, local
profile/template/account policy, current epoch, and registered scope. Database
admission binds ID, digest and source, reserves a complete sorted lock set and
capacity, or saves terminal refusal. A duplicate identical ID is a lookup;
changed content is `REQUEST_ID_REUSED`. `BUSY` never waits or retries. Cancellation
is an authenticated operation exempt from execution capacity and bound to the
same workspace as its target.

SQLite schema version 2 adds projects, workspaces, operations, steps, runs,
outputs, resources, faults, locks, effect intents and append-only journal on
top of EP-00 migration metadata. Intent is durable before provider writes.
The existing wire schemas remain v1.0; `INTERRUPTED` and `UNKNOWN` are already
available, and no new wire enum was invented. Current repository/archive has no
Git metadata. EP-02 and later packets will populate source/project/run records.

The Drive installation recorded in `config/installation.existing.json` already
exists. EP-04 will attach by known IDs, read and validate `INSTALLATION INDEX`,
check per-file OAuth access, and only then perform an authorized live smoke test.
`drive-seed/` is a reference, not a command to bootstrap a second workspace.
