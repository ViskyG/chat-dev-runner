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

Follow the owner authorizations and current `STATUS.json` for each live gate. EP-04/05 have
already used the authorized existing Drive installation, full Drive OAuth scope and private
GitHub test projects; do not bootstrap a second Drive workspace. Implement new behavior against
fakes and disposable local fixtures first. Never execute project code on the credential-bearing
runner host under the pretext that pytest is allowlisted; use the contained broker. No public
GitHub repositories, force pushes, automatic merges, OAuth scopes beyond the owner-approved
`https://www.googleapis.com/auth/drive`, or production application deployments by default.
The owner reports chat-side Drive and GitHub access configured. EP-07 has
installed an enabled owner-user wheel service at version `0.2.2`, created one
disposable project through its local UI and another through a Drive root
COMMAND from this chat connector session, and added a second workspace through
the local UI. Both workspaces completed separate passing and failing contained
verification/publication routes with private draft PRs and Drive reports. The
installed project COMMAND also accepted a current-chat `FETCH` and published a
matching project RECEIPT. The service is currently disarmed. See
[release-upgrade-0.2.2.json](../verification/implementation/EP-07/release-upgrade-0.2.2.json),
[live-ui-acceptance-20260928T105918Z.json](../verification/implementation/EP-07/live-ui-acceptance-20260928T105918Z.json),
[live-drive-chat-roundtrip-20260928.json](../verification/implementation/EP-07/live-drive-chat-roundtrip-20260928.json),
[live-multiworkspace-readback-20260928.json](../verification/implementation/EP-07/live-multiworkspace-readback-20260928.json),
[live-publish-primary-20260928.jsonl](../verification/implementation/EP-07/live-publish-primary-20260928.jsonl),
[live-publish-parallel-20260928.jsonl](../verification/implementation/EP-07/live-publish-parallel-20260928.jsonl) and
[live-project-fetch-20260928.json](../verification/implementation/EP-07/live-project-fetch-20260928.json).
A project-level Drive `CREATE_WORKSPACE` command, a command from a fresh
separate chat and the remaining EP-07 recovery/safety gates are unverified.

Use existing schemas, reference tests and design decisions. Treat the reference core as a tested
behavioral specification, not a production daemon. Do not claim live capabilities implemented by
substituting the demo UI or stub outputs. Record deviations with evidence in the checkpoint.

For the current packet: state the files you will change, implement a complete vertical slice,
run its acceptance tests, and write codex/checkpoints/<packet-id>.md plus machine-readable evidence.
At a context limit, leave a precise resumable checkpoint; do not rewrite the roadmap or speculate
that unrun tests passed. Finish by listing changed files, test commands/results, remaining blockers,
and the next packet. Keep tokens focused: inspect relevant files rather than dumping the full repo.
