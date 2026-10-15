"""One local fixture-backed browser check of the additive v2 UI; no account writes."""
from __future__ import annotations

import argparse
import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / "runner_app/web"
OUT = Path(__file__).resolve().parent
DONE = "11111111-1111-4111-8111-111111111111"
QUEUED = "22222222-2222-4222-8222-222222222222"
UNKNOWN = "33333333-3333-4333-8333-333333333333"
INCOMPLETE = "44444444-4444-4444-8444-444444444444"
CONTROL = "55555555-5555-4555-8555-555555555555"
SESSION = "66666666-6666-4666-8666-666666666666"
STAMP = "2026-10-15T10:00:00Z"
LOCATOR = '/managed/runner-results/report-<img src=x onerror="window.__cdr_xss=1">.xml'


class Fixture:
    legacy = False
    failure = False
    calls: ClassVar[list[str]] = []
    mutations: ClassVar[list[str]] = []
    requests: ClassVar[list[dict[str, Any]]] = [
        {"request_id": QUEUED, "project_id": "example", "action": "RUN_TASK", "status": "QUEUED",
         "sequence": 2, "result": None, "updated_at": STAMP, "session_id": SESSION},
        {"request_id": UNKNOWN, "project_id": "example", "action": "RUN_TASK", "status": "ACK_UNKNOWN",
         "sequence": None, "code": "ACK_WRITE_UNKNOWN", "result": None, "updated_at": STAMP},
        {"request_id": DONE, "project_id": "example", "action": "RUN_TASK", "status": "COMPLETED",
         "sequence": 1, "updated_at": STAMP, "result": {
             "bundle_path": "/managed/runner-results/bundle", "bundle_sha256": "a" * 64,
             "branch": "feature/test", "delivery": {"status": "UNKNOWN", "code": "READBACK_UNAVAILABLE",
                 "commit": None, "local_available": True, "local_locator": "/managed/runner-results/bundle"},
             "manifest": {"protocol": "cdr-result/2", "evidence_level": "OBSERVED_RUN",
                 "operation_status": "COMPLETED", "source": {"branch": "feature/test", "commit": "b" * 40,
                     "workload_sha256": "c" * 64, "dirty": False},
                 "execution": {"outcome": "FAILED", "quality_gate": "FAILED", "cleanup": "CONFIRMED",
                               "exit_code": 1},
                 "environment": {"mode": "native", "execution_os": "linux", "execution_arch": "x86_64",
                                 "host_os": "linux", "host_arch": "x86_64", "profile_id": "python-native"},
                 "delivery": {"status": "LOCAL_ONLY", "reason": "PUBLICATION_PENDING"},
                 "artifacts": [{"id": "junit", "kind": "test_result", "format": "junit-xml", "bytes": 42,
                     "sha256": "d" * 64, "availability": "LOCAL_ONLY", "local_path": LOCATOR,
                     "remote_path": None, "complete": False, "reason": "LOCAL_CAPTURE_INCOMPLETE"}],
                 "warnings": ["Report warning <script>window.__cdr_xss=2</script>"]}}},
        {"request_id": INCOMPLETE, "project_id": "example", "action": "SHELL", "status": "COMPLETED",
         "sequence": 3, "updated_at": STAMP, "result": {"manifest": {"protocol": "cdr-result/2",
             "execution": {"outcome": "PASSED"}, "artifacts": []}}},
        {"request_id": CONTROL, "project_id": "example", "action": "STATUS", "status": "COMPLETED",
         "sequence": None, "updated_at": STAMP,
         "result": {"target_request_id": QUEUED, "status": "QUEUED", "sequence": 2, "code": None}},
    ]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def json(self, status, value):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(value).encode())

    def do_POST(self):
        Fixture.mutations.append(self.path)
        self.json(405, {"error": "READ_ONLY_CHECK"})

    def do_GET(self):
        path = urlsplit(self.path).path
        Fixture.calls.append(path)
        if path == "/api/v1/local/session":
            self.json(200, {"csrf_token": "fixture-session"})
        elif path == "/api/v1/system":
            self.json(200, {"runner_id": "runner-1", "epoch": SESSION, "armed": False,
                            "capabilities": [], "connections": {}, "max_parallel": 1,
                            "reserved_capacity": 0, "approved": {}})
        elif path in {"/api/v1/projects", "/api/v1/operations"}:
            self.json(200, {"items": [], "next_cursor": None})
        elif path == "/api/v2/status":
            self.json(404, {"error": "NOT_FOUND"}) if Fixture.legacy else self.json(200, {
                "protocol": "cdr-runtime/2", "runner_id": "runner-1", "session_id": SESSION,
                "armed": False, "configured": True, "channels": {"github-example": "CONFIGURED_NOT_OBSERVED"},
                "projects": ["example"], "queue_limit": 100, "native_profiles": ["python-native"],
                "capability_status": "IMPLEMENTED_NOT_QUALIFIED"})
        elif path == "/api/v2/requests":
            self.json(503, {"error": "FIXTURE_READ_FAILED"}) if Fixture.failure else self.json(
                200, {"requests": Fixture.requests})
        elif path.startswith("/api/v2/requests/"):
            request = next((item for item in Fixture.requests if item["request_id"] == path.rsplit("/", 1)[-1]), None)
            self.json(200, request) if request else self.json(404, {"error": "REQUEST_NOT_FOUND"})
        else:
            name = "index.html" if path == "/" else path.removeprefix("/assets/")
            if name != Path(name).name or not (WEB / name).is_file():
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
            self.end_headers()
            self.wfile.write((WEB / name).read_bytes())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", type=Path, help="Existing Chromium executable; never installs one")
    args = parser.parse_args()
    checks = []
    errors = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(args.browser) if args.browser else None,
                headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin, wait_until="networkidle")
            page.locator('[data-action="view"][data-value="v2"]').click()
            view = page.get_by_test_id("v2-view")
            assert "Реализовано, квалификация не завершена" in view.inner_text()
            assert view.locator('[data-action="v2-request"]').count() == 5
            checks.append("optional v2 status, qualification and queue")

            page.locator(f'[data-action="v2-request"][data-value="{DONE}"]').click()
            detail = page.get_by_test_id("v2-request-detail")
            for axis, expected in (("Операция", "Обслужена"), ("Исполнение", "Не пройдено"),
                                    ("Критерий качества", "Не пройдено"), ("Очистка", "Подтверждена"),
                                    ("Доставка сейчас", "Неизвестно")):
                assert expected in detail.locator(f'[data-axis="{axis}"]').inner_text()
            assert LOCATOR in detail.inner_text()
            assert detail.locator("a,img,script").count() == 0
            assert page.evaluate("window.__cdr_xss || null") is None
            checks.append("separate result axes, exact escaped local locator, no invented links")

            page.locator(f'[data-action="v2-request"][data-value="{INCOMPLETE}"]').click()
            for axis in ("Критерий качества", "Очистка", "Доставка сейчас"):
                assert "Нет данных" in detail.locator(f'[data-axis="{axis}"]').inner_text()
            checks.append("missing quality/cleanup/delivery never inherit process pass")
            page.locator(f'[data-action="v2-request"][data-value="{CONTROL}"]').click()
            assert "Целевой запрос" in detail.inner_text() and QUEUED in detail.inner_text()
            assert "Нет данных" in detail.locator('[data-axis="Исполнение"]').inner_text()
            checks.append("control target stays separate from execution result")

            page.locator(f'[data-action="v2-request"][data-value="{DONE}"]').click()
            Fixture.requests[2]["result"]["delivery"].update(status="DELIVERED", code="READBACK_CONFIRMED", commit="e" * 40)
            view.locator('[data-action="refresh"]').click()
            page.wait_for_function("document.querySelector('[data-axis=\"Доставка сейчас\"]')?.textContent.includes('Доставлено')")
            assert "Только локально" in detail.inner_text()
            assert any(path == f"/api/v2/requests/{DONE}" for path in Fixture.calls)
            checks.append("latest delivery refresh replaces frozen publication-pending status")
            page.screenshot(path=str(OUT / "v2-ui.png"), full_page=True)

            Fixture.failure = True
            view.locator('[data-action="refresh"]').click()
            page.get_by_test_id("v2-stale").wait_for()
            assert page.locator(".errorline").count() == 0
            assert page.locator("#toast-root").inner_text() == ""
            checks.append("v2 refresh failure retains marked-stale data without a v1 error banner")
            page.locator('[data-action="view"][data-value="system"]').click()
            assert page.locator('[data-action="arm"]').is_enabled()
            checks.append("existing authenticated arm control remains available for a v2-only runtime")
            page.close()

            Fixture.legacy = True
            prior_requests = Fixture.calls.count("/api/v2/requests")
            legacy = browser.new_page(viewport={"width": 1280, "height": 900})
            legacy.on("pageerror", lambda error: errors.append(str(error)))
            legacy.goto(origin, wait_until="networkidle")
            legacy.locator('[data-action="view"][data-value="work"]').wait_for()
            assert legacy.locator('[data-action="view"][data-value="v2"]').count() == 0
            assert legacy.locator(".errorline").count() == 0
            assert legacy.locator("#toast-root").inner_text() == ""
            assert Fixture.calls.count("/api/v2/requests") == prior_requests
            legacy.locator('[data-action="view"][data-value="system"]').click()
            assert legacy.locator('[data-action="arm"]').is_disabled()
            checks.append("legacy v2 404 is silent; v1 navigation and arm rules remain unchanged")
            assert not Fixture.mutations and not errors
            record = {"packet": "GP-07", "scope": "fixture-backed additive v2 UI", "status": "PASSED",
                      "base_commit": "ff82594",
                      "command": "python verification/implementation/GP-07/v2_ui_check.py --browser " + str(args.browser),
                      "browser": browser.version, "browser_path": str(args.browser) if args.browser else None,
                      "checks": checks, "browser_errors": errors,
                      "mutations": Fixture.mutations, "screenshot": "v2-ui.png",
                      "live_provider_or_service_qualification": "NOT_RUN",
                      "integration_dependency": "Root-owned runtime _WEB_ASSETS must allow v2.js"}
            (OUT / "v2-ui-check.json").write_text(json.dumps(record, indent=2) + "\n")
            browser.close()
            print(json.dumps({"status": "PASSED", "checks": len(checks), "browser_errors": len(errors)}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
