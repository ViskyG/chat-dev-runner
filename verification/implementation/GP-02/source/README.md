# GP-02/03 current-source resolver

Base: `0923fc3` (isolated `gp-source-v2` worktree).

Changed behavior: `SourceResolver.resolve_current(project_id, request_id,
source_selector)` freezes current source into `<runs_root>/<request_id>/source`.
Controller configuration binds a managed bare repository and registered local
workspace IDs; mailbox paths cannot select a host directory. GitHub branch
resolution requires the configured exact-branch fetch callback on every call;
fetch failure/missing branch never uses cached refs. `SourceSnapshot.commit`
is the actual original commit; `.tree` is the filtered workload tree for the
existing rootless broker. `workload_snapshot_sha256` hashes the copied file
index (path, actual bytes digest, byte size and mode). Exclusions are explicitly
versioned. Local file identity and approved dirty/untracked paths are retained
in the source manifest outside the workload.

Executed against disposable local Git repositories on Linux:

```
/home/visky/Work/GPTRunner/chat-dev-runner/.venv/bin/python -m pytest tests/test_v2_source.py -q --junitxml=verification/implementation/GP-02/source/tests.xml
/home/visky/Work/GPTRunner/chat-dev-runner/.venv/bin/python -m ruff check runner_app/git/source_v2.py tests/test_v2_source.py
/home/visky/Work/GPTRunner/chat-dev-runner/.venv/bin/python -m mypy --follow-imports=silent runner_app/git/source_v2.py
```

Results: **19 passed**, Ruff passed, strict mypy passed. Covered branch advance
while a request waits; frozen copy after later changes; failed/missing fresh
fetch; actual dirty content, untracked approval, staged addition and deletion;
source/index/ref preservation; command/result/state and credential exclusions;
retained project manifest, task scripts and existing CI files; mutation while
copying -> `INPUT_CHANGED` with no published partial snapshot; symlink, hardlink,
case collision, filter, submodule and object-alternate refusal; bounded copies;
registered paths and exact request scope; detached branch refusal; rootless Git
tree verification over actual copied bytes.

`initial-tests.*` retains the initial 17/18 pass: the failed test configured no
registered workspace then expected that unregistered directory to count as an
original source. The fixture was corrected to register the workspace. Static
findings were corrected before the final pass. No broad regression, network
provider operation, installation, original-user checkout mutation or real
Windows/macOS source qualification was performed. Secure local capture on a
host lacking no-follow descriptor primitives is explicitly unsupported; native
execution qualification is separate from this resolver's Linux evidence.
