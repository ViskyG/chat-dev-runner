#!/usr/bin/env python3
"""Installed 0.2.7 UI failure and recovery through browser GET interception only."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from installed_ui_readonly_probe import BASE_URL, EXISTING, PROJECT_ID, bootstrap, get_json
from playwright.sync_api import Route, sync_playwright

from runner_app.storage.paths import Paths

HERE = Path(__file__).parent
REPORT = HERE / "installed-ui-0.2.7-offline.json"
SCREENSHOT = HERE / "installed-ui-0.2.7-offline.png"


def check(value: bool, code: str) -> None:
    if not value:
        raise RuntimeError(code)


def main() -> int:
    if os.environ.get("CDR_APPLY_INSTALLED_UI_027_OFFLINE") != "1":
        print(json.dumps({"mode": "DRY_RUN", "service_contacted": False}))
        return 0
    report: dict[str, object] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "mode": "INSTALLED_027_UI_BROWSER_GET_FAILURE_READBACK",
        "source_commit": "888b4ae",
        "installed_version": None,
        "auth_only_post": 0,
        "blocked_mutations": [],
        "off_origin_requests": [],
        "intercepted_gets": [],
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
            mode = {"value": "normal"}

            def guard(route: Route) -> None:
                url = urlsplit(route.request.url)
                same_origin = f"{url.scheme}://{url.netloc}" == BASE_URL
                if not same_origin:
                    report["off_origin_requests"].append(url.path)  # type: ignore[attr-defined]
                    route.abort()
                elif route.request.method in {"GET", "HEAD"}:
                    is_api = url.path.startswith("/api/v1/")
                    connection_get = is_api and url.path.endswith("/connections")
                    if is_api and (mode["value"] == "all" or
                                   (mode["value"] == "connection" and connection_get)):
                        report["intercepted_gets"].append(url.path)  # type: ignore[attr-defined]
                        route.abort("failed")
                    else:
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
                report["stage"] = "normal_auth"
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
                page.locator(
                    f'[data-testid="project-list"] [data-action="project"][data-value="{PROJECT_ID}"]'
                ).click()
                page.locator('[data-testid="workspace-list"] [data-action="workspace"][data-value="primary"]').click()
                panel = page.locator("#run-panel")
                panel.wait_for(state="visible", timeout=15_000)
                baseline_run_id = EXISTING["primary"][0]
                check(baseline_run_id in panel.inner_text(), "BASELINE_RUN_NOT_VISIBLE")
                baseline_panel = panel.inner_text()
                refresh = page.get_by_role("button", name="Обновить реальные данные")
                check(refresh.count() == 1 and refresh.is_enabled(), "RETRY_CONTROL_MISSING")
                check(page.locator('[data-action="export-context"]').is_enabled(),
                      "BASELINE_CONTEXT_DISABLED")
                report["baseline"] = {
                    "primary_run_id": baseline_run_id,
                    "runner_online": "Локальный runner · приём включён" in
                    page.get_by_test_id("connection-strip").inner_text(),
                    "retry_accessible_name": "Обновить реальные данные",
                }
                check(report["baseline"]["runner_online"], "BASELINE_RUNNER_NOT_ONLINE")  # type: ignore[index]

                report["stage"] = "workspace_projection_failure"
                mode["value"] = "connection"
                before_connection = len(report["intercepted_gets"])  # type: ignore[arg-type]
                page.locator('[data-testid="workspace-list"] [data-action="workspace"][data-value="primary"]').click()
                page.get_by_test_id("connection-strip").get_by_text(
                    "Локальная проекция недоступна", exact=False).first.wait_for(timeout=10_000)
                connection_paths = report["intercepted_gets"][before_connection:]  # type: ignore[index]
                strip = page.get_by_test_id("connection-strip").inner_text()
                check(any(path.endswith("/connections") for path in connection_paths)
                      and strip.count("Локальная проекция недоступна") >= 2
                      and "Локальный runner · приём включён" in strip,
                      "PARTIAL_CONNECTION_FAILURE_NOT_MARKED")
                report["workspace_projection_failure"] = {
                    "intercepted_connection_get": True,
                    "git_fetch_and_remote_marked_unavailable": True,
                    "runner_not_falsely_offline": True,
                }

                report["stage"] = "global_get_failure"
                mode["value"] = "all"
                before_global = len(report["intercepted_gets"])  # type: ignore[arg-type]
                refresh.focus()
                page.keyboard.press("Enter")
                page.get_by_test_id("connection-strip").get_by_text(
                    "Локальный runner · недоступен", exact=True).wait_for(timeout=10_000)
                strip = page.get_by_test_id("connection-strip").inner_text()
                global_paths = report["intercepted_gets"][before_global:]  # type: ignore[index]
                check(any(path in {"/api/v1/system", "/api/v1/projects"}
                          or path.startswith("/api/v1/operations") for path in global_paths),
                      "GLOBAL_GET_NOT_INTERCEPTED")
                check(strip.count("Runner недоступен") == 4, "PROVIDER_STATUS_NOT_UNKNOWN")
                check(panel.inner_text() == baseline_panel, "HISTORICAL_RUN_CHANGED_OFFLINE")
                check(page.locator('[data-action="export-context"]').is_disabled(),
                      "OFFLINE_CONTEXT_NOT_DISABLED")
                check(page.locator('[data-action="new-project"]').first.is_disabled(),
                      "OFFLINE_CREATE_NOT_DISABLED")
                check("Локальный сервис недоступен; обновите состояние." in
                      (page.locator('[data-action="export-context"]').get_attribute("aria-description") or ""),
                      "OFFLINE_ACTION_REASON_MISSING")
                check(refresh.is_enabled() and refresh.get_attribute("aria-label") ==
                      "Обновить реальные данные", "OFFLINE_RETRY_NOT_ACCESSIBLE")
                page.screenshot(path=str(SCREENSHOT), full_page=False, animations="disabled")
                raw = SCREENSHOT.read_bytes()
                check(raw.startswith(b"\x89PNG\r\n\x1a\n")
                      and int.from_bytes(raw[16:20], "big") == 1600
                      and int.from_bytes(raw[20:24], "big") == 1120,
                      "OFFLINE_SCREENSHOT_INVALID")
                report["global_get_failure"] = {
                    "intercepted_api_gets": len(global_paths),
                    "runner_explicitly_unavailable": True,
                    "provider_checks_prefixed_unavailable": 4,
                    "historical_run_unchanged": True,
                    "command_controls_disabled_with_reason": True,
                    "retry_enabled_and_labelled": True,
                    "screenshot": SCREENSHOT.name,
                    "screenshot_sha256": hashlib.sha256(raw).hexdigest(),
                }

                report["stage"] = "recovery"
                mode["value"] = "normal"
                refresh.focus()
                page.keyboard.press("Enter")
                page.get_by_test_id("connection-strip").get_by_text(
                    "Локальный runner · приём включён", exact=True).wait_for(timeout=15_000)
                check(page.locator('[data-action="export-context"]').is_enabled()
                      and baseline_run_id in panel.inner_text(), "RECOVERY_STATE_MISMATCH")
                report["recovery"] = {
                    "runner_online_again": True,
                    "command_controls_restored": True,
                    "same_registered_run": True,
                }
                report["page_errors"] = len(errors)
                check(report["auth_only_post"] == 1
                      and not report["blocked_mutations"]
                      and not report["off_origin_requests"]
                      and not errors, "REQUEST_OR_PAGE_ERROR")
                report["result"] = "PASS"
                report.pop("stage", None)
            finally:
                context.close()
                browser.close()
    except Exception as error:  # noqa: BLE001 -- do not expose browser or credential values
        report["result"] = "FAIL"
        report["failure_code"] = str(error) if isinstance(error, RuntimeError) else "PROBE_EXCEPTION"
        report["failure_type"] = type(error).__name__
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"result": report["result"], "failure_code": report.get("failure_code"),
                      "installed_version": report["installed_version"]}))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
