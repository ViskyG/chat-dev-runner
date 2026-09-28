# EP-07 installed UI evidence checkpoint

Reviewer: Codex, 2026-09-28. Installed wheel `0.2.6` served the already
registered `cdr-ep07-ui-20260928` primary and parallel runs from the owner-user
loopback service. `installed-ui-026-20260928T1755Z/report.json` is the passing
read-only Chromium/API comparison. The single local bootstrap exchange created
an ephemeral session; all subsequent requests were GET/HEAD. No project
command, provider write, retention mutation, editor launch or new run occurred.

The primary operation is `COMPLETED`, execution `PASSED` (2 of 2 JUnit cases),
publication `DRAFT_PUBLISHED`, delivery `DELIVERED`. The parallel operation is
also `COMPLETED`, while execution is `FAILED` (1 of 2 JUnit cases); its failed
case opens with the exact registered ID and trace. It still has a draft PR and
delivered report. The UI used these four independent API axes, showed a
project-code failure notice rather than a runner fault, and did not infer test
counts from the operation or JUnit suite attributes. Each run exposed eight
registered outputs; the browser's IDs, formats, sizes and completeness matched
the installed authenticated API. The report and run-manifest assets were
confirmed by the existing delivery result. This is UI/API readback of previous
real publication, not a new cloud report read.

The probe saved and hashed 18 screenshots: primary and parallel in dark, light
and reduced-motion settings at 1600×1120, 1280×800 and 2560×1440. Pixel sizes
matched the requested viewports. All 18 were visually reviewed in contact
sheets, with representative full-size dark and light screenshots inspected:
project/workspace identity, selected branch, outcome badges, controls and
readable content remained in their intended layout; shorter viewports scroll
vertically. The probe reported zero page errors, off-origin requests or blocked
mutations. A second installed, read-only keyboard check in `installed-0.2.6-keyboard.json`
selected the exact workspace with Enter, opened the palette with Ctrl+K,
returned focus on Escape, toggled theme by keyboard and found no horizontal
overflow at the three target widths. No product command was sent.

No registered application-log/v1, screenshot image, SVG or hostile JUnit marker
existed in these two runs. The installed read model therefore does not claim
real-data coverage for those optional formats; the 0.2.6 source/fake browser
checks in `ui-evidence-implementation.md` cover their parser and inert render
behavior. The first installed probe attempt under
`installed-ui-026-20260928T1753Z/report.json` failed because the test expected
the raw publication enum while the product correctly displayed its Russian
label. Only the probe expectation was corrected, then the same existing runs
passed; the product code and provider state were not changed.
