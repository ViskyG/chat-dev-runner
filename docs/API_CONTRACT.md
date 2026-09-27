---
id: CDR-API
status: partially-implemented-EP-01
---
# Local API outline

One loopback origin, authenticated local session. GET never mutates. Paths below are the
target v1 contract. EP-01 implements the authenticated system, operations, project/workspace
read views, run/output index views, local arm/disarm, session exchange and finite SSE batch.
The output-byte route returns `OUTPUT_STORAGE_UNAVAILABLE` until EP-03. Local editor/import
handlers and live adapters remain later-packet work; the default server cannot arm without a
production handler. See `DOMAIN_API.md` for the tested current behavior.

| Method/path | Purpose |
|---|---|
| GET /api/v1/system | runner health, mode, arming epoch, integration freshness; no tokens |
| GET /api/v1/projects | stable project IDs and display metadata |
| GET /api/v1/projects/{id} | repo/Drive associations, capabilities and provisioning state |
| GET /api/v1/projects/{id}/workspaces | branch, source generation and last run per area |
| POST /api/v1/operations | same canonical command as Drive; immediate admission or refusal |
| GET /api/v1/operations/{uuid} | frozen intent, actual steps, results and external effects |
| GET /api/v1/operations | bounded cursor pagination; filters; no queued work |
| GET /api/v1/runs/{uuid} | immutable run-manifest, parser/collection findings |
| GET /api/v1/runs/{uuid}/outputs | typed frozen output index |
| GET /api/v1/runs/{uuid}/outputs/{id} | safe bounded view/download by registered ID |
| POST /api/v1/local/open-editor | workspace_id → local approved VS Code argv, no remote path |
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

Page result sample:
`{items: [...], next_cursor: null}`. SSE event sample:
`{event_id, request_id, step_id, timestamp, type, detail}`. Events are optional views; missed
SSE must not lose operation truth stored in SQLite. Redact event payloads as rigorously as logs.

Large outputs stream with bounded ranges. Never load a whole multi-GB file into an API JSON body.
Escape returned content in frontend. Mutations require session + CSRF token + validated Origin/Host.
