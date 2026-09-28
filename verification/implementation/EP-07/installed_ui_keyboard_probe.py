#!/usr/bin/env python3
"""One auth exchange, then read-only installed UI keyboard/layout acceptance."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from installed_ui_readonly_probe import BASE_URL, PROJECT_ID, bootstrap
from playwright.sync_api import Route, sync_playwright

from runner_app.storage.paths import Paths


def main() -> int:
    if os.environ.get("CDR_APPLY_INSTALLED_UI_KEYBOARD") != "1":
        print(json.dumps({"mode": "DRY_RUN", "service_contacted": False}))
        return 0
    report: dict[str, object] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "mode": "INSTALLED_026_KEYBOARD_AND_LAYOUT_READ_ONLY",
        "auth_only_post": 0, "blocked_mutations": [], "off_origin_requests": [],
        "viewport_checks": [], "page_errors": 0, "result": "INCOMPLETE",
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium"),
                headless=True, args=["--no-sandbox"])
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
                page.goto(BASE_URL, wait_until="domcontentloaded", timeout=15_000)
                page.locator("#bootstrap-input").wait_for(state="visible", timeout=15_000)
                secret = bootstrap(Paths.discover().token_dir / "session-bootstrap.json")
                try:
                    page.locator("#bootstrap-input").fill(secret)
                    page.locator('#login-form button[type="submit"]').click()
                finally:
                    del secret
                page.get_by_test_id("project-list").wait_for(state="visible", timeout=20_000)
                page.locator(f'[data-action="project"][data-value="{PROJECT_ID}"]').click()
                page.locator('[data-action="workspace"][data-value="primary"]').click()
                target = page.locator('[data-action="workspace"][data-value="parallel"]')
                target.focus()
                page.keyboard.press("Enter")
                if target.get_attribute("aria-pressed") != "true":
                    raise RuntimeError("KEYBOARD_WORKSPACE_SELECTION_FAILED")
                report["keyboard_workspace_selection"] = "PASS"
                palette = page.get_by_test_id("palette-open")
                palette.focus()
                page.keyboard.press("Control+k")
                if (not page.get_by_test_id("dialog").is_visible() or
                        not page.evaluate("document.activeElement?.id === 'palette-search'")):
                    raise RuntimeError("KEYBOARD_PALETTE_FOCUS_FAILED")
                page.keyboard.press("Escape")
                if (page.get_by_test_id("dialog").count() != 0 or
                        not page.evaluate("document.activeElement?.dataset?.testid === 'palette-open'")):
                    raise RuntimeError("KEYBOARD_ESCAPE_FOCUS_FAILED")
                report["keyboard_palette_focus_return"] = "PASS"
                original = page.locator("html").get_attribute("data-theme")
                page.get_by_test_id("theme-toggle").focus()
                page.keyboard.press("Enter")
                if page.locator("html").get_attribute("data-theme") == original:
                    raise RuntimeError("KEYBOARD_THEME_TOGGLE_FAILED")
                report["keyboard_theme_toggle"] = "PASS"
                page.emulate_media(reduced_motion="reduce")
                if not page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches"):
                    raise RuntimeError("REDUCED_MOTION_MEDIA_FAILED")
                report["reduced_motion_media"] = "PASS"
                for width, height in ((1280, 800), (1600, 1120), (2560, 1440)):
                    page.set_viewport_size({"width": width, "height": height})
                    width_actual = page.evaluate("document.documentElement.scrollWidth")
                    report["viewport_checks"].append({  # type: ignore[attr-defined]
                        "width": width, "height": height,
                        "document_scroll_width": width_actual,
                        "horizontal_overflow": width_actual > width,
                    })
                    if width_actual > width:
                        raise RuntimeError("HORIZONTAL_OVERFLOW")
                report["page_errors"] = len(errors)
                if errors or report["auth_only_post"] != 1 or report["blocked_mutations"] or report["off_origin_requests"]:
                    raise RuntimeError("REQUEST_OR_PAGE_ERROR")
                report["result"] = "PASS"
            finally:
                context.close()
                browser.close()
    except Exception as error:  # noqa: BLE001 -- sanitize browser and credential failures
        report["result"] = "FAIL"
        report["failure_code"] = str(error) if isinstance(error, RuntimeError) else "PROBE_EXCEPTION"
    path = Path(__file__).with_name("installed-0.2.6-keyboard.json")
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"result": report["result"], "failure_code": report.get("failure_code")}))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
