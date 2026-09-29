# EP-07 owner-local retarget continuation — isolated source checkpoint

Date: 2026-09-29. Branch: `ep07/retarget-recovery`, based on
`2d32168f4b3b839bbe06422db4672c5e904dadca`. This is source and
disposable real-Git/fake-Drive evidence only. The installed 0.2.6 service,
existing Drive installation, GitHub, and owner projects were untouched.

The new schema-v1 `RESUME_RETARGET` command is available only to the
authenticated `local-owner` origin. It names the original retarget UUID and
the same project/workspace, with `expected_head: null` and
`params: {"resume_version":"1.0","target_request_id":"<UUID>"}`. It can
run while command intake is disarmed, because the original uncertain
retarget retains its `repo:<project>` lock and blocks normal repo admission.
A second running attempt for the workspace receives immediate terminal
`BUSY`; there is no queue. A reused request ID returns its original
receipt without another attempt.

Eligibility is deliberately narrow. First, owner-local read-only
`RECONCILE` must confirm the original Git effect and leave the original
lock with `RETARGET_STATE_INCOMPLETE`. Continuation then requires the
exact frozen original command/digest, its original retained lock, a clean
registered worktree at the commanded target branch/head and generation,
both exact registered Drive Docs still at the old association, and **no
original Drive state effects**. The continuation never switches Git,
retries an uncertain original Docs write, or changes the original
terminal receipt.

The actor claims the original operation and journals both Drive state
intents on it in one SQLite transaction before the first Docs CAS. Each
write uses its registered file ID and observed revision; exact text/hash
readback confirms the effect. A fresh Git and two-Doc readback precedes
release of the original repository lock. A claim is one-shot. If the
process stops after it, the original intents remain for explicit read-only
reconciliation; a new continuation cannot silently repeat the write.

Crash cuts and manual boundary, proved in disposable fixtures:

- Before the claim, dirty Git or stale Drive state refuses without a write
  or claim. An interrupted actor with no effects can be cleared by
  owner-local RECONCILE without releasing the original lock.
- After the atomic claim or a partial Docs write, the original repo lock
  stays held. A second continuation is rejected; original intent
  readback through RECONCILE can release it only when both desired hashes
  are present. A deliberately simulated partial second write needed a
  separate exact manual Doc repair before read-only RECONCILE released it.
- After both Docs read back but before local release, owner-local
  RECONCILE can independently verify the original effects and release.
  After release but before the actor terminal receipt, RECONCILE clears
  only the empty actor reservation.
- If the original retarget already has INTENDED/UNKNOWN Drive state
  effects whose desired bytes are missing, RESUME_RETARGET remains closed.
  The owner must explicitly repair the exact registered Doc to its
  original intent's desired hash, then run read-only RECONCILE; if the
  intended bytes or Git/registry transition cannot be proven, no safe
  automatic unlock exists. Google Docs provides no transaction spanning
  the two Docs, so concurrent external writers must coordinate during
  repair and validation.

Verification: `retarget-recovery-baseline-20260929.xml` recorded 28
passing baseline retarget/reconcile tests before implementation.
`retarget-recovery-focused-20260929.xml` records 126 passing tests
across the new continuation, old retarget, read-only reconciliation,
command schema and runtime assembly. The new tests use real disposable
host Git, fake exact-ID Drive Docs and a local SQLite ledger. Ruff,
mypy on 99 source files, JavaScript syntax and `git diff --check`
passed. No live authorization, command, release upgrade, or provider
write was performed.

Integration note: `runner_app/engine/integrations.py` adds the
`RESUME_RETARGET` handler next to `RETARGET_WORKSPACE`; this source
slice may overlap concurrent EXPORT_CONTEXT integration. Workspace
terminal RECEIPT action guards were not changed here.
