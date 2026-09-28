#!/usr/bin/env python3
"""Bounded owner-side observer for one chat-written Drive CREATE_PROJECT.

The default invocation is inert. ``arm`` and ``observe`` require ``--apply``.
The chat writes the registered Drive COMMAND separately, using the epoch
returned by ``arm``. No credential, token, cookie, or raw response is logged.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deployed_ui_acceptance import BASE_URL, GateError, _disarm, _json_get, _read_bootstrap
from playwright.sync_api import APIRequestContext, sync_playwright

from runner_app.storage.paths import Paths

PROJECT_ID = "cdr-ep07-drive-20260928"
TERMINAL = frozenset({"COMPLETED", "REJECTED", "BLOCKED", "FAULT", "INTERRUPTED", "CANCELLED"})


def _login(request: APIRequestContext, bootstrap_file: Path) -> str:
    secret = _read_bootstrap(bootstrap_file)
    try:
        response = request.post(
            "/api/v1/local/session", data=json.dumps({"bootstrap": secret}),
            headers={"Origin": BASE_URL, "Content-Type": "application/json"},
            timeout=10_000, fail_on_status_code=False,
        )
    finally:
        del secret
    if response.status != 200:
        raise GateError("SESSION_EXCHANGE_FAILED")
    try:
        csrf = response.json().get("csrf_token")
    except ValueError as error:
        raise GateError("SESSION_EXCHANGE_FAILED") from error
    if not isinstance(csrf, str) or not csrf:
        raise GateError("SESSION_EXCHANGE_FAILED")
    return csrf


def _arm(request: APIRequestContext, csrf: str) -> dict[str, Any]:
    system = _json_get(request, "/api/v1/system", "SYSTEM_READ_FAILED")
    if system.get("armed") is not False:
        raise GateError("RUNNER_ALREADY_ARMED")
    if "CREATE_PROJECT" not in system.get("capabilities", []):
        raise GateError("CREATE_PROJECT_UNAVAILABLE")
    approved = system.get("approved")
    if (not isinstance(approved, dict) or "python-basic" not in approved.get("templates", [])
            or "viskyg" not in approved.get("github_identities", [])):
        raise GateError("OWNER_POLICY_MISMATCH")
    projects = _json_get(request, "/api/v1/projects", "PROJECT_PREFLIGHT_FAILED")
    if any(isinstance(p, dict) and p.get("project_id") == PROJECT_ID
           for p in projects.get("items", [])):
        raise GateError("PROJECT_ID_ALREADY_USED")
    response = request.post(
        "/api/v1/local/arm", data="{}", timeout=10_000,
        headers={"Origin": BASE_URL, "Content-Type": "application/json",
                 "X-CSRF-Token": csrf}, fail_on_status_code=False,
    )
    if response.status != 200:
        raise GateError("ARM_FAILED")
    try:
        state = _json_get(request, "/api/v1/system", "SYSTEM_READ_FAILED")
        epoch = state.get("epoch")
        if state.get("armed") is not True or not isinstance(epoch, str) or not epoch:
            raise GateError("ARM_NOT_CONFIRMED")
    except BaseException:
        _disarm(request, csrf)
        raise
    return {"runner_id": state.get("runner_id"), "epoch": epoch,
            "armed": True, "project_id": PROJECT_ID}


def _observe(request: APIRequestContext, request_id: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = request.get(f"/api/v1/operations/{request_id}", timeout=10_000,
                               fail_on_status_code=False)
        if response.status == 404:
            time.sleep(3)
            continue
        if response.status != 200:
            raise GateError("OPERATION_READ_FAILED")
        try:
            operation = response.json()
        except ValueError as error:
            raise GateError("OPERATION_READ_FAILED") from error
        if not isinstance(operation, dict) or operation.get("request_id") != request_id:
            raise GateError("OPERATION_IDENTITY_MISMATCH")
        if operation.get("status") in TERMINAL:
            result = operation.get("result")
            github = result.get("github") if isinstance(result, dict) else None
            drive = result.get("drive") if isinstance(result, dict) else None
            project = _json_get(request, f"/api/v1/projects/{PROJECT_ID}",
                                "PROJECT_READ_FAILED")
            return {
                "request_id": request_id,
                "operation_status": operation.get("status"),
                "operation_code": operation.get("code"),
                "project_state": project.get("state"),
                "repository_private": isinstance(github, dict) and github.get("private") is True,
                "drive_workspace_registered": isinstance(drive, dict)
                and isinstance(drive.get("workspace_folder_id"), str),
            }
        time.sleep(3)
    raise GateError("OPERATION_TIMEOUT")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("arm", "observe"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--request-id")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"mode": "DRY_RUN", "cloud_writes": False,
                          "bootstrap_read": False, "project_id": PROJECT_ID}, sort_keys=True))
        return 0
    if args.mode == "observe" and not args.request_id:
        parser.error("observe requires --request-id")
    if not 30 <= args.timeout_seconds <= 3600:
        parser.error("timeout must be 30..3600 seconds")
    bootstrap_file = Paths.discover().token_dir / "session-bootstrap.json"
    report: dict[str, Any] = {"packet": "EP-07", "schema_version": "1.0",
                              "mode": args.mode.upper(), "project_id": PROJECT_ID,
                              "checked_at": datetime.now(timezone.utc).isoformat(),
                              "result": "FAIL"}
    with sync_playwright() as playwright:
        request = playwright.request.new_context(base_url=BASE_URL)
        csrf: str | None = None
        try:
            csrf = _login(request, bootstrap_file)
            if args.mode == "arm":
                report.update(_arm(request, csrf))
                report["result"] = "PASS"
            else:
                try:
                    report.update(_observe(request, args.request_id, args.timeout_seconds))
                    report["result"] = "PASS" if (
                        report["operation_status"] == "COMPLETED"
                        and report["project_state"] == "READY"
                        and report["repository_private"]
                        and report["drive_workspace_registered"]
                    ) else "FAIL"
                finally:
                    report["disarmed_after"] = _disarm(request, csrf)
                    report["final_armed"] = _json_get(
                        request, "/api/v1/system", "FINAL_SYSTEM_READ_FAILED"
                    ).get("armed")
                    if report["final_armed"] is not False:
                        report["result"] = "FAIL"
                        report["failure_code"] = "DISARM_NOT_CONFIRMED"
        except GateError as error:
            report["failure_code"] = error.code
        except Exception as error:  # noqa: BLE001 - no provider/session exception text in evidence
            report["failure_code"] = f"UNEXPECTED_{type(error).__name__}"
        finally:
            request.dispose()
    if args.output is not None:
        if not args.output.is_absolute() or ".." in args.output.parts or not args.output.parent.is_dir():
            parser.error("output must be a new absolute file in an existing directory")
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, sort_keys=True, indent=2)
            stream.write("\n")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
