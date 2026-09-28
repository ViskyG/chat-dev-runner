#!/usr/bin/env python3
"""Gated EP-06 Chromium check against a real, isolated loopback runner process.

Starts `runner serve` with fresh owner-private XDG roots. This process has no owner
provider configuration and remains disarmed. The one-time local session value is
used in memory only; neither its bytes nor session/CSRF values enter evidence.
"""

from __future__ import annotations

import http.client
import json
import os
import platform
import socket
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


def _health(port: int) -> dict[str, object] | None:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.3)
    try:
        connection.request("GET", "/api/v1/health")
        response = connection.getresponse()
        body = response.read(4096)
        if response.status != 200:
            return None
        value = json.loads(body)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        connection.close()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    checks: list[dict[str, object]] = []
    requests: list[dict[str, object]] = []
    errors: list[str] = []
    screenshot = OUT / "real-local-process-disarmed.png"
    port = _port()
    origin = f"http://127.0.0.1:{port}"
    evidence: dict[str, object] = {
        "schema_version": "1.0", "packet": "EP-06",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "mode": "real-uvicorn-isolated-unconfigured",
        "command": [sys.executable, "-m", "runner_app.cli", "serve", "--port", "<ephemeral>"],
        "environment": {"python": platform.python_version(),
                        "system": platform.system(), "machine": platform.machine(),
                        "xdg_roots": "fresh-owner-private-temporary", "provider_config": "absent"},
        "bind_host": "127.0.0.1", "bind_port": port,
        "checks": checks, "requests": requests, "page_errors": errors,
        "provider_writes": False,
    }

    def check(name: str, condition: bool) -> None:
        checks.append({"name": name, "passed": condition})
        if not condition:
            raise AssertionError(name)

    process: subprocess.Popen[bytes] | None = None
    failure: str | None = None
    with tempfile.TemporaryDirectory(prefix="cdr-ep06-", dir="/tmp") as temporary:
        base = Path(temporary)
        check("Temporary root owner-only 0700", stat.S_IMODE(base.stat().st_mode) == 0o700)
        environment = os.environ.copy()
        for name, directory in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                                ("XDG_CACHE_HOME", "cache"), ("XDG_RUNTIME_DIR", "runtime")):
            environment[name] = str(base / directory)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        bootstrap_file = base / "config" / "chat-dev-runner" / "tokens" / "session-bootstrap.json"
        owner_runtime_config = base / "config" / "chat-dev-runner" / "owner-runtime.json"
        check("No provider configuration in isolated XDG root", not owner_runtime_config.exists())
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "runner_app.cli", "serve", "--port", str(port)],
                cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            deadline = time.monotonic() + 15
            health = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(f"Local runner exited before health: {process.returncode}")
                health = _health(port)
                if health is not None and bootstrap_file.exists():
                    break
                time.sleep(0.05)
            check("Real loopback runner started", health is not None and bootstrap_file.exists())
            assert health is not None
            check("Health starts unarmed", health.get("armed") is False)
            file_info = bootstrap_file.stat()
            check("Session handoff owner-only 0600",
                  file_info.st_uid == os.geteuid() and stat.S_IMODE(file_info.st_mode) == 0o600)
            check("Token directory owner-only 0700",
                  stat.S_IMODE(bootstrap_file.parent.stat().st_mode) == 0o700)
            handoff = json.loads(bootstrap_file.read_text())
            secret = handoff.get("bootstrap")
            check("One-time handoff readable in isolated process",
                  isinstance(secret, str) and 16 <= len(secret) <= 128)
            evidence["health"] = {key: health.get(key) for key in
                                  ("service", "version", "status", "armed", "capabilities")}

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
                    check("Unauthenticated UI shows local login",
                          page.get_by_test_id("bootstrap-input").is_visible())
                    page.get_by_test_id("bootstrap-input").fill(secret)
                    page.locator("#login-form button[type=submit]").click()
                    page.get_by_test_id("app").wait_for(timeout=5000)
                    page.get_by_test_id("project-list").wait_for(timeout=5000)
                    check("Authenticated browser reads real project list",
                          "Проектов пока нет" in page.get_by_test_id("project-list").inner_text())
                    system = page.evaluate("async () => (await fetch('/api/v1/system', {credentials:'same-origin'})).json()")
                    projects = page.evaluate("async () => (await fetch('/api/v1/projects', {credentials:'same-origin'})).json()")
                    check("System reports disarmed and no queue",
                          system.get("armed") is False and system.get("execution_queue") is False)
                    check("No production action can be admitted",
                          system.get("capabilities") == [])
                    check("Project registry is empty in fresh state", projects == {"items": [], "next_cursor": None})
                    check("No provider setup is misreported as connected",
                          system.get("integrations", {}).get("state") == "NOT_CONFIGURED")
                    page.locator('[data-action="view"][data-value="system"]').click()
                    check("System page renders actual disarmed state",
                          "Выключен" in page.locator("main").inner_text())
                    page.locator('[data-action="view"][data-value="drive"]').click()
                    check("Drive page describes attach-existing workspace",
                          "существующей установке" in page.locator("main").inner_text())
                    page.reload(wait_until="networkidle")
                    page.get_by_test_id("project-list").wait_for(timeout=5000)
                    check("Session survives page reload without exposing cookie",
                          page.get_by_test_id("login").count() == 0)
                    page.screenshot(path=str(screenshot), full_page=True)
                    check("All browser traffic stays at runner loopback origin",
                          bool(requests) and all(request["same_origin"] for request in requests))
                    check("Only explicit session exchange mutates local API",
                          [r["path"] for r in requests if r["method"] == "POST"] ==
                          ["/api/v1/local/session"])
                    check("No browser script errors", not errors)
                    evidence["system"] = {
                        "runner_id": system.get("runner_id"), "armed": system.get("armed"),
                        "execution_queue": system.get("execution_queue"),
                        "capabilities": system.get("capabilities"),
                        "integration_state": system.get("integrations", {}).get("state"),
                    }
                    evidence["project_count"] = len(projects["items"])
                finally:
                    browser.close()
        except Exception as error:  # noqa: BLE001 - always write gate evidence before cleanup
            # Browser call logs can include form values; never persist their text.
            detail = str(error) if isinstance(error, AssertionError) else "browser-or-process-gate"
            failure = f"{type(error).__name__}: {detail}"
        finally:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=8)
                evidence["process_exit_code"] = process.returncode
            evidence["failure"] = failure
            evidence["all_passed"] = failure is None and all(c["passed"] for c in checks)
            (OUT / "local-process-slice.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    if failure is not None:
        raise SystemExit(f"EP-06 real local-process slice failed: {failure}")
    print(f"EP-06 real local-process slice: {len(checks)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
