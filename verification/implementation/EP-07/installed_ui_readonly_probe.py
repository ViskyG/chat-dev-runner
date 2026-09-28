#!/usr/bin/env python3
"""Inspect existing passing/failing runs in the installed owner-only UI.

Default invocation is inert. ``--apply`` requires the installed 0.2.6 loopback
service and exchanges one owner-only bootstrap for an ephemeral browser session.
After that auth-only POST, every API and browser request is GET or HEAD. The
probe never submits a command, checks a provider, opens an editor or changes
retention. It writes screenshots and a redacted result into a new evidence dir.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import APIRequestContext, Page, Route, sync_playwright

from runner_app.storage.paths import Paths, open_private_file

BASE_URL = "http://127.0.0.1:8765"
PROJECT_ID = "cdr-ep07-ui-20260928"
EXISTING = {
    "primary": ("d4e83e2e-cb8f-4768-84bf-13784aa9072f", "PASSED"),
    "parallel": ("cbc20739-bfe2-451e-9440-07118e08105a", "FAILED"),
}
VIEWPORTS = ((1600, 1120), (1280, 800), (2560, 1440))
COUNTS = ("tests", "passed", "failed", "errors", "skipped")
STATUS_LABELS = {
    "COMPLETED": "Обслужена", "PASSED": "Пройдены", "FAILED": "Не пройдены",
    "DELIVERED": "Доставлено", "PENDING": "Ожидает доставки",
    "PARTIAL": "Частично", "NOT_ATTEMPTED": "Не выполнялось",
    "DRAFT_PUBLISHED": "Черновик PR создан", "NOT_REQUESTED": "Не запрашивалось",
}
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[1-8][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")


class ProbeError(Exception):
    """Stable failure code; neither browser nor credential data is printed."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ProbeError(code)


def args_parser() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="open installed UI and write evidence")
    parser.add_argument("--bootstrap-file", type=Path,
                        default=Paths.discover().token_dir / "session-bootstrap.json")
    parser.add_argument("--output-dir", type=Path, help="new absolute evidence directory")
    args = parser.parse_args()
    if args.apply and args.output_dir is None:
        parser.error("--apply requires --output-dir")
    for path in (args.bootstrap_file, args.output_dir):
        if path is not None and (not path.is_absolute() or ".." in path.parts):
            parser.error("paths must be absolute without '..'")
    return args


def bootstrap(path: Path) -> str:
    descriptor = open_private_file(path)
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(4097)
    require(len(raw) <= 4096, "BOOTSTRAP_OVERSIZE")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProbeError("BOOTSTRAP_INVALID") from error
    require(isinstance(value, dict) and set(value) == {"bootstrap", "expires_in_seconds"},
            "BOOTSTRAP_INVALID")
    assert isinstance(value, dict)
    secret = value["bootstrap"]
    require(isinstance(secret, str) and 1 <= len(secret) <= 128, "BOOTSTRAP_INVALID")
    assert isinstance(secret, str)
    return secret


def get_json(request: APIRequestContext, path: str) -> dict[str, Any]:
    require(path.startswith("/api/v1/") and "?" not in path and ".." not in path,
            "API_PATH_INVALID")
    response = request.get(path, timeout=10_000, max_redirects=0, fail_on_status_code=False)
    require(response.status == 200, "API_READ_FAILED")
    try:
        value = response.json()
    except ValueError as error:
        raise ProbeError("API_RESPONSE_INVALID") from error
    require(isinstance(value, dict), "API_RESPONSE_INVALID")
    assert isinstance(value, dict)
    return value


def get_bytes(request: APIRequestContext, path: str) -> bytes:
    require(path.startswith("/api/v1/runs/") and "?" not in path and ".." not in path,
            "OUTPUT_PATH_INVALID")
    response = request.get(path, timeout=10_000, max_redirects=0, fail_on_status_code=False)
    require(response.status == 200, "OUTPUT_READ_FAILED")
    return response.body()


