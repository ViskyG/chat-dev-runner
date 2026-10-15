# Chat-Dev Runner — next-stage design / 2026-10-15

**Status: DESIGN HANDOFF, NOT AN INSTALLED RELEASE.** Repository baseline:
`ViskyG/chat-dev-runner@ac7d55ddce58bcb422bae821b2550483592f7975`.
The owner confirms this repository matches their local checkout. The installed service was not probed in this design session.

## Read only this frontier first
1. `01_OWNER_DECISIONS_RU.md` — final owner choices; supersedes contradictory earlier proposals.
2. `02_DESIGN.md` — boundaries, component seams and lifecycle.
3. `06_MIGRATION_AND_ROADMAP.md` and `packets/GP-00.md` — start here, not in the last incomplete old EP packet.
4. Read only the next packet's named contracts/source anchors. `SOURCE_AUDIT.json` pins the observed files.

This directory is a design sidecar, not a source overlay. Place it under an explicitly selected local design directory, such as `design/next-github-stage/`; do not overwrite repository-root AGENTS.md, schemas, source, credentials or existing Drive registrations by unpacking it.

## Intended capability
A user-owned local service, independent of ChatGPT or any model API, accepts commands from a registered GitHub command slot or optional Google Doc. It observes new submissions only during live intake. One task runs per runner; other live-accepted tasks wait FIFO. It resolves current source when the task starts, executes natively or in an explicitly selected container, freezes results locally, then attempts publication to the **same repository and tested branch**. Files not publishable stay beside the managed checkout with a precise local locator.

Five languages: Python, JavaScript, TypeScript, Java, C#. Native Linux, Windows and macOS are first-class targets, not relabelled WSL/container runs. Compose follows the core; Unity is a bounded final qualification packet.

## Permissions and implementation boundary
This request authorized DESIGN and a handoff package, not production source changes, cloud writes, installation, credential changes or live task submission. No repository or Drive file was modified. The Python in `verification/` is an offline contract model and disposable Git experiment, not a replacement daemon.

For implementation, present numbered changes with purpose, affected paths, consequences, temporary reversible alternative and rollback. Apply each item only after the owner personally approves that item. A clearly bounded batch approval naming several item IDs is acceptable; silence is not. Ordinary use of an already approved task and its expected result publication does not need per-run approval.

## Suggested Codex / Claude launch text

> Read `00_START_HERE.md` in this design package and begin GP-00. The package is a next-stage design for the current `ViskyG/chat-dev-runner`; do not treat its reference model as production code. Final owner decisions override old project constraints only after their explicit migration is approved. Inspect the pinned source anchors, compare the actual checkout, and produce the itemized approval proposal. Do not modify project source/configuration or install anything before the owner approves the corresponding item. Then implement only approved packets in dependency order. Preserve the existing Drive installation and historical evidence. Never restore offline backlog replay, BUSY-instead-of-live-queue, results in another branch, mandatory Google authorization, container-only Windows/macOS claims, extra operator roles, or automatic large-artifact uploads. Record real tests separately from design-model checks. Stop at an actual permission/capability blocker, preserve a checkpoint, and continue independent approved work rather than pretending a target-platform check passed.

## Navigation
- `03_PROTOCOL.md`: mailbox ownership, READY/ACK, queue, recovery and cancellation.
- `04_EXECUTION_AND_LANGUAGES.md`: source-at-start, native/container, language profiles.
- `05_RESULTS_AND_PLATFORM.md`: evidence, same-branch publishing, local fallback, platforms.
- `06_MIGRATION_AND_ROADMAP.md`: integration, approval items and packet dependencies.
- `07_ACCEPTANCE.md` / `acceptance_scenarios.json`: scenario catalogue and live gates.
- `08_OPEN_POINTS.md`: design assumptions and bounded technical spikes, not repeated owner questions.
- `contracts/`: proposed version-2 schemas and inert DRAFT examples.
- `skills/`: ready-to-review bilingual skill sources; NOT installed or registered in the canonical library.
- `verification/REPORT.md`: exactly what was and was not tested in this session.
- `SOURCES.md`: primary external references and repository observations.

The package does not claim a complete audit of every repository line. It maps the relevant inspected seams and requires targeted integration checks.
