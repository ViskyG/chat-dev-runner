# Verification report — executed evidence and limits

**Final state: PASS for the offline checks listed below. This is not a release acceptance of Chat-Dev Runner.**

Executed at `2026-10-15T08:16:21.241911+00:00` in the working container: Python `3.13.5`, OS `Linux`, architecture `x86_64`. The final recorded check invocation took 2.959 seconds; this is a measurement of the check invocation, not an estimate for integration.

## What actually ran

| Check | Actual result | What it establishes |
|---|---|---|
| SQLite-backed protocol model | **62 tests, 0 failures, 0 errors** | Declared session, queue, acknowledgement, duplicate, cancellation, source-selection and quality-gate transitions in the model |
| Event ordering | **120 sequential permutations**, inside one of those tests | Invariants for those logical orders, not 120 separate live or concurrent runs |
| JSON contracts | **5 schemas, 16 valid inert examples, 14 negative checks** | Schema consistency and selected semantic/path rejections; not authorisation or target-framework execution |
| Local Git publication experiment | **PASS**, `git version 2.47.3` | A stale result push is rejected; rebuilding a result-only commit preserves the newest source and another runner's results on the same branch |

The local Git experiment used generated repositories and a local bare remote, with no network or GitHub API. The commit labelled `tested_commit` is a fixture identity, not a claim that an application test ran against it. Temporary repositories were removed at the end. Exact generated object IDs and assertions are in `git-experiment-results.json`.

## Reproduce the checks

From the extracted package root, use an existing Python environment with the `jsonschema` package available:

```sh
python verification/run_all.py
```

The protocol model alone needs only the Python standard library:

```sh
python -m unittest discover -s verification -p 'test_protocol_model.py' -v
```

Do not install packages globally merely to run this design package. `run_all.py` invokes the contract check and disposable local Git experiment and refreshes the evidence files. A new run changes their contents and therefore invalidates the original package checksums; keep the original package when comparing evidence.

## What was not run

The actual target repository test suite, installed runner service, live GitHub/Google Docs operations, real provider multiprocess races, real five-language projects, native Windows/macOS installation or process cleanup, Compose, and Unity were **not executed** in this session. No cloud or user project files were changed. Linux in this report is the model's environment, not native qualification of the new runner backend.

`FakeMailbox` substitutes for provider conditional writes. The model abstracts the full transport schema; schema validation is a separate check. Cancellation models a transition, not actual OS cleanup. The local Git experiment verifies Git mechanics, not GitHub app permissions, branch rules or API behavior. The supplied model must not be imported into production as the daemon.

## Iteration and found defects

The first reported pass had 57 model checks. Further review added restart/ACK/duplicate cases. An intermediate 59-check run had three failures after an incorrect broad edit to the model's ACK invariant; they were fixed before the final 62-check pass. The intermediate transcript is retained as `intermediate-failure.txt`; the explanation is in `iteration-findings.md`. The final evidence, not the intermediate transcript, is the current model result. In particular, finalized duplicate submissions now clear READY without re-executing, and an uncertain old execution blocks a new lane after restart.

## Acceptance designed, not executed

`../acceptance_scenarios.json` contains **74 scenario families** traced to **20 requirements**. Their live status is NOT_RUN. The planned minimum native matrix is five languages × three OS targets × two transports × passing/failing = 60 future end-to-end routes; it is not part of the 62 model tests. Requirements and scenario count are coverage planning, not proof of completeness.

## Source inspection

`../SOURCE_AUDIT.json` records 17 source/document anchors, exact observed blob references and read scope at `ac7d55ddce58bcb422bae821b2550483592f7975`. Some scopes are excerpts or prior-turn documents; no whole-repository audit is claimed. The owner confirms local checkout parity. The installed process was not independently inspected.

Authoritative machine records: `verification-results.json`, `contract-results.json`, `git-experiment-results.json`; unit-test transcript: `model-test-output.txt`. External factual references are in `../SOURCES.md`.
