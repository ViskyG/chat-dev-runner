#!/usr/bin/env python3
"""One owner-local installed UI editor click for an existing registered workspace.

The default mode is inert. --apply performs one local session exchange and one
exact-ID VS Code launch. No credential, cookie, CSRF token, or browser storage is
serialized to evidence. The browser refuses all other mutations and off-origin
requests. The probe never sends a Runner command or touches a provider.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import Route, sync_playwright

from runner_app.git.snapshots import workspace_fingerprint
from runner_app.storage.paths import Paths, open_private_file

BASE = "http://127.0.0.1:8765"
PROJECT = "cdr-ep07-ui-20260928"
SELECTED = "primary"
WORKSPACES = ("primary", "parallel")
EDITOR_ENDPOINT = "/api/v1/local/open-editor"


class GateError(RuntimeError):
    """Stable failure code without secret or browser payload."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise GateError(code)


def bootstrap(path: Path) -> str:
    descriptor = open_private_file(path)
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(4097)
    require(len(raw) <= 4096, "BOOTSTRAP_OVERSIZE")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GateError("BOOTSTRAP_INVALID") from error
    require(isinstance(value, dict) and set(value) == {"bootstrap", "expires_in_seconds"},
            "BOOTSTRAP_INVALID")
    secret = value["bootstrap"]
    require(isinstance(secret, str) and 1 <= len(secret) <= 128, "BOOTSTRAP_INVALID")
    return secret


def git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(path), *args], check=True,
                            capture_output=True, text=True, timeout=10)
    return result.stdout.strip()


def source_view(path: Path) -> dict[str, object]:
    status = git(path, "status", "--porcelain=v1", "--untracked-files=all")
    return {
        "head": git(path, "rev-parse", "HEAD"),
        "tree": git(path, "rev-parse", "HEAD^{tree}"),
        "branch": git(path, "symbolic-ref", "--short", "HEAD"),
        "visible_status": status,
        "source_fingerprint": workspace_fingerprint(path),
    }


def registrations(state_db: Path, managed: Path) -> dict[str, dict[str, object]]:
    require(state_db.is_file(), "STATE_MISSING")
    uri = f"file:{quote(str(state_db))}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT w.workspace_id,w.branch,w.state,s.worktree_path,s.head,s.archived "
            "FROM workspaces w JOIN source_workspaces s USING(project_id,workspace_id) "
            "WHERE w.project_id=? ORDER BY w.workspace_id", (PROJECT,),
        ).fetchall()
        operation_count = connection.execute("SELECT count(*) FROM operations").fetchone()[0]
    require({row["workspace_id"] for row in rows} == set(WORKSPACES),
            "WORKSPACE_SET_CHANGED")
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        workspace_id = row["workspace_id"]
        expected = managed / PROJECT / "workspaces" / workspace_id
        require(row["worktree_path"] == str(expected), "REGISTERED_PATH_CHANGED")
        require(row["state"] == "ACTIVE" and row["archived"] == 0,
                "REGISTERED_WORKSPACE_UNAVAILABLE")
        require(expected.is_dir() and not expected.is_symlink(), "WORKSPACE_PATH_UNSAFE")
        result[workspace_id] = {
            "registered_path": str(expected), "registered_head": row["head"],
            "registered_branch": row["branch"], "before": source_view(expected),
        }
    result["_ledger"] = {"operation_count_before": operation_count}
    return result


def source_hash_match() -> dict[str, dict[str, object]]:
    installed = Path("/home/visky/Work/GPTRunner/.runner-install/venv/lib/python3.10/site-packages/runner_app")
    result: dict[str, dict[str, object]] = {}
    for relative in ("api/editor.py", "api/runtime.py", "web/app.js", "web/render.js", "web/api.js"):
        source_path = ROOT / "runner_app" / relative
        installed_path = installed / relative
        require(source_path.is_file() and installed_path.is_file(), "INSTALLED_MEMBER_MISSING")
        source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
        installed_sha = hashlib.sha256(installed_path.read_bytes()).hexdigest()
        require(source_sha == installed_sha, "INSTALLED_SOURCE_DRIFT")
        result[relative] = {"sha256": source_sha, "installed_matches_source": True}
    return result


