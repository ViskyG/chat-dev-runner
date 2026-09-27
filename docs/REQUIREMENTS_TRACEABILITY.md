---
id: CDR-TRACE
status: owner-requirements
---
# Requirement → design → evidence

| Owner requirement | Design location | Delivery/acceptance |
|---|---|---|
| Several projects from the beginning | ARCHITECTURE / domain registry | UI multi-project; SC-02–04; EP-02 |
| Create new project through Drive | ROOT COMMAND / approved templates | CREATE_PROJECT schema; SC-02; EP-04/05 |
| Full instructions stored in Drive | Drive seed / CHAT PROTOCOL | bootstrap manifest; SC-13 |
| Local + GitHub + Drive provisioning | Git lifecycle / provisioning journal | Wizard; SC-02/03; EP-05 |
| Git branch changes from commands | RETARGET_WORKSPACE | UI dirty refusal; SC-07; EP-02 |
| Parallel local developments | Workspace as first-class entity | VS Code selector; SC-04/08 |
| Commit/push/PR must be automatic | draft_always route | SC-05/06; EP-05 mandatory, not deferred |
| Grant ChatGPT repo access eventually | Separate MANUAL_REQUIRED capability | Deployment blocker/notification; explicitly deferred |
| Jobs called «Операции» | UX navigation / API | Prototype Russian labels |
| No execution queue or task KPI | Immediate admission/BUSY | Reference no-queue and capacity tests |
| Remove ambiguous project/activity KPI | UX hierarchy | No project KPI/Active badge |
| No success-rate/run-quality dashboard | Separate historical runs | No percentages on overview |
| Runner faults separate from test findings | Result axes | Reference FAILED workload + COMPLETED operation; SC-06 |
| Drive and Git sync are distinct | Channel directions / freshness | Drive view; scoped fetch |
| New-project button is substantial | Three-path wizard | SC-02/03 / EP-05 |
| VS Code button lists worktrees | Project selector / workspace direct action | Interactive UI menu |
| Return logs/console/other outputs | Type registry / manifest | Raw capture, tests, OUTPUTS, EP-03 |
| UI not internet-accessible | Loopback architecture/security | Static preview bind; production security gates |
| Avoid autonomous coding loop | Chat protocol ends after dispatch | SC-05/10/13; no OpenAI dependency |
| Beautiful, calm animated control center | UX tokens + CSS motion policy | Dark/light views; reduced motion; browser smoke |
| Codex-ready deployment guidance | Index + execution packets + checkpoints | START_HERE / LAUNCH_PROMPT / EP-00–07 |

“Delivered prototype” and “accepted future behavior” are different columns. Cloud functionality
not exercised on the owner's account must not be counted as passed merely because a UI exists.