def expected_facts(request: APIRequestContext, workspace: str,
                   request_id: str, outcome: str) -> dict[str, Any]:
    operation = get_json(request, f"/api/v1/operations/{request_id}")
    result = operation.get("result")
    require(operation.get("request_id") == request_id
            and operation.get("project_id") == PROJECT_ID
            and operation.get("workspace_id") == workspace
            and operation.get("action") == "DELIVER_VERIFY_PUBLISH"
            and operation.get("status") == "COMPLETED"
            and isinstance(result, dict), "OPERATION_IDENTITY_MISMATCH")
    assert isinstance(result, dict)
    run_id = result.get("run_id")
    require(isinstance(run_id, str) and UUID.fullmatch(run_id) is not None,
            "RUN_ID_INVALID")
    run = get_json(request, f"/api/v1/runs/{run_id}")
    require(run.get("run_id") == run_id and run.get("request_id") == request_id
            and run.get("project_id") == PROJECT_ID
            and run.get("workspace_id") == workspace
            and isinstance(run.get("execution"), dict)
            and run["execution"].get("outcome") == outcome,
            "RUN_IDENTITY_MISMATCH")
    execution = result.get("execution")
    delivery = result.get("delivery")
    publication = result.get("publication")
    require(isinstance(execution, dict) and execution.get("outcome") == outcome
            and isinstance(delivery, dict) and delivery.get("status") == "DELIVERED"
            and isinstance(publication, dict) and isinstance(publication.get("status"), str),
            "RESULT_AXES_MISMATCH")
    assert isinstance(delivery, dict)
    require(isinstance(delivery.get("confirmed_assets"), list)
            and {"report.md", "run-manifest.json"} <= set(delivery["confirmed_assets"]),
            "REPORT_ASSETS_NOT_CONFIRMED")
    index = get_json(request, f"/api/v1/runs/{run_id}/outputs").get("items")
    manifest = run.get("outputs")
    require(isinstance(index, list) and isinstance(manifest, list)
            and sorted(index, key=lambda item: item["output_id"])
            == sorted(manifest, key=lambda item: item["output_id"]),
            "OUTPUT_INDEX_MANIFEST_MISMATCH")
    assert isinstance(index, list)
    require(bool(index), "OUTPUTS_ABSENT")
    for item in index:
        require(isinstance(item, dict) and isinstance(item.get("output_id"), str)
                and SLUG.fullmatch(item["output_id"]) is not None
                and isinstance(item.get("kind"), str) and isinstance(item.get("format"), str)
                and isinstance(item.get("size_bytes"), int) and item["size_bytes"] >= 0
                and isinstance(item.get("complete"), bool)
                and isinstance(item.get("sha256"), str) and SHA.fullmatch(item["sha256"]) is not None,
                "OUTPUT_METADATA_INVALID")
    junit = [item for item in index if item["kind"] == "test_result"
             and item["format"] == "junit-xml"]
    require(bool(junit), "JUNIT_ABSENT")
    inspected = {}
    for item in junit:
        detail = get_json(request, f"/api/v1/runs/{run_id}/outputs/{item['output_id']}/inspection")
        require(detail.get("output_id") == item["output_id"], "INSPECTION_ID_MISMATCH")
        inspected[item["output_id"]] = detail
    require(any(detail.get("status") == "PARSED" for detail in inspected.values()),
            "PARSED_JUNIT_ABSENT")
    return {"operation": operation, "run": run, "outputs": index,
            "junit_inspections": inspected}


def normalize(value: str) -> str:
    return " ".join(value.replace("\u00a0", " ").replace("\u202f", " ").split())