def run_browser(token_file: Path) -> dict[str, object]:
    requests: dict[str, object] = {"session_post_count": 0, "editor_post_count": 0,
                                   "blocked_mutations": [], "off_origin_requests": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("CHROMIUM_PATH", playwright.chromium.executable_path),
            headless=True, args=["--no-sandbox"],
        )
        try:
            context = browser.new_context(base_url=BASE, viewport={"width": 1600, "height": 1120})
            page = context.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda _error: page_errors.append("PAGE_ERROR"))

            def guard(route: Route) -> None:
                request = route.request
                parsed = urlsplit(request.url)
                if f"{parsed.scheme}://{parsed.netloc}" != BASE:
                    requests["off_origin_requests"].append(parsed.path)  # type: ignore[attr-defined]
                    route.abort()
                elif request.method in {"GET", "HEAD"}:
                    route.continue_()
                elif (request.method == "POST" and parsed.path == "/api/v1/local/session"
                      and requests["session_post_count"] == 0):
                    requests["session_post_count"] = 1
                    route.continue_()
                elif (request.method == "POST" and parsed.path == EDITOR_ENDPOINT
                      and requests["editor_post_count"] == 0
                      and request.post_data_json == {"project_id": PROJECT,
                                                     "workspace_id": SELECTED}):
                    requests["editor_post_count"] = 1
                    route.continue_()
                else:
                    requests["blocked_mutations"].append(  # type: ignore[attr-defined]
                        f"{request.method} {parsed.path}")
                    route.abort()

            page.route("**/*", guard)
            page.goto(BASE, wait_until="domcontentloaded", timeout=15_000)
            page.locator("#bootstrap-input").wait_for(state="visible", timeout=15_000)
            secret = bootstrap(token_file)
            try:
                page.locator("#bootstrap-input").fill(secret)
                page.locator('#login-form button[type="submit"]').click()
            finally:
                del secret
            page.get_by_test_id("project-list").wait_for(state="visible", timeout=20_000)
            page.locator(f'[data-action="project"][data-value="{PROJECT}"]').click()
            page.locator('[data-action="code-menu"]').click()
            menu = page.locator('[data-action="open-editor"]')
            menu_values = sorted(menu.evaluate_all(
                "nodes => nodes.map(node => node.dataset.value)"))
            require(menu_values == sorted(WORKSPACES), "EDITOR_MENU_IDENTITY_MISMATCH")
            with page.expect_response(
                lambda response: urlsplit(response.url).path == EDITOR_ENDPOINT,
                timeout=15_000,
            ) as response_info:
                page.locator(f'[data-action="open-editor"][data-value="{SELECTED}"]').click()
            response = response_info.value
            require(response.status == 202, "EDITOR_REQUEST_NOT_ACCEPTED")
            body = response.json()
            require(body == {"status": "OPEN_REQUESTED", "project_id": PROJECT,
                             "workspace_id": SELECTED}, "EDITOR_RESPONSE_MISMATCH")
            require(not page_errors, "BROWSER_PAGE_ERROR")
            require(requests["session_post_count"] == 1
                    and requests["editor_post_count"] == 1
                    and not requests["blocked_mutations"]
                    and not requests["off_origin_requests"], "BROWSER_REQUEST_GUARD_FAILED")
            return {"menu_workspace_ids": menu_values, "selected_workspace_id": SELECTED,
                    "editor_http_status": response.status, "editor_response": body,
                    "requests": requests, "page_error_count": len(page_errors)}
        finally:
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"mode": "DRY_RUN", "service_contacted": False,
                          "editor_launched": False}))
        return 0
    require(args.output is not None and args.output.is_absolute()
            and not args.output.exists(), "OUTPUT_PATH_INVALID")
    paths = Paths.discover()
    require(str(paths.state_dir) == "/home/visky/Work/GPTRunner/.runner-data/chat-dev-runner",
            "STATE_DIR_UNEXPECTED")
    state_db = paths.state_dir / "state.sqlite"
    managed = paths.state_dir / "managed"
    report: dict[str, object] = {
        "schema_version": "1.0", "packet": "EP-07",
        "mode": "INSTALLED_OWNER_UI_EXACT_EDITOR_GATE",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "project_id": PROJECT, "result": "INCOMPLETE",
        "installed_version": "0.2.6", "installed_source": source_hash_match(),
        "workspace_registration": registrations(state_db, managed),
        "launcher_contract": {
            "owner_shell_resolved_executable": "/usr/bin/code", "argv_shape": ["<shutil.which(code)>", "--reuse-window", "<registered_worktree_path>"],
            "claim": "Installed editor.py is byte-identical to reviewed source; route accepts only two slug IDs and resolves its registered path locally.",
        },
    }
    try:
        report["browser"] = run_browser(paths.token_dir / "session-bootstrap.json")
        for workspace_id in WORKSPACES:
            view = report["workspace_registration"][workspace_id]  # type: ignore[index]
            worktree = Path(view["registered_path"])
            view["after"] = source_view(worktree)
            require(view["before"] == view["after"], "WORKSPACE_SOURCE_CHANGED")
        with sqlite3.connect(f"file:{quote(str(state_db))}?mode=ro", uri=True) as connection:
            report["workspace_registration"]["_ledger"]["operation_count_after"] = (  # type: ignore[index]
                connection.execute("SELECT count(*) FROM operations").fetchone()[0])
        report["result"] = "PASS"
        report["limits"] = [
            "OPEN_REQUESTED confirms that the installed route accepted the fixed local VS Code launch; it does not prove a GUI window was displayed on a particular monitor.",
            "Pre/post source fingerprints cover HEAD, index, tracked files and visible untracked names; ignored files and transient edits between observations are outside this check.",
            "No project command, Drive write, GitHub write, service restart or arm/disarm action was issued by this probe.",
        ]
    except BaseException as error:
        report["failure_code"] = error.code if isinstance(error, GateError) else type(error).__name__
        for workspace_id in WORKSPACES:
            view = report["workspace_registration"][workspace_id]  # type: ignore[index]
            view["after"] = source_view(Path(view["registered_path"]))
        raise
    finally:
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"result": report["result"], "evidence": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
