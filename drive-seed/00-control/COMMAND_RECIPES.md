# Command recipes — inert examples

All commands are DRAFT, use a zero epoch and fixture timestamps. Regenerate identity/time/commit fields from live state; never simply change DRAFT to READY.

## CREATE_PROJECT

```json
{
  "schema_version": "1.0",
  "request_id": "00000000-0000-0000-0000-000000000001",
  "dispatch": "DRAFT",
  "runner_id": "local-runner",
  "epoch": "00000000-0000-0000-0000-000000000000",
  "issued_at": "2026-09-26T00:00:00Z",
  "expires_at": "2026-09-26T00:15:00Z",
  "action": "CREATE_PROJECT",
  "project_id": "example-lab",
  "workspace_id": null,
  "expected_head": null,
  "params": {
    "name": "Example laboratory",
    "source": {
      "kind": "new"
    },
    "template_id": "python-basic",
    "github_identity": "personal",
    "visibility": "private"
  },
  "intent": "DRAFT EXAMPLE ONLY: CREATE_PROJECT. Not an authorized live command."
}
```

## CREATE_WORKSPACE

```json
{
  "schema_version": "1.0",
  "request_id": "00000000-0000-0000-0000-000000000002",
  "dispatch": "DRAFT",
  "runner_id": "local-runner",
  "epoch": "00000000-0000-0000-0000-000000000000",
  "issued_at": "2026-09-26T00:00:00Z",
  "expires_at": "2026-09-26T00:15:00Z",
  "action": "CREATE_WORKSPACE",
  "project_id": "example-lab",
  "workspace_id": "experiment",
  "expected_head": "1111111111111111111111111111111111111111",
  "params": {
    "name": "Second experiment",
    "branch": "feature/second-experiment",
    "base_commit": "1111111111111111111111111111111111111111",
    "profile_id": "python-quick"
  },
  "intent": "DRAFT EXAMPLE ONLY: CREATE_WORKSPACE. Not an authorized live command."
}
```

## RETARGET_WORKSPACE

```json
{
  "schema_version": "1.0",
  "request_id": "00000000-0000-0000-0000-000000000003",
  "dispatch": "DRAFT",
  "runner_id": "local-runner",
  "epoch": "00000000-0000-0000-0000-000000000000",
  "issued_at": "2026-09-26T00:00:00Z",
  "expires_at": "2026-09-26T00:15:00Z",
  "action": "RETARGET_WORKSPACE",
  "project_id": "example-lab",
  "workspace_id": "experiment",
  "expected_head": "1111111111111111111111111111111111111111",
  "params": {
    "expected_branch": "feature/experiment",
    "target_branch": "feature/second-experiment",
    "target_commit": "2222222222222222222222222222222222222222"
  },
  "intent": "DRAFT EXAMPLE ONLY: RETARGET_WORKSPACE. Not an authorized live command."
}
```

## DELIVER_VERIFY_PUBLISH

```json
{
  "schema_version": "1.0",
  "request_id": "00000000-0000-0000-0000-000000000004",
  "dispatch": "DRAFT",
  "runner_id": "local-runner",
  "epoch": "00000000-0000-0000-0000-000000000000",
  "issued_at": "2026-09-26T00:00:00Z",
  "expires_at": "2026-09-26T00:15:00Z",
  "action": "DELIVER_VERIFY_PUBLISH",
  "project_id": "example-lab",
  "workspace_id": "experiment",
  "expected_head": "1111111111111111111111111111111111111111",
  "params": {
    "changeset_doc_id": "LOCAL_OAUTH_CREATED_DOC_ID",
    "changeset_revision": "CURRENT_DOCS_REVISION",
    "profile_id": "python-quick",
    "publication": "draft_always"
  },
  "intent": "DRAFT EXAMPLE ONLY: DELIVER_VERIFY_PUBLISH. Not an authorized live command."
}
```

## VERIFY

```json
{
  "schema_version": "1.0",
  "request_id": "00000000-0000-0000-0000-000000000005",
  "dispatch": "DRAFT",
  "runner_id": "local-runner",
  "epoch": "00000000-0000-0000-0000-000000000000",
  "issued_at": "2026-09-26T00:00:00Z",
  "expires_at": "2026-09-26T00:15:00Z",
  "action": "VERIFY",
  "project_id": "example-lab",
  "workspace_id": "experiment",
  "expected_head": "1111111111111111111111111111111111111111",
  "params": {
    "profile_id": "python-quick"
  },
  "intent": "DRAFT EXAMPLE ONLY: VERIFY. Not an authorized live command."
}
```

## PUBLISH

```json
{
  "schema_version": "1.0",
  "request_id": "00000000-0000-0000-0000-000000000006",
  "dispatch": "DRAFT",
  "runner_id": "local-runner",
  "epoch": "00000000-0000-0000-0000-000000000000",
  "issued_at": "2026-09-26T00:00:00Z",
  "expires_at": "2026-09-26T00:15:00Z",
  "action": "PUBLISH",
  "project_id": "example-lab",
  "workspace_id": "experiment",
  "expected_head": "1111111111111111111111111111111111111111",
  "params": {
    "source_run_id": "11111111-1111-4111-8111-111111111111",
    "publication": "draft_always"
  },
  "intent": "DRAFT EXAMPLE ONLY: PUBLISH. Not an authorized live command."
}
```

