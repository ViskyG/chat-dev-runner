# Codex entry point

Current frontier: **GP-07**, with actual GitHub/Linux and installed Docs v2
integration evidenced. The 2026-10-16 owner request authorized the canary gate;
it passed, and a separate registered Docs v2 channel is active for the existing
test project. Read `checkpoints/GP-07.md`; preserve current bindings and newer
intent. Read-only GP-00 matched commit `ac7d55d`
and all 17 design source anchors. Read the selected packet under
[the approved v2 design](../design/github-native-v2/00_START_HERE.md).
The owner approved the complete proposed GP implementation stage in chat on
2026-10-15. Preserve old EP evidence and v1 routes; carry relevant recovery gaps
forward without making unrelated EP-07 sign-off a prerequisite. Use focused
checks while building, integration proof at capability boundaries and one
justified broad regression after source freeze. No repeated unchanged-state
re-proving or extensive intermediate documentation cycles.

The text below describes the historical v1 frontier, not the current GP order.

This repository is the source for an **installed owner-user runner**; EP-07 release
acceptance is still in progress. EP-00 through EP-06 are verified: transient loopback
intake, the existing Drive installation, private GitHub writes,
rootless contained execution, draft PRs, Drive result delivery, and a packaged authenticated
local UI with fake-backed browser and real disarmed-process acceptance. The original prototype
remains a separate simulation. The selected packet is EP-07: the durable user service and
backup/restore are installed and evidenced, while remaining host routes, recovery and release
acceptance stay open. Read `STATUS.json` first.

## Intended outcome
A local multi-project development bridge. An explicitly authorized one-shot command from an
existing Google Doc or the local UI can provision local/GitHub/Drive resources, target an exact
worktree, stage changes, capture an immutable source snapshot, run approved checks, publish a
branch/draft PR, and return typed evidence. No LLM API, browser automation, automatic follow-up
prompt, work queue, public UI, or production deployment of user applications is involved.

## Read order / context economy
1. `../AGENTS.md`, `../STATUS.json`, `../docs/DECISIONS.md` — controlling constraints.
2. `MASTER_ROADMAP.md` — select the first incomplete packet and check dependencies.
3. Read **that packet's Required inputs**, not the whole repository indiscriminately.
4. Read the exact schema and relevant reference test before changing a contract.
5. Use `CHECKPOINT_TEMPLATE.md` at every stop; store evidence, not an uncheckable narrative.

English implementation documents are authoritative for machine contracts. Russian owner guide
is authoritative for product intent. Where these disagree, preserve the user's explicit constraints,
record the conflict, and resolve it before side effects; never silently simplify the core automation.

## Run the supplied baseline
```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
python -m bridge_reference.demo
python scripts/bootstrap_drive.py --plan
./start-prototype.sh
```
Use Python >=3.10. The reference package is designed for **editable source-tree installation**;
its schemas are source-relative. The EP-06 wheel packages schemas, offline starter resources and the local UI,
with an asset audit recorded in its checkpoint. Recheck installed-wheel behavior when
packaging changes; release acceptance still belongs to EP-07.

`python -m bridge_reference.demo` creates its own temporary toy Git repository; it never touches
existing user repositories. The reference process capturer is **not** a sandbox for arbitrary code.
The prototype has simulated data and no network connections. Do not remove its DEMO badge until
all displayed production facts come from real backend read models.

## Authorization boundary
Implement and test offline by default. Do not inspect unrelated user files/accounts or create
live cloud resources merely because a packet describes a workflow. The owner has already provisioned the
existing Drive workspace and configured the local OAuth/GitHub integrations; do not create a
second Drive root. New live host/provider writes must be recorded against exact approved
resources and the packet gate. Never ask for secrets in chat; use a local
secret store. Publishing within an already configured project policy is deliberately automatic;
do not add a confirmation dialog to every push/PR.

## Completion standard
A packet is complete only when its tests and evidence exist. The project is deployable only when
EP-07's live gates pass on the owner's machine. If a provider permission or account feature is
unavailable, return a precise blocked gate, not a fake connected/success state.
