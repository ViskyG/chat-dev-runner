# GP-03 fixed Python rootless v2 bridge

Base: root `a385671`, plus unchanged source resolver `f995e54` cherry-picked as
`cb7f287` in this isolated worktree. The rootless change owns only
`runner_app/executors/rootless_v2.py`, `tests/test_rootless_v2.py` and this evidence.

## API and scope

`RootlessTaskBackend(profile_dir, runs_root, *, require_running, record_event)`
loads the existing owner-only `python-quick-grant.json` through
`load_python_quick_grant`. `profile("python-quick")` returns the exact approved
profile, allowing the integrator to compare its argv, image, platform and output
collection with a project task before invoking it. The run API is:

```
backend.run(snapshot, request_id=request_id, profile_id="python-quick", cancel=event)
```

`RootlessTaskResult` retains execution outcome/exit, verified cleanup, frozen
output/log paths, capture completeness, duration, exact profile argv/image and
execution OS/architecture, provenance and an optional error code.
`RootlessError.code` is safe to report and `.cleanup_verified=False` means keep
the execution lane until reconciliation. Failed preparation returns TEST
`NOT_RUN` with the preparation result and its preserved raw logs.

There is no dynamic task/argv substitution or generic container capability.
The existing Python preparation and test profile argv are passed unchanged.
No v1 operation, command or approval is synthesized: `_PhaseJournal` implements
the coordinator's protocol using v2 callbacks. `require_running(request_id)`
must establish that this exact v2 request currently owns the execution lane.
`record_event(request_id, event, detail)` must commit durably before returning;
exceptions fail closed. It records phase reservations, container intent before
creation, exact cleanup proof, preparation result/handoff, quota allocation,
sealed evidence and exact-volume retirement. Callbacks should not log their
raw provider diagnostics.

The backend stores a durable one-shot request marker under
`<runs_root>/rootless-v2/requests/<request_id>`. Execution and persistent evidence
have separate managed subdirectories. `SourceSnapshot.path` stays read-only.
The actual broker independently rechecks source bytes, daemon/rootlessness,
pinned image/profile, mounts, preparation handoff and hard quotas.

`_V2Volumes` replaces the legacy admission lookup with the callback and adds
allocation journaling. It reuses the existing fixed root-owned helper,
allocation response checks, mount/quota verification and no-follow cleanup
without weakening them. The backend adds no helper installation, arbitrary
mount, elevation, network or credential capability. Completed volumes are
retired only after removed-container proofs and copied/hashed capture/output
evidence. Incomplete evidence or uncertain cleanup retains the mounts and
reports unverified cleanup; request replay is refused. Frozen output names
follow the existing collector's output-ID naming (`junit.xml`, optional
`app-log.jsonl`), with hashed descriptors in `backend.json`.

## Executed evidence

```
/home/visky/Work/GPTRunner/chat-dev-runner/.venv/bin/python -m pytest tests/test_rootless_v2.py -q --junitxml=verification/implementation/GP-03/rootless-v2/tests.xml
/home/visky/Work/GPTRunner/chat-dev-runner/.venv/bin/python -m ruff check runner_app/executors/rootless_v2.py tests/test_rootless_v2.py
/home/visky/Work/GPTRunner/chat-dev-runner/.venv/bin/python -m mypy --follow-imports=silent runner_app/executors/rootless_v2.py
```

**12 passed**, Ruff passed, strict mypy passed. Tests use disposable real Git
source capture and private installed-profile receipts; exercise unchanged
preparation planning/coordinator, byte-attested build handoff, capture/output
freezing and no-follow volume clearing. Only Docker, actual tmpfs/mount
verification and the root-helper OS boundary are replaced for deterministic
integration. This is not live Docker/quota qualification.

The tests establish successful two-phase fixed-argv execution flow; failed
tests vs failed preparation/TEST NOT_RUN; cleanup proof and journal failure;
wrong broker identity; release refusal; incomplete capture/output evidence;
repeated admission before test; exact request/profile scope; cancellation
before allocation; missing quota prerequisite; one-shot replay refusal; and
frozen evidence before any exact volume release. Post-allocation admission
failure was tightened to retain its slots/lane; the final focused run passed.
No unchanged source tests or broad regressions were rerun.

A read-only local check observed a rootless socket and quota base, but
`HardQuotaVolumeProvider._installed_helper()` returned false. Live v2 canary
is **NOT_RUN / VOLUME_HELPER_UNAVAILABLE**, as recorded in
`live-prerequisites.json`. No privileged helper/daemon execution, mount,
installation, provider operation or installed-service change was attempted.
The new v2 route remains **IMPLEMENTED_NOT_QUALIFIED** pending that gate and a
real owner-approved local canary; historical v1 evidence is unchanged.
