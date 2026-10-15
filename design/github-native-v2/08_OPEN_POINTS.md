# Explicit assumptions and bounded technical probes

No further owner Q&A is required before implementing the approved frontier. Do not convert these probes into a fresh broad questionnaire.

| Topic | Chosen design / check | When to revise |
|---|---|---|
| Queue after restart | Accepted history retained; unstarted entries suspended, never auto-replayed. New READY required. | Only a later explicit request for resumable durable backlog changes this semantic. |
| Offline observation | Baseline on start/gap; old READY => NEEDS_RESUBMIT, same remote-slot resubmission. | Reliability study may refine transient-gap threshold, never reintroduce historical replay silently. |
| Human request IDs | Runner-managed slot token and derived ID; manual author need not calculate hashes/UUIDs. | Usability test may simplify display, not weaken dedup/ACK binding. |
| ACK lost then doc reused | Persist candidate, require exact surviving proof; otherwise explicit uncertainty, no execute. | A proven provider receipt mechanism may improve availability without guessing. |
| Native cleanup | POSIX supervision / Windows Job Object; ordinary-user mode is not hostile-code isolation. | Missing lifecycle primitive -> explicit target blocker, not forced container replacement. |
| Mutable task definitions | Task manifest resolves from current source at execution start and its hash is reported. | Changes needing source edits still require user approval through the preparation skill. |
| Same-branch publication | Add results on latest remote HEAD, not tested old tree; bounded conflicts then local fallback. | PR, other branch, infinite retry require an owner change. |
| Local fallback | Sibling managed result root, no automatic expiry of unpublished/large originals in first stage. | Storage pressure should produce explicit policy proposal, not silent deletion. |
| Project framework diversity | Qualify defined profiles first; unknown framework gets precise unsupported/preparation report. | Add separate reporters using the same contract, don't force framework migration. |
| Current branch local vs remote | Explicit source kind; never silently use dirty local files for github_branch. | New selector needs a versioned meaning, not a heuristic. |
| Windows/macOS availability | Acceptance requires actual targets; no source-only or container-only certification. | Record any unavailable target as NOT_QUALIFIED. |
| Google Docs revisions/tabs | requiredRevisionId from the same local principal; exact tab ID; no retrieval of old text assumed. | Probe current connector/API behavior on a disposable document. |
| Publication-size budgets | Proposed lower local budgets 10/25 MiB; actual provider limit separately stored. | Configuration approval may adjust budgets; no LFS/Releases automatic fallback. |
| Multiple runner IDs | Independent instances; no automatic platform matching/work stealing. | Distributed scheduling is outside this scope. |
| Source changed during local capture | Freeze and validate stable file set or fail INPUT_CHANGED. | Never claim a mixed snapshot equals a Git commit. |

## Deliberate non-goals
No unattended offline command collection, no public webhook server, no automatic agent iteration, no production application hosting, no mandatory Google account, no operator-role layer, no mandatory separate OS user, no default automatic source fixes, no full dependency-manager coverage, no implied Unity graphical validation, no remote access link invented for a local-only file.

## Evidence boundaries
Repo/source anchors were read through the connected GitHub tool. Owner confirms checkout parity. Reference-model tests and a disposable local Git experiment validate only the stated algorithms in the available environment. They do not prove live Google/GitHub writes, race-free multiprocess implementation, Windows/macOS service lifecycle, toolchain installation or Unity licensing. The final verification report gives executed counts. No API credentials or live installations are bundled.
