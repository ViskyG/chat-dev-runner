# EP-07 v1 evidence viewer — focused checkpoint

Base commit: `33d74fbbc19802d1adbb7be6f2331946d6d3aaae` in isolated `ep07/ui-evidence-v1` worktree. The installed 0.2.5 service, Drive installation, managed projects, `main`, v1 command/output/run schemas and SQLite schema were not changed. This source-only wheel was built but not installed.

The authenticated local `GET /api/v1/runs/{run_id}/outputs/{output_id}/inspection` reads only the exact registered, sealed, SHA-checked output, with a 2 MiB inspection ceiling. Existing JUnit parsing supplies case-derived counts and at most 50 failed-case traces; malformed or incomplete files show no counts. Existing application-log/v1 validation supplies at most 100 ERROR/FATAL events, with invalid and omitted lines disclosed. Complete registered PNG/JPEG/WebP screenshots receive preview metadata only when their actual headers, MIME type and dimensions agree; SVG stays escaped text and other formats keep raw download. The browser shows these facts on the existing Tests/Console/Files tabs, with separate operation, execution, publication and delivery statuses. Raster bytes remain behind the existing authenticated attachment endpoint and are rendered only as a bounded image Blob; the UI CSP permits `blob:` for images, not scripts. The page revokes its temporary object URL on close.

Focused source-freeze evidence:

| Boundary | Evidence | Result |
| --- | --- | --- |
| Parser/real SQLite and Git/registered API output/session/Host/CSP | `ui-evidence-final-tests.xml` | 14 passed; one existing Starlette/httpx deprecation warning |
| Packaged HTML/CSS/JS with fake local API in Chromium | `ui-evidence-browser.json`, `ui-evidence-raster-fake.png` | 10/10; no mutation request, provider write or page error |
| Static checks | Ruff on changed Python, mypy on changed runtime modules, `node --check` on changed JS, `git diff --check` | pass |
| Build artifact | `ui-evidence-wheel.json` | wheel built; five changed source/package members byte-identical |

The browser proof is fake-backed for the new presentation. It does not sign off the installed-service UI acceptance: once the host recovery gate is resolved, open the real passing and failing disposable runs in the installed UI and compare displayed axes, output counts, failed-case traces and optional raster preview to their registered API/manifest facts. Do not infer test totals from a failed exit code or from JUnit suite attributes. Only the current v1 formats above are parsed; unknown formats and files beyond the cap remain explicit raw evidence. No live Drive command or project execution was issued for this checkpoint.
