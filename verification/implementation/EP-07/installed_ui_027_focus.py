#!/usr/bin/env python3
"""Focused, auth-only installed UI readback for the 0.2.7 source freeze.

All browser requests are same-origin GET/HEAD except a single local session POST.
No command, Drive document, provider, project repository or retention is changed.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from installed_ui_readonly_probe import BASE_URL, EXISTING, PROJECT_ID, bootstrap, get_json
from playwright.sync_api import Route, sync_playwright

from runner_app.storage.paths import Paths

HERE = Path(__file__).parent
REPORT = HERE / "installed-ui-0.2.7.json"
IMPORT_REASON = "GitHub-импорт недоступен: нет утверждённых репозиториев в локальной политике."


def check(value: bool, code: str) -> None:
    if not value:
        raise RuntimeError(code)


def main() -> int:
    if os.environ.get("CDR_APPLY_INSTALLED_UI_027") != "1":
        print(json.dumps({"mode": "DRY_RUN", "service_contacted": False}))
        return 0
    report: dict[str, object] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "mode": "INSTALLED_027_UI_AUTH_ONLY_READBACK",
        "source_commit": "888b4ae",
        "installed_version": None,
        "auth_only_post": 0,
        "blocked_mutations": [],
        "off_origin_requests": [],
        "inspection_gets": [],
        "runs": [],
        "page_errors": 0,
        "result": "INCOMPLETE",
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium"),
                headless=True, args=["--no-sandbox"],
            )
            context = browser.new_context(base_url=BASE_URL, viewport={"width": 1600, "height": 1120})
            page = context.new_page()
            errors: list[bool] = []
            page.on("pageerror", lambda _error: errors.append(True))

            def guard(route: Route) -> None:
                url = urlsplit(route.request.url)
                if f"{url.scheme}://{url.netloc}" != BASE_URL:
                    report["off_origin_requests"].append(url.path)  # type: ignore[attr-defined]
                    route.abort()
                elif route.request.method in {"GET", "HEAD"}:
                    if url.path.endswith("/inspection"):
                        report["inspection_gets"].append(url.path)  # type: ignore[attr-defined]
                    route.continue_()
                elif (route.request.method == "POST" and url.path == "/api/v1/local/session"
                      and report["auth_only_post"] == 0):
                    report["auth_only_post"] = 1
                    route.continue_()
                else:
                    report["blocked_mutations"].append(  # type: ignore[attr-defined]
                        f"{route.request.method} {url.path}")
                    route.abort()

            page.route("**/*", guard)
            try:
                health = get_json(context.request, "/api/v1/health")
                check(health.get("service") == "chat-dev-runner"
                      and health.get("version") == "0.2.7", "INSTALLED_VERSION_MISMATCH")
                report["installed_version"] = health["version"]
                page.goto(BASE_URL, wait_until="domcontentloaded", timeout=15_000)
                page.locator("#bootstrap-input").wait_for(state="visible", timeout=15_000)
                secret = bootstrap(Paths.discover().token_dir / "session-bootstrap.json")
                try:
                    page.locator("#bootstrap-input").fill(secret)
                    page.locator('#login-form button[type="submit"]').click()
                finally:
                    del secret
                page.get_by_test_id("project-list").wait_for(state="visible", timeout=20_000)
                check(get_json(context.request, "/api/v1/local/session").get("expires_in", 0) > 0,
                      "SESSION_NOT_ACTIVE")
                system = get_json(context.request, "/api/v1/system")
                approved = system.get("approved") or {}
                check(isinstance(approved, dict)
                      and approved.get("github_repositories") == [], "IMPORT_POLICY_UNEXPECTED")

                page.locator('[data-action="new-project"]').first.click()
                dialog = page.get_by_test_id("dialog")
                dialog.wait_for(state="visible", timeout=10_000)
                option = dialog.locator('select[name="sourceKind"] option[value="github"]')
                check(option.get_attribute("disabled") is not None
                      and option.get_attribute("title") == IMPORT_REASON
                      and option.get_attribute("aria-description") == IMPORT_REASON
                      and dialog.locator(".disabled-reason").filter(has_text=IMPORT_REASON).count() == 1,
                      "GITHUB_IMPORT_REASON_MISSING")
                report["github_import"] = {
                    "approved_repository_count": 0,
                    "option_disabled": True,
                    "visible_reason": IMPORT_REASON,
                    "aria_reason": True,
                }
                page.keyboard.press("Escape")
                check(dialog.count() == 0, "PROJECT_DIALOG_NOT_CLOSED")

                report["stage"] = "select_project"
                page.locator(f'[data-testid="project-list"] [data-action="project"][data-value="{PROJECT_ID}"]').click()
                for workspace, (request_id, outcome) in EXISTING.items():
                    report["stage"] = f"select_{workspace}"
                    page.locator(f'[data-testid="workspace-list"] [data-action="workspace"][data-value="{workspace}"]').click()
                    panel = page.locator("#run-panel")
                    panel.wait_for(state="visible", timeout=15_000)
                    operation = get_json(context.request, f"/api/v1/operations/{request_id}")
                    run_id = operation.get("result", {}).get("run_id")
                    check(isinstance(run_id, str), "RUN_ID_MISSING")
                    run = get_json(context.request, f"/api/v1/runs/{run_id}")
                    outputs = get_json(context.request, f"/api/v1/runs/{run_id}/outputs").get("items")
                    check(run.get("execution", {}).get("outcome") == outcome
                          and operation.get("status") == "COMPLETED"
                          and isinstance(outputs, list), "REGISTERED_RUN_MISMATCH")
                    junit = [item for item in outputs if item.get("kind") == "test_result"
                             and item.get("format") == "junit-xml"]
                    application_logs = [item for item in outputs if item.get("kind") == "application_log"]
                    check(len(junit) >= 1, "REGISTERED_JUNIT_ABSENT")

                    tab = page.locator('[data-action="tab"][data-value="summary"]')
                    tab.focus()
                    report["stage"] = f"keyboard_{workspace}"
                    before_console = len(report["inspection_gets"])  # type: ignore[arg-type]
                    page.keyboard.press("ArrowRight")
                    check(page.locator('[data-action="tab"][data-value="console"]')
                          .get_attribute("aria-selected") == "true", "KEYBOARD_CONSOLE_NOT_SELECTED")
                    if not application_logs:
                        check(len(report["inspection_gets"]) == before_console,  # type: ignore[arg-type]
                              "CONSOLE_INSPECTION_UNEXPECTED")

                    page.locator('[data-action="tab"][data-value="console"]').focus()
                    before_tests = len(report["inspection_gets"])  # type: ignore[arg-type]
                    inspection_suffix = f"/runs/{run_id}/outputs/{junit[0]['output_id']}/inspection"
                    with page.expect_response(
                        lambda response, suffix=inspection_suffix: response.request.method == "GET"
                        and response.url.endswith(suffix)
                        and response.status == 200,
                        timeout=10_000,
                    ):
                        page.keyboard.press("ArrowRight")
                    check(page.locator('[data-action="tab"][data-value="tests"]')
                          .get_attribute("aria-selected") == "true", "KEYBOARD_TESTS_NOT_SELECTED")
                    page.get_by_text("Из элементов testcase:", exact=False).wait_for(timeout=10_000)
                    tests_gets = report["inspection_gets"][before_tests:]  # type: ignore[index]
                    check(any(path.endswith(f"/{junit[0]['output_id']}/inspection") for path in tests_gets),
                          "KEYBOARD_TESTS_INSPECTION_GET_MISSING")

                    report["runs"].append({  # type: ignore[attr-defined]
                        "workspace_id": workspace,
                        "request_id": request_id,
                        "run_id": run_id,
                        "execution_outcome": outcome,
                        "registered_outputs": len(outputs),
                        "registered_junit": len(junit),
                        "registered_application_logs": len(application_logs),
                        "keyboard_tests_selected": True,
                        "keyboard_tests_inspection_get": True,
                        "keyboard_console_selected": True,
                        "keyboard_console_inspection_get": "NOT_APPLICABLE_NO_APPLICATION_LOG"
                        if not application_logs else "UNVERIFIED",
                    })

                page.locator('[data-testid="workspace-list"] [data-action="workspace"][data-value="primary"]').click()
                report["stage"] = "local_context"
                context_button = page.locator('[data-action="export-context"]')
                check(context_button.count() == 1 and context_button.inner_text() == "Локальный контекст",
                      "LOCAL_CONTEXT_CONTROL_MISSING")
                check(context_button.is_enabled(), "LOCAL_CONTEXT_CONTROL_DISABLED")
                context_button.click()
                dialog = page.get_by_test_id("dialog")
                dialog.wait_for(state="visible", timeout=10_000)
                check("Результат остаётся в локальной операции; в Drive уходит только статус"
                      in dialog.inner_text()
                      and "Diff недоступен" in dialog.inner_text(),
                      "LOCAL_CONTEXT_SCOPE_MISSING")
                check(dialog.locator('textarea[name="paths"]').count() == 1,
                      "LOCAL_CONTEXT_PATH_INPUT_MISSING")
                report["local_context"] = {
                    "control_enabled": True,
                    "dialog_local_only_scope": True,
                    "path_input": True,
                    "diff_unavailable_explained": True,
                    "operation_submitted": False,
                }
                page.keyboard.press("Escape")
                check(dialog.count() == 0, "LOCAL_CONTEXT_DIALOG_NOT_CLOSED")

                report["page_errors"] = len(errors)
                check(report["auth_only_post"] == 1 and not report["blocked_mutations"]
                      and not report["off_origin_requests"] and not errors,
                      "REQUEST_OR_PAGE_ERROR")
                report["result"] = "PASS"
                report.pop("stage", None)
            finally:
                context.close()
                browser.close()
    except Exception as error:  # noqa: BLE001 -- never expose browser or credential values
        report["result"] = "FAIL"
        report["failure_code"] = str(error) if isinstance(error, RuntimeError) else "PROBE_EXCEPTION"
        report["failure_type"] = type(error).__name__
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"result": report["result"], "failure_code": report.get("failure_code"),
                      "installed_version": report["installed_version"]}))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
