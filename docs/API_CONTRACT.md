---
id: CDR-API
status: EP-06-verified
---
# Local API outline

One loopback origin, authenticated local session. GET never mutates. Paths below are the
implemented v1 API plus the current EP-06 UI endpoints. EP-05 added real owner-configured
Drive/GitHub adapters, contained execution and output storage; an unconfigured instance still
starts disarmed and cannot admit production actions. See `DOMAIN_API.md` for current behavior.

| Method/path | Purpose |
|---|---|
| GET / | static local UI shell; no private state or bootstrap value |
| GET /assets/{name} | fixed packaged CSS/JS allowlist only; no project artifacts |
| POST /api/v1/local/session | exchange one-time owner bootstrap for HttpOnly cookie and CSRF |
| GET /api/v1/local/session | recover CSRF for an existing cookie after page reload |
| GET /api/v1/system | runner mode, epoch, exact approved policy choices and integration freshness; no tokens |
| GET /api/v1/projects | stable project IDs and display metadata |
| GET /api/v1/projects/{id} | locally recorded project state and metadata |
| GET /api/v1/projects/{id}/workspaces | locally recorded branch, source generation and workspace state |
| GET /api/v1/projects/{id}/workspaces/{wid}/source | current checked Git HEAD, branch and dirty state beside recorded source identity |
| GET /api/v1/projects/{id}/workspaces/{wid}/connections | separate GitHub `ls-remote` observation, live workspace-snapshot staleness, and durable project-scoped FETCH freshness |
| GET /api/v1/projects/{id}/workspaces/{wid}/drive-docs | on-demand exact registered Docs IDs and links, revalidated by provider reads |
| POST /api/v1/local/check-drive | explicit read of existing root and unchanged Installation Index |
| POST /api/v1/local/check-github | explicit authenticated API identity read; PR rights remain unprobed |
| POST /api/v1/local/check-git-remote | selected workspace's exact GitHub `ls-remote` head; **not** a fetch |
| POST /api/v1/operations | same canonical command as Drive; immediate admission or refusal |
| GET /api/v1/operations/{uuid} | frozen intent, actual steps, results and external effects |
| GET /api/v1/operations | bounded cursor pagination; no queued work |
| GET /api/v1/runs/{uuid} | immutable run-manifest, parser/collection findings |
| GET /api/v1/runs/{uuid}/retention | current owner pin and output availability/tombstones |
| POST /api/v1/runs/{uuid}/retention/pin | explicit owner pin/unpin decision |
| GET /api/v1/runs/{uuid}/outputs | typed frozen output index |
| GET /api/v1/runs/{uuid}/outputs/{id} | safe bounded view/download by registered ID |
| POST /api/v1/runs/{uuid}/outputs/{id}/expire | retire one registered output after age/reference/pin gates |
| POST /api/v1/local/open-editor | project_id + workspace_id → local fixed VS Code argv, no client path |
| POST /api/v1/local/import-handles | owner-only local path grant; returns opaque handle |
| POST /api/v1/local/arm | local owner enables intake and establishes new epoch |
| POST /api/v1/local/disarm | stop admission; existing operation policy explicit |
| GET /api/v1/events | authenticated SSE with resumable event cursor; no LLM connection |

POST operations: 202 means immediately admitted and executing, **not queued**; 200 may return
an existing identical request receipt without re-execution; 409 BUSY/CONFLICT/ID_REUSED;
422 invalid schema; 403 policy rejection; 503 unavailable/unarmed. Persist refusal receipt
when request identity can safely be parsed, so polling does not cause repeated surprises.
Do not accept client-supplied operation status, output path, credential scope or raw shell.

Server derives mailbox scope from registered document ID, not merely project_id written inside.
Local UI derives scope from authenticated target selection and policy. Both call the same domain
service. Any difference between source transports must be explicit in provenance, not authority.

Use stable error codes and human Russian explanations. API IDs and enums remain English.
Durations numeric seconds; timestamps timezone-aware ISO 8601; byte sizes integers; complete
hashes in transport. App-level schema version is independent of API and database migration version.

`GET /api/v1/system` exposes Drive and GitHub API observations independently as
`UNKNOWN`, `OK`, or `ERROR`, with separate `last_checked_at` and `last_success_at`.
Failures retain the last successful observation and a sanitized code. Git remote
knowledge is scoped to one project/workspace, and its response carries
`workspace_snapshot_current`. The same connection response has a separate
`git_fetch` projection for the selected project, sourced only from a completed
`FETCH` operation with a confirmed local tracking-ref effect. Its observation
records the exact repository ID, fixed refspec, last successful fetch time and
tracking-ref digest/count. A later failed fetch reports `ERROR` while retaining
the prior successful time. The Drive/API/remote-head observations reset to
`UNKNOWN` after restart; the confirmed FETCH history is durable. Neither a
service heartbeat nor configured credentials counts as a live check or fetch.

Page result sample:
`{items: [...], next_cursor: null}`. SSE event sample:
`{event_id, request_id, step_id, timestamp, type, detail}`. Events are optional views; missed
SSE must not lose operation truth stored in SQLite. Redact event payloads as rigorously as logs.

Local retention is explicit: GET `/retention` exposes the latest owner PIN/UNPIN
and an output index with `AVAILABLE`/`EXPIRED` state and tombstone metadata. Pin
mutation requires `{ "pinned": boolean }`; per-output expire requires `{}` and enforces
the existing 14-day minimum, completed execution/delivery, no live references or
uncertain effects, and no owner pin. Refusal has a stable error code, including
`RETENTION_NOT_DUE`, `EVIDENCE_REFERENCED`, or `EVIDENCE_PINNED`. A successful
expiration commits an append-only tombstone before verified private-file unlink;
immutable run manifest and remaining outputs stay available. UI confirmation must
name the exact run and output.

Large outputs stream with bounded ranges. Never load a whole multi-GB file into an API JSON body.
Escape returned content in frontend. Mutations require session + CSRF token + validated Origin/Host. The unauthenticated
local shell and fixed assets contain no bootstrap or private state. Their CSP permits only
same-origin scripts, styles and API connections; project output bytes are always attachments
under the authenticated API, never executable same-origin UI assets.
