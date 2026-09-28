#!/usr/bin/env python3
"""One explicit, disposable CREATE_PROJECT through the deployed loopback UI.

Without --apply this script is inert: it does not open the browser, read the
bootstrap file, arm the runner, or contact either cloud provider. The live run
uses the real UI and its same-origin session, then reads the local API for
durable operation/project/workspace evidence. It never records credentials,
tokens, response bodies, browser storage, or provider URLs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import APIRequestContext, BrowserContext, Page, sync_playwright

from runner_app.storage.paths import Paths, open_private_file

BASE_URL = "http://127.0.0.1:8765"
PROJECT_ID = "cdr-ep07-ui-20260928"
PROJECT_NAME = "EP-07 UI acceptance 2026-09-28"
TERMINAL = frozenset({"COMPLETED", "REJECTED", "BLOCKED", "FAULT", "INTERRUPTED", "CANCELLED"})
OUT = Path(__file__).resolve().parent
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")


class GateError(Exception):
    """Safe, stable failure code only; no provider or browser payload."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="log in, arm, create one private project, then disarm")
    parser.add_argument("--bootstrap-file", type=Path,
                        default=Paths.discover().token_dir / "session-bootstrap.json",
                        help="owner-only one-time local session file")
    parser.add_argument("--template-id", help="exact template in authenticated /api/v1/system")
    parser.add_argument("--github-identity", help="exact identity in authenticated /api/v1/system")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--output", type=Path,
                        help="new JSON evidence file; existing files are never replaced")
    args = parser.parse_args()
    if not 30 <= args.timeout_seconds <= 3600:
        parser.error("--timeout-seconds must be between 30 and 3600")
    if not args.bootstrap_file.is_absolute() or ".." in args.bootstrap_file.parts:
        parser.error("--bootstrap-file must be an absolute path without '..'")
    for value in (args.template_id, args.github_identity):
        if value is not None and SLUG.fullmatch(value) is None:
            parser.error("template and identity must be valid exact slug IDs")
    if args.output is not None and (not args.output.is_absolute() or ".." in args.output.parts):
        parser.error("--output must be an absolute path without '..'")
    return args


def _read_bootstrap(path: Path) -> str:
    """Read the owner-only handoff once; never serialize or log its value."""
    descriptor = open_private_file(path)
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise GateError("BOOTSTRAP_FILE_OVERSIZE")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GateError("BOOTSTRAP_FILE_INVALID") from error
    if not isinstance(value, dict) or set(value) != {"bootstrap", "expires_in_seconds"}:
        raise GateError("BOOTSTRAP_FILE_INVALID")
    secret: object = value["bootstrap"]
    if not isinstance(secret, str) or not 1 <= len(secret) <= 128:
        raise GateError("BOOTSTRAP_FILE_INVALID")
    return secret


def _json_get(request: APIRequestContext, path: str, code: str) -> dict[str, Any]:
    response = request.get(path, timeout=10_000, fail_on_status_code=False)
    if response.status != 200:
        raise GateError(code)
    try:
        value = response.json()
    except ValueError as error:
        raise GateError(code) from error
    if not isinstance(value, dict):
        raise GateError(code)
    return value


def _choose(approved: Any, selected: str | None, kind: str) -> str:
    if not isinstance(approved, list) or not approved or not all(
        isinstance(value, str) and SLUG.fullmatch(value) for value in approved
    ):
        raise GateError(f"{kind}_POLICY_UNAVAILABLE")
    if selected is not None:
        if selected not in approved:
            raise GateError(f"{kind}_NOT_APPROVED")
        return selected
    if len(approved) != 1:
        raise GateError(f"{kind}_SELECTION_REQUIRED")
    return str(approved[0])


def _assert_unused(request: APIRequestContext) -> None:
    projects = _json_get(request, "/api/v1/projects", "PROJECT_PREFLIGHT_UNAVAILABLE")
    items = projects.get("items")
    if not isinstance(items, list):
        raise GateError("PROJECT_PREFLIGHT_INVALID")
    if any(isinstance(item, dict) and item.get("project_id") == PROJECT_ID for item in items):
        raise GateError("PROJECT_ID_ALREADY_USED")
    cursor: str | None = None
    for _ in range(100):
        suffix = f"&cursor={cursor}" if cursor is not None else ""
        page = _json_get(request, f"/api/v1/operations?limit=100{suffix}",
                         "OPERATION_PREFLIGHT_UNAVAILABLE")
        rows = page.get("items")
        if not isinstance(rows, list):
            raise GateError("OPERATION_PREFLIGHT_INVALID")
        if any(isinstance(row, dict) and row.get("project_id") == PROJECT_ID
               and row.get("action") == "CREATE_PROJECT" for row in rows):
            raise GateError("PROJECT_ID_ALREADY_USED")
        next_cursor = page.get("next_cursor")
        if next_cursor is None:
            return
        if not isinstance(next_cursor, str) or not next_cursor.isdecimal():
            raise GateError("OPERATION_PREFLIGHT_INVALID")
        cursor = next_cursor
    raise GateError("OPERATION_HISTORY_TOO_LARGE")