## FETCH

```json
{
  "schema_version": "1.0",
  "request_id": "00000000-0000-0000-0000-000000000007",
  "dispatch": "DRAFT",
  "runner_id": "local-runner",
  "epoch": "00000000-0000-0000-0000-000000000000",
  "issued_at": "2026-09-26T00:00:00Z",
  "expires_at": "2026-09-26T00:15:00Z",
  "action": "FETCH",
  "project_id": "example-lab",
  "workspace_id": null,
  "expected_head": null,
  "params": {},
  "intent": "DRAFT EXAMPLE ONLY: FETCH. Not an authorized live command."
}
```

## SYNC_WORKSPACE

```json
{
  "schema_version": "1.0",
  "request_id": "00000000-0000-0000-0000-000000000008",
  "dispatch": "DRAFT",
  "runner_id": "local-runner",
  "epoch": "00000000-0000-0000-0000-000000000000",
  "issued_at": "2026-09-26T00:00:00Z",
  "expires_at": "2026-09-26T00:15:00Z",
  "action": "SYNC_WORKSPACE",
  "project_id": "example-lab",
  "workspace_id": "experiment",
  "expected_head": "1111111111111111111111111111111111111111",
  "params": {
    "mode": "remote_ff",
    "remote_ref": "origin/feature/experiment",
    "target_commit": "2222222222222222222222222222222222222222"
  },
  "intent": "DRAFT EXAMPLE ONLY: SYNC_WORKSPACE. Not an authorized live command."
}
```

## EXPORT_CONTEXT

```json
{
  "schema_version": "1.0",
  "request_id": "00000000-0000-0000-0000-000000000009",
  "dispatch": "DRAFT",
  "runner_id": "local-runner",
  "epoch": "00000000-0000-0000-0000-000000000000",
  "issued_at": "2026-09-26T00:00:00Z",
  "expires_at": "2026-09-26T00:15:00Z",
  "action": "EXPORT_CONTEXT",
  "project_id": "example-lab",
  "workspace_id": "experiment",
  "expected_head": "1111111111111111111111111111111111111111",
  "params": {
    "paths": [
      "src",
      "tests"
    ],
    "include_diff": true
  },
  "intent": "DRAFT EXAMPLE ONLY: EXPORT_CONTEXT. Not an authorized live command."
}
```

## ARCHIVE_WORKSPACE

```json
{
  "schema_version": "1.0",
  "request_id": "00000000-0000-0000-0000-00000000000a",
  "dispatch": "DRAFT",
  "runner_id": "local-runner",
  "epoch": "00000000-0000-0000-0000-000000000000",
  "issued_at": "2026-09-26T00:00:00Z",
  "expires_at": "2026-09-26T00:15:00Z",
  "action": "ARCHIVE_WORKSPACE",
  "project_id": "example-lab",
  "workspace_id": "experiment",
  "expected_head": "1111111111111111111111111111111111111111",
  "params": {
    "expected_branch": "feature/experiment",
    "delete_local_clean": false,
    "delete_remote_merged": false
  },
  "intent": "DRAFT EXAMPLE ONLY: ARCHIVE_WORKSPACE. Not an authorized live command."
}
```

## PUBLISH_REPORT

```json
{
  "schema_version": "1.0",
  "request_id": "00000000-0000-0000-0000-00000000000b",
  "dispatch": "DRAFT",
  "runner_id": "local-runner",
  "epoch": "00000000-0000-0000-0000-000000000000",
  "issued_at": "2026-09-26T00:00:00Z",
  "expires_at": "2026-09-26T00:15:00Z",
  "action": "PUBLISH_REPORT",
  "project_id": "example-lab",
  "workspace_id": "experiment",
  "expected_head": "1111111111111111111111111111111111111111",
  "params": {
    "source_run_id": "11111111-1111-4111-8111-111111111111"
  },
  "intent": "DRAFT EXAMPLE ONLY: PUBLISH_REPORT. Not an authorized live command."
}
```

## CANCEL_OPERATION

```json
{
  "schema_version": "1.0",
  "request_id": "00000000-0000-0000-0000-00000000000c",
  "dispatch": "DRAFT",
  "runner_id": "local-runner",
  "epoch": "00000000-0000-0000-0000-000000000000",
  "issued_at": "2026-09-26T00:00:00Z",
  "expires_at": "2026-09-26T00:15:00Z",
  "action": "CANCEL_OPERATION",
  "project_id": "example-lab",
  "workspace_id": "experiment",
  "expected_head": "1111111111111111111111111111111111111111",
  "params": {
    "target_request_id": "11111111-1111-4111-8111-111111111111"
  },
  "intent": "DRAFT EXAMPLE ONLY: CANCEL_OPERATION. Not an authorized live command."
}
```