def inspect_workspace(page: Page, workspace: str, facts: dict[str, Any]) -> dict[str, Any]:
    operation, run, outputs = facts["operation"], facts["run"], facts["outputs"]
    page.locator(f'[data-testid="project-list"] [data-action="project"][data-value="{PROJECT_ID}"]').click()
    page.locator(f'[data-testid="workspace-list"] [data-action="workspace"][data-value="{workspace}"]').click()
    panel = page.locator("#run-panel")
    panel.wait_for(state="visible", timeout=15_000)
    require(run["run_id"] in panel.inner_text(), "UI_LATEST_RUN_MISMATCH")
    summary = normalize(panel.inner_text())
    badges = page.locator(".summary-grid .badge").all_inner_texts()
    axes = [operation["status"], run["execution"]["outcome"],
            operation["result"]["delivery"]["status"]]
    require(badges == [STATUS_LABELS.get(axis, axis) for axis in axes], "UI_AXES_MISMATCH")
    publication = operation["result"]["publication"]["status"]
    require("Публикация" in summary and STATUS_LABELS.get(publication, publication) in summary,
            "UI_PUBLICATION_MISMATCH")
    if run["execution"]["outcome"] == "FAILED":
        require("Проверка кода проекта не прошла" in summary
                and "Сбой runner или инфраструктуры" not in summary,
                "UI_FAILURE_CLASSIFICATION_MISMATCH")
    page.locator('[data-action="tab"][data-value="tests"]').click()
    junit_report: list[dict[str, Any]] = []
    for output_id, inspection in facts["junit_inspections"].items():
        block = page.locator(".evidence-detail").filter(
            has=page.locator(f'[data-action="output"][data-value="{output_id}"]'))
        block.wait_for(state="visible", timeout=15_000)
        if inspection["status"] != "PARSED":
            block.get_by_text("Число тестов неизвестно", exact=False).wait_for(timeout=15_000)
            require("Число тестов неизвестно" in block.inner_text(), "UI_UNKNOWN_COUNTS_MISSING")
            junit_report.append({"output_id": output_id, "status": inspection["status"]})
            continue
        counts = inspection.get("summary")
        cases = inspection.get("failed_cases")
        require(isinstance(counts, dict) and isinstance(cases, list)
                and all(isinstance(counts.get(key), int) for key in COUNTS),
                "JUNIT_INSPECTION_INVALID")
        block.get_by_text("Из элементов testcase:", exact=False).wait_for(timeout=15_000)
        block_text = normalize(block.inner_text())
        for key, label in zip(COUNTS, ("всего", "прошло", "ошибок проверки",
                                       "ошибок запуска", "пропущено"), strict=True):
            formatted = normalize(f"{counts[key]:,}".replace(",", " "))
            require(f"{formatted} {label}" in block_text, "UI_JUNIT_COUNT_MISMATCH")
        details = block.locator("details.test-case")
        require(details.count() == len(cases), "UI_JUNIT_CASE_COUNT_MISMATCH")
        for position, case in enumerate(cases):
            item = details.nth(position)
            require(normalize(item.locator("summary").inner_text())
                    == normalize(f"{case['outcome']} · {case['id']}"),
                    "UI_JUNIT_CASE_ID_MISMATCH")
            item.locator("summary").click()
            require(item.get_attribute("open") is not None, "UI_JUNIT_CASE_NAVIGATION_FAILED")
            trace = case.get("trace") or "Текст причины не записан в JUnit."
            require(item.locator("pre").inner_text() == trace, "UI_JUNIT_TRACE_MISMATCH")
            if "<" in trace:
                require(item.locator("script,img,svg,iframe").count() == 0,
                        "UI_JUNIT_TRACE_ACTIVE_MARKUP")
            item.locator("summary").click()
        junit_report.append({"output_id": output_id, "status": "PARSED",
                             "counts": {key: counts[key] for key in COUNTS},
                             "failed_case_count": len(cases),
                             "case_ids_sha256": hashlib.sha256(
                                 json.dumps([case["id"] for case in cases],
                                            ensure_ascii=False).encode("utf-8")).hexdigest()})
    page.locator('[data-action="tab"][data-value="artifacts"]').click()
    listing = page.get_by_test_id("output-list")
    require(listing.locator(".artifact-row").count() == len(outputs),
            "UI_OUTPUT_COUNT_MISMATCH")
    for output in outputs:
        row = listing.locator(".artifact-row").filter(
            has=page.locator(f'[data-action="output"][data-value="{output["output_id"]}"]'))
        require(row.count() == 1, "UI_OUTPUT_ID_MISSING")
        label = normalize(row.inner_text())
        size = f"{output['size_bytes']:,}".replace(",", " ")
        completeness = "полный" if output["complete"] else "неполный"
        expected = f"{output['kind']} · {output['format']} · {size} байт · {completeness}"
        require(expected in label, "UI_OUTPUT_METADATA_MISMATCH")
    return {"workspace_id": workspace, "request_id": operation["request_id"],
            "run_id": run["run_id"], "operation": axes[0], "execution": axes[1],
            "delivery": axes[2], "publication": publication,
            "report_asset_confirmed": True, "outputs_verified": len(outputs),
            "junit": junit_report}



