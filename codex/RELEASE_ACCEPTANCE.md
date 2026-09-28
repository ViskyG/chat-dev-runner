# EP-07 release acceptance — not yet completed on the owner's host

Check a box only for its evidenced **EP-07 release gate** criterion. EP-03/05/06
predecessor evidence exists, but a fake browser scenario or transient live smoke does not
sign off a durable host installation. Record test IDs, evidence paths, environment and
reviewer/date next to each completed check. The installed configured `0.2.5`
service arms by default after clean startup, but the full release matrix is not
yet signed off.

Predecessor evidence: `codex/checkpoints/EP-03.md` (rootless host acceptance),
`codex/checkpoints/EP-04.md` (exact existing-Drive attachment, full-scope OAuth and
cross-app canary), `codex/checkpoints/EP-05.md` (disposable private project runs and
publication), and `codex/checkpoints/EP-06.md` (all 14 fake-backed UI scenarios and a
real disarmed local process). EP-07 records installed-service CREATE_PROJECT, local-UI CREATE_WORKSPACE, passing/failing publication and current-chat project FETCH/RECEIPT gates below. Wheel `0.2.5` is active and armed after a successful graceful restart; its enabled user unit plus `Linger=yes` are configured for boot startup. Configured clean startup arms by default; `--start-unarmed` and `NEEDS_RECONCILIATION` keep intake off. `verification/implementation/EP-07/default-intake-live-20260928.json` records matching local/Drive epoch readback. A real power-cycle and the full 14-scenario release matrix remain open.
The `0.2.5` CREATE_PROJECT performance update passed 1361 source tests with one expected skip, Ruff and mypy, and was installed after a verified 463-file private backup. Exact existing Drive attachment and matching armed SYSTEM STATE epoch passed; no extra project was created, so the new live elapsed time is still unmeasured (`verification/implementation/EP-07/drive-create-performance-upgrade-0.2.5.json`).

## Product
- [x] New project is created locally, on private GitHub and in Drive through one operation. EP-07 `verification/implementation/EP-07/live-ui-acceptance-20260928T105918Z.json` and `verification/implementation/EP-07/live-drive-chat-roundtrip-20260928.json`: both CREATE_PROJECT operations completed with managed local workspace, private GitHub repository and registered Drive workspace under the exact existing installation. Environment: owner-user installed loopback service, real GitHub/Drive; reviewer: Codex, 2026-09-28.
- [ ] Existing GitHub and local imports preserve original repositories.
- [x] Two projects and two independent workspaces coexist; only selected workspace changes. EP-07 `verification/implementation/EP-07/live-ui-acceptance-20260928T105918Z.json`, `verification/implementation/EP-07/live-drive-chat-roundtrip-20260928.json` and `verification/implementation/EP-07/live-multiworkspace-readback-20260928.json` prove the project/workspace registrations. `source-identity-readonly-20260928.json` independently joins both installed publication runs to their selected branches and verifies that both editor workspaces retain the same clean base head, tree and source fingerprint after the routes. This is net visible source identity, excluding ignored files and transient edits between observations. Environment: installed owner-user state and managed Git worktrees; reviewer: Codex, 2026-09-28.
- [ ] Clean branch retarget works; dirty, busy and duplicate-checkout cases refuse.
- [ ] Change delivery checks base SHA + individual file hashes; no whole-tree blind overwrite.
- [x] Verification uses immutable source + recorded environment; publication uses that same source. EP-07 `verification/implementation/EP-07/source-identity-readonly-20260928.json` joins each live run to its immutable snapshot, verified rootless execution record, retained Git commit/tree, matching input-manifest hash and confirmed push effect. The two prior live publish journals record independent GitHub readback of the matching branch and draft PR heads. This proves the two completed routes; no new provider read was made for this audit. Environment: installed owner-user SQLite, managed Git, delivery bundles, and prior real GitHub readbacks; reviewer: Codex, 2026-09-28.
- [x] Commit/push/draft PR and report delivery are automatic within the configured policy. EP-07 `verification/implementation/EP-07/live-publish-primary-20260928.jsonl` and `verification/implementation/EP-07/live-publish-parallel-20260928.jsonl`: each single READY workspace request completed with `git_status=PUBLISHED`, a remote head on its selected branch, a private draft PR independently reconciled on GitHub, a delivered Drive report/results readback and the report link in the PR. Environment: installed wheel `0.2.1` owner-user service, rootless contained execution, real GitHub/Drive, disposable project; reviewer: Codex, 2026-09-28.
- [ ] No queue, no self-started next command, no LLM/browser automation.
- [ ] Project test failure is displayed independently from runner/infrastructure faults. The EP-07 `parallel` journal proves operation `COMPLETED` with execution `FAILED` (exit 1), `[FAILED]` draft PR and delivered Drive report; installed UI presentation of the separate axes remains to be reviewed.
- [ ] Console/log/test/coverage/report/image/binary evidence has typed manifests and completeness.
- [ ] UI editor menu resolves exact workspaces and does not execute remote shell text.

