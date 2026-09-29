#!/usr/bin/env python3
"""Submit one bounded EXPORT_CONTEXT to the installed owner-local API.

Without --apply this is inert. The one authenticated operation reads only a
committed README from an existing disposable workspace. Source text and the
one-time bootstrap stay in memory; the evidence file contains hashes and
metadata only. This probe never writes a Drive command or calls a provider.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

from runner_app.storage.paths import Paths, open_private_file

BASE = "http://127.0.0.1:8765"
PROJECT = "cdr-ep07-ui-20260928"
WORKSPACE = "chat-route-20260929"
BRANCH = "feature/ep07-retarget-acceptance-20260929"
COMMIT = "1eac4fb222d5ace7af33e086e0e19cde65c13bb1"
PATH = "README.md"
REPO = (Path("/home/visky/Work/GPTRunner/.runner-data/chat-dev-runner/managed")
        / PROJECT / "repo.git")
TERMINAL = {"COMPLETED", "BLOCKED", "FAULT", "INTERRUPTED", "REJECTED", "CANCELLED"}


class GateError(Exception):
    """Safe failure code; never include API bodies or source data."""


def require(value: bool, code: str) -> None:
    if not value:
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


def api(opener, method: str, path: str, body: dict | None = None,
        csrf: str | None = None) -> tuple[int, dict]:
    require(path.startswith("/api/v1/") and ".." not in path, "API_PATH_INVALID")
    raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if raw is not None:
        headers.update({"Content-Type": "application/json", "Origin": BASE})
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
    request = Request(BASE + path, data=raw, method=method, headers=headers)
    try:
        response = opener.open(request, timeout=12)
    except HTTPError as error:
        # Do not emit a response body; it may contain mutable provider details.
        raise GateError(f"API_HTTP_{error.code}_{method}_{path.rsplit('/', 1)[-1]}") from None
    with response:
        status = response.status
        payload = response.read(2_000_001)
    require(len(payload) <= 2_000_000, "API_RESPONSE_OVERSIZE")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GateError("API_RESPONSE_INVALID") from error
    require(isinstance(value, dict), "API_RESPONSE_INVALID")
    return status, value


def source_facts(opener) -> dict:
    status, source = api(opener, "GET", f"/api/v1/projects/{PROJECT}/workspaces/{WORKSPACE}/source")
    require(status == 200 and source.get("recorded_head") == COMMIT
            and source.get("observed_head") == COMMIT
            and source.get("recorded_branch") == BRANCH
            and source.get("observed_branch") == BRANCH
            and source.get("recorded_generation") == 1
            and source.get("dirty") is False
            and source.get("matches_recorded") is True, "SOURCE_PREFLIGHT_MISMATCH")
    return source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.apply:
        print("INERT: pass --apply and an absolute --output path for one local operation")
        return 0
    require(args.output is not None and args.output.is_absolute()
            and ".." not in args.output.parts and not args.output.exists(),
            "OUTPUT_PATH_INVALID")
    old_umask = os.umask(0o077)
    try:
        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        status, health = api(opener, "GET", "/api/v1/health")
        require(status == 200 and health.get("version") == "0.2.7"
                and health.get("armed") is True, "INSTALLED_HEALTH_MISMATCH")
        secret = bootstrap(Paths.discover().token_dir / "session-bootstrap.json")
        try:
            status, session = api(opener, "POST", "/api/v1/local/session",
                                  {"bootstrap": secret})
        finally:
            del secret
        require(status == 200 and isinstance(session.get("csrf_token"), str)
                and len(cookies) == 1, "LOCAL_SESSION_FAILED")
        csrf = session["csrf_token"]
        status, system = api(opener, "GET", "/api/v1/system")
        require(status == 200 and system.get("armed") is True
                and "EXPORT_CONTEXT" in system.get("capabilities", [])
                and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", system.get("runner_id", ""))
                and re.fullmatch(r"[0-9a-f-]{36}", system.get("epoch", "")),
                "EXPORT_NOT_AVAILABLE")
        before = source_facts(opener)
        blob = subprocess.run(
            ["git", "-C", str(REPO), "--no-replace-objects", "show", f"{COMMIT}:{PATH}"],
            check=True, capture_output=True, timeout=10,
        ).stdout
        require(0 < len(blob) <= 64 * 1024, "SELECTED_BLOB_NOT_SMALL")
        expected_sha = hashlib.sha256(blob).hexdigest()
        del blob
        now = datetime.now(timezone.utc)
        request_id = str(uuid4())
        command = {
            "schema_version": "1.0", "request_id": request_id, "dispatch": "READY",
            "runner_id": system["runner_id"], "epoch": system["epoch"],
            "issued_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "expires_at": (now + timedelta(minutes=5)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "action": "EXPORT_CONTEXT", "project_id": PROJECT, "workspace_id": WORKSPACE,
            "expected_head": COMMIT, "params": {"paths": [PATH], "include_diff": False},
            "intent": "EP-07 installed owner-local committed README context acceptance",
        }
        accepted_status, accepted = api(opener, "POST", "/api/v1/operations", command, csrf)
        require(accepted_status == 202 and accepted.get("request_id") == request_id,
                "EXPORT_NOT_ADMITTED")
        deadline = time.monotonic() + 60
        operation = {}
        while time.monotonic() < deadline:
            status, operation = api(opener, "GET", f"/api/v1/operations/{request_id}")
            require(status == 200 and operation.get("request_id") == request_id,
                    "OPERATION_READBACK_INVALID")
            if operation.get("status") in TERMINAL:
                break
            time.sleep(0.5)
        require(operation.get("status") == "COMPLETED" and operation.get("code") is None
                and operation.get("source_id") == "local-owner"
                and operation.get("action") == "EXPORT_CONTEXT"
                and operation.get("reconcile_required") == 0,
                f"EXPORT_TERMINAL_{operation.get('status', 'TIMEOUT')}")
        report = operation.get("result", {}).get("context", {})
        inventory = report.get("inventory", [])
        selection = report.get("selection", [])
        require(isinstance(inventory, list) and len(inventory) <= 5_000
                and isinstance(selection, list) and len(selection) == 1,
                "CONTEXT_BOUNDS_INVALID")
        selected = selection[0]
        entry = next((item for item in inventory if item.get("path") == PATH), None)
        require(isinstance(entry, dict) and selected.get("path") == PATH
                and selected.get("status") == "INCLUDED"
                and isinstance(selected.get("text"), str)
                and hashlib.sha256(selected["text"].encode()).hexdigest() == expected_sha
                and entry.get("sha256") == expected_sha,
                "SELECTED_CONTEXT_MISMATCH")
        text_bytes = len(selected["text"].encode())
        require(report.get("source") == "COMMITTED_TREE"
                and report.get("base_commit") == COMMIT
                and report.get("branch") == BRANCH
                and report.get("source_generation") == 1
                and report.get("dirty_at_export") is False
                and report.get("include_diff") is False
                and report.get("privacy_review_required") is True
                and report.get("delivery_status") == "LOCAL_ONLY"
                and report.get("excerpt_bytes") == text_bytes
                and 0 < text_bytes <= 256 * 1024,
                "CONTEXT_RESULT_INVALID")
        receipt = operation.get("receipt", {})
        require(receipt.get("request_id") == request_id
                and receipt.get("status") == "COMPLETED"
                and receipt.get("request_digest") == operation.get("digest")
                and "context" not in receipt and PATH not in json.dumps(receipt)
                and operation.get("effects") == [], "LOCAL_RECEIPT_BOUNDARY_FAILED")
        after = source_facts(opener)
        require(all(before[key] == after[key] for key in (
            "recorded_head", "observed_head", "recorded_branch", "observed_branch",
            "recorded_generation", "dirty", "matches_recorded")), "SOURCE_CHANGED")
        evidence = {
            "schema_version": "1.0", "packet": "EP-07", "mode": "INSTALLED_OWNER_LOCAL_EXPORT_CONTEXT",
            "result": "PASS", "checked_at": datetime.now(timezone.utc).isoformat(),
            "installed_version": health["version"], "request_id": request_id,
            "project_id": PROJECT, "workspace_id": WORKSPACE,
            "runner_epoch": system["epoch"], "source_id": "local-owner",
            "source_commit": COMMIT, "source_branch": BRANCH, "source_generation": 1,
            "selected_path": PATH, "selected_blob_sha256": expected_sha,
            "selected_bytes": text_bytes, "inventory_files": len(inventory),
            "inventory_with_sha256": sum(item.get("sha256") is not None for item in inventory),
            "excerpt_bytes": report["excerpt_bytes"], "privacy_review_required": True,
            "delivery_status": "LOCAL_ONLY", "include_diff": False,
            "terminal_status": operation["status"], "receipt_digest_matches": True,
            "receipt_contains_source": False, "external_effects": 0,
            "source_head_branch_generation_and_cleanliness_preserved": True,
            "drive_command_submitted": False, "provider_writes_by_probe": 0,
        }
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(evidence, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print(f"PASS: local EXPORT_CONTEXT {request_id}; redacted evidence {args.output}")
        return 0
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateError as error:
        raise SystemExit(f"GATE_ERROR:{error}") from None
