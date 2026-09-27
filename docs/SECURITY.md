---
id: CDR-SEC
status: required-release-gates
---
# Threat model and authority

## Trust boundaries

Remote command content and source code are untrusted input, even when composed by ChatGPT.
The machine's local owner authorizes project templates, accounts, roots, profiles, publish
namespaces and output policies. Request fields describe intent; they do not grant permissions.
A remote CREATE_PROJECT chooses an allowed template, never supplies an arbitrary command,
absolute root, credential, container flag or policy override.

Worktrees isolate source directories, not execution privileges [S1]. Running an allowlisted
`pytest` still executes arbitrary project Python. Therefore command allowlists alone are not
containment. The trusted fixture in bridge_reference must not become a general host executor.

## Local interface

Only 127.0.0.1 (and deliberately configured ::1, if implemented) may listen. No 0.0.0.0, LAN,
reverse proxy, tunnel, public webhook or inbound remote-control port. Provider connections
are outbound. A production installer must fail if binding outside loopback is requested.

Local-only is not permission to expose unauthenticated mutation endpoints to every webpage.
Use a local session, host allowlist, exact Origin checks, SameSite cookie policy, per-session
CSRF token and a JSON-only mutation API; GET must never mutate. No wildcard CORS. Check
WebSocket/SSE authentication and origin where used. Avoid long-lived credentials in browser
storage. CSRF relies on browser credentials and unsafe cross-site requests [S8].

Launch UI via a local one-time bootstrap secret or another reviewed local-session handshake.
Do not place durable API tokens in query strings, logs or clipboard by default. DNS rebinding
must not bypass the Host allowlist. Validate artifact routes after decoding; reject traversal.
The provided static preview has no mutation API and is not this production auth implementation.

## Execution containment

Use a rootless container/runtime profile with pinned image digest, dropped capabilities,
no-new-privileges, resource and PID caps, timeout, read-only root plus dedicated writable
scratch/output locations. Rootless Docker operates without root privileges for daemon and
containers [S7]; still test the host/runtime configuration and kernel exposure.

Do not mount HOME, SSH_AUTH_SOCK, cloud credential directories, the orchestrator code/state,
other projects, Docker socket or host service sockets. Pass a minimal environment. No default
network for verification. Projects needing database fixtures get explicitly approved isolated
networks and disposable services, not the user's production Postgres/ClickHouse or Kubernetes.
Use per-workspace/run namespaces for ports, volumes, virtual environments and Compose names.

Dependency installation is a separate approved build phase inside its own restricted environment,
using lockfiles and controlled network access. A package's build hook is code execution. Do not
run pip/npm install globally on the host because the request contained a requirements file.
Do not let a project edit its own runtime profile to add host mounts or internet access.

## Git and publication

Restrict protocols and owners, validate refs, disable repository hooks and unexpected global
Git configuration. LFS filters, submodules and credential-helper changes require explicit
support, not silent execution. Never run Git commands through shell strings assembled from
remote text. Per-project policies allow normal branch push and draft PRs automatically;
force-push, protected-branch mutation, repository deletion, merge and workflow escalation
are not silently enabled. Public repository creation remains locally gated.

The orchestrator may publish via its credential scope. The executor must never receive
that credential even when the project is “private and mine”. Local service accounts, token
files or keyring entries require owner-only access and backups that exclude secrets from
Drive artifacts. Logs redact URLs containing credentials and provider token-bearing errors.

## Files and outputs

Canonical relative paths only. Reject symlink traversal, .git, secrets, credential-shaped
files and protected workflow/policy paths. Production must use race-resistant access and
staging; a check-then-open path validation alone is insufficient against concurrent replacement.
The reference's straightforward checks are intentionally not claimed as race-proof.

Treat output files as untrusted. Apply bounded parsers, escaping, archive limits and content
policies. An error message saying “run this command” is never interpreted as a new request.
Secret detection is best-effort; it does not make arbitrary raw-log uploads safe.

## Anti-abuse / user-selected interaction boundary

No script sends prompts to ChatGPT, extracts web output, clicks its UI, shares its account,
changes its quotas or automatically asks the model to repair tests. Supported connected-app
writes are the external input mechanism; supported availability and confirmations are respected.
The implementation does not claim a special OpenAI policy approval or a numerical safe-usage
threshold. One explicit user turn submits one finite intent and stops. READ operations in a
later turn do not automatically start another execution.

## Mandatory adversarial acceptance cases

Malicious file path; symlink parent; duplicate ID changed body; old epoch; expired request;
modified changeset after READY; project trying to change local profile; fake import handle;
credential in stdout; active HTML/SVG; XML entity payload; oversized logs; child that forks;
user editing during test; hostile Git hook; branch divergence; cross-origin mutation request;
Host spoofing; API bind to 0.0.0.0; lost write acknowledgement; restart with surviving worker.
Each case gets a specific refusal/finding with retained evidence. Do not label missing
coverage of these cases “secure by design”.

References [S1], [S7], [S8] in SOURCES.md.