## Safety and reliability
- [x] UI is loopback-only; no inbound public listener; origin/host/session/CSRF checks tested. EP-07 `verification/implementation/EP-07/service-gate.json` (installed service binds only `127.0.0.1:8765`, nonloopback refused, unauthenticated/spoofed Host/cross-origin refused) and EP-06 `verification/implementation/EP-06/backend-shell-tests.xml` (`tests/test_domain_api.py` session and CSRF rejection cases). Environment: owner-user installed service plus local API test process; reviewer: Codex, 2026-09-28.
- [ ] Project execution cannot read tokens, host home, sockets or adjacent workspace data.
- [ ] Time, PID, memory, CPU, filesystem and log budgets actually enforced.
- [ ] No arbitrary host path, submodule/filter/hook execution, workflow-policy escalation or force push.
- [ ] Crash recovery reconciles effects; duplicates and BUSY requests never silently execute later.
- [ ] Cancellation works with all execution slots occupied.
- [ ] Upload outage leaves recoverable local evidence and explicit delivery status.
- [ ] Large/malformed/malicious artifacts and secrets redaction paths tested.
- [ ] SQLite and immutable evidence backup/restore, retention and uninstall tested.

## Integrations

Installed `0.2.2` advertised `CREATE_PROJECT`, `CREATE_WORKSPACE`, `DELIVER_VERIFY_PUBLISH` and `FETCH` in Drive SYSTEM STATE; the newer clean-start intake default does not change these action contracts. The current-chat FETCH project mailbox roundtrip passed with a matching RECEIPT; a live Drive-origin `CREATE_WORKSPACE` project command remains unverified (`verification/implementation/EP-07/live-project-fetch-20260928.json`). The full source suite after `0.2.2` was not run for this narrow change; `verification/implementation/EP-07/release-upgrade-0.2.2.json` records 49 + 19 focused passes and static checks.
- [x] Owner-approved full Drive OAuth scope, exact existing-installation attachment and real cross-app canary remain valid under the installed service. EP-04 `verification/implementation/EP-04/live-existing-installation-canary.json` records the owner-approved full Drive scope and cross-app canary. EP-07 `verification/implementation/EP-07/installed-runtime-read-after-upgrade.json` confirms exact existing root/Index attachment and Drive OK from installed `0.2.1`; `verification/implementation/EP-07/release-upgrade-0.2.2.json` and `verification/implementation/EP-07/live-project-fetch-20260928.json` confirm the then-installed `0.2.2` service retained that exact attachment and completed a current-chat project COMMAND with a matching RECEIPT. Environment: owner-user installed service and existing Drive account; reviewer: Codex, 2026-09-28.
- [ ] ChatGPT Drive and GitHub access are marked with their independently verified or owner-reported state; one fresh chat-origin command passes end to end. Current-chat root CREATE_PROJECT and installed `0.2.2` project FETCH commands each reached matching terminal RECEIPTs (`verification/implementation/EP-07/live-drive-chat-roundtrip-20260928.json`, `verification/implementation/EP-07/live-project-fetch-20260928.json`). The owner reports a separate-chat calculator CREATE_PROJECT; installed ledger and connected Drive independently confirm its matching COMPLETED root RECEIPT (`verification/implementation/EP-07/chat-origin-calculator-create-20260928.json`), while originating-session provenance is owner-reported. Owner-local GitHub confirms its repository exists and is private, but this chat GitHub connector returned 404; new private project repository access remains open.
- [x] Account supports private draft PRs or owner-approved explicit alternative recorded. EP-05 `verification/implementation/EP-05/live-workspace-pass-final-result.json`, `verification/implementation/EP-05/live-workspace-fail-fresh-result.json` and `codex/checkpoints/EP-05.md`: real private GitHub repositories each received draft PR #1, including the known-failing run. Environment: owner GitHub account, real GitHub/Drive and rootless Docker; reviewer: Codex, 2026-09-28.
- [ ] No secrets, repository visibility changes or expanded scopes without explicit authority.
- [ ] Disposable cloud resources/effects listed; retention/cleanup agreed, not silently deleted.

## User experience and handoff
- [ ] Dark/light, keyboard, reduced-motion and target desktop viewport screenshots reviewed.
- [ ] Real UI contains no fake project status, run count, test result or progress.
- [ ] Every control has an actual behavior or specific disabled explanation.
- [ ] Russian owner guide + English Codex/engineering documents agree with final implementation.
- [x] Locked install commands and service configuration tested on target host. EP-07 `verification/implementation/EP-07/release-install.json`, `verification/implementation/EP-07/service-unit-syntax.json`, `verification/implementation/EP-07/service-gate.json`, `verification/implementation/EP-07/release-upgrade-0.2.2.json` and `verification/implementation/EP-07/default-intake-live-20260928.json`: hash-locked wheel install, `runner install --dry-run`, `install --user`, unit syntax and earlier stop/restart gates passed. The installed `0.2.5` unit is active, enabled and loopback-only; `Linger=yes` configures boot startup, and the upgraded service is armed with a fresh epoch matching Drive SYSTEM STATE. Its `0.2.4` predecessor also completed a 20-second graceful restart. The `0.2.5` stopped-service backup, wheel and final source suite are recorded in `verification/implementation/EP-07/drive-create-performance-upgrade-0.2.5.json`. A real power-cycle remains untested. Environment: owner Linux x86_64, Python 3.10.12, systemd 249; reviewer: Codex, 2026-09-28.
- [ ] All 14 walkthroughs have EP-07 evidence at the required fake/real boundary; STATUS.json and release notes describe actual capabilities.
