---
id: CDR-ADR
status: design-baseline
---
# Decision register

| ID | Decision | Reason / rejected alternative |
|---|---|---|
| ADR-01 | One human-triggered finite route | No automatic model follow-up, even if technically possible |
| ADR-02 | Automatic GitHub publication is v1 core | Manual commit/push plumbing defeats the purpose |
| ADR-03 | Project → Workspace → Snapshot/Run | A global current branch loses parallel-development context |
| ADR-04 | No execution queue | Busy commands terminate; no surprising later execution |
| ADR-05 | Native Docs per registered mailbox | Avoid shared-sheet row/status races and source-file edit assumptions |
| ADR-06 | Runner creates its OAuth-scoped control Docs | Do not assume folder membership grants per-file OAuth access |
| ADR-07 | Git source history + explicit Drive changesets | Reject blind two-way directory mirroring and last-write-wins |
| ADR-08 | Local trusted templates and profiles | Remote new project may use authority, not grant itself authority |
| ADR-09 | Snapshot before test and publication | Published result must correspond to actual tested bytes |
| ADR-10 | Separate operation/project/delivery/publication outcomes | Failing tests do not mean runner is broken |
| ADR-11 | Loopback-only API with session protection | No public service; loopback itself is not sufficient authentication |
| ADR-12 | Rootless restricted executor | Worktree + command allowlist alone is not containment |
| ADR-13 | Raw local capture + typed sanitized upload | Preserve evidence without uncontrolled secret transfer or disk growth |
| ADR-14 | Branch retarget allowed when clean/idle | Do not remove requested capability; do not force it through conflicts |
| ADR-15 | GitHub access for ChatGPT manually configured first | Runner token and chat connector authorization are independent |
| ADR-16 | Vanilla prototype reused initially | Avoid a framework rewrite before live protocol works |
| ADR-17 | Quiet workshop visual system | Aesthetic comfort without dashboards, fake activity or quotas imagery |
| ADR-18 | Reference/demo/live statuses explicit | Do not sell a pretty simulation as a tested production service |

## Owner decisions still needed at installation, with proposed defaults

GitHub owner/organization and account entitlement: choose locally; no default inferred from
Linux username. Managed data root: `~/.local/share/chat-dev-runner`. New repos: private.
Publication: working branch + draft PR; test findings allowed and disclosed. Parallel executions:
2. Default request TTL: 15 minutes. Runner startup after uncertain stop: unarmed/new epoch.
Runtime: rootless containers; no host-code fallback. Raw logs: local only with explicit retention.
Existing project imports: separate managed copy, never mutate current user's checkout.

These are installation decisions, not questions the implementation model may silently answer
with credentials or business-data access. Document deviations in the same register.

## Post-archive owner update (2026-09-26)

ADR-06 installation exception: the owner has already provisioned the Drive workspace.
Attach the real adapter to the existing root and Installation Index in
`config/installation.existing.json`; validate per-file authorization and index mappings before
writes. Do not create a second workspace or overwrite existing documents from `drive-seed/`.
This explicit owner instruction supersedes the bootstrap assumption for this installation.
See `docs/LOCAL_SETUP.md`. The runner remains NOT_CONFIGURED and unarmed.

## Owner Drive guidance and live compatibility (2026-09-27)

The Drive account is a shared experiments account, not a personal mailbox. The owner
permits clearly labelled files created by this runner for integration tests within
the existing installation; preserve all pre-existing resources and other experiments,
and do not delete or overwrite them. This does not authorize a second bootstrap or
turn an arbitrary Drive document into a command mailbox.

The existing installation was created by a different OAuth app and has a human-readable
Index and variant names. App-private properties from that installer are not visible to
the current app. For these legacy files only, a private local exact-ID manifest is
pinned to the current Index text hash; every indexed ID must appear in that Index and
the adapter checks the actual file's parent, type, name, capabilities and unchanged
Index revision. Newly provisioned files retain strict current-app ownership checks.
The dedicated cross-app canary is recorded in
`verification/implementation/EP-04/live-existing-installation-canary.json`.