def inspect_optional(page: Page, request: APIRequestContext,
                     facts: dict[str, Any]) -> dict[str, Any]:
    """Exercise optional real output types only when already registered."""
    run_id = facts["run"]["run_id"]
    outputs = facts["outputs"]
    coverage: dict[str, Any] = {
        "structured_application_log": "ABSENT",
        "raster_screenshot": "ABSENT",
        "svg_screenshot": "ABSENT",
        "unsafe_junit_marker": "ABSENT",
    }
    if any("<" in case.get("trace", "")
           for inspection in facts["junit_inspections"].values()
           for case in inspection.get("failed_cases", [])):
        coverage["unsafe_junit_marker"] = "INERT_TEXT_VERIFIED"
    logs = [item for item in outputs if item["kind"] == "application_log"
            and item["format"] == "application-log/v1"]
    if logs:
        page.locator('[data-action="tab"][data-value="console"]').click()
        page.locator('[data-action="log-errors-only"]').click()
        parsed_logs = 0
        for item in logs:
            detail = get_json(request, f"/api/v1/runs/{run_id}/outputs/{item['output_id']}/inspection")
            if detail.get("status") != "PARSED":
                continue
            parsed_logs += 1
            events = detail.get("error_events", [])
            for event in events:
                page.get_by_text(event["message"][:4096], exact=False).wait_for(timeout=15_000)
            if not events:
                page.get_by_text("ERROR/FATAL не зарегистрированы", exact=False).wait_for(
                    timeout=15_000)
            panel = normalize(page.locator("#run-panel").inner_text())
            for event in events:
                require(normalize(event["message"][:4096]) in panel, "UI_LOG_FILTER_MISMATCH")
            invalid = detail.get("summary", {}).get("invalid_lines", 0)
            if invalid:
                require(f"Некорректных строк: {invalid}" in panel, "UI_LOG_INVALID_LINES_MISSING")
        coverage["structured_application_log"] = "VERIFIED" if parsed_logs else "NO_PARSED_LOG"
    page.locator('[data-action="tab"][data-value="artifacts"]').click()
    for item in outputs:
        media = item["media_type"]
        raster = item["kind"] == "screenshot" and media in {
            "image/png", "image/jpeg", "image/webp"}
        svg = media == "image/svg+xml"
        if not (raster or svg):
            continue
        page.locator(f'[data-action="output"][data-value="{item["output_id"]}"]').click()
        dialog = page.get_by_test_id("dialog")
        dialog.wait_for(state="visible", timeout=10_000)
        if raster:
            detail = get_json(request, f"/api/v1/runs/{run_id}/outputs/{item['output_id']}/inspection")
            if detail.get("safe_preview"):
                image = dialog.get_by_test_id("image-preview")
                image.wait_for(state="visible", timeout=10_000)
                dimensions = image.evaluate(
                    "node => [node.complete, node.naturalWidth, node.naturalHeight]")
                require(dimensions == [True, detail["width"], detail["height"]],
                        "UI_RASTER_PREVIEW_MISMATCH")
                coverage["raster_screenshot"] = "VERIFIED"
            else:
                require(dialog.get_by_test_id("image-preview").count() == 0,
                        "UI_UNSAFE_RASTER_PREVIEW")
                coverage["raster_screenshot"] = "NO_SAFE_PREVIEW"
        elif svg:
            preview = dialog.get_by_test_id("output-preview")
            preview.wait_for(state="visible", timeout=10_000)
            require(dialog.get_by_test_id("image-preview").count() == 0
                    and dialog.locator("svg,script,iframe").count() == 0,
                    "UI_SVG_ACTIVE_MARKUP")
            if item["complete"] and item["size_bytes"] <= 262144:
                raw = get_bytes(request, f"/api/v1/runs/{run_id}/outputs/{item['output_id']}")
                require(len(raw) == item["size_bytes"]
                        and hashlib.sha256(raw).hexdigest() == item["sha256"],
                        "SVG_OUTPUT_BYTES_MISMATCH")
                try:
                    expected_text = raw.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    coverage["svg_screenshot"] = "INVALID_UTF8_NO_IMAGE"
                else:
                    page.wait_for_function(
                        "() => !document.getElementById('output-preview')?.textContent?.startsWith('Читаем файл')",
                        timeout=10_000)
                    require(preview.inner_text() == (expected_text or "Файл пустой."),
                            "UI_SVG_TEXT_MISMATCH")
                    coverage["svg_screenshot"] = "INERT_TEXT_VERIFIED"
            else:
                coverage["svg_screenshot"] = "PREVIEW_LIMIT_NO_IMAGE"
        page.keyboard.press("Escape")
        require(dialog.count() == 0, "UI_PREVIEW_NOT_CLOSED")
    return coverage


