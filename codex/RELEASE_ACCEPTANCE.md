# Release acceptance — not yet executed on the owner's host

Every box starts unchecked. Record test IDs and evidence paths next to completed checks.

## Product
- [ ] New project is created locally, on private GitHub and in Drive through one operation.
- [ ] Existing GitHub and local imports preserve original repositories.
- [ ] Two projects and two independent workspaces coexist; only selected workspace changes.
- [ ] Clean branch retarget works; dirty, busy and duplicate-checkout cases refuse.
- [ ] Change delivery checks base SHA + individual file hashes; no whole-tree blind overwrite.
- [ ] Verification uses immutable source + recorded environment; publication uses that same source.
- [ ] Commit/push/draft PR and report delivery are automatic within the configured policy.
- [ ] No queue, no self-started next command, no LLM/browser automation.
- [ ] Project test failure is displayed independently from runner/infrastructure faults.
- [ ] Console/log/test/coverage/report/image/binary evidence has typed manifests and completeness.
- [ ] UI editor menu resolves exact workspaces and does not execute remote shell text.

## Safety and reliability
- [ ] UI is loopback-only; no inbound public listener; origin/host/session/CSRF checks tested.
- [ ] Project execution cannot read tokens, host home, sockets or adjacent workspace data.
- [ ] Time, PID, memory, CPU, filesystem and log budgets actually enforced.
- [ ] No arbitrary host path, submodule/filter/hook execution, workflow-policy escalation or force push.
- [ ] Crash recovery reconciles effects; duplicates and BUSY requests never silently execute later.
- [ ] Cancellation works with all execution slots occupied.
- [ ] Upload outage leaves recoverable local evidence and explicit delivery status.
- [ ] Large/malformed/malicious artifacts and secrets redaction paths tested.
- [ ] SQLite and immutable evidence backup/restore, retention and uninstall tested.

## Integrations
- [ ] OAuth scope and app-owned file visibility verified with a real cross-app canary.
- [ ] ChatGPT repository access accurately marked verified/manual/unknown.
- [ ] Account supports private draft PRs or owner-approved explicit alternative recorded.
- [ ] No secrets, repository visibility changes or expanded scopes without explicit authority.
- [ ] Disposable cloud resources/effects listed; retention/cleanup agreed, not silently deleted.

## User experience and handoff
- [ ] Dark/light, keyboard, reduced-motion and target desktop viewport screenshots reviewed.
- [ ] Real UI contains no fake project status, run count, test result or progress.
- [ ] Every control has an actual behavior or specific disabled explanation.
- [ ] Russian owner guide + English Codex/engineering documents agree with final implementation.
- [ ] Locked install commands and service configuration tested on target host.
- [ ] All 14 walkthroughs have evidence; STATUS.json and release notes describe actual capabilities.
