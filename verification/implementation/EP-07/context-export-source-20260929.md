# EP-07 source gate: local-only EXPORT_CONTEXT

The configured source Runtime now accepts a finite `EXPORT_CONTEXT` operation for an exact
registered workspace. Its existing `ContextExporter` reads a pinned committed Git tree,
enforces managed path/object/hash limits, observes dirty working files without reading them,
and refuses `include_diff=true` until a committed comparison basis is defined. The complete
bounded index and selected excerpts are kept in the private local operation result. The
owner-local UI labels this as a local export and exposes the result through its authenticated
operation JSON download, with a review warning.

An existing workspace Drive COMMAND can request the operation, but its registered RECEIPT
contains only the committed terminal receipt identity, digest, status and code. It does not
contain file inventory or excerpts, and this route does not write `SOURCE CONTEXT`. In a fake
Drive test, the exact mailbox callback made one receipt CAS and confirmed its effect; the
project state, source context and both workspace Git identities were unchanged. This means
chat can observe completion but cannot yet read the exported source through Drive.

Focused verification: 35 tests passed across `test_drive_context.py`,
`test_retarget_route.py`, `test_workspace_mailboxes.py` and `test_runtime_integrations.py`.
Ruff passed on touched Python files, mypy on the new route, Node syntax checks on touched
JavaScript, and `git diff --check` passed. No installed service, real Drive/GitHub resource,
credential or owner workspace was touched.

The installed `0.2.6` service has no such route. Before advertising it in live Drive SYSTEM
STATE, update the installed `CHAT PROTOCOL` through a versioned mapping: its seed/reference
still says optional diff is available. Reviewed publication of source excerpts or index to
Drive needs its own privacy and writer-coordination contract with retarget, plus live proof.
PUBLISH_REPORT and DELIVER_VERIFY_PUBLISH workspace receipt behavior is separate.
