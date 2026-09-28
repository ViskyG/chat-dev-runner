"""Authenticated API over the real ledger plus explicitly synthetic finite handlers."""
from __future__ import annotations

import http.client
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from runner_app.api.auth import Sessions
from runner_app.api.runtime import create_runtime_app
from runner_app.config import Settings
from runner_app.domain.commands import FrozenCommand, LocalPolicy
from runner_app.engine.runtime import Runtime
from runner_app.engine.service import Context, Engine, OperationResult
from runner_app.storage import Database, Paths
from runner_app.storage.ledger import Ledger

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def api(tmp_path: Path) -> Iterator[dict[str, Any]]:
    ledger = Ledger(Database(tmp_path / "database" / "state.sqlite"), max_parallel=1)
    ledger.startup()
    entered = threading.Event()
    calls: list[str] = []

    def handler(command: FrozenCommand, context: Context) -> OperationResult:
        calls.append(command.command["request_id"])
        entered.set()
        cancelled = context.cancel.wait(timeout=5)
        return OperationResult(status="CANCELLED" if cancelled else "COMPLETED")

    policy = LocalPolicy(
        approved_profiles=frozenset({"fake-verification"}), project_ids=frozenset({"fixture"}),
        workspace_pairs=frozenset({("fixture", "workspace")}),
    )
    engine = Engine(ledger, policy=policy, handlers={"VERIFY": handler})
    sessions = Sessions()
    bootstrap = sessions.issue_bootstrap()
    with TestClient(create_runtime_app(Settings(), engine, sessions), base_url=ORIGIN) as client:
        response = client.post("/api/v1/local/session", json={"bootstrap": bootstrap},
                               headers={"Origin": ORIGIN})
        assert response.status_code == 200
        csrf = response.json()["csrf_token"]
        yield {"client": client, "engine": engine, "ledger": ledger, "csrf": csrf,
               "headers": {"Origin": ORIGIN, "X-CSRF-Token": csrf}, "bootstrap": bootstrap,
               "entered": entered, "calls": calls, "cookie_header": response.headers["set-cookie"]}
    engine.shutdown()


