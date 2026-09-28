# EP-07 installed UI read-only probe

Source checkpoint: `a25be072b5f44d24ddbe4758fe6915c355d90e58`.
This is a prepared probe, **not** installed-service acceptance evidence yet. Run it only
after the owner-user service has been upgraded to wheel 0.2.6 and is listening
on `127.0.0.1:8765`. Its dry run does not contact the service or read the
bootstrap handoff:

```bash
cd /home/visky/Work/GPTRunner/chat-dev-runner
.venv/bin/python verification/implementation/EP-07/installed_ui_readonly_probe.py
```

For the live probe, choose a **new absolute** directory under
`verification/implementation/EP-07/` and use:

```bash
.venv/bin/python verification/implementation/EP-07/installed_ui_readonly_probe.py \
  --apply \
  --output-dir /home/visky/Work/GPTRunner/chat-dev-runner/verification/implementation/EP-07/installed-ui-026-UTC-STAMP
```

The installed runtime rotates the owner-only `session-bootstrap.json` handoff.
The probe reads it with the existing private-file guard and exchanges it once
for an ephemeral browser session. That auth POST consumes the one-time code;
all subsequent browser/API requests are GET or HEAD. The probe does not submit
operations, change arm/retention state, check providers, open an editor, or
issue Drive/GitHub writes. It blocks any other browser mutation and off-origin
request. The report never contains bootstrap values, cookies, response bodies,
raw output text or browser storage.

The two exact existing UI-project requests are `primary`
`d4e83e2e-cb8f-4768-84bf-13784aa9072f` (passed) and `parallel`
`cbc20739-bfe2-451e-9440-07118e08105a` (failed). The probe fetches each
operation, sealed run, registered output index and JUnit inspection from the
authenticated installed API. It requires the UI to select those exact latest
runs and show operation, execution, publication and delivery independently.
It compares displayed case-derived JUnit counts, failed-case navigation and
output kind/format/size/completeness to the API. It also requires the delivered
asset list to include `report.md` and `run-manifest.json`. This proves the
local UI's report status and metadata; it does not reread the cloud report.

The probe captures 18 viewport PNGs: two workspaces × dark/light/reduced-motion
× 1600×1120, 1280×800, 2560×1440. It checks each PNG's pixel dimensions and
SHA-256, and records them in `report.json`. Review the actual images for
clipping, focus/keyboard affordances and visual quality before checking the
EP-07 UI gate. If a registered application log, raster screenshot, SVG or
unsafe JUnit trace exists, its relevant presentation is checked. Otherwise
`optional_coverage` explicitly says `ABSENT`; the probe does not fabricate
one. The probe does not establish real boot behavior, a fresh chat-origin
request, editor launch or provider outage handling.