def capture_variants(page: Page, output_dir: Path, workspace: str) -> list[dict[str, Any]]:
    captured = []
    for mode in ("dark", "light", "reduced-motion"):
        desired_theme = "light" if mode == "light" else "dark"
        if page.locator("html").get_attribute("data-theme") != desired_theme:
            page.get_by_test_id("theme-toggle").click()
        page.emulate_media(reduced_motion="reduce" if mode == "reduced-motion" else "no-preference")
        page.locator('[data-action="view"][data-value="system"]').click()
        switch = page.locator('[data-action="motion"]')
        desired_motion = mode == "reduced-motion"
        if (switch.get_attribute("aria-checked") == "true") != desired_motion:
            switch.click()
        require(page.locator("html").get_attribute("data-theme") == desired_theme
                and page.locator("html").get_attribute("data-motion")
                == ("off" if desired_motion else "on"), "UI_PREFERENCE_MISMATCH")
        page.locator('[data-action="view"][data-value="work"]').click()
        page.locator(f'[data-testid="workspace-list"] [data-action="workspace"][data-value="{workspace}"]').click()
        page.locator('[data-action="tab"][data-value="summary"]').click()
        page.evaluate("window.scrollTo(0, 0)")
        for width, height in VIEWPORTS:
            page.set_viewport_size({"width": width, "height": height})
            page.locator("#run-panel").wait_for(state="visible", timeout=15_000)
            filename = f"{workspace}-{mode}-{width}x{height}.png"
            path = output_dir / filename
            page.screenshot(path=str(path), full_page=False, animations="disabled")
            raw = path.read_bytes()
            require(raw.startswith(b"\x89PNG\r\n\x1a\n")
                    and int.from_bytes(raw[16:20], "big") == width
                    and int.from_bytes(raw[20:24], "big") == height, "SCREENSHOT_DIMENSIONS_MISMATCH")
            captured.append({"file": filename, "sha256": hashlib.sha256(raw).hexdigest(),
                             "viewport": [width, height], "theme": desired_theme,
                             "reduced_motion": desired_motion})
    return captured