def _csrf(request: APIRequestContext) -> str:
    session = _json_get(request, "/api/v1/local/session", "SESSION_UNAVAILABLE")
    token = session.get("csrf_token")
    if not isinstance(token, str) or not token:
        raise GateError("SESSION_UNAVAILABLE")
    return token


def _disarm(request: APIRequestContext, csrf: str) -> bool:
    response = request.post(
        "/api/v1/local/disarm", data="{}", timeout=10_000,
        headers={"Origin": BASE_URL, "Content-Type": "application/json",
                 "X-CSRF-Token": csrf}, fail_on_status_code=False,
    )
    if response.status != 200:
        return False
    try:
        value = response.json()
    except ValueError:
        return False
    return isinstance(value, dict) and value.get("armed") is False


def _login(page: Page, bootstrap_file: Path) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=15_000)
    page.locator("#bootstrap-input").wait_for(state="visible", timeout=15_000)
    secret = _read_bootstrap(bootstrap_file)
    try:
        page.locator("#bootstrap-input").fill(secret)
        page.locator("#login-form button[type=submit]").click()
    finally:
        del secret
    page.locator('[data-testid="project-list"]').wait_for(state="visible", timeout=20_000)


def _arm_in_ui(page: Page) -> None:
    page.locator('[data-action="view"][data-value="system"]').click()
    page.locator('[data-action="arm"]').click()
    page.locator('[data-action="disarm"]').wait_for(state="visible", timeout=20_000)


def _create_in_ui(page: Page, template_id: str, github_identity: str) -> str:
    page.locator('aside.sidebar [data-action="new-project"]').click()
    form = page.locator("#project-form")
    form.locator('[name="name"]').fill(PROJECT_NAME)
    form.locator('[name="id"]').fill(PROJECT_ID)
    form.locator('[name="sourceKind"]').select_option("new")
    form.locator('[name="templateId"]').select_option(template_id)
    form.locator('[name="githubIdentity"]').select_option(github_identity)
    form.locator('button[type="submit"]').click()
    confirm = page.locator('[data-action="project-confirm"]')
    confirm.wait_for(state="visible", timeout=10_000)
    preview = page.locator("#overlay-root")
    if (PROJECT_ID not in preview.inner_text() or template_id not in preview.inner_text()
            or github_identity not in preview.inner_text()):
        raise GateError("UI_PLAN_MISMATCH")
    with page.expect_response(
        lambda response: response.url == BASE_URL + "/api/v1/operations"
        and response.request.method == "POST", timeout=20_000,
    ) as submitted:
        confirm.click()
    response = submitted.value
    if response.status != 202:
        raise GateError("CREATE_NOT_ADMITTED")
    try:
        receipt = response.json()
        request_id = receipt.get("request_id")
        if (not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id
                or receipt.get("status") not in {"ACCEPTED", "RUNNING"}):
            raise ValueError("Invalid receipt")
    except (ValueError, AttributeError, TypeError) as error:
        raise GateError("CREATE_RECEIPT_INVALID") from error
    return request_id


