# RESUME_PROVISION recovery status and remaining design

Scope: EP-05 partial CREATE_PROJECT recovery. No live provider call or write was made.
The versioned owner-local admission, original-target checks, repo/global locks, and
atomic Ledger effect adoption are implemented. `runner_app/workflows/provision_resume.py`
remains a read-only preview; no executing resume coordinator is registered.

## Required sequence

1. A local owner submits a fresh `RESUME_PROVISION` request with `target_request_id` and
   the same project ID. The target must be a terminal partial `CREATE_PROJECT`, never
   a completed/rejected or active operation. Its command digest, source kind,
   template, identity alias, and project ID are read from the original Ledger row.
   The new request never supplies replacement creation parameters.
2. Resolve any old `INTENDED`/`UNKNOWN` effect with the read-only `RECONCILE` route.
   A negative provider search is not proof that an ambiguous create did not land.
   Release retained old locks only after exact positive resource proof or an
   explicit owner resolution. Then admit the new request under `repo:<project>`,
   `global:<identity-alias>`, and `global:drive-installation` locks.
3. Under those locks, inspect each old **confirmed** effect by exact provider ID,
   original intent, and current local/remote state. GitHub requires numeric repo
   and owner IDs, private flag, clone URL, original `cdr:v1` marker and exact
   baseline ref/head; the managed bare repo and worktree require expected path,
   origin kind/reference, branch/head, clean state, and exact origin URL. Drive
   requires the existing installation attachment plus each recorded file ID,
   parent, type, scoped appProperties, and, for written docs, exact content hash.
   A path/title/name collision without old confirmed effect is a conflict.
4. Use the implemented atomic Ledger adoption primitive to copy each proven old effect
   identity into the new request with a durable `source_effect_id` link, exact
   intent/provider ID and proof timestamp. It must verify target relationship,
   RUNNING status and current locks in the same SQLite transaction. A crash in
   adoption remains reconcilable; no adapter may interpret an unproved new
   `INTENDED` effect as permission to replay the provider write.
5. The resume coordinator uses the **old** frozen creation parameters, original
   request ID for the GitHub marker, and original `received_at` for deterministic
   Drive project/context content. It calls adapters only for truly missing steps;
   adopted steps take exact-ID readback paths. SourceService and DriveProjectProvisioner
   need narrow `RESUME_PROVISION` effective-command support. The local origin binder
   must accept an already configured `origin` only after exact URL/readback.
6. For a Drive-originated original request, root receipt recovery is separate.
   Current `RootCreateProjectReceiptPublisher.publish` requires the original request
   to be RUNNING and the root COMMAND to remain that exact READY command. A new
   resume operation cannot call it. Add a resume-specific CAS method that journals
   under the new request but emits the original request ID/digest only while the
   root COMMAND is still exact; otherwise return `ROOT_COMMAND_MISMATCH` and retain
   the truthful partial result. Never edit COMMAND or overwrite an unrelated receipt.

## Remaining blockers to an executing coordinator

- The trusted read-only provider/local verifiers have not yet been connected to
  `Ledger.adopt_confirmed_effect`; Ledger checks proof equality with the old
  confirmed intent/provider ID but cannot itself inspect a cloud resource.
- `SourceService._command` and `DriveProjectProvisioner._preflight/populate` require
  `CREATE_PROJECT` under the same request ID. `create_initial_workspace_source`
  also requires the current request's confirmed `local-project` effect.
- `DriveProjectProvisioner._file` refuses an existing keyed file without a same-request
  effect. Current `populate` uses current request `received_at`, changing content hashes.
- `_configure_origin` refuses any existing remote, including an exact confirmed one.
- The root receipt writer cannot complete a terminal original CREATE_PROJECT from a
  fresh request, even when all resources are present.

Until the remaining coordinator/adapter work and fake lost-ack/crash tests pass, a partial create
must remain `PROVISIONING_PARTIAL`/reconciliation required. Re-running normal
`CREATE_PROJECT` or provisioning calls with a fresh request is unsafe and can
recreate or ambiguously adopt resources.
