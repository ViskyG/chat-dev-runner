# Agent entry contract

Read `STATUS.json`, then `codex/START_HERE.md`. Execute only the selected packet.
The current repository is a design kit + offline UI + reference implementation, not a live daemon.
Do not present mock network actions as implemented integrations.

Non-negotiable requirements:
- One explicit user instruction may initiate one finite operation. Never build a ChatGPT UI bot,
  output scraper, automatic model follow-up loop, or OpenAI API client into this project.
- NO execution queue. Immediate scoped admission or terminal BUSY. Retrying requires a NEW ID.
- Multi-project and first-class workspaces from v1. Do not collapse to a global current branch.
- Automatic provisioning, code delivery, environment setup, verification, commit, push,
  draft PR creation/update and evidence return are CORE. Do not postpone GitHub publication.
- Loopback-only UI/API. No public listener, ingress, tunnel, webhook endpoint or LAN binding.
- A failing project test is not a runner fault. Preserve separate execution/delivery/publication axes.
- Drive instructions must be usable by a new chat without memorized context.
- Never overwrite a dirty workspace, enable force-push, merge main, or silently stash.
- Trusted local profiles define authority. A new remote project may select an approved template,
  but cannot grant itself shell access, credentials, host paths or extra capabilities.
- Worktrees are NOT sandboxes; an allowlisted test command still runs project code.
- Preserve the quiet visual design, no KPI clutter, accessible focus and reduced-motion support.

Use source-relative schemas from this editable checkout. Pin production dependencies in EP-00.
Do not silently introduce Kubernetes, Redis, Celery, external telemetry, CDNs or cloud hosting.
Tests must produce evidence under `verification/` or `evidence/<packet>/`.
Stop with an exact checkpoint on quota exhaustion; never conceal unfinished integrations.
