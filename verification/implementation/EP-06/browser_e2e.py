#!/usr/bin/env python3
"""Fake-backed browser check of the packaged EP-06 UI.

The browser loads the real HTML/CSS/modules over a loopback HTTP origin. Only
``/api/v1/*`` is intercepted, with responses shaped like the existing local API.
There is no runner execution, credential use, GitHub or Drive access here.
The gated local-process vertical slice is verified separately.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from playwright.sync_api import Page, Route, sync_playwright

from runner_app.resources import load_schema

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / "runner_app" / "web"
OUT = Path(__file__).resolve().parent
STAMP = "2026-09-28T10:00:00+00:00"
RUN = "11111111-1111-4111-8111-111111111111"
VERIFY = RUN
PUBLISH = "22222222-2222-4222-8222-222222222222"
CREATE = "33333333-3333-4333-8333-333333333333"
PUBLISH_PR = "66666666-6666-4666-8666-666666666666"
CANCEL_TARGET = "77777777-7777-4777-8777-777777777777"
XSS = '<img src=x onerror="window.__cdr_xss=1">'
HTML_OUTPUT = '<h1>log</h1><script>window.__cdr_xss=1</script>' + ('\nretained log line' * 100)
SVG_OUTPUT = '<svg xmlns="http://www.w3.org/2000/svg" onload="window.__cdr_xss=1"></svg>'


def _project(project_id: str, name: str, state: str = "LOCAL_READY") -> dict[str, object]:
    return {
        "project_id": project_id, "display_name": name, "state": state,
        "metadata": {}, "created_at": STAMP, "updated_at": STAMP,
    }


def _workspace(project_id: str, workspace_id: str, name: str, branch: str,
               head: str, generation: int = 0) -> dict[str, object]:
    return {
        "project_id": project_id, "workspace_id": workspace_id,
        "display_name": name, "branch": branch, "state": "ACTIVE",
        "metadata": {"head": head, "source_generation": generation,
                     "profile_id": "python-quick", "template_id": "python-basic"},
        "created_at": STAMP, "updated_at": STAMP,
    }


def _operation(request_id: str, action: str, status: str, project_id: str,
               workspace_id: str | None, *, result: dict[str, object] | None = None,
               delivery_status: str = "NOT_ATTEMPTED",
               effects: list[dict[str, object]] | None = None,
               steps: list[dict[str, object]] | None = None) -> dict[str, object]:
    command = {
        "schema_version": "1.0", "request_id": request_id, "dispatch": "READY",
        "runner_id": "local-runner", "epoch": "44444444-4444-4444-8444-444444444444",
        "issued_at": STAMP, "expires_at": "2026-09-28T10:10:00+00:00",
        "action": action, "project_id": project_id, "workspace_id": workspace_id,
        "expected_head": "a" * 40 if action != "CREATE_PROJECT" else None,
        "intent": "Explicit fixture operation",
        "params": ({"profile_id": "python-quick"} if action == "VERIFY" else
                   {"source_run_id": RUN, "publication": "draft_always"} if action == "PUBLISH" else
                   {"source_run_id": RUN} if action == "PUBLISH_REPORT" else
                   {"name": "Veiled Gold", "source": {"kind": "new"},
                    "template_id": "python-basic", "github_identity": "fixture",
                    "visibility": "private"}),
    }
    receipt = {
        "schema_version": "1.0", "request_id": request_id,
        "request_digest": "f" * 64, "status": status, "code": None,
        "message": "Состояние операции сохранено.", "observed_at": STAMP,
        "run_id": request_id if action in {"VERIFY", "DELIVER_VERIFY_PUBLISH"} else None,
    }
    return {
        "sequence": {VERIFY: 1, PUBLISH: 2, CREATE: 3, PUBLISH_PR: 4, CANCEL_TARGET: 5}[request_id],
        "request_id": request_id, "digest": "f" * 64,
        "source_id": "local-owner", "project_id": project_id,
        "workspace_id": workspace_id, "action": action, "status": status,
        "code": None, "received_at": STAMP, "updated_at": STAMP,
        "capacity_held": 0, "reconcile_required": 0,
        "delivery_status": delivery_status, "command": command,
        "receipt": receipt, "result": result, "effects": effects or [],
        "steps": steps or [],
    }


class FakeApi:
    """Stable fixture responses for the real v1 GET shape, with no fake mutation."""

    def __init__(self, *, armed: bool = False, authenticated: bool = True) -> None:
        self.requests: list[dict[str, str]] = []
        self.unexpected: list[str] = []
        self.authenticated = authenticated
        self.armed = armed
        self.reject_next_busy = False
        self.submitted_commands: list[dict[str, object]] = []
        self.import_paths: list[str] = []
        self.source_dirty = False
        self.source_diverged = False
        self.source_unavailable = False
        self.pinned = False
        self.expired_output_id: str | None = None
        self.project_rows = [
            _project("notes-lab", "Notes Lab"),
            _project("veiled-gold", "Veiled Gold"),
            _project("malicious-name", XSS),
            _project("new-provisioning", "New provisioning", "PROVISIONING"),
        ]
        self.workspace_rows = {
            "notes-lab": [
                _workspace("notes-lab", "rc-dashboard", "RC dashboard",
                           "feature/rc-dashboard", "b" * 40, generation=2),
                _workspace("notes-lab", "airflow-refactor", "Airflow refactor",
                           "feature/airflow-refactor", "c" * 40),
            ],
            "veiled-gold": [
                _workspace("veiled-gold", "ornament-motion", "Движение орнамента",
                           "feature/ornament-motion", "d" * 40),
            ],
            "malicious-name": [],
            "new-provisioning": [],
        }
        self.run = {
            "schema_version": "1.0", "run_id": RUN, "request_id": VERIFY,
            "project_id": "notes-lab", "workspace_id": "rc-dashboard",
            "snapshot": {"commit": "a" * 40, "tree": "e" * 40,
                         "source_dirty_at_capture": False},
            "operation_status": "COMPLETED",
            "execution": {"outcome": "FAILED", "exit_code": 1,
                          "duration_seconds": 2.5},
            "delivery": {"status": "PENDING", "reason": "DRIVE_UNAVAILABLE"},
            "outputs": [
                {"output_id": "stderr-raw", "kind": "console", "format": "raw-bytes-v1",
                 "media_type": "application/octet-stream", "relative_path": "capture/stderr.bin",
                 "size_bytes": len(XSS.encode()),
                 "sha256": hashlib.sha256(XSS.encode()).hexdigest(),
                 "complete": True, "truncated_reason": None, "publication": "local_only"},
                {"output_id": "unsafe-html", "kind": "report", "format": "html-v1",
                 "media_type": "text/html", "relative_path": "capture/report.html",
                 "size_bytes": len(HTML_OUTPUT.encode()),
                 "sha256": hashlib.sha256(HTML_OUTPUT.encode()).hexdigest(),
                 "complete": True, "truncated_reason": None, "publication": "local_only"},
                {"output_id": "unsafe-svg", "kind": "screenshot", "format": "svg-v1",
                 "media_type": "image/svg+xml", "relative_path": "capture/preview.svg",
                 "size_bytes": len(SVG_OUTPUT.encode()),
                 "sha256": hashlib.sha256(SVG_OUTPUT.encode()).hexdigest(),
                 "complete": True, "truncated_reason": None, "publication": "local_only"},
            ],
            "created_at": STAMP,
        }
        self.operations = [
            _operation(
                VERIFY, "VERIFY", "COMPLETED", "notes-lab", "rc-dashboard",
                result={"run_id": RUN,
                        "execution": {"outcome": "FAILED", "exit_code": 1},
                        "publication": {"status": "NOT_REQUESTED"},
                        "delivery": {"status": "PENDING", "reason": "DRIVE_UNAVAILABLE"}},
                delivery_status="PENDING",
                steps=[{"request_id": VERIFY, "step_id": "source-snapshot",
                        "last_event_type": "SNAPSHOT_RECORDED",
                        "created_at": STAMP, "updated_at": STAMP}],
            ),
            _operation(
                PUBLISH, "PUBLISH_REPORT", "BLOCKED", "notes-lab", "rc-dashboard",
                result={"run_id": RUN, "delivery": {"status": "FAILED",
                                                  "reason": "DRIVE_UNAVAILABLE"}},
                delivery_status="FAILED",
            ),
            _operation(
                CREATE, "CREATE_PROJECT", "INTERRUPTED", "veiled-gold", None,
                result={"execution": {"outcome": "NOT_RUN"},
                        "publication": {"status": "NOT_ATTEMPTED"},
                        "delivery": {"status": "NOT_ATTEMPTED"}},
                effects=[{"effect_id": "55555555-5555-4555-8555-555555555555",
                          "request_id": CREATE, "step_id": "github-repo",
                          "kind": "github_repository", "state": "CONFIRMED",
                          "provider_id": "123456", "intent": {},
                          "created_at": STAMP, "updated_at": STAMP}],
            ),
            _operation(
                PUBLISH_PR, "PUBLISH", "COMPLETED", "notes-lab", "rc-dashboard",
                result={"source_run_id": RUN, "tested_commit": "a" * 40,
                        "editor_head": "b" * 40, "editor_matches_tested": False,
                        "execution_outcome": "FAILED",
                        "publication": {"status": "DRAFT_PUBLISHED",
                                        "pull_request_id": "fixture-pr-17"}},
            ),
        ]

    @staticmethod
    def _unknown_check() -> dict[str, object]:
        return {"status": "UNKNOWN", "last_checked_at": None,
                "last_success_at": None, "code": None, "observation": None}

    @property
    def system(self) -> dict[str, object]:
        return {
            "schema_version": "1.0", "runner_id": "local-runner",
            "epoch": "44444444-4444-4444-8444-444444444444",
            "armed": self.armed, "started_at": STAMP, "updated_at": STAMP,
            "max_parallel": 2, "reserved_capacity": 0,
            "capabilities": ["VERIFY", "CREATE_PROJECT", "CREATE_WORKSPACE", "PUBLISH",
                             "PUBLISH_REPORT", "CANCEL_OPERATION", "FETCH",
                             "RETARGET_WORKSPACE", "EXPORT_CONTEXT"],
            "execution_queue": False,
            "approved": {"templates": ["python-basic"], "profiles": ["python-quick"],
                         "github_identities": ["fixture"],
                         "github_repositories": ["fixture/repo"]},
            "integrations": {
                "state": "PARTIALLY_CONFIGURED",
                "google": {"status": "UNKNOWN", "probe_performed": False},
                "github": {"status": "UNKNOWN", "probe_performed": False},
                "local_policy": {"approved_environment_templates": ["python-basic"]},
            },
            "connections": {
                "drive": self._unknown_check(), "github_api": self._unknown_check(),
                "git_fetch": self._unknown_check(), "git_remote_scope": "PER_WORKSPACE",
            },
        }

    def _retention(self) -> dict[str, object]:
        return {"run_id": RUN, "pinned": self.pinned,
                "outputs": [{"output_id": item["output_id"], "sha256": item["sha256"],
                             "size_bytes": item["size_bytes"],
                             "state": "EXPIRED" if item["output_id"] == self.expired_output_id else "AVAILABLE",
                             "tombstone_reason": "RETENTION_WINDOW" if item["output_id"] == self.expired_output_id else None,
                             "tombstone_at": STAMP if item["output_id"] == self.expired_output_id else None}
                            for item in self.run["outputs"]]}

    def _response(self, method: str, path: str) -> tuple[int, dict[str, object] | bytes]:
        if path == "/api/v1/health" and method == "GET":
            return 200, {"service": "chat-dev-runner", "version": "fixture",
                         "status": "PARTIALLY_CONFIGURED", "armed": False,
                         "capabilities": ["local_api"]}
        if path == "/api/v1/local/session" and method == "GET":
            return (200, {"csrf_token": "fixture-csrf", "expires_in": 28800}) if self.authenticated else (
                403, {"error": "SESSION_REQUIRED"})
        if path == "/api/v1/system" and method == "GET":
            return 200, self.system
        if path == "/api/v1/projects" and method == "GET":
            return 200, {"items": self.project_rows, "next_cursor": None}
        if path.startswith("/api/v1/projects/") and method == "GET":
            parts = path.split("/")
            project_id = parts[4] if len(parts) > 4 else ""
            if (len(parts) == 8 and parts[5] == "workspaces"
                    and parts[7] == "connections"
                    and any(w["workspace_id"] == parts[6]
                            for w in self.workspace_rows.get(project_id, []))):
                return 200, {"git_remote": {**self._unknown_check(),
                                            "workspace_snapshot_current": None}}
            if (len(parts) == 8 and parts[5] == "workspaces"
                    and parts[7] == "source"):
                if self.source_unavailable:
                    return 409, {"error": "WORKSPACE_UNAVAILABLE"}
                workspace = next((item for item in self.workspace_rows.get(project_id, [])
                                  if item["workspace_id"] == parts[6]), None)
                if workspace is None:
                    return 404, {"error": "SOURCE_NOT_FOUND"}
                head = workspace["metadata"]["head"]
                return 200, {"project_id": project_id, "workspace_id": parts[6],
                             "checked_at": datetime.now(timezone.utc).isoformat(),
                             "recorded_head": head, "recorded_branch": workspace["branch"],
                             "recorded_generation": workspace["metadata"]["source_generation"],
                             "observed_head": head, "observed_branch": workspace["branch"],
                             "dirty": self.source_dirty, "upstream": None,
                             "ahead": None, "behind": None,
                             "diverged": self.source_diverged, "matches_recorded": True}
            if (len(parts) == 8 and parts[5] == "workspaces"
                    and parts[7] == "drive-docs"):
                if not any(item["workspace_id"] == parts[6]
                           for item in self.workspace_rows.get(project_id, [])):
                    return 404, {"error": "WORKSPACE_NOT_FOUND"}
                document_id = "FixtureCommand123"
                return 200, {"project_id": project_id, "workspace_id": parts[6],
                             "checked_at": datetime.now(timezone.utc).isoformat(),
                             "workspace_folder": {"id": "FixtureFolder123",
                                                  "url": "https://drive.google.com/drive/folders/FixtureFolder123"},
                             "documents": {"command": {"id": document_id,
                                                       "url": f"https://docs.google.com/document/d/{document_id}/edit"}}}
            row = next((item for item in self.project_rows
                        if item["project_id"] == project_id), None)
            if len(parts) == 5:
                return (200, row) if row is not None else (404, {"error": "NOT_FOUND"})
            if len(parts) == 6 and parts[5] == "workspaces" and row is not None:
                return 200, {"items": self.workspace_rows[project_id], "next_cursor": None}
        if path == "/api/v1/operations" and method == "GET":
            return 200, {"items": self.operations, "next_cursor": None}
        if path.startswith("/api/v1/operations/") and method == "GET":
            operation = next((item for item in self.operations
                              if item["request_id"] == path.rsplit("/", 1)[-1]), None)
            return (200, operation) if operation is not None else (404, {"error": "NOT_FOUND"})
        if path == f"/api/v1/runs/{RUN}" and method == "GET":
            return 200, self.run
        if path == f"/api/v1/runs/{RUN}/retention" and method == "GET":
            return 200, self._retention()
        if path == f"/api/v1/runs/{RUN}/outputs" and method == "GET":
            return 200, {"items": self.run["outputs"], "next_cursor": None}
        if path == f"/api/v1/runs/{RUN}/outputs/stderr-raw" and method == "GET":
            return 200, XSS.encode()
        if path == f"/api/v1/runs/{RUN}/outputs/unsafe-html" and method == "GET":
            return 200, HTML_OUTPUT.encode()
        if path == f"/api/v1/runs/{RUN}/outputs/unsafe-svg" and method == "GET":
            return 200, SVG_OUTPUT.encode()
        self.unexpected.append(f"{method} {path}")
        return 404, {"error": "FIXTURE_ROUTE_MISSING"}

    def route(self, route: Route) -> None:
        request = route.request
        path = urlsplit(request.url).path
        self.requests.append({"method": request.method, "path": path})
        if request.method == "POST" and path == "/api/v1/local/session":
            submitted = request.post_data_json
            if submitted == {"bootstrap": "offline-browser-fixture"}:
                self.authenticated = True
                status, payload = 200, {"csrf_token": "fixture-csrf", "expires_in": 28800}
            else:
                status, payload = 403, {"error": "INVALID_BOOTSTRAP"}
        elif request.method == "POST" and path == "/api/v1/local/import-handles":
            submitted = request.post_data_json
            if isinstance(submitted, dict) and isinstance(submitted.get("path"), str):
                self.import_paths.append(submitted["path"])
                status, payload = 200, {"import_handle": "fixture-handle"}
            else:
                status, payload = 422, {"error": "INVALID_SCHEMA"}
        elif request.method == "POST" and path == f"/api/v1/runs/{RUN}/retention/pin":
            submitted = request.post_data_json
            if isinstance(submitted, dict) and set(submitted) == {"pinned"} and type(submitted["pinned"]) is bool:
                changed = self.pinned != submitted["pinned"]
                self.pinned = submitted["pinned"]
                status, payload = 200, {**self._retention(), "changed": changed}
            else:
                status, payload = 422, {"error": "INVALID_SCHEMA"}
        elif request.method == "POST" and path == f"/api/v1/runs/{RUN}/outputs/unsafe-html/expire":
            status, payload = (409, {"error": "RETENTION_NOT_DUE"}) if request.post_data_json == {} else (
                422, {"error": "INVALID_SCHEMA"})
        elif request.method == "POST" and path == "/api/v1/operations":
            submitted = request.post_data_json
            if isinstance(submitted, dict):
                Draft202012Validator(load_schema("command"),
                                     format_checker=FormatChecker()).validate(submitted)
                self.submitted_commands.append(submitted)
                status = 409 if self.reject_next_busy else 202
                self.reject_next_busy = False
                payload = {"schema_version": "1.0", "request_id": submitted["request_id"],
                           "request_digest": "f" * 64,
                           "status": "REJECTED" if status == 409 else "RUNNING",
                           "code": "BUSY" if status == 409 else None,
                           "message": "Рабочая область занята." if status == 409 else "Операция выполняется.",
                           "observed_at": STAMP, "run_id": None}
            else:
                status, payload = 422, {"error": "INVALID_SCHEMA"}
        else:
            status, payload = self._response(request.method, path)
        if isinstance(payload, bytes):
            route.fulfill(status=status, body=payload,
                          content_type="application/octet-stream",
                          headers={"X-Content-Type-Options": "nosniff",
                                   "Content-Security-Policy": "sandbox"})
        else:
            route.fulfill(status=status, body=json.dumps(payload, ensure_ascii=False),
                          content_type="application/json")


class WebHandler(BaseHTTPRequestHandler):
    """Static origin for the browser; API requests are intercepted by Playwright."""

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            file = WEB / "index.html"
        elif path.startswith("/assets/") and path.count("/") == 2:
            file = WEB / path.rsplit("/", 1)[-1]
        else:
            self.send_error(404)
            return
        if not file.is_file():
            self.send_error(404)
            return
        content = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(file.name)[0] or "text/plain")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        pass


def _record(checks: list[dict[str, object]], name: str, condition: bool,
            *, scenario: str) -> None:
    checks.append({"name": name, "scenario": scenario, "passed": condition})
    if not condition:
        raise AssertionError(name)


def _no_overflow(page: Page, width: int, height: int) -> bool:
    page.set_viewport_size({"width": width, "height": height})
    page.wait_for_timeout(100)
    return bool(page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))


def main() -> int:
    if not (WEB / "index.html").is_file():
        raise SystemExit("EP-06 web assets have not been built yet")
    OUT.mkdir(parents=True, exist_ok=True)
    fake = FakeApi()
    validator = lambda name: Draft202012Validator(load_schema(name), format_checker=FormatChecker())
    validator("run-manifest").validate(fake.run)
    for operation in fake.operations:
        validator("command").validate(operation["command"])
    checks: list[dict[str, object]] = []
    errors: list[str] = []
    network: list[str] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), WebHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    result: dict[str, object] = {}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium"),
                headless=True, args=["--no-sandbox"],
            )
            page = browser.new_page(viewport={"width": 1600, "height": 1120})
            page.route("**/api/v1/**", fake.route)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: network.append(request.url))
            try:
                page.goto(origin, wait_until="networkidle")
                page.get_by_test_id("project-list").wait_for(timeout=5000)
                _record(checks, "Authenticated read model renders projects",
                        page.get_by_test_id("project-list").get_by_text("Notes Lab").count() > 0,
                        scenario="SC-04")
                _record(checks, "Two independent workspaces visible",
                        all(page.get_by_text(name).count() > 0
                            for name in ("RC dashboard", "Airflow refactor")),
                        scenario="SC-04")
                _record(checks, "Provider checks remain individually unknown",
                        page.get_by_test_id("connection-strip").inner_text().count("Неизвестно") == 4,
                        scenario="SC-14")
                page.locator('[data-action="project"][data-value="new-provisioning"]').click()
                _record(checks, "Provisioning project is visibly incomplete",
                        "Создаётся" in page.locator(".projecthead").inner_text()
                        and "Областей пока нет" in page.get_by_test_id("workspace-list").inner_text(),
                        scenario="SC-02")
                page.screenshot(path=str(OUT / "fake-ui-provisioning.png"), full_page=True)
                page.locator('[data-action="project"][data-value="notes-lab"]').click()
                _record(checks, "No injected project markup executed",
                        page.evaluate("window.__cdr_xss === undefined") and
                        page.locator("img[onerror]").count() == 0,
                        scenario="SC-12")
                page.locator('[data-action="workspace"][data-value="airflow-refactor"]').focus()
                page.keyboard.press("Enter")
                _record(checks, "Keyboard selects exact workspace",
                        page.locator('[data-action="workspace"][data-value="airflow-refactor"]')
                        .get_attribute("aria-pressed") == "true", scenario="SC-04")
                page.locator('[data-action="workspace"][data-value="rc-dashboard"]').click()
                page.screenshot(path=str(OUT / "fake-ui-workspace.png"), full_page=True)
                _record(checks, "Unarmed command has a specific disabled reason",
                        page.locator('[data-action="verify"]').is_disabled() and
                        bool(page.locator('[data-action="verify"]').get_attribute("title")),
                        scenario="SC-10")
                for view, test_id in (("operations", "operation-list"),
                                      ("results", "result-list"),
                                      ("drive", "connection-strip"),
                                      ("system", "app"),
                                      ("work", "workspace-list")):
                    page.locator(f'[data-action="view"][data-value="{view}"]').first.click()
                    _record(checks, f"Navigate {view}",
                            page.get_by_test_id(test_id).count() > 0,
                            scenario="SC-14")
                    if view == "operations":
                        _record(checks, "Project failure and delivery are separate axes",
                                "Не пройдены" in page.get_by_test_id("operation-list").inner_text()
                                and "Ожидает доставки" in page.get_by_test_id("operation-list").inner_text(),
                                scenario="SC-06")
                        page.locator(f'[data-action="operation"][data-value="{VERIFY}"]').click()
                        _record(checks, "Only recorded operation steps are shown",
                                page.get_by_test_id("operation-detail").locator(".stage-list li").count() == 1,
                                scenario="SC-05")
                        page.locator(f'[data-action="operation"][data-value="{CREATE}"]').click()
                        _record(checks, "Interrupted create retains confirmed external effect",
                                "github_repository" in page.get_by_test_id("operation-detail").inner_text(),
                                scenario="SC-11")
                        _record(checks, "Create does not invent a test stage",
                                "Записанных шагов пока нет" in page.get_by_test_id("operation-detail").inner_text(),
                                scenario="SC-05")
                        page.screenshot(path=str(OUT / "fake-ui-interrupted.png"), full_page=True)
                        page.locator(f'[data-action="operation"][data-value="{PUBLISH_PR}"]').click()
                        _record(checks, "Publish-only operation does not invent executor stages",
                                "Записанных шагов пока нет" in page.get_by_test_id("operation-detail").inner_text(),
                                scenario="SC-05")
                        page.locator(f'[data-action="operation"][data-value="{PUBLISH}"]').click()
                        page.screenshot(path=str(OUT / "fake-ui-delivery-pending.png"), full_page=True)
                    if view == "results":
                        result_list = page.get_by_test_id("result-list")
                        _record(checks, "Result is tied to frozen snapshot and marked stale",
                                "a" * 40 in result_list.inner_text()
                                and "устарел" in result_list.inner_text(),
                                scenario="SC-08")
                        _record(checks, "Publication updates original result without duplicate card",
                                result_list.locator("section.card").count() == 1
                                and "Черновик PR создан" in result_list.inner_text()
                                and "Не пройдены" in result_list.inner_text(),
                                scenario="SC-09")
                page.locator('[data-action="workspace-drive"]').click()
                drive_dialog = page.get_by_test_id("dialog")
                drive_dialog.locator('a[href="https://docs.google.com/document/d/FixtureCommand123/edit"]').wait_for(timeout=5000)
                _record(checks, "Drive document links use exact registered IDs",
                        drive_dialog.locator('a[href="https://drive.google.com/drive/folders/FixtureFolder123"]').count() == 1
                        and drive_dialog.locator('a[href="https://docs.google.com/document/d/FixtureCommand123/edit"]').count() == 1
                        and drive_dialog.locator('a[href^="https://"]').count() == 2,
                        scenario="SC-13")
                page.keyboard.press("Escape")
                page.locator('[data-action="code-menu"]').click()
                code_dialog = page.get_by_test_id("dialog")
                _record(checks, "VS Code menu names exact registered workspaces",
                        code_dialog.locator('[data-action="open-editor"][data-value="rc-dashboard"]').count() == 1
                        and code_dialog.locator('[data-action="open-editor"][data-value="airflow-refactor"]').count() == 1,
                        scenario="SC-04")
                page.keyboard.press("Escape")
                page.locator('[data-action="tab"][data-value="artifacts"]').click()
                page.get_by_test_id("output-list").wait_for(timeout=5000)
                for output_id, attack in (("unsafe-html", "<script>"),
                                          ("unsafe-svg", "<svg")):
                    page.locator(f'[data-action="output"][data-value="{output_id}"]').click()
                    preview = page.get_by_test_id("output-preview")
                    preview.wait_for(timeout=5000)
                    page.wait_for_function("(value) => document.querySelector('[data-testid=output-preview]').textContent.includes(value)", arg=attack)
                    _record(checks, f"{output_id} is inert text",
                            attack in preview.inner_text() and
                            page.evaluate("window.__cdr_xss === undefined") and
                            page.locator("svg[onload], img[onerror], script:not([src])").count() == 0,
                            scenario="SC-12")
                    if output_id == "unsafe-html":
                        before = preview.evaluate("""el => {
                          el.scrollTop = 80;
                          const range = document.createRange();
                          range.setStart(el.firstChild, 0);
                          range.setEnd(el.firstChild, 8);
                          const selection = window.getSelection();
                          selection.removeAllRanges(); selection.addRange(range);
                          return {scroll: el.scrollTop, text: selection.toString()};
                        }""")
                        page.locator('[data-action="refresh"]').evaluate("el => el.click()")
                        page.wait_for_timeout(300)
                        after = preview.evaluate("el => ({scroll: el.scrollTop, text: window.getSelection().toString()})")
                        _record(checks, "Refresh preserves open preview scroll and selection",
                                before == after and before["scroll"] > 0 and bool(before["text"]),
                                scenario="SC-12")
                    page.keyboard.press("Escape")
                dark = page.locator("html").get_attribute("data-theme")
                page.get_by_test_id("theme-toggle").click()
                _record(checks, "Theme toggle changes document theme",
                        page.locator("html").get_attribute("data-theme") != dark,
                        scenario="SC-14")
                page.screenshot(path=str(OUT / "fake-ui-light.png"), full_page=True)
                page.get_by_test_id("theme-toggle").click()
                page.screenshot(path=str(OUT / "fake-ui-dark.png"), full_page=True)
                palette = page.get_by_test_id("palette-open")
                palette.focus()
                page.keyboard.press("Control+k")
                _record(checks, "Ctrl+K opens accessible command palette",
                        page.get_by_test_id("dialog").is_visible() and
                        page.evaluate("document.activeElement === document.querySelector(\"#palette-search\")"), scenario="SC-14")
                page.keyboard.press("Escape")
                _record(checks, "Escape closes palette and returns focus",
                        page.get_by_test_id("dialog").count() == 0 and page.evaluate("document.activeElement === document.querySelector(\"[data-testid=palette-open]\")"),
                        scenario="SC-14")
                for width, height in ((1280, 800), (1600, 1120),
                                      (2560, 1440), (390, 844)):
                    _record(checks, f"No horizontal overflow {width}px",
                            _no_overflow(page, width, height), scenario="SC-14")
                    page.screenshot(path=str(OUT / f"fake-ui-layout-{width}.png"), full_page=True)
                    if width == 390:
                        page.screenshot(path=str(OUT / "fake-ui-mobile-390.png"), full_page=True)
                page.set_viewport_size({"width": 1600, "height": 1120})
                page.emulate_media(reduced_motion="reduce")
                _record(checks, "Reduced-motion media query honoured",
                        bool(page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")),
                        scenario="SC-14")
                _record(checks, "No spontaneous mutation requests",
                        all(item["method"] == "GET" for item in fake.requests),
                        scenario="SC-10")
                _record(checks, "All network stays on loopback origin",
                        all(url.startswith(origin + "/") for url in network),
                        scenario="SC-01")
                _record(checks, "No browser script errors", not errors, scenario="SC-14")
                _record(checks, "No unmodelled API paths", not fake.unexpected,
                        scenario="SC-14")

                active = FakeApi(armed=True)
                active.reject_next_busy = True
                action_page = browser.new_page(viewport={"width": 1280, "height": 900})
                action_page.route("**/api/v1/**", active.route)
                action_page.on("pageerror", lambda error: errors.append(str(error)))
                action_page.on("request", lambda request: network.append(request.url))
                action_page.goto(origin, wait_until="networkidle")
                action_page.get_by_test_id("workspace-list").wait_for(timeout=5000)
                action_page.locator('[data-action="workspace-edit"]').click()
                editor = action_page.get_by_test_id("dialog")
                editor.locator('#retarget-form button[type="submit"]').wait_for(timeout=5000)
                _record(checks, "Clean registered source enables retarget",
                        "Чисто" in editor.inner_text()
                        and not editor.locator('#retarget-form button[type="submit"]').is_disabled(),
                        scenario="SC-07")
                editor.locator('[name="targetBranch"]').fill('feature/review-candidate')
                editor.locator('[name="targetCommit"]').fill('e' * 40)
                editor.locator('#retarget-form button[type="submit"]').click()
                action_page.get_by_test_id("dialog").get_by_role("alert").wait_for(timeout=5000)
                first = active.submitted_commands[-1]
                _record(checks, "BUSY is terminal with no implicit retry or queue",
                        len(active.submitted_commands) == 1
                        and first["action"] == "RETARGET_WORKSPACE"
                        and "BUSY" in action_page.get_by_test_id("dialog").inner_text()
                        and active.system["execution_queue"] is False,
                        scenario="SC-10")
                action_page.wait_for_timeout(250)
                _record(checks, "BUSY does not resend automatically",
                        len(active.submitted_commands) == 1, scenario="SC-10")
                action_page.locator('[name="targetBranch"]').fill('feature/review-candidate')
                action_page.locator('[name="targetCommit"]').fill('e' * 40)
                action_page.locator('#retarget-form button[type="submit"]').click()
                action_page.wait_for_function("() => document.querySelector('[data-testid=dialog]') === null", timeout=5000)
                second = active.submitted_commands[-1]
                _record(checks, "Explicit retarget retry uses new canonical request",
                        len(active.submitted_commands) == 2
                        and first["request_id"] != second["request_id"]
                        and second["project_id"] == "notes-lab"
                        and second["workspace_id"] == "rc-dashboard"
                        and second["expected_head"] == "b" * 40
                        and second["params"] == {"expected_branch": "feature/rc-dashboard",
                                                   "target_branch": "feature/review-candidate",
                                                   "target_commit": "e" * 40},
                        scenario="SC-07")
                _record(checks, "Retarget leaves old frozen run intact",
                        any(item["result"] and item["result"].get("run_id") == RUN
                            for item in active.operations), scenario="SC-07")
                action_page.locator('[data-action="view"][data-value="work"]').first.click()
                action_page.locator('[data-action="export-context"]').click()
                export_dialog = action_page.get_by_test_id("dialog")
                export_dialog.locator('[name="paths"]').fill('/etc/passwd')
                export_dialog.locator('#export-form button[type="submit"]').click()
                _record(checks, "Absolute context path refused before command",
                        len(active.submitted_commands) == 2
                        and export_dialog.get_by_role("alert").count() == 1,
                        scenario="SC-13")
                export_dialog.locator('[name="paths"]').fill('../outside.txt')
                export_dialog.locator('#export-form button[type="submit"]').click()
                _record(checks, "Traversal context path refused before command",
                        len(active.submitted_commands) == 2,
                        scenario="SC-13")
                export_dialog.locator('[name="paths"]').fill('README.md\nsrc/main.py')
                export_dialog.locator('[name="includeDiff"]').check()
                export_dialog.locator('#export-form button[type="submit"]').click()
                action_page.get_by_test_id("dialog").wait_for(state="detached", timeout=5000)
                context_command = active.submitted_commands[-1]
                _record(checks, "Scoped context export uses schema-valid relative paths",
                        len(active.submitted_commands) == 3
                        and context_command["action"] == "EXPORT_CONTEXT"
                        and context_command["project_id"] == "notes-lab"
                        and context_command["workspace_id"] == "rc-dashboard"
                        and context_command["params"] == {"paths": ["README.md", "src/main.py"],
                                                           "include_diff": True},
                        scenario="SC-13")
                action_page.locator('[data-action="view"][data-value="work"]').first.click()
                action_page.locator('[data-action="operation-modal"]').click()
                report_dialog = action_page.get_by_test_id("dialog")
                report_dialog.locator('[name="action"]').select_option('PUBLISH_REPORT')
                report_dialog.locator('#operation-form button[type="submit"]').click()
                report_dialog.wait_for(state="detached", timeout=5000)
                report_command = active.submitted_commands[-1]
                _record(checks, "Report retry references existing run and no expected HEAD",
                        len(active.submitted_commands) == 4
                        and report_command["action"] == "PUBLISH_REPORT"
                        and report_command["expected_head"] is None
                        and report_command["params"] == {"source_run_id": RUN},
                        scenario="SC-09")
                action_page.locator('[data-action="view"][data-value="work"]').first.click()
                action_page.locator('[data-action="new-project"]').first.click()
                project_dialog = action_page.get_by_test_id("dialog")
                _record(checks, "Project wizard offers only approved template and identity",
                        project_dialog.locator('[name="templateId"] option').all_text_contents() == ['python-basic']
                        and project_dialog.locator('[name="githubIdentity"] option').all_text_contents() == ['fixture']
                        and project_dialog.locator('[name="repository"] option').all_text_contents() == ['fixture/repo'],
                        scenario="SC-02")
                project_dialog.locator('[name="name"]').fill('Fresh Lab')
                project_dialog.locator('[name="id"]').fill('fresh-lab')
                project_dialog.locator('#project-form button[type="submit"]').click()
                _record(checks, "New project review states private scope and one operation",
                        'Репозиторий будет приватным' in project_dialog.inner_text()
                        and 'CREATE_PROJECT' in project_dialog.inner_text(),
                        scenario="SC-02")
                project_dialog.locator('[data-action="project-confirm"]').click()
                project_dialog.wait_for(state="detached", timeout=5000)
                create_command = active.submitted_commands[-1]
                _record(checks, "New project submits one private canonical command",
                        len(active.submitted_commands) == 5
                        and create_command["action"] == "CREATE_PROJECT"
                        and create_command["project_id"] == "fresh-lab"
                        and create_command["workspace_id"] is None
                        and create_command["params"] == {"name": "Fresh Lab", "source": {"kind": "new"},
                                                         "template_id": "python-basic",
                                                         "github_identity": "fixture", "visibility": "private"}
                        and len(active.run["outputs"]) == 3,
                        scenario="SC-02")
                action_page.locator('[data-action="view"][data-value="work"]').first.click()
                action_page.locator('[data-action="new-project"]').first.click()
                import_dialog = action_page.get_by_test_id("dialog")
                import_dialog.locator('[name="name"]').fill('Imported Lab')
                import_dialog.locator('[name="id"]').fill('imported-lab')
                import_dialog.locator('[name="sourceKind"]').select_option('local')
                import_dialog.locator('[name="localPath"]').fill('/tmp/fixture-existing-repo')
                import_dialog.locator('#project-form button[type="submit"]').click()
                import_dialog.locator('[data-action="project-confirm"]').click()
                import_dialog.wait_for(state="detached", timeout=5000)
                import_command = active.submitted_commands[-1]
                _record(checks, "Local import exchanges path for opaque handle",
                        active.import_paths == ['/tmp/fixture-existing-repo']
                        and import_command["action"] == "CREATE_PROJECT"
                        and import_command["params"]["source"] ==
                        {"kind": "local", "import_handle": "fixture-handle"}
                        and '/tmp/fixture-existing-repo' not in json.dumps(import_command),
                        scenario="SC-03")
                action_page.locator('[data-action="view"][data-value="work"]').first.click()
                action_page.locator('[data-action="new-project"]').first.click()
                github_dialog = action_page.get_by_test_id("dialog")
                github_dialog.locator('[name="name"]').fill('GitHub Import')
                github_dialog.locator('[name="id"]').fill('github-import')
                github_dialog.locator('[name="sourceKind"]').select_option('github')
                github_dialog.locator('[name="repository"]').select_option('fixture/repo')
                github_dialog.locator('#project-form button[type="submit"]').click()
                github_dialog.locator('[data-action="project-confirm"]').click()
                github_dialog.wait_for(state="detached", timeout=5000)
                github_command = active.submitted_commands[-1]
                _record(checks, "GitHub import uses approved repository only",
                        github_command["action"] == "CREATE_PROJECT"
                        and github_command["project_id"] == "github-import"
                        and github_command["params"]["source"] ==
                        {"kind": "github", "repository": "fixture/repo"},
                        scenario="SC-03")
                action_page.locator('[data-action="view"][data-value="work"]').first.click()
                action_page.locator('[data-action="new-workspace"]').click()
                workspace_dialog = action_page.get_by_test_id("dialog")
                workspace_dialog.locator('[name="name"]').fill('Parallel Design')
                workspace_dialog.locator('[name="id"]').fill('parallel-design')
                workspace_dialog.locator('[name="branch"]').fill('feature/parallel-design')
                workspace_dialog.locator('[name="baseCommit"]').fill('b' * 40)
                workspace_dialog.locator('#workspace-form button[type="submit"]').click()
                workspace_dialog.wait_for(state="detached", timeout=5000)
                workspace_command = active.submitted_commands[-1]
                _record(checks, "New workspace command binds exact branch and base",
                        workspace_command["action"] == "CREATE_WORKSPACE"
                        and workspace_command["project_id"] == "notes-lab"
                        and workspace_command["workspace_id"] == "parallel-design"
                        and workspace_command["params"] == {"name": "Parallel Design",
                                                          "branch": "feature/parallel-design",
                                                          "base_commit": "b" * 40,
                                                          "profile_id": "python-quick"},
                        scenario="SC-04")
                action_page.close()

                dirty = FakeApi(armed=True)
                dirty.source_dirty = True
                dirty_page = browser.new_page(viewport={"width": 1280, "height": 900})
                dirty_page.route("**/api/v1/**", dirty.route)
                dirty_page.on("pageerror", lambda error: errors.append(str(error)))
                dirty_page.on("request", lambda request: network.append(request.url))
                dirty_page.goto(origin, wait_until="networkidle")
                dirty_page.get_by_test_id("workspace-list").wait_for(timeout=5000)
                dirty_page.locator('[data-action="workspace-edit"]').click()
                dirty_editor = dirty_page.get_by_test_id("dialog")
                dirty_editor.locator('#retarget-form button[type="submit"]').wait_for(timeout=5000)
                _record(checks, "Dirty source refuses branch switch before POST",
                        dirty_editor.locator('#retarget-form button[type="submit"]').is_disabled()
                        and "Есть изменения" in dirty_editor.inner_text()
                        and len(dirty.submitted_commands) == 0,
                        scenario="SC-07")
                dirty_page.close()

                unauthenticated = FakeApi(authenticated=False)
                login_page = browser.new_page(viewport={"width": 1280, "height": 900})
                login_page.route("**/api/v1/**", unauthenticated.route)
                login_page.on("pageerror", lambda error: errors.append(str(error)))
                login_page.on("request", lambda request: network.append(request.url))
                login_page.goto(origin, wait_until="networkidle")
                _record(checks, "Unauthenticated owner sees local bootstrap login",
                        login_page.get_by_test_id("login").is_visible()
                        and login_page.get_by_test_id("project-list").count() == 0,
                        scenario="SC-01")
                login_page.get_by_test_id("bootstrap-input").fill("offline-browser-fixture")
                login_page.locator('#login-form button[type="submit"]').click()
                login_page.get_by_test_id("project-list").wait_for(timeout=5000)
                _record(checks, "Local session unlocks authenticated read model",
                        unauthenticated.authenticated
                        and login_page.get_by_test_id("project-list").get_by_text("Notes Lab").count() > 0
                        and [item["path"] for item in unauthenticated.requests
                             if item["method"] == "POST"] == ["/api/v1/local/session"],
                        scenario="SC-01")
                login_page.close()

                retention = FakeApi()
                retention_page = browser.new_page(viewport={"width": 1280, "height": 900})
                retention_page.route("**/api/v1/**", retention.route)
                retention_page.on("pageerror", lambda error: errors.append(str(error)))
                retention_page.on("request", lambda request: network.append(request.url))
                retention_page.goto(origin, wait_until="networkidle")
                retention_page.get_by_test_id("workspace-list").wait_for(timeout=5000)
                retention_page.locator(f'[data-action="retention"][data-run="{RUN}"]').first.click()
                retention_dialog = retention_page.get_by_test_id("dialog")
                retention_dialog.locator('[data-action="pin-run"]').wait_for(timeout=5000)
                _record(checks, "Retention dialog shows exact available output identities",
                        "не закреплён" in retention_dialog.inner_text()
                        and all(retention_dialog.get_by_text(output_id).count() > 0
                                for output_id in ("stderr-raw", "unsafe-html", "unsafe-svg"))
                        and not any(item["state"] == "EXPIRED"
                                    for item in retention._retention()["outputs"]),
                        scenario="SC-12")
                retention_dialog.locator('[data-action="pin-run"]').click()
                retention_page.wait_for_function("() => document.querySelector('[data-action=pin-run]').textContent.includes('Снять')", timeout=5000)
                _record(checks, "Owner pin blocks cleanup of retained evidence",
                        retention.pinned
                        and retention_dialog.locator('[data-action="expire-output"][data-value="unsafe-html"]').is_disabled(),
                        scenario="SC-12")
                retention_dialog.locator('[data-action="pin-run"]').click()
                retention_page.wait_for_function("() => document.querySelector('[data-action=pin-run]').textContent.includes('Закрепить')", timeout=5000)
                retention_dialog.locator('[data-action="expire-output"][data-value="unsafe-html"]').click()
                _record(checks, "Output cleanup requires exact-ID confirmation",
                        retention_dialog.locator('[data-action="expire-output-confirm"]').count() == 1
                        and not any(item["path"].endswith("/expire")
                                    for item in retention.requests if item["method"] == "POST"),
                        scenario="SC-12")
                retention_dialog.locator('[data-action="expire-output-confirm"]').click()
                retention_dialog.get_by_role("alert").wait_for(timeout=5000)
                _record(checks, "Age refusal leaves output available and no tombstone",
                        "RETENTION_NOT_DUE" in retention_dialog.inner_text()
                        and any(item["path"] == f"/api/v1/runs/{RUN}/outputs/unsafe-html/expire"
                                for item in retention.requests if item["method"] == "POST")
                        and all(item["state"] == "AVAILABLE"
                                for item in retention._retention()["outputs"]),
                        scenario="SC-12")
                retention.expired_output_id = 'unsafe-svg'
                retention_dialog.locator('[data-action="refresh-retention"]').click()
                retention_dialog.locator('[data-action="expire-output"][data-value="unsafe-svg"]').wait_for(timeout=5000)
                _record(checks, "Tombstoned output stays visible but cleanup is disabled",
                        "Удалён" in retention_dialog.inner_text()
                        and retention_dialog.locator('[data-action="expire-output"][data-value="unsafe-svg"]').is_disabled(),
                        scenario="SC-12")
                retention_page.keyboard.press("Escape")
                retention_page.locator('[data-action="tab"][data-value="artifacts"]').click()
                _record(checks, "Tombstoned output cannot be reopened as preview",
                        retention_page.locator('[data-action="output"][data-value="unsafe-svg"]').is_disabled(),
                        scenario="SC-12")
                retention_page.close()

                cancellation = FakeApi(armed=True)
                cancellation.operations.append(_operation(
                    CANCEL_TARGET, "VERIFY", "RUNNING", "notes-lab", "airflow-refactor",
                    steps=[{"request_id": CANCEL_TARGET, "step_id": "source-snapshot",
                            "last_event_type": "STARTED", "created_at": STAMP,
                            "updated_at": STAMP}],
                ))
                cancel_page = browser.new_page(viewport={"width": 1280, "height": 900})
                cancel_page.route("**/api/v1/**", cancellation.route)
                cancel_page.on("pageerror", lambda error: errors.append(str(error)))
                cancel_page.on("request", lambda request: network.append(request.url))
                cancel_page.goto(origin, wait_until="networkidle")
                cancel_page.get_by_test_id("workspace-list").wait_for(timeout=5000)
                cancel_page.locator('[data-action="view"][data-value="operations"]').first.click()
                cancel_page.locator(f'[data-action="operation"][data-value="{CANCEL_TARGET}"]').click()
                _record(checks, "Only running operation exposes cancellation",
                        cancel_page.get_by_test_id("operation-detail").locator('[data-action="cancel"]').is_enabled(),
                        scenario="SC-11")
                cancel_button = cancel_page.get_by_test_id("operation-detail").locator('[data-action="cancel"]')
                cancel_button.focus()
                _record(checks, "Cancellation control receives keyboard focus",
                        cancel_button.evaluate("element => document.activeElement === element"),
                        scenario="SC-11")
                cancel_page.keyboard.press("Enter")
                cancel_page.wait_for_function("() => document.querySelector('[data-testid=dialog]') === null", timeout=5000)
                cancel_command = cancellation.submitted_commands[-1]
                _record(checks, "Cancel submits separate exact-target command",
                        len(cancellation.submitted_commands) == 1
                        and cancel_command["action"] == "CANCEL_OPERATION"
                        and cancel_command["params"] == {"target_request_id": CANCEL_TARGET}
                        and cancel_command["expected_head"] is None
                        and any(effect["state"] == "CONFIRMED"
                                for operation in cancellation.operations for effect in operation["effects"]),
                        scenario="SC-11")
                cancel_page.close()
                _record(checks, "Every tested browser flow stayed on loopback origin",
                        all(url.startswith(origin + "/") for url in network), scenario="SC-01")
                _record(checks, "All fake API routes and scripts remained accounted for",
                        not errors and all(not fixture.unexpected for fixture in
                                           (fake, active, dirty, unauthenticated,
                                            retention, cancellation)), scenario="SC-14")
            except Exception:
                page.screenshot(path=str(OUT / "fake-ui-failure.png"), full_page=True)
                raise
            finally:
                result = {
                    "schema_version": "1.0", "packet": "EP-06",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "mode": "packaged-ui-plus-fake-v1-api",
                    "browser": browser.version,
                    "checks": checks, "page_errors": errors,
                    "api_requests": fake.requests,
                    "flow_requests": {name: fixture.requests for name, fixture in (
                        ("armed_commands", active), ("dirty_refusal", dirty),
                        ("owner_login", unauthenticated), ("retention", retention),
                        ("cancellation", cancellation))} if "cancellation" in locals() else {},
                    "submitted_action_sequence": [item["action"] for item in active.submitted_commands]
                    if "active" in locals() else [],
                    "unexpected_api": fake.unexpected,
                    "network_requests": network,
                    "live_external_actions": False,
                    "scenario_scope": ["SC-01", "SC-02", "SC-03", "SC-04", "SC-05", "SC-06", "SC-07", "SC-08", "SC-09", "SC-10", "SC-11", "SC-12", "SC-13", "SC-14"],
                }
                (OUT / "browser-e2e.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    print(f"EP-06 browser: {len(checks)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