def command(api: dict[str, Any], **updates: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    value = {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": "local-runner", "epoch": api["ledger"].state()["epoch"],
        "issued_at": now.isoformat(), "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "action": "VERIFY", "project_id": "fixture", "workspace_id": "workspace",
        "expected_head": "a" * 40, "params": {"profile_id": "fake-verification"},
        "intent": "Explicit deterministic API test; no project code or provider calls.",
    }
    value.update(updates)
    return value


def test_ui_shell_is_public_but_api_session_is_private(api: dict[str, Any]) -> None:
    client = api["client"]
    client.cookies.clear()
    index = client.get("/")
    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert "no-store" in index.headers["cache-control"]
    assert index.headers["x-content-type-options"] == "nosniff"
    csp = index.headers["content-security-policy"]
    for directive in ("script-src 'self'", "style-src 'self'", "connect-src 'self'",
                      "frame-ancestors 'none'", "object-src 'none'"):
        assert directive in csp
    assert "unsafe-inline" not in csp
    assert "img-src 'self' data: blob:" in csp
    assert "script-src 'self';" in csp
    assert api["bootstrap"] not in index.text

    for filename, media_type in (("app.js", "text/javascript"),
                                 ("api.js", "text/javascript"),
                                 ("model.js", "text/javascript"),
                                 ("render.js", "text/javascript"),
                                 ("styles.css", "text/css")):
        asset = client.get(f"/assets/{filename}")
        assert asset.status_code == 200
        assert asset.headers["content-type"].startswith(media_type)
        assert asset.headers["content-security-policy"] == csp
    assert client.get("/api/v1/local/session").json()["error"] == "SESSION_REQUIRED"
    assert client.get("/api/v1/system").json()["error"] == "SESSION_REQUIRED"
    assert client.get("/assets/unknown.js").status_code == 403


def test_ui_shell_rejects_forged_origin_and_host(api: dict[str, Any]) -> None:
    client = api["client"]
    for path in ("/", "/assets/app.js"):
        assert client.get(path, headers={"Host": "attacker.example"}).json()["error"] == "INVALID_HOST"
        assert client.get(path, headers={"Origin": "http://attacker.example"}).json()["error"] == "INVALID_ORIGIN"
    assert client.get("/assets/unknown.js").status_code == 404
    assert client.get("/assets/secret.svg").status_code == 404


def test_existing_session_rehydrates_csrf_without_new_bootstrap(api: dict[str, Any]) -> None:
    response = api["client"].get("/api/v1/local/session")
    assert response.status_code == 200
    assert response.json()["csrf_token"] == api["csrf"]
    assert 0 < response.json()["expires_in"] <= 28_800
    assert api["bootstrap"] not in response.text
    assert api["client"].cookies.get("cdr_session") not in response.text


def test_bootstrap_is_single_use_and_cookie_is_private(api: dict[str, Any]) -> None:
    cookie = api["cookie_header"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    repeated = api["client"].post("/api/v1/local/session", json={"bootstrap": api["bootstrap"]},
                                  headers={"Origin": ORIGIN})
    assert repeated.status_code == 403
    assert repeated.json()["error"] == "INVALID_BOOTSTRAP"
    system = api["client"].get("/api/v1/system")
    assert system.status_code == 200
    assert system.json()["armed"] is False
    assert system.json()["execution_queue"] is False
    assert system.json()["approved"]["profiles"] == ["fake-verification"]
    assert system.json()["approved"]["templates"] == []
    assert api["bootstrap"] not in system.text
    assert api["csrf"] not in system.text
    assert api["client"].cookies.get("cdr_session") not in system.text


@pytest.mark.parametrize("path", ["/api/v1/system", "/api/v1/operations", "/api/v1/events"])
def test_read_and_sse_endpoints_require_a_session(api: dict[str, Any], path: str) -> None:
    api["client"].cookies.clear()
    response = api["client"].get(path)
    assert response.status_code == 403
    assert response.json()["error"] == "SESSION_REQUIRED"


@pytest.mark.parametrize("headers,error,status", [
    ({}, "ORIGIN_REQUIRED", 403),
    ({"Origin": "http://attacker.example"}, "INVALID_ORIGIN", 403),
    ({"Origin": ORIGIN}, "INVALID_CSRF", 403),
    ({"Origin": ORIGIN, "X-CSRF-Token": "forged"}, "INVALID_CSRF", 403),
    ({"Origin": ORIGIN, "Host": "attacker.example"}, "INVALID_HOST", 403),
    ({"Origin": ORIGIN, "Content-Type": "text/plain"}, "JSON_REQUIRED", 415),
])
def test_mutations_require_valid_origin_host_json_and_csrf(
    api: dict[str, Any], headers: dict[str, str], error: str, status: int,
) -> None:
    before = api["ledger"].state()
    response = api["client"].post("/api/v1/local/arm", json={}, headers=headers)
    assert response.status_code == status
    assert response.json()["error"] == error
    assert api["ledger"].state() == before


def test_duplicate_csrf_header_is_refused(api: dict[str, Any]) -> None:
    response = api["client"].post("/api/v1/local/arm", json={}, headers=[
        ("Origin", ORIGIN), ("X-CSRF-Token", api["csrf"]), ("X-CSRF-Token", api["csrf"]),
    ])
    assert response.status_code == 403
    assert response.json()["error"] == "INVALID_CSRF"


@pytest.mark.parametrize("path", ["/api/v1/system", "/api/v1/events"])
def test_read_routes_reject_forged_host_and_origin(api: dict[str, Any], path: str) -> None:
    client = api["client"]
    for headers, error in (
        ({"Host": "attacker.example"}, "INVALID_HOST"),
        ({"Origin": "http://attacker.example"}, "INVALID_ORIGIN"),
        ({"Origin": "null"}, "INVALID_ORIGIN"),
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 403
        assert response.json()["error"] == error
        assert "access-control-allow-origin" not in response.headers
    duplicate_host = client.get(path, headers=[("Host", "127.0.0.1:8765"),
                                               ("Host", "attacker.example")])
    assert duplicate_host.status_code == 403
    assert duplicate_host.json()["error"] == "INVALID_HOST"


def test_get_cannot_arm_and_invalid_json_is_terminal(api: dict[str, Any]) -> None:
    before = api["ledger"].state()
    assert api["client"].get("/api/v1/local/arm").status_code == 405
    headers = {**api["headers"], "Content-Type": "application/json"}
    response = api["client"].post("/api/v1/operations", content=b'{"x":1,"x":2}', headers=headers)
    assert response.status_code == 422
    assert response.json()["error"] == "DUPLICATE_JSON_KEY"
    assert api["ledger"].state() == before


def test_gets_cannot_disarm_or_change_durable_state(api: dict[str, Any]) -> None:
    client = api["client"]
    assert client.post("/api/v1/local/arm", json={}, headers=api["headers"]).status_code == 200
    state = api["ledger"].state()
    journal = api["ledger"].events()
    for path in ("/api/v1/local/arm", "/api/v1/local/disarm"):
        assert client.get(path).status_code == 405
    assert client.get("/api/v1/local/session").json()["csrf_token"] == api["csrf"]
    for path in ("/api/v1/system", "/api/v1/projects", "/api/v1/operations",
                 "/api/v1/events"):
        assert client.get(path).status_code == 200
    assert api["ledger"].state() == state
    assert api["ledger"].events() == journal
    assert api["ledger"].list_operations()["items"] == []


def test_body_limit_and_artifact_path_traversal(api: dict[str, Any]) -> None:
    response = api["client"].post("/api/v1/operations", content=b" " * 262_145,
                                  headers={**api["headers"], "Content-Type": "application/json"})
    assert response.status_code == 413
    for path in ("/api/v1/runs/run/outputs/%2e%2e%2fsecret", "/api/v1/runs/run/outputs/.env"):
        response = api["client"].get(path)
        assert response.status_code in {404, 422}
    assert api["ledger"].list_operations()["items"] == []


def test_oversized_json_is_refused_with_and_without_content_length(api: dict[str, Any]) -> None:
    payload = b'{"x":"' + b"a" * 262_144 + b'"}'
    headers = {**api["headers"], "Content-Type": "application/json"}
    declared = api["client"].post("/api/v1/operations", content=payload, headers=headers)
    streaming_request = api["client"].build_request(
        "POST", "/api/v1/operations", content=iter((payload[:32], payload[32:])), headers=headers,
    )
    assert "content-length" not in streaming_request.headers
    streamed = api["client"].send(streaming_request)
    for response in (declared, streamed):
        assert response.status_code == 413
        assert response.json()["error"] == "PAYLOAD_TOO_LARGE"
    assert api["ledger"].list_operations()["items"] == []


@pytest.mark.parametrize("path", [
    "/api/v1/operations/not-a-uuid",
    "/api/v1/runs/not-a-uuid",
    "/api/v1/runs/not-a-uuid/outputs",
])
def test_malformed_resource_uuids_are_rejected(api: dict[str, Any], path: str) -> None:
    response = api["client"].get(path)
    assert response.status_code == 422
    assert api["ledger"].list_operations()["items"] == []


def test_output_route_rejects_unsafe_ids_while_storage_is_unavailable(api: dict[str, Any]) -> None:
    run_id = str(uuid4())
    prefix = f"/api/v1/runs/{run_id}/outputs/"
    for suffix in (".env", "%2e%2e%2fsecret", "%252e%252e%252fsecret", "secret.txt"):
        response = api["client"].get(prefix + suffix)
        assert response.status_code in {404, 422}
        assert "access-control-allow-origin" not in response.headers
    unavailable = api["client"].get(prefix + "safe-output-id")
    assert unavailable.status_code == 503
    assert unavailable.json()["error"] == "OUTPUT_STORAGE_UNAVAILABLE"
    assert api["ledger"].list_operations()["items"] == []


def test_immediate_admission_dedup_busy_and_cancellation_without_free_slot(api: dict[str, Any]) -> None:
    client, headers = api["client"], api["headers"]
    assert client.post("/api/v1/local/arm", json={}, headers=headers).status_code == 200
    first = command(api)
    admitted = client.post("/api/v1/operations", json=first, headers=headers)
    assert admitted.status_code == 202
    assert admitted.json()["status"] == "RUNNING"
    assert api["entered"].wait(timeout=2)
    duplicate = client.post("/api/v1/operations", json=first, headers=headers)
    assert duplicate.status_code == 200
    assert duplicate.json() == admitted.json()
    refused = command(api)
    busy = client.post("/api/v1/operations", json=refused, headers=headers)
    assert busy.status_code == 409 and busy.json()["code"] == "BUSY"
    assert api["ledger"].state()["reserved_capacity"] == 1
    cancellation = command(api, action="CANCEL_OPERATION",
                           params={"target_request_id": first["request_id"]})
    cancelled = client.post("/api/v1/operations", json=cancellation, headers=headers)
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "COMPLETED"
    deadline = time.monotonic() + 2
    while api["ledger"].get(first["request_id"])["status"] == "RUNNING":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert api["ledger"].get(first["request_id"])["status"] == "CANCELLED"
    assert api["calls"] == [first["request_id"]]
    repeated_busy = client.post("/api/v1/operations", json=refused, headers=headers)
    assert repeated_busy.status_code == 200
    assert repeated_busy.json()["code"] == "BUSY"
    assert api["calls"] == [first["request_id"]]
    events = client.get("/api/v1/events")
    assert events.status_code == 200 and "text/event-stream" in events.headers["content-type"]
    ids = [int(line[4:]) for line in events.text.splitlines() if line.startswith("id: ")]
    assert ids and ids == sorted(ids)
    resumed = client.get("/api/v1/events", headers={"Last-Event-ID": str(ids[-1])})
    assert resumed.status_code == 200
    assert "id: " not in resumed.text
    assert client.get("/api/v1/events", headers={"Last-Event-ID": "../secret"}).status_code == 422



def test_sse_rejects_invalid_query_cursors(api: dict[str, Any]) -> None:
    client = api["client"]
    for after in ("-1", "nan", "999999999999999999999999"):
        response = client.get(f"/api/v1/events?after={after}")
        assert response.status_code == 422


def test_runtime_reissues_owner_only_bootstrap_without_restarting_or_revoking_session(
    tmp_path: Path,
) -> None:
    paths = Paths(config_dir=tmp_path / "config", state_dir=tmp_path / "state",
                  cache_dir=tmp_path / "cache")
    runtime = Runtime(Settings(), paths)
    bootstrap_file = paths.token_dir / "session-bootstrap.json"
    first = json.loads(bootstrap_file.read_text())["bootstrap"]
    assert stat.S_IMODE(bootstrap_file.stat().st_mode) == 0o600
    try:
        with TestClient(runtime.app, base_url=ORIGIN) as client:
            exchange = client.post("/api/v1/local/session", json={"bootstrap": first},
                                   headers={"Origin": ORIGIN})
            assert exchange.status_code == 200
            csrf = exchange.json()["csrf_token"]
            deadline = time.monotonic() + 2
            while json.loads(bootstrap_file.read_text())["bootstrap"] == first:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            second = json.loads(bootstrap_file.read_text())["bootstrap"]
            assert second != first
            assert stat.S_IMODE(bootstrap_file.stat().st_mode) == 0o600
            assert client.get("/api/v1/local/session").json()["csrf_token"] == csrf
            assert client.get("/api/v1/system").status_code == 200
            assert client.post("/api/v1/local/session", json={"bootstrap": first},
                               headers={"Origin": ORIGIN}).status_code == 403
            assert client.post("/api/v1/local/session", json={"bootstrap": second},
                               headers={"Origin": ORIGIN}).status_code == 200
    finally:
        runtime.close()
    assert not bootstrap_file.exists()


def test_failed_bootstrap_publication_keeps_previous_secret() -> None:
    sessions = Sessions()
    first = sessions.issue_bootstrap()

    def fail(_secret: str) -> None:
        raise OSError("fake publication failure")

    with pytest.raises(OSError):
        sessions.issue_bootstrap(fail)
    _, session = sessions.exchange(first)
    assert session.csrf


def test_real_runtime_private_handoff_and_unavailable_execution(tmp_path: Path) -> None:
    environment = os.environ.copy()
    for name, directory in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                            ("XDG_CACHE_HOME", "cache"), ("XDG_RUNTIME_DIR", "runtime")):
        environment[name] = str(tmp_path / directory)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    bootstrap_file = tmp_path / "config/chat-dev-runner/tokens/session-bootstrap.json"
    process = subprocess.Popen(
        [sys.executable, "-m", "runner_app.cli", "serve", "--port", str(port)],
        cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    secret = ""
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"Runtime failed: {process.returncode}; {stdout}; {stderr}")
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.3)
            try:
                connection.request("GET", "/api/v1/health")
                response = connection.getresponse()
                response.read()
                if response.status == 200 and bootstrap_file.exists():
                    break
            except OSError:
                time.sleep(0.025)
            finally:
                connection.close()
        else:
            pytest.fail("Runtime did not start within ten seconds")
        assert stat.S_IMODE(bootstrap_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(bootstrap_file.parent.stat().st_mode) == 0o700
        secret = json.loads(bootstrap_file.read_text())["bootstrap"]
        origin = f"http://127.0.0.1:{port}"
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", "/api/v1/system")
            response = connection.getresponse()
            assert response.status == 403
            response.read()
            connection.request("POST", "/api/v1/local/session", json.dumps({"bootstrap": secret}),
                               {"Origin": origin, "Content-Type": "application/json"})
            response = connection.getresponse()
            assert response.status == 200
            csrf = json.loads(response.read())["csrf_token"]
            cookie = response.getheader("Set-Cookie").split(";", 1)[0]
            connection.request("GET", "/api/v1/system", headers={"Cookie": cookie})
            response = connection.getresponse()
            body = response.read()
            assert response.status == 200
            system = json.loads(body)
            assert system["armed"] is False and system["capabilities"] == []
            assert secret.encode() not in body and csrf.encode() not in body
            connection.request("POST", "/api/v1/local/arm", "{}", {
                "Origin": origin, "Content-Type": "application/json", "Cookie": cookie,
                "X-CSRF-Token": csrf,
            })
            response = connection.getresponse()
            assert response.status == 503
            assert json.loads(response.read())["error"] == "ACTION_UNAVAILABLE"
            connection.request("POST", "/api/v1/local/session", json.dumps({"bootstrap": secret}),
                               {"Origin": origin, "Content-Type": "application/json"})
            response = connection.getresponse()
            assert response.status == 403
            response.read()
        finally:
            connection.close()
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        if secret:
            assert secret not in stdout + stderr
    assert not bootstrap_file.exists()

    restarted = subprocess.Popen(
        [sys.executable, "-m", "runner_app.cli", "serve", "--port", str(port)],
        cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    replacement_secret = ""
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if restarted.poll() is not None:
                stdout, stderr = restarted.communicate()
                pytest.fail(f"Restarted runtime failed: {restarted.returncode}; {stdout}; {stderr}")
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.3)
            try:
                connection.request("GET", "/api/v1/health")
                response = connection.getresponse()
                response.read()
                if response.status == 200 and bootstrap_file.exists():
                    break
            except OSError:
                time.sleep(0.025)
            finally:
                connection.close()
        else:
            pytest.fail("Restarted runtime did not start within ten seconds")
        replacement_secret = json.loads(bootstrap_file.read_text())["bootstrap"]
        assert replacement_secret != secret
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", "/api/v1/system", headers={"Cookie": cookie})
            response = connection.getresponse()
            assert response.status == 403
            assert json.loads(response.read())["error"] == "SESSION_EXPIRED"
            connection.request("POST", "/api/v1/local/session", json.dumps({"bootstrap": secret}),
                               {"Origin": origin, "Content-Type": "application/json"})
            response = connection.getresponse()
            assert response.status == 403
            assert json.loads(response.read())["error"] == "INVALID_BOOTSTRAP"
            connection.request("POST", "/api/v1/local/session",
                               json.dumps({"bootstrap": replacement_secret}),
                               {"Origin": origin, "Content-Type": "application/json"})
            response = connection.getresponse()
            assert response.status == 200
            response.read()
            replacement_cookie = response.getheader("Set-Cookie").split(";", 1)[0]
            assert replacement_cookie != cookie
            connection.request("GET", "/api/v1/system", headers={"Cookie": replacement_cookie})
            response = connection.getresponse()
            assert response.status == 200
            restarted_system = json.loads(response.read())
            assert restarted_system["armed"] is False
            assert restarted_system["epoch"] != system["epoch"]
        finally:
            connection.close()
    finally:
        if restarted.poll() is None:
            restarted.terminate()
        try:
            stdout, stderr = restarted.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            restarted.kill()
            stdout, stderr = restarted.communicate(timeout=5)
        if replacement_secret:
            assert replacement_secret not in stdout + stderr
    assert not bootstrap_file.exists()
