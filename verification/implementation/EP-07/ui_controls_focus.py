#!/usr/bin/env python3
"""Focused fake-backed browser check for EP-07 disabled reason and keyboard tabs."""
from __future__ import annotations

import hashlib
import json
import os
import runpy
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).with_name("ui-controls-focus-20260929.json")
prior = runpy.run_path(str(ROOT / "verification/implementation/EP-06/browser_e2e.py"))
FakeApi = prior["FakeApi"]
WebHandler = prior["WebHandler"]
RUN = prior["RUN"]
STAMP = prior["STAMP"]
REASON = "GitHub-импорт недоступен: нет утверждённых репозиториев в локальной политике."


class FocusFake(FakeApi):  # type: ignore[misc, valid-type]
    def __init__(self) -> None:
        super().__init__(armed=True)
        self.run["outputs"].extend([
            {"output_id": "kbd-log", "kind": "application_log", "format": "application-log/v1",
             "media_type": "application/x-ndjson", "relative_path": "capture/kbd-log.jsonl",
             "size_bytes": 20, "sha256": "1" * 64, "complete": True,
             "truncated_reason": None, "publication": "local_only"},
            {"output_id": "kbd-junit", "kind": "test_result", "format": "junit-xml",
             "media_type": "application/xml", "relative_path": "capture/kbd-junit.xml",
             "size_bytes": 20, "sha256": "2" * 64, "complete": True,
             "truncated_reason": None, "publication": "local_only"},
        ])

    @property
    def system(self) -> dict[str, object]:
        value = super().system
        value["approved"]["github_repositories"] = []
        return value

    def _response(self, method: str, path: str) -> tuple[int, dict[str, object] | bytes]:
        if method == "GET" and path == f"/api/v1/runs/{RUN}/outputs/kbd-log/inspection":
            return 200, {
                "output_id": "kbd-log", "kind": "application_log",
                "format": "application-log/v1", "status": "PARSED", "findings": [],
                "summary": {"valid_events": 1, "invalid_lines": 0,
                            "levels": {"ERROR": 1}},
                "error_events": [{"timestamp": STAMP, "level": "ERROR",
                                  "service": "fixture", "event": "keyboard",
                                  "message": "keyboard log error", "message_truncated": False}],
                "omitted_error_events": 0,
            }
        if method == "GET" and path == f"/api/v1/runs/{RUN}/outputs/kbd-junit/inspection":
            return 200, {
                "output_id": "kbd-junit", "kind": "test_result",
                "format": "junit-xml", "status": "PARSED", "findings": [],
                "summary": {"tests": 1, "passed": 0, "failed": 1,
                            "errors": 0, "skipped": 0},
                "failed_cases": [{"id": "keyboard::case", "outcome": "FAILURE",
                                  "message": "keyboard case failed", "trace": "trace"}],
                "omitted_failed_cases": 0,
            }
        return super()._response(method, path)


def run() -> int:
    fake = FocusFake()
    errors: list[str] = []
    network: list[str] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), WebHandler)
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
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.route("**/api/v1/**", fake.route)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: network.append(request.url))
                page.goto(origin, wait_until="networkidle")
                page.get_by_test_id("workspace-list").wait_for(timeout=5000)
                page.locator('[data-action="new-project"]').first.click()
                dialog = page.get_by_test_id("dialog")
                option = dialog.locator('option[value="github"]')
                checks["github_option_disabled"] = option.get_attribute("disabled") is not None
                checks["github_option_exact_reason"] = (
                    option.get_attribute("title") == REASON
                    and option.get_attribute("aria-description") == REASON
                    and dialog.locator(".disabled-reason").get_by_text(REASON).is_visible()
                )
                page.keyboard.press("Escape")
                page.locator('[data-action="tab"][data-value="summary"]').focus()
                page.keyboard.press("ArrowRight")
                page.locator('[data-action="tab"][data-value="console"][aria-selected="true"]').wait_for()
                page.locator('[data-action="log-errors-only"]').click()
                page.get_by_text("keyboard log error").wait_for(timeout=5000)
                checks["keyboard_console_inspection"] = any(
                    item == {"method": "GET", "path": f"/api/v1/runs/{RUN}/outputs/kbd-log/inspection"}
                    for item in fake.requests)
                page.locator('[data-action="tab"][data-value="console"]').focus()
                page.keyboard.press("ArrowRight")
                page.locator('[data-action="tab"][data-value="tests"][aria-selected="true"]').wait_for()
                page.get_by_text("FAILURE · keyboard::case").wait_for(timeout=5000)
                checks["keyboard_junit_inspection"] = any(
                    item == {"method": "GET", "path": f"/api/v1/runs/{RUN}/outputs/kbd-junit/inspection"}
                    for item in fake.requests)
                page.locator('[data-action="tab"][data-value="tests"]').focus()
                page.keyboard.press("End")
                checks["end_selects_artifacts"] = page.locator(
                    '[data-action="tab"][data-value="artifacts"][aria-selected="true"]').count() == 1
                page.locator('[data-action="tab"][data-value="artifacts"]').focus()
                page.keyboard.press("Home")
                checks["home_selects_summary"] = page.locator(
                    '[data-action="tab"][data-value="summary"][aria-selected="true"]').count() == 1
                checks["no_mutations"] = all(item["method"] == "GET" for item in fake.requests)
                checks["no_unmodelled_routes"] = not fake.unexpected
                checks["no_browser_errors"] = not errors
                checks["same_origin_only"] = all(url.startswith(origin + "/") for url in network)
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    result: dict[str, Any] = {
        "schema_version": "1.0", "kind": "EP-07 focused fake-backed UI control behavior",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_sha256": {name: hashlib.sha256((ROOT / "runner_app/web" / name).read_bytes()).hexdigest()
                          for name in ("app.js", "render.js")},
        "checks": checks, "passed": all(checks.values()),
        "api_request_count": len(fake.requests), "api_mutation_count": sum(
            item["method"] != "GET" for item in fake.requests),
        "service_or_provider_calls": False,
    }
    OUT.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"UI controls: {len(checks)} checks, passed={result['passed']}; {OUT}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(run())
