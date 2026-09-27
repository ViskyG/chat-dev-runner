---
id: CDR-UX
status: prototype-baseline
language: en-with-ru-copy
---
# Interaction and visual system

## Product feeling

A quiet personal workshop. Warm graphite rather than cold near-black, sage accents rather
than neon success green, restrained gold for orientation and amber for project findings.
No faux radar, fake CPU telemetry, huge counters, flashing error walls or perpetual animated
ornaments. The owner should feel oriented, not monitored or judged.

System fonts only; no distributed font files, analytics, CDNs or external asset fetches.
The prototype uses CSS/SVG line icons. Transitions 180–300 ms. A moving ring appears only
while an explicit operation is running. Honor prefers-reduced-motion and an in-app motion
switch. Reduced motion changes visual behavior, never execution speed or meaning [S9].

## Information hierarchy

1. Global frame: section navigation, local-only identity and runner/integration faults.
2. Project context: name and repo, VS Code workspace selector, new operation.
3. Workspace context: stable name/ID, branch, HEAD, dirty state, Git freshness and publication.
4. Operation: actual finite steps, initiator/intent/time, cancellation and exact result axes.
5. Run evidence: summary → console/logs/tests/artifacts → retained source files.

The sidebar count next to “Проекты” is a compact list count, not the rejected KPI card.
There is no “5 projects / 2 active”, no queue metric, no success rate, no global test-error
counter and no ambiguous “Активен” badge. A historical count can live in the history header.

## Main screen

The selected project is explicit. Left column contains its workspaces, not branches of every
project mixed together. The detail pane never silently chooses a different area after an
operation. A result points to its recorded snapshot; a stale message is visible when editor
files changed after verification. Do not show last result as current merely because its tests
passed. Pending output visibility is not mistaken for missing output.

Global connection strip distinguishes a successful Drive command check from GitHub credential
availability. Per-project/workspace fetch and push times are separate. No “fully synchronized”
label that merges unrelated directions. Failed checks retain the last successful time plus
an explicit stale/error indicator.

## Buttons and actual semantics

- “Новый проект”: wizard NEW / GITHUB / LOCAL → preflight → plan → one finite provisioning op.
  Covers local repository, GitHub, environment profile and Drive, not just a Drive record.
- “Новая рабочая область”: name, stable ID, new/existing branch, exact base SHA, profile;
  leaves sibling workspaces untouched. Existing checked-out branch causes a clear refusal.
- “Новая операция”: route + exact target + policy summary. A single human confirmation covers
  permitted automatic steps. Provider-required confirmations are not bypassed.
- “Проверить”: VERIFY on the selected area, no push/PR implied. Dirty snapshot inclusion must
  be explicitly disclosed. Busy means not admitted, never queued.
- “Ветка”: retarget THIS clean idle area or create another. No stash/reset/force buttons hidden
  behind a generic confirmation. Branch identity does not replace workspace identity.
- “Изменения”: exact reviewable diff against stated base; source inclusion list and conflicts.
- “VS Code” in project header: dropdown of worktrees. In a workspace: its exact managed path.
  The live local endpoint maps stable IDs to paths; remote command text cannot supply argv.
- “Файлы в Drive”: links to that area's exact registered Docs, not a search by display title.
- Drive refresh: check commands/state only. Git fetch: refresh remote knowledge only.
- “Остановить”: request cancellation, signal process group/container, finalize partial outputs.
  Completed external side effects are not magically undone.
- “Опубликовать отчёт повторно”: delivery-only operation against frozen run ID. Never rerun tests.

## Dialogs and overview screens

Native semantic controls, visible labels, stable focus, Escape to close, focus return and
keyboard-accessible workspace cards. Ctrl/Cmd+K palette. Dialogs announce title and errors.
Use non-color labels for status. Avoid stealing focus or replacing a whole open form on a
background update. Keep text selection/scroll in long logs. Live frontend should use keyed
partial updates rather than the prototype's full render loop.

Operations list has execution status and project result as separate columns. Results list
is organized by source snapshot. System view owns runner problems, credentials health,
local mode, retention and motion settings; it does not display project-quality alarms.

## Output viewer

Summary: outcome, elapsed time, counts only if parsed, delivery/publication links, tested SHA.
Console: stdout/stderr selector; explicit truncation and redaction; no fabricated total order.
Logs: known structured fields plus raw fallback. Tests: cases and failure traces. Artifacts:
kind, format, size, hash, completeness, publication/privacy state and safe preview/download.
Missing, unparsed, unavailable and empty are four different states.

## Responsive targets

Desktop: test 1600×1120 and user's 2560×1440; comfortable maximum content width, not a wall
of tiny text. Laptop: 1280×800; independent content scroll. Narrow viewport: collapse sidebar,
keep a usable project switcher and action access (prototype navigation is simplified on mobile).
The target is local desktop use; mobile polish must not delay the core delivery route.

## Prototype limitations to fix before production

All Git/Drive operations are currently simulations. The pipeline art uses action-specific stages; a live adapter must additionally show
actual skipped/failed/partial stages rather than simulated progress. UI data use abbreviated fake SHAs and sample repo
names. Fixture counts are not business data. LocalStorage is only demo state, not production
authority. No same-origin active HTML artifact preview is implemented. Not every production
error state has a complete interactive mock. Those are explicit implementation tasks, not
silent implied functionality.

References: [S9]. Colors, copy and layouts are original project design choices.
