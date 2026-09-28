#!/usr/bin/env python3
"""One explicit second workspace in the disposable deployed UI project.

The default invocation is inert. --apply logs into the owner-only loopback UI,
checks the exact ready project and primary HEAD, arms for one request, submits
CREATE_WORKSPACE through the real form, checks two independent registrations,
and disarms even on failure. No token or provider response body enters evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deployed_ui_acceptance import (
    BASE_URL,
    GateError,
    _arm_in_ui,
    _choose,
    _csrf,
    _disarm,
    _json_get,
    _login,
    _terminal_operation,
    _write_report,
)
from playwright.sync_api import APIRequestContext, BrowserContext, Page, sync_playwright

from runner_app.storage.paths import Paths

PROJECT_ID = "cdr-ep07-ui-20260928"
WORKSPACE_ID = "parallel"
WORKSPACE_NAME = "EP-07 parallel workspace"
BRANCH = "feature/ep07-parallel"
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
OUT = Path(__file__).resolve().parent


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--bootstrap-file", type=Path,
                        default=Paths.discover().token_dir / "session-bootstrap.json")
    parser.add_argument("--profile-id")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 30 <= args.timeout_seconds <= 3600:
        parser.error("--timeout-seconds must be between 30 and 3600")
    for path in (args.bootstrap_file, args.output):
        if path is not None and (not path.is_absolute() or ".." in path.parts):
            parser.error("paths must be absolute without '..'")
    return args


def _preflight(request: APIRequestContext) -> tuple[str, str]:
    project = _json_get(request, f"/api/v1/projects/{PROJECT_ID}",
                        "PROJECT_READ_UNAVAILABLE")
    if project.get("project_id") != PROJECT_ID or project.get("state") != "READY":
        raise GateError("PROJECT_NOT_READY")
    workspaces = _json_get(request, f"/api/v1/projects/{PROJECT_ID}/workspaces",
                           "WORKSPACE_READ_UNAVAILABLE")
    items = workspaces.get("items")
    if not isinstance(items, list):
        raise GateError("WORKSPACE_READ_INVALID")
    if any(isinstance(item, dict) and item.get("workspace_id") == WORKSPACE_ID
           for item in items):
        raise GateError("WORKSPACE_ID_ALREADY_USED")
    primary = next((item for item in items if isinstance(item, dict)
                    and item.get("workspace_id") == "primary"), None)
    if not isinstance(primary, dict) or primary.get("state") != "ACTIVE":
        raise GateError("PRIMARY_WORKSPACE_UNAVAILABLE")
    metadata = primary.get("metadata")
    head = metadata.get("head") if isinstance(metadata, dict) else None
    branch = primary.get("branch")
    if not isinstance(head, str) or SHA.fullmatch(head) is None:
        raise GateError("PRIMARY_HEAD_INVALID")
    if not isinstance(branch, str) or branch == BRANCH:
        raise GateError("BRANCH_ALREADY_USED")
    source = _json_get(
        request, f"/api/v1/projects/{PROJECT_ID}/workspaces/primary/source",
        "PRIMARY_SOURCE_UNAVAILABLE",
    )
    if (source.get("recorded_head") != head or source.get("observed_head") != head
            or source.get("matches_recorded") is not True or source.get("dirty") is not False):
        raise GateError("PRIMARY_SOURCE_CHANGED")
    cursor: str | None = None
    for _ in range(100):
        suffix = f"&cursor={cursor}" if cursor is not None else ""
        page = _json_get(request, f"/api/v1/operations?limit=100{suffix}",
                         "OPERATION_PREFLIGHT_UNAVAILABLE")
        rows = page.get("items")
        if not isinstance(rows, list):
            raise GateError("OPERATION_PREFLIGHT_INVALID")
        if any(isinstance(row, dict) and row.get("action") == "CREATE_WORKSPACE"
               and row.get("project_id") == PROJECT_ID
               and row.get("workspace_id") == WORKSPACE_ID for row in rows):
            raise GateError("WORKSPACE_ID_ALREADY_USED")
        cursor = page.get("next_cursor")
        if cursor is None:
            return head, branch
        if not isinstance(cursor, str) or not cursor.isdecimal():
            raise GateError("OPERATION_PREFLIGHT_INVALID")
    raise GateError("OPERATION_HISTORY_TOO_LARGE")


def _create_in_ui(page: Page, base_commit: str, profile_id: str) -> str:
    project_button = page.locator(
        f'[data-testid="project-list"] [data-action="project"][data-value="{PROJECT_ID}"]'
    )
    project_button.wait_for(state="visible", timeout=20_000)
    project_button.click()
    page.locator('[data-testid="workspace-list"] [data-action="workspace"]'
                 '[data-value="primary"]').wait_for(state="visible", timeout=20_000)
    page.locator('[data-action="new-workspace"]').click()
    form = page.locator("#workspace-form")
    form.locator('[name="name"]').fill(WORKSPACE_NAME)
    form.locator('[name="id"]').fill(WORKSPACE_ID)
    form.locator('[name="branch"]').fill(BRANCH)
    form.locator('[name="baseCommit"]').fill(base_commit)
    form.locator('[name="profileId"]').select_option(profile_id)
    with page.expect_response(
        lambda response: response.url == BASE_URL + "/api/v1/operations"
        and response.request.method == "POST", timeout=20_000,
    ) as submitted:
        form.locator('button[type="submit"]').click()
    response = submitted.value
    if response.status != 202:
        raise GateError("CREATE_WORKSPACE_NOT_ADMITTED")
    try:
        receipt = response.json()
        request_id = receipt.get("request_id")
        if (not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id
                or receipt.get("status") not in {"ACCEPTED", "RUNNING"}):
            raise ValueError
    except (ValueError, TypeError, AttributeError) as error:
        raise GateError("WORKSPACE_RECEIPT_INVALID") from error
    return request_id


def _verify(request: APIRequestContext, page: Page, operation: dict[str, Any],
            base_commit: str, primary_branch: str) -> dict[str, Any]:
    result = operation.get("result")
    drive = result.get("drive") if isinstance(result, dict) else None
    if (operation.get("action") != "CREATE_WORKSPACE"
            or operation.get("workspace_id") != WORKSPACE_ID
            or operation.get("status") != "COMPLETED" or operation.get("code") is not None
            or not isinstance(result, dict) or result.get("workspace_state") != "ACTIVE"
            or result.get("branch") != BRANCH or result.get("base_commit") != base_commit
            or not isinstance(drive, dict)
            or not isinstance(drive.get("workspace_folder_id"), str)
            or not drive["workspace_folder_id"]):
        raise GateError("WORKSPACE_NOT_COMPLETED")
    workspaces = _json_get(request, f"/api/v1/projects/{PROJECT_ID}/workspaces",
                           "WORKSPACE_READ_UNAVAILABLE")
    rows = workspaces.get("items")
    if not isinstance(rows, list):
        raise GateError("WORKSPACE_READ_INVALID")
    primary = next((row for row in rows if isinstance(row, dict)
                    and row.get("workspace_id") == "primary"), None)
    second = next((row for row in rows if isinstance(row, dict)
                   and row.get("workspace_id") == WORKSPACE_ID), None)
    if (not isinstance(primary, dict) or not isinstance(second, dict)
            or primary.get("state") != "ACTIVE" or second.get("state") != "ACTIVE"
            or primary.get("branch") != primary_branch or second.get("branch") != BRANCH
            or primary.get("branch") == second.get("branch")
            or second.get("metadata", {}).get("head") != base_commit):
        raise GateError("INDEPENDENT_WORKSPACES_UNPROVEN")
    source = _json_get(
        request, f"/api/v1/projects/{PROJECT_ID}/workspaces/{WORKSPACE_ID}/source",
        "SECOND_SOURCE_UNAVAILABLE",
    )
    if (source.get("recorded_head") != base_commit
            or source.get("observed_head") != base_commit
            or source.get("recorded_branch") != BRANCH
            or source.get("matches_recorded") is not True
            or source.get("dirty") is not False):
        raise GateError("SECOND_SOURCE_CHANGED")
    links = _json_get(
        request, f"/api/v1/projects/{PROJECT_ID}/workspaces/{WORKSPACE_ID}/drive-docs",
        "SECOND_DRIVE_REGISTRATION_UNAVAILABLE",
    )
    folder = links.get("workspace_folder")
    docs = links.get("documents")
    if (not isinstance(folder, dict)
            or folder.get("id") != drive["workspace_folder_id"]
            or not isinstance(docs, dict)
            or any(not isinstance(docs.get(role), dict)
                   or docs[role].get("id") != drive.get(key)
                   for role, key in (("command", "command_doc_id"),
                                     ("changeset", "changeset_doc_id"),
                                     ("receipt", "receipt_doc_id"),
                                     ("latest_result", "latest_result_doc_id"),
                                     ("source_context", "context_doc_id")))):
        raise GateError("SECOND_DRIVE_REGISTRATION_MISMATCH")
    page.reload(wait_until="domcontentloaded", timeout=20_000)
    page.locator('[data-testid="project-list"]').wait_for(state="visible", timeout=20_000)
    page.locator(
        f'[data-testid="project-list"] [data-action="project"][data-value="{PROJECT_ID}"]'
    ).click()
    page.locator('[data-testid="workspace-list"] [data-action="workspace"]'
                 f'[data-value="{WORKSPACE_ID}"]').wait_for(state="visible", timeout=20_000)
    return {"two_active_workspaces": True, "distinct_branches": True,
            "same_explicit_base": True, "second_drive_registered": True,
            "second_ui_visible": True}


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
            if "CREATE_WORKSPACE" not in system.get("capabilities", []):
                raise GateError("CREATE_WORKSPACE_UNAVAILABLE")
            approved = system.get("approved")
            if not isinstance(approved, dict):
                raise GateError("APPROVAL_POLICY_UNAVAILABLE")
            profile_id = _choose(approved.get("profiles"), args.profile_id, "PROFILE")
            report["profile_id"] = profile_id
            base_commit, primary_branch = _preflight(request)
            report["preflight"] = "PASS"
            armed_by_gate = True
            _arm_in_ui(page)
            report["armed_for_one_request"] = True
            current = _json_get(request, "/api/v1/system", "SYSTEM_READ_UNAVAILABLE")
            if current.get("armed") is not True:
                raise GateError("ARM_NOT_CONFIRMED")
            request_id = _create_in_ui(page, base_commit, profile_id)
            report["request_id"] = request_id
            report["request_submitted"] = True
            operation = _terminal_operation(request, request_id, args.timeout_seconds)
            report["operation_status"] = operation.get("status")
            report["operation_code"] = operation.get("code")
            report.update(_verify(request, page, operation, base_commit, primary_branch))
            report["result"] = "PASS"
        finally:
            if armed_by_gate:
                try:
                    report["disarmed_after"] = bool(csrf and _disarm(context.request, csrf))
                    report["final_armed"] = _json_get(
                        context.request, "/api/v1/system", "FINAL_SYSTEM_READ_UNAVAILABLE"
                    ).get("armed")
                except Exception:  # noqa: BLE001 -- keep cleanup evidence sanitized
                    report["disarmed_after"] = False
                    report["final_armed"] = "UNKNOWN"
                if report["disarmed_after"] is not True or report["final_armed"] is not False:
                    report["result"] = "FAIL"
                    report["failure_code"] = "DISARM_NOT_CONFIRMED"
            context.close()
            browser.close()


def main() -> int:
    args = _args()
    if not args.apply:
        print(json.dumps({"mode": "DRY_RUN", "cloud_writes": False,
                          "bootstrap_read": False, "project_id": PROJECT_ID,
                          "workspace_id": WORKSPACE_ID, "apply_required": True}, sort_keys=True))
        return 0
    report: dict[str, Any] = {
        "schema_version": "1.0", "packet": "EP-07",
        "mode": "LIVE_MULTIWORKSPACE_ACCEPTANCE",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "project_id": PROJECT_ID, "workspace_id": WORKSPACE_ID,
        "result": "FAIL", "request_submitted": False, "disarmed_after": None,
    }
    try:
        _apply(args, report)
    except GateError as error:
        report.setdefault("failure_code", error.code)
    except Exception as error:  # noqa: BLE001 -- no browser/provider text in evidence
        report.setdefault("failure_code", f"UNEXPECTED_{type(error).__name__}")
    if report["result"] == "PASS" and report.get("disarmed_after") is not True:
        report["result"] = "FAIL"
        report["failure_code"] = "DISARM_NOT_CONFIRMED"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = args.output or OUT / f"live-multiworkspace-acceptance-{stamp}.json"
    try:
        _write_report(path, report)
    except (OSError, GateError):
        print(json.dumps({"result": "FAIL", "failure_code": "EVIDENCE_WRITE_FAILED"}))
        return 1
    print(json.dumps({"result": report["result"],
                      "failure_code": report.get("failure_code"),
                      "project_id": PROJECT_ID, "workspace_id": WORKSPACE_ID,
                      "request_id": report.get("request_id"),
                      "evidence": str(path)}, sort_keys=True))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
