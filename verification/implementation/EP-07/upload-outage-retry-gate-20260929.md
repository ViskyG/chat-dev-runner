# EP-07 upload-outage and delivery-retry gate, 2026-09-29

Source checkpoint: `8d2e4c39e99c47dd5b8cde37a80fc43a57cf8ca5`, isolated
`ep07/delivery-outage-gate` worktree. The installed owner-user service, its
SQLite database, Drive documents, command mailboxes and GitHub repositories
were not changed by this probe.

## Evidence and result

The already accepted installed full routes are
`live-publish-primary-20260928.jsonl` (request
`d4e83e2e-cb8f-4768-84bf-13784aa9072f`, PASSED, private draft PR #1,
Drive report read back) and `live-publish-parallel-20260928.jsonl` (request
`cbc20739-bfe2-451e-9440-07118e08105a`, FAILED project test, private
draft PR #2, Drive report read back). Those files prove normal provider
delivery; neither contains an upload outage. They were read only and were not
repeated for this gate.

The focused regression
`test_drive_provider_uncertainty_preserves_confirmed_git_and_one_run`
now exercises a response-loss outage in the full
`DELIVER_VERIFY_PUBLISH` route using real disposable Git, SQLite ledger,
immutable local run evidence and bundle preparation, with fake execution and
provider ports. The original request ends `BLOCKED` with
`DELIVERY_OUTCOME_UNKNOWN`, a durable `PENDING` delivery status, a
`DRAFT_PUBLISHED` publication axis, and a saved `run-manifest.json` plus
`bundle-index.json`. A distinct admitted `PUBLISH_REPORT` request then
loads that exact run and original operation manifest and ends `COMPLETED`
with `DELIVERED` on its own operation. Assertions verify identical run
manifest bytes across the two local bundles, one broker execution, one draft
PR POST, no PR update, the same PR URL, and an unchanged remote branch commit.
The retry returns a delivery status; it does not attach the newly
delivered report link to the already created PR.

The existing adapter tests
`test_ambiguous_upload_leaves_intended_effect_and_same_request_never_replays`
and
`test_reconciled_partial_upload_is_reused_by_explicit_new_report`
add the provider boundary: a fake Google upload writes the report then loses
its response, leaving an `INTENDED` effect and explicit
`PARTIAL/UPLOAD_WRITE_UNCERTAIN`; replaying the same request does not
upload again. After trusted reconciliation, a new report request reuses the
confirmed asset without executing a run. The full-route retry regression
uses a simpler fake publisher, so the three focused cases together establish
the local workflow and adapter invariants, not an actual Google outage.

Focused command:

```text
PYTHONPATH="$PWD" /home/visky/Work/GPTRunner/chat-dev-runner/.venv/bin/python -m pytest -q tests/test_deliver_verify_publish_workflow.py::test_drive_provider_uncertainty_preserves_confirmed_git_and_one_run tests/test_drive_publish.py::test_ambiguous_upload_leaves_intended_effect_and_same_request_never_replays tests/test_drive_publish.py::test_reconciled_partial_upload_is_reused_by_explicit_new_report
3 passed, 1 upstream Starlette deprecation warning in 1.22s
```

`ruff check tests/test_deliver_verify_publish_workflow.py` and
`git diff --check` passed. After adding the explicit zero-PR-update
assertion, the single changed regression passed again in 0.88s. No broad
regression was rerun.

## Release boundary

`codex/RELEASE_ACCEPTANCE.md` keeps “Upload outage leaves recoverable
local evidence and explicit delivery status” **open**. The local and
provider-fake proofs above support the implementation, but no real Google
Drive upload outage, installed-service recovery, or live
`PUBLISH_REPORT` request was performed. A forced live ambiguous upload
could leave a real remote file and uncertain effect while the service and
Drive command intake are active. The remaining release proof is one
controlled outage/reconciliation/retry on a disposable registered run at
the installed boundary, with independent readback of local evidence,
delivery status, and unchanged GitHub PR count.