def apply(args: argparse.Namespace) -> dict[str, Any]:
    assert args.output_dir is not None
    report: dict[str, Any] = {
        "schema_version": "1.0", "packet": "EP-07", "mode": "LIVE_INSTALLED_UI_READ_ONLY",
        "checked_at": datetime.now(timezone.utc).isoformat(), "base_source_commit":
        "a25be072b5f44d24ddbe4758fe6915c355d90e58", "installed_version": None,
        "auth_only_post": 0, "blocked_mutations": [], "off_origin_requests": [],
        "project_id": PROJECT_ID, "runs": [], "screenshots": [], "optional_coverage": {},
        "result": "INCOMPLETE",
    }
    old_umask = os.umask(0o077)
    try:
        args.output_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium"),
                headless=True, args=["--no-sandbox"],
            )
            context = browser.new_context(base_url=BASE_URL,
                                          viewport={"width": 1600, "height": 1120})
            page = context.new_page()
            page_errors: list[bool] = []
            page.on("pageerror", lambda _error: page_errors.append(True))
            try:
                def route_guard(route: Route) -> None:
                    url = urlsplit(route.request.url)
                    same_origin = f"{url.scheme}://{url.netloc}" == BASE_URL
                    if not same_origin:
                        report["off_origin_requests"].append(url.path)
                        route.abort()
                    elif route.request.method not in {"GET", "HEAD"}:
                        if route.request.method == "POST" and url.path == "/api/v1/local/session" \
                                and report["auth_only_post"] == 0:
                            report["auth_only_post"] = 1
                            route.continue_()
                        else:
                            report["blocked_mutations"].append(f"{route.request.method} {url.path}")
                            route.abort()
                    else:
                        route.continue_()

                page.route("**/*", route_guard)
                health = get_json(context.request, "/api/v1/health")
                require(health.get("service") == "chat-dev-runner"
                        and health.get("version") == "0.2.6", "INSTALLED_VERSION_MISMATCH")
                report["installed_version"] = health["version"]
                page.goto(BASE_URL, wait_until="domcontentloaded", timeout=15_000)
                page.locator("#bootstrap-input").wait_for(state="visible", timeout=15_000)
                secret = bootstrap(args.bootstrap_file)
                try:
                    page.locator("#bootstrap-input").fill(secret)
                    page.locator('#login-form button[type="submit"]').click()
                finally:
                    del secret
                page.get_by_test_id("project-list").wait_for(state="visible", timeout=20_000)
                require(get_json(context.request, "/api/v1/local/session").get("expires_in", 0) > 0,
                        "SESSION_NOT_ACTIVE")
                facts = {workspace: expected_facts(context.request, workspace, *identity)
                         for workspace, identity in EXISTING.items()}
                for workspace, item in facts.items():
                    report["runs"].append(inspect_workspace(page, workspace, item))
                    report["optional_coverage"][workspace] = inspect_optional(
                        page, context.request, item)
                    report["screenshots"].extend(capture_variants(page, args.output_dir, workspace))
                report["page_error_count"] = len(page_errors)
                require(report["auth_only_post"] == 1 and not report["blocked_mutations"]
                        and not report["off_origin_requests"], "REQUEST_BOUNDARY_FAILED")
                require(not page_errors, "UI_PAGE_ERROR")
                report["result"] = "PASS"
            finally:
                context.close()
                browser.close()
    except Exception as error:  # noqa: BLE001 -- sanitize every browser/credential failure
        report["result"] = "FAIL"
        report["failure_code"] = error.args[0] if isinstance(error, ProbeError) else "PROBE_EXCEPTION"
    finally:
        os.umask(old_umask)
    report_path = args.output_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report_path.chmod(0o600)
    return report


def main() -> int:
    args = args_parser()
    if not args.apply:
        print(json.dumps({"mode": "DRY_RUN", "service_contacted": False,
                          "bootstrap_read": False, "provider_writes": 0,
                          "project_id": PROJECT_ID, "existing_runs": len(EXISTING),
                          "apply_required": True}, sort_keys=True))
        return 0
    report = apply(args)
    print(json.dumps({"result": report["result"], "failure_code": report.get("failure_code"),
                      "installed_version": report["installed_version"],
                      "screenshots": len(report["screenshots"]),
                      "output_dir": str(args.output_dir)}, sort_keys=True))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
