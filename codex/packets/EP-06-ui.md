---
id: EP-06
status: NOT_STARTED
depends_on: ['EP-05']
---
# EP-06 — Polished control centre wired to real state

## Required inputs
`prototype/index.html`, `prototype/styles.css`, `prototype/app.js`, `docs/UX_SPEC.md`,
`docs/API_CONTRACT.md`, `docs/OWNER_GUIDE_RU.md`.

## Outcome
Preserve the finished visual language while replacing demo state with real projections. Do not
rebuild this as a generic CI dashboard, introduce queues, or hide the workspace identity.

## Implementation tasks
1. Separate render/model/API layers. Reuse CSS tokens and components. Pick the minimum frontend
   tooling; vanilla modules are acceptable. Do not add a large framework solely to replace the demo.
2. Implement all visible controls or disable them with a specific reason. Project creation wizard
   covers new/GitHub/local; project editor menu lists exact workspaces; workspace editor is direct.
3. Render actual action stages, step timestamps, external side effects and output evidence. No
   simulated spinner progress, invented duration, stale copied pass badge or estimated success rate.
4. Explain operation versus project execution, publication and delivery status in Russian. A failing
   test is scoped to run/workspace; only genuine infrastructure faults affect the runner indicator.
5. Preserve snapshot provenance/staleness after branch/source changes. Latest results reference
   a run ID and frozen snapshot, not simply the current branch name.
6. Add connection freshness individually for Drive check, Git fetch and GitHub API capability. A
   service heartbeat does not prove OAuth works. Render unknown/offline/partial states honestly.
7. Implement keyboard/focus flow, command palette, light/dark themes, reduced motion, bounded
   live console tail, copy/download, safe output previews and useful empty/loading/conflict states.
8. Use actual readiness before removing DEMO labels. Production has no “simulate failure” controls;
   storybook/test fixtures may retain explicit synthetic scenarios.

## Acceptance and evidence
- browser E2E: all 14 scenarios navigable with fakes plus gated live vertical slice;
- 1280/1600/2560 desktop layouts, 390 mobile readable fallback, no horizontal page overflow;
- full keyboard modal/selection/escape/focus-return coverage and reduced-motion media query;
- operation stage count correct for create/verify/publish; no fake test run on create/publish;
- BUSY declines without pending state; new operation requires a new explicit user action;
- malicious log/HTML/SVG content remains inert; real endpoint addresses cannot be injected by Drive;
- screenshots of dark/light, workspace, provisioning and interrupted/delivery-pending states.

## Scope boundary
Keep aesthetic restraint: no persistent moving aurora, auto-scrolling unreadable logs, confetti,
red project-error counters or metric cards added for visual filler.

## Packet closing rule
Write `codex/checkpoints/EP-06.md` using the checkpoint template. Put machine evidence in
`verification/implementation/EP-06/`. Record actual commands, exit codes and environment identity.
Do not mark this packet VERIFIED if only mocks pass but the specified gate requires a real local
process. Live provider gates may be BLOCKED while offline implementation is tested, but the final
release remains not deployable until they pass.
