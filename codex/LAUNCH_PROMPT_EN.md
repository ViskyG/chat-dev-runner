# Copy this into Codex after unpacking the kit

You are implementing Chat-Dev Runner from this repository's design and executable prototype.
Read AGENTS.md, STATUS.json and codex/START_HERE.md. Inspect git status and preserve all existing
user changes. Work through codex/MASTER_ROADMAP.md in dependency order, beginning with the first
incomplete packet. Each packet contains its scope, exact read set, tests and evidence gate.

Preserve the product's purpose: automatic local/GitHub/Drive provisioning, changes, verification,
commit/push/draft-PR publication, and typed result delivery, initiated once by the user through
Drive or the local UI. Automatic GitHub publication is a core feature, not a later optional extra.
Do not add an execution queue or autonomous model-feedback loop. The UI must remain loopback-only.
Keep the quiet, polished visual design and its reduced-motion mode.

Do not deploy, authenticate to, or modify real cloud accounts until I authorize that live gate.
Implement adapters against fakes and disposable local fixtures first. Never execute project code
on the credential-bearing runner host under the pretext that pytest is allowlisted. Use the
security/sandbox design. No public GitHub repositories, force pushes, automatic merges, broad
OAuth scopes, or production application deployments by default.

Use existing schemas, reference tests and design decisions. Treat the reference core as a tested
behavioral specification, not a production daemon. Do not claim live capabilities implemented by
substituting the demo UI or stub outputs. Record deviations with evidence in the checkpoint.

For the current packet: state the files you will change, implement a complete vertical slice,
run its acceptance tests, and write codex/checkpoints/<packet-id>.md plus machine-readable evidence.
At a context limit, leave a precise resumable checkpoint; do not rewrite the roadmap or speculate
that unrun tests passed. Finish by listing changed files, test commands/results, remaining blockers,
and the next packet. Keep tokens focused: inspect relevant files rather than dumping the full repo.
