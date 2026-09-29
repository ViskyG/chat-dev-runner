# EP-07 clean workspace retarget route — isolated source checkpoint

Date: 2026-09-29. Branch: `ep07/retarget-handler` in
`chat-dev-runner-ep07-retarget`, based on `c590fc2`. This is a source
implementation and offline capability-boundary proof, not an installed release
or a live Google/GitHub acceptance. The installed service, owner workspaces,
Drive installation and GitHub were not changed.

The finite `RETARGET_WORKSPACE` handler is wired into runtime integrations and
the existing authenticated local operation API. Exact registered workspace
mailboxes can poll it independently of the rootless execution route. The UI
capability guard therefore enables the existing retarget form only when the
route is advertised. `DELIVER_VERIFY_PUBLISH` keeps its separate route and
delivery-report receipt format. A Drive-origin retarget writes the exact terminal
SQLite receipt to its registered workspace `RECEIPT` Doc, including routine
refusals, after frozen/current READY identity and Docs CAS readback checks.
Receipt publication has one durable terminal intent; a lost or ambiguous write
is not replayed blindly.

An admitted retarget requires an active registered project/workspace, its
repository lock, exact expected branch/head/generation, a clean managed
worktree, an existing target branch at the specified commit and no competing
worktree occupant. It changes only that worktree and registry generation.
Before Git mutation it reads the exact registered `PROJECT STATE` and
`SOURCE CONTEXT` Docs. After mutation it CAS-updates the workspace association
in both, retaining other workspace entries and later context text. Both Drive
write intents precede the first write. The Git effect remains open until both
Docs have exact readback, so a crash between Git and Drive cannot release the
repository lock as an ordinary handler fault.

Owner-local `RECONCILE` can read back the exact Git branch/head/generation and
registered Drive document hashes. It releases a retained retarget lock only
when Git remained unchanged with no Drive effects, or when the Git transition
and both Drive state effects are confirmed. It does not replay a Git switch or
Docs write.

Verification, using editable checkout source via
`PYTHONPATH="$PWD:$PWD/tests"` and the shared pinned project virtualenv:

- `retarget-focused-20260929.xml`: 150 passed across retarget route, runtime
  integration, mailbox, receipt, Git, reconciliation, Drive polling/provision
  and local API tests. The retarget tests use real disposable host Git, fake
  exact-ID Drive Docs, and the authenticated loopback API.
- `retarget-manager-final-20260929.xml`: 19 passed after requiring a terminal
  publisher for Drive retarget mailbox activation.
- `mypy runner_app --no-incremental`: 97 source files checked, no issues.
  Ruff on source and changed tests, `node --check` on the changed UI module,
  and `git diff --check` passed.

**Separate release gate:** an actual partial Drive association write, or a
Git switch that completed before registry transition, can have no safe
automatic continuation. The original operation remains terminal
`NEEDS_RECONCILIATION` with its repository lock held. Read-only
`RECONCILE` reports the exact unresolved effect or
`RETARGET_STATE_INCOMPLETE`; a finite owner-approved repair/continuation and
live installed validation are still required before claiming full EP-07
retarget recovery acceptance. No live Drive command or canary was issued in
this branch.
