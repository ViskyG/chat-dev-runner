#!/usr/bin/env python3
"""Read-only browser vertical slice on the disposable EP-05 failed-run state.

Constructs Runtime using the already authorized owner configuration, but runs its
ASGI app directly: no DrivePollingBridge and no workspace-mailbox polling thread.
Construction attaches to the existing Drive installation and reads GitHub identity;
this script sends no operation or provider-write POST; three explicit checks read providers.
"""

from __future__ import annotations

import json
import os
import platform
import sqlite3
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from local_process_slice import _port
from playwright.sync_api import sync_playwright

from runner_app.config import DRIVE_INDEX_ID, DRIVE_ROOT_ID, Settings
from runner_app.engine.runtime import Runtime
from runner_app.storage.paths import Paths

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
SMOKE = Path("/home/visky/Work/GPTRunner/.ep05-smoke/cdr-ep05-fail-20260928")
PROJECT = "cdr-ep05-fail-20260928"
WORKSPACE = "primary"


def _preflight() -> tuple[str, str, str, int]:
    """Read only local public IDs/manifest status; reject active/uncertain state."""
    if stat.S_IMODE(SMOKE.stat().st_mode) != 0o700:
        raise AssertionError("Disposable state must be owner-only 0700")
    paths = Paths.discover()
    owner_config = paths.config_dir / "owner-runtime.json"
    if not owner_config.is_file() or stat.S_IMODE(owner_config.stat().st_mode) != 0o600:
        raise AssertionError("Existing owner configuration is missing or not 0600")
    database = SMOKE / "state.sqlite"
    if stat.S_IMODE(database.stat().st_mode) != 0o600:
        raise AssertionError("Disposable ledger is not owner-only 0600")
    connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    try:
        armed = connection.execute("SELECT armed FROM runner_state").fetchone()[0]
        active = connection.execute(
            "SELECT count(*) FROM operations WHERE status='RUNNING' OR capacity_held=1 OR reconcile_required=1"
        ).fetchone()[0]
        project = connection.execute(
            "SELECT project_id FROM projects WHERE project_id=?", (PROJECT,),
        ).fetchone()
        workspace = connection.execute(
            "SELECT workspace_id FROM workspaces WHERE project_id=? AND workspace_id=?",
            (PROJECT, WORKSPACE),
        ).fetchone()
        failed = []
        for run_id, raw in connection.execute(
            "SELECT run_id,manifest_json FROM runs WHERE project_id=? AND workspace_id=?",
            (PROJECT, WORKSPACE),
        ):
            manifest = json.loads(raw)
            if manifest.get("execution", {}).get("outcome") == "FAILED":
                failed.append((run_id, manifest))
        if armed or active or project is None or workspace is None or len(failed) != 1:
            raise AssertionError("Disposable EP-05 state is not terminal/disarmed with one failed run")
        run_id, manifest = failed[0]
        outputs = manifest.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            raise AssertionError("Failed run has no frozen outputs")
        stdout = next((item for item in outputs if item.get("output_id") == "stdout-view"), None)
        if stdout is None or stdout.get("size_bytes", 0) < 1:
            raise AssertionError("Failed run lacks readable captured stdout")
        return run_id, manifest["snapshot"]["commit"], stdout["output_id"], stdout["size_bytes"]
    finally:
        connection.close()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    run_id, snapshot_sha, stdout_id, stdout_size = _preflight()
    port = _port()
    origin = f"http://127.0.0.1:{port}"
    paths = Paths(config_dir=Paths.discover().config_dir,
                  state_dir=SMOKE, cache_dir=SMOKE / "cache")
    evidence: dict[str, object] = {
        "schema_version": "1.0", "packet": "EP-06",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "mode": "real-owner-config-disposable-ep05-state-no-pollers",
        "environment": {"python": platform.python_version(),
                        "system": platform.system(), "machine": platform.machine(),
                        "state": "existing-disarmed-disposable-ep05-failed-run",
                        "owner_config": "present-0600-outside-repository"},
        "bind_host": "127.0.0.1", "bind_port": port,
        "project_id": PROJECT, "workspace_id": WORKSPACE,
        "run_id": run_id, "snapshot_sha": snapshot_sha,
        "checks": [], "browser_requests": [], "page_errors": [],
        "provider_writes": False, "drive_poller_started": False,
        "workspace_mailbox_poller_started": False,
    }
    checks: list[dict[str, object]] = evidence["checks"]  # type: ignore[assignment]
    requests: list[dict[str, object]] = evidence["browser_requests"]  # type: ignore[assignment]
    errors: list[str] = evidence["page_errors"]  # type: ignore[assignment]

    def check(name: str, condition: bool) -> None:
        checks.append({"name": name, "passed": condition})
        if not condition:
            raise AssertionError(name)

    runtime: Runtime | None = None
    server: uvicorn.Server | None = None
    thread: threading.Thread | None = None
    failure: str | None = None
    try:
        runtime = Runtime(Settings(listen_port=port), paths)
        check("Runtime attached exact existing Drive installation",
              runtime.integrations is not None
              and runtime.integrations.attached.config.root_id == DRIVE_ROOT_ID
              and runtime.integrations.attached.config.index_id == DRIVE_INDEX_ID)
        check("Runtime remains disarmed", runtime.ledger.state()["armed"] is False)
        check("No Drive polling bridge started", runtime.drive_bridge is None)
        check("No workspace mailbox polling thread started",
              runtime.workspace_mailboxes is not None
              and runtime.workspace_mailboxes._thread is None)
        config = uvicorn.Config(runtime.app, host="127.0.0.1", port=port,
                                log_level="error", access_log=False,
                                proxy_headers=False, server_header=False)
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True, name="cdr-ep06-live-browser")
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        check("Real Uvicorn served existing state on loopback", server.started)
        bootstrap_file = paths.token_dir / "session-bootstrap.json"
        check("Bootstrap file remains outside repository and owner-only",
              bootstrap_file.is_file() and stat.S_IMODE(bootstrap_file.stat().st_mode) == 0o600)
        handoff = json.loads(bootstrap_file.read_text())
        secret = handoff.get("bootstrap")
        check("One-time local session handoff available", isinstance(secret, str) and len(secret) >= 16)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium"),
                headless=True, args=["--no-sandbox"],
            )
            evidence["browser_version"] = browser.version
            page = browser.new_page(viewport={"width": 1600, "height": 1120})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append({
                "method": request.method, "path": urlsplit(request.url).path,
                "same_origin": request.url.startswith(origin + "/"),
            }))
            try:
                page.goto(origin, wait_until="networkidle")
                page.get_by_test_id("login").wait_for(timeout=5000)
                page.get_by_test_id("bootstrap-input").fill(secret)
                with page.expect_response(lambda response: response.url.endswith("/api/v1/local/session")
                                          and response.request.method == "POST") as session_response:
                    page.locator("#login-form button[type=submit]").click()
                csrf = session_response.value.json()["csrf_token"]
                page.get_by_test_id("project-list").wait_for(timeout=10000)
                check("Browser renders registered disposable project",
                      page.locator(f'[data-action="project"][data-value="{PROJECT}"]').count() == 1)
                check("Browser renders exact primary workspace",
                      page.locator(f'[data-action="workspace"][data-value="{WORKSPACE}"]').count() == 1)
                page.locator('[data-action="view"][data-value="results"]').click()
                result_text = page.get_by_test_id("result-list").inner_text()
                check("Browser renders frozen failed run and full tested SHA",
                      run_id in result_text and snapshot_sha in result_text
                      and "Не пройдены" in result_text)
                page.locator('[data-action="view"][data-value="work"]').click()
                page.locator('[data-action="tab"][data-value="artifacts"]').click()
                page.get_by_test_id("output-list").wait_for(timeout=5000)
                check("Browser renders registered output identities",
                      page.get_by_test_id("output-list").locator('[data-action="output"]').count() >= 1
                      and page.locator(f'[data-action="output"][data-value="{stdout_id}"]').count() >= 1)
                page.locator(f'[data-action="output"][data-value="{stdout_id}"]').click()
                preview = page.get_by_test_id("output-preview")
                preview.wait_for(timeout=5000)
                page.wait_for_function("() => !document.querySelector('[data-testid=output-preview]').textContent.includes('Читаем файл')")
                check("Browser reads bounded real stdout as inert text",
                      len(preview.inner_text()) > 0 and page.locator("script:not([src])").count() == 0)
                page.keyboard.press("Escape")
                response = page.evaluate("""async ([run, output]) => {
                  const result = await fetch(`/api/v1/runs/${run}/outputs/${output}`, {credentials:'same-origin'});
                  return {status: result.status, size: (await result.blob()).size};
                }""", [run_id, stdout_id])
                check("Registered output route serves exact retained bytes",
                      response == {"status": 200, "size": stdout_size})

                def read_json(path: str) -> dict[str, object]:
                    return page.evaluate("""async path => {
                      const response = await fetch(path, {credentials:'same-origin', cache:'no-store'});
                      return {http_status: response.status, data: await response.json()};
                    }""", path)

                def post_check(path: str, body: dict[str, str]) -> dict[str, object]:
                    return page.evaluate("""async ([path, body, csrf]) => {
                      const response = await fetch(path, {
                        method:'POST', credentials:'same-origin', cache:'no-store',
                        headers:{'Content-Type':'application/json', 'X-CSRF-Token':csrf},
                        body:JSON.stringify(body)
                      });
                      return {http_status: response.status, data: await response.json()};
                    }""", [path, body, csrf])

                before_system = read_json("/api/v1/system")
                before_connection = read_json(
                    f"/api/v1/projects/{PROJECT}/workspaces/{WORKSPACE}/connections")
                check("Provider checks initially unknown after process start",
                      before_system["http_status"] == 200
                      and all(before_system["data"]["connections"][key]["status"] == "UNKNOWN"
                              for key in ("drive", "github_api"))
                      and before_connection["http_status"] == 200
                      and before_connection["data"]["git_remote"]["status"] == "UNKNOWN")
                probe_results = {}
                for key, path, body in (
                    ("drive", "/api/v1/local/check-drive", {}),
                    ("github_api", "/api/v1/local/check-github", {}),
                    ("git_remote", "/api/v1/local/check-git-remote",
                     {"project_id": PROJECT, "workspace_id": WORKSPACE}),
                ):
                    outcome = post_check(path, body)
                    value = outcome["data"]
                    check(f"Explicit {key} check returns a dated read observation",
                          outcome["http_status"] == 200
                          and value.get("status") in {"OK", "ERROR"}
                          and isinstance(value.get("last_checked_at"), str)
                          and (value.get("last_success_at") is not None) ==
                          (value.get("status") == "OK"))
                    code = value.get("code")
                    probe_results[key] = {"status": value["status"],
                                          "code": code if isinstance(code, str) else None,
                                          "checked_at": value["last_checked_at"]}
                after_system = read_json("/api/v1/system")
                after_connection = read_json(
                    f"/api/v1/projects/{PROJECT}/workspaces/{WORKSPACE}/connections")
                check("Drive and GitHub API retain independent check state",
                      after_system["http_status"] == 200
                      and all(after_system["data"]["connections"][key]["status"] ==
                              probe_results[key]["status"]
                              and after_system["data"]["connections"][key]["last_checked_at"] ==
                              probe_results[key]["checked_at"]
                              for key in ("drive", "github_api")))
                check("Selected Git remote check is scoped to exact workspace",
                      after_connection["http_status"] == 200
                      and after_connection["data"]["git_remote"]["status"] ==
                      probe_results["git_remote"]["status"]
                      and after_connection["data"]["git_remote"]["last_checked_at"] ==
                      probe_results["git_remote"]["checked_at"])
                evidence["provider_read_checks"] = probe_results
                check("Only session and three explicit read-check POSTs were issued",
                      [item["path"] for item in requests if item["method"] == "POST"] ==
                      ["/api/v1/local/session", "/api/v1/local/check-drive",
                       "/api/v1/local/check-github", "/api/v1/local/check-git-remote"])
                check("Every browser request stayed on loopback origin",
                      bool(requests) and all(item["same_origin"] for item in requests))
                check("No browser script errors", not errors)
                page.screenshot(path=str(OUT / "real-ep05-failed-run.png"), full_page=True)
            finally:
                browser.close()
        evidence["run_outcome"] = "FAILED"
        evidence["output_id"] = stdout_id
        evidence["output_size_bytes"] = stdout_size
    except Exception as error:  # noqa: BLE001 - preserve evidence and close real state on all gate failures
        # Never persist provider/browser exception text: it may contain credentials.
        detail = str(error) if isinstance(error, AssertionError) else "live-browser-or-provider-gate"
        failure = f"{type(error).__name__}: {detail}"
    finally:
        if runtime is not None:
            runtime.ledger.disarm()
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=10)
            evidence["server_thread_stopped"] = not thread.is_alive()
        if runtime is not None:
            runtime.close()
            evidence["final_armed"] = runtime.ledger.state()["armed"]
            evidence["bootstrap_removed"] = not (paths.token_dir / "session-bootstrap.json").exists()
        evidence["failure"] = failure
        evidence["all_passed"] = failure is None and all(item["passed"] for item in checks)
        (OUT / "live-ep05-state-slice.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    if failure is not None:
        raise SystemExit(f"EP-06 live disposable-state browser slice failed: {failure}")
    print(f"EP-06 live disposable-state browser slice: {len(checks)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