def _terminal_operation(request: APIRequestContext, request_id: str,
                        timeout_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        operation = _json_get(request, f"/api/v1/operations/{request_id}",
                              "OPERATION_READ_UNAVAILABLE")
        if operation.get("request_id") != request_id or operation.get("project_id") != PROJECT_ID:
            raise GateError("OPERATION_IDENTITY_MISMATCH")
        if operation.get("status") in TERMINAL:
            return operation
        time.sleep(3)
    raise GateError("OPERATION_TIMEOUT")


def _verify_ready(request: APIRequestContext, page: Page,
                  operation: dict[str, Any]) -> dict[str, Any]:
    result = operation.get("result")
    github = result.get("github") if isinstance(result, dict) else None
    drive = result.get("drive") if isinstance(result, dict) else None
    if (operation.get("status") != "COMPLETED" or not isinstance(result, dict)
            or result.get("project_state") != "READY"):
        raise GateError("CREATE_NOT_COMPLETED")
    if not isinstance(github, dict) or github.get("private") is not True:
        raise GateError("PRIVATE_REPOSITORY_UNPROVEN")
    if (not isinstance(drive, dict)
            or not isinstance(drive.get("workspace_folder_id"), str)
            or not drive["workspace_folder_id"]):
        raise GateError("DRIVE_WORKSPACE_UNPROVEN")
    project = _json_get(request, f"/api/v1/projects/{PROJECT_ID}", "PROJECT_READ_UNAVAILABLE")
    if project.get("project_id") != PROJECT_ID or project.get("state") != "READY":
        raise GateError("PROJECT_NOT_READY")
    workspaces = _json_get(request, f"/api/v1/projects/{PROJECT_ID}/workspaces",
                           "WORKSPACE_READ_UNAVAILABLE")
    items = workspaces.get("items")
    if not isinstance(items, list):
        raise GateError("WORKSPACE_READ_INVALID")
    primary = next((row for row in items if isinstance(row, dict)
                    and row.get("workspace_id") == "primary"), None)
    if primary is None or primary.get("project_id") != PROJECT_ID or primary.get("state") != "ACTIVE":
        raise GateError("PRIMARY_WORKSPACE_NOT_REGISTERED")
    page.locator('[data-action="refresh"]').first.click()
    project_button = page.locator(
        f'[data-testid="project-list"] [data-action="project"][data-value="{PROJECT_ID}"]'
    )
    project_button.wait_for(state="visible", timeout=20_000)
    project_button.click()
    page.locator('[data-testid="workspace-list"] [data-action="workspace"]'
                 '[data-value="primary"]').wait_for(state="visible", timeout=20_000)
    return {"project_state": "READY", "workspace_id": "primary",
            "workspace_state": "ACTIVE", "repository_private": True,
            "drive_workspace_registered": True, "ui_workspace_visible": True}


def _apply(args: argparse.Namespace, report: dict[str, Any]) -> None:
    armed_by_gate = False
    csrf: str | None = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium"),
            headless=True, args=["--no-sandbox"],
        )
        context: BrowserContext = browser.new_context(base_url=BASE_URL)
        page = context.new_page()
        try:
            _login(page, args.bootstrap_file)
            report["ui_login"] = "PASS"
            request = context.request
            csrf = _csrf(request)
            system = _json_get(request, "/api/v1/system", "SYSTEM_READ_UNAVAILABLE")
            if system.get("armed") is not False:
                raise GateError("RUNNER_ALREADY_ARMED")
            if "CREATE_PROJECT" not in system.get("capabilities", []):
                raise GateError("CREATE_PROJECT_UNAVAILABLE")
            approved = system.get("approved")
            if not isinstance(approved, dict):
                raise GateError("APPROVAL_POLICY_UNAVAILABLE")
            template_id = _choose(approved.get("templates"), args.template_id, "TEMPLATE")
            github_identity = _choose(approved.get("github_identities"),
                                      args.github_identity, "GITHUB_IDENTITY")
            report["template_id"] = template_id
            report["github_identity"] = github_identity
            _assert_unused(request)
            report["preflight"] = "PASS"
            armed_by_gate = True  # The click may succeed before Playwright observes it.
            _arm_in_ui(page)
            report["armed_for_one_request"] = True
            current = _json_get(request, "/api/v1/system", "SYSTEM_READ_UNAVAILABLE")
            if current.get("armed") is not True:
                raise GateError("ARM_NOT_CONFIRMED")
            request_id = _create_in_ui(page, template_id, github_identity)
            report["request_id"] = request_id
            report["request_submitted"] = True
            operation = _terminal_operation(request, request_id, args.timeout_seconds)
            report["operation_status"] = operation.get("status")
            report["operation_code"] = operation.get("code")
            report.update(_verify_ready(request, page, operation))
            report["result"] = "PASS"
        finally:
            if armed_by_gate:
                try:
                    report["disarmed_after"] = bool(csrf and _disarm(context.request, csrf))
                    report["final_armed"] = _json_get(
                        context.request, "/api/v1/system", "FINAL_SYSTEM_READ_UNAVAILABLE"
                    ).get("armed")
                except Exception:  # noqa: BLE001 -- cleanup evidence must stay sanitized
                    report["disarmed_after"] = False
                    report["final_armed"] = "UNKNOWN"
                if report["disarmed_after"] is not True or report["final_armed"] is not False:
                    report["result"] = "FAIL"
                    report["failure_code"] = "DISARM_NOT_CONFIRMED"
            context.close()
            browser.close()


def _write_report(path: Path, report: dict[str, Any]) -> None:
    if not path.parent.is_dir():
        raise GateError("EVIDENCE_PARENT_MISSING")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def main() -> int:
    args = _parse_args()
    if not args.apply:
        print(json.dumps({"mode": "DRY_RUN", "cloud_writes": False,
                          "bootstrap_read": False, "project_id": PROJECT_ID,
                          "apply_required": True}, sort_keys=True))
        return 0
    report: dict[str, Any] = {
        "schema_version": "1.0", "packet": "EP-07", "mode": "LIVE_UI_ACCEPTANCE",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "project_id": PROJECT_ID, "source_kind": "new", "result": "FAIL",
        "request_submitted": False, "disarmed_after": None,
    }
    try:
        _apply(args, report)
    except GateError as error:
        report.setdefault("failure_code", error.code)
    except Exception as error:  # noqa: BLE001 -- never serialize provider/browser exception text
        report.setdefault("failure_code", f"UNEXPECTED_{type(error).__name__}")
    if report["result"] == "PASS" and report.get("disarmed_after") is not True:
        report["result"] = "FAIL"
        report["failure_code"] = "DISARM_NOT_CONFIRMED"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = args.output or OUT / f"live-ui-acceptance-{stamp}.json"
    try:
        _write_report(path, report)
    except (OSError, GateError):
        print(json.dumps({"result": "FAIL", "failure_code": "EVIDENCE_WRITE_FAILED"}))
        return 1
    print(json.dumps({"result": report["result"],
                      "failure_code": report.get("failure_code"),
                      "project_id": PROJECT_ID, "request_id": report.get("request_id"),
                      "evidence": str(path)}, sort_keys=True))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
