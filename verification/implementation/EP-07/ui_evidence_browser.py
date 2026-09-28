#!/usr/bin/env python3
"""Focused packaged-browser slice for v1 evidence inspection; no provider or service."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Route, sync_playwright

from runner_app.outputs.inspection import inspect_registered_output

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
source = ROOT / "verification" / "implementation" / "EP-06" / "browser_e2e.py"
spec = importlib.util.spec_from_file_location("ep06_browser_fixture", source)
assert spec is not None and spec.loader is not None
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
RUN = fixture.RUN
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/d2kAAAAASUVORK5CYII="
)
JUNIT = (b'<testsuite tests="999"><testcase name="ok"/>'
         b'<testcase classname="demo" name="bad"><failure message="failed">'
         b'&lt;script&gt;window.__cdr_xss=1&lt;/script&gt;</failure></testcase></testsuite>')
LOG = (b'{"timestamp":"2026-09-28T00:00:00Z","level":"INFO","service":"demo",'
       b'"event":"start","message":"ordinary"}\n'
       b'{"timestamp":"2026-09-28T00:00:01Z","level":"ERROR","service":"demo",'
       b'"event":"test","message":"bad case"}\ninvalid\n')


def descriptor(output_id: str, kind: str, format_name: str, media_type: str, raw: bytes) -> dict:
    return {"output_id": output_id, "kind": kind, "format": format_name,
            "media_type": media_type, "relative_path": f"capture/{output_id}.bin",
            "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "complete": True, "truncated_reason": None, "publication": "local_only"}


def main() -> int:
    fake = fixture.FakeApi()
    samples = {
        "junit": (descriptor("junit", "test_result", "junit-xml", "application/xml", JUNIT), JUNIT),
        "app-log": (descriptor("app-log", "application_log", "application-log/v1", "application/x-ndjson", LOG), LOG),
        "raster": (descriptor("raster", "screenshot", "png", "image/png", PNG), PNG),
    }
    fake.run["outputs"].extend(output for output, _raw in samples.values())
    network: list[str] = []
    mutations: list[str] = []
    page_errors: list[str] = []

    def route(request_route: Route) -> None:
        request = request_route.request
        path = urlsplit(request.url).path
        if request.method != "GET":
            mutations.append(f"{request.method} {path}")
        prefix = f"/api/v1/runs/{RUN}/outputs/"
        if request.method == "GET" and path.startswith(prefix):
            suffix = path.removeprefix(prefix)
            output_id = suffix.removesuffix("/inspection")
            if output_id in samples and suffix in {output_id, output_id + "/inspection"}:
                output, raw = samples[output_id]
                if suffix.endswith("/inspection"):
                    request_route.fulfill(status=200, body=json.dumps(inspect_registered_output(output, raw)),
                                          content_type="application/json")
                else:
                    request_route.fulfill(status=200, body=raw, content_type="application/octet-stream",
                                          headers={"X-Content-Type-Options": "nosniff"})
                return
        fake.route(request_route)

    server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.WebHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    checks: dict[str, bool] = {}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium"),
                headless=True, args=["--no-sandbox"],
            )
            page = browser.new_page(viewport={"width": 1600, "height": 1120})
            page.route("**/api/v1/**", route)
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on("request", lambda request: network.append(request.url))
            page.goto(origin, wait_until="networkidle")
            page.get_by_test_id("workspace-list").wait_for(timeout=5000)
            page.locator('[data-action="workspace"][data-value="rc-dashboard"]').click()
            checks["project_failure_not_runner_fault"] = (
                "Проверка кода проекта не прошла" in page.get_by_test_id("app").inner_text()
                and "Локальный runner · приём выключен" in page.get_by_test_id("connection-strip").inner_text()
                and "Сбой runner" not in page.get_by_test_id("connection-strip").inner_text()
            )
            page.locator('[data-action="tab"][data-value="tests"]').click()
            page.get_by_text("2 всего", exact=False).wait_for(timeout=5000)
            checks["real_case_count_not_suite_claim"] = "999" not in page.locator("#run-panel").inner_text()
            details = page.locator("details.test-case")
            checks["failed_case_navigation"] = details.count() == 1 and "demo::bad" in details.inner_text()
            details.locator("summary").click()
            checks["trace_inert"] = (
                "<script>window.__cdr_xss=1</script>" in details.inner_text()
                and page.evaluate("window.__cdr_xss === undefined")
            )
            page.locator('[data-action="tab"][data-value="console"]').click()
            page.locator('[data-action="log-errors-only"]').click()
            page.get_by_text("bad case", exact=False).wait_for(timeout=5000)
            checks["structured_error_filter"] = (
                "bad case" in page.locator("#run-panel").inner_text()
                and "ordinary" not in page.locator("#run-panel").inner_text()
                and "Некорректных строк: 1" in page.locator("#run-panel").inner_text()
            )
            page.locator('[data-action="tab"][data-value="artifacts"]').click()
            page.locator('[data-action="output"][data-value="raster"]').click()
            page.get_by_test_id("image-preview").wait_for(timeout=5000)
            checks["raster_preview"] = page.get_by_test_id("image-preview").evaluate(
                "node => node.complete && node.naturalWidth === 1 && node.naturalHeight === 1"
            )
            page.screenshot(path=str(OUT / "ui-evidence-raster-fake.png"), full_page=True)
            page.keyboard.press("Escape")
            page.locator('[data-action="output"][data-value="unsafe-svg"]').click()
            page.get_by_test_id("output-preview").wait_for(timeout=5000)
            page.wait_for_function("document.querySelector('[data-testid=output-preview]')?.textContent.includes('<svg')", timeout=5000)
            checks["svg_remains_inert"] = (
                page.get_by_test_id("image-preview").count() == 0
                and "<svg" in page.get_by_test_id("output-preview").inner_text()
                and page.evaluate("window.__cdr_xss === undefined")
            )
            checks["no_page_errors"] = not page_errors
            checks["loopback_only"] = all(url.startswith(origin) for url in network)
            checks["no_mutation_requests"] = not mutations
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    report = {"schema_version": "1.0", "packet": "EP-07", "mode": "PACKAGED_UI_FAKE_API_FOCUSED",
              "base_commit": "33d74fbbc19802d1adbb7be6f2331946d6d3aaae",
              "checks": checks, "page_errors": page_errors,
              "mutation_requests": mutations, "provider_writes": 0, "service_started": False}
    (OUT / "ui-evidence-browser.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if not all(checks.values()):
        raise AssertionError({key: value for key, value in checks.items() if not value})
    print(f"{len(checks)}/{len(checks)} focused browser checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
