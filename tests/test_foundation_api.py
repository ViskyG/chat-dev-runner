"""EP-00 local setup, packaged contracts and real health-server boundaries."""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from bridge_reference.protocol import validate_schema
from runner_app.api.app import create_app
from runner_app.config import Settings, setup_requirements
from runner_app.resources import SCHEMA_NAMES, load_schema

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8765"
DRIVE_ROOT_ID = "177IEHeBVezHIlbIijZsIvRDSbTJ42kxe"
DRIVE_INDEX_ID = "1x2KIBZCZfHs2w-s_Ah3yMW80elgG4KseTe7OZq7L_hA"


@pytest.fixture
def isolated_environment(tmp_path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return environment


def run_cli(environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "runner_app.cli", *arguments],
        env=environment, cwd=ROOT, capture_output=True, text=True, timeout=15, check=False,
    )


def filesystem_snapshot(root: Path) -> dict[str, tuple[int, bytes | None]]:
    return {
        str(path.relative_to(root)): (
            path.stat().st_mode, path.read_bytes() if path.is_file() else None,
        )
        for path in root.rglob("*")
    }


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "::1", "localhost"])
def test_settings_refuse_unimplemented_or_nonloopback_bind(host: str) -> None:
    with pytest.raises(ValidationError):
        Settings(listen_host=host)


@pytest.mark.parametrize("settings", [
    {"listen_port": 0}, {"listen_port": 65536}, {"listen_port": "8765"},
    {"armed": True}, {"drive_mode": "bootstrap"}, {"shell": "bash"},
    {"allow_host_execution": True},
])
def test_settings_do_not_coerce_or_expand_local_authority(settings: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**settings)


def test_setup_preserves_existing_installation_and_unconfigured_providers() -> None:
    setup = setup_requirements()
    assert setup["state"] == "NOT_CONFIGURED"
    assert setup["armed"] is False
    assert setup["ready_commands"] == 0
    google = setup["google"]
    assert google["root_folder_id"] == DRIVE_ROOT_ID
    assert google["installation_index_document_id"] == DRIVE_INDEX_ID
    assert google["mode"] == "attach_existing"
    assert google["workspace_status"] == "EXISTS_OWNER_REPORTED"
    assert google["bootstrap_allowed"] is False
    for provider in (google, setup["github"]):
        assert provider["status"] == "NOT_CONFIGURED"
        assert provider["probe_performed"] is False
        assert provider["required"]
    assert setup["local_policy"]["permitted_import_roots"] == []
    assert setup["local_policy"]["approved_environment_templates"] == []


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765"])
def test_health_only_reports_observed_foundation_capabilities(host: str) -> None:
    with TestClient(create_app(), base_url=BASE_URL) as client:
        response = client.get("/api/v1/health", headers={"Host": host})
    assert response.status_code == 200
    assert response.json()["status"] == "NOT_CONFIGURED"
    assert response.json()["armed"] is False
    assert response.json()["capabilities"] == ["health_only"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("headers,error", [
    ([("Host", "attacker.example")], "INVALID_HOST"),
    ([("Host", "127.0.0.1:8766")], "INVALID_HOST"),
    ([("Host", "localhost")], "INVALID_HOST"),
    ([("Host", "127.0.0.1:8765.attacker.example")], "INVALID_HOST"),
    ([("Host", "127.0.0.1:8765"), ("Host", "attacker.example")], "INVALID_HOST"),
    ([("Host", "127.0.0.1:8765"), ("Host", "127.0.0.1:8765")], "INVALID_HOST"),
    ([("Origin", "https://attacker.example")], "INVALID_ORIGIN"),
    ([("Origin", "http://127.0.0.1:8766")], "INVALID_ORIGIN"),
    ([("Origin", "null")], "INVALID_ORIGIN"),
    ([("Origin", "")], "INVALID_ORIGIN"),
    ([("Origin", BASE_URL), ("Origin", "https://attacker.example")], "INVALID_ORIGIN"),
    ([("Origin", BASE_URL), ("Origin", BASE_URL)], "INVALID_ORIGIN"),
])
def test_health_rejects_spoofed_or_duplicate_boundary_headers(
    headers: list[tuple[str, str]], error: str,
) -> None:
    with TestClient(create_app(), base_url=BASE_URL) as client:
        response = client.get("/api/v1/health", headers=headers)
    assert response.status_code == 403
    assert response.json() == {"error": error}


@pytest.mark.parametrize("origin", [BASE_URL, "http://localhost:8765"])
def test_exact_local_origin_is_allowed(origin: str) -> None:
    with TestClient(create_app(), base_url=BASE_URL) as client:
        assert client.get("/api/v1/health", headers={"Origin": origin}).status_code == 200


@pytest.mark.parametrize("method,path,status", [
    ("GET", "/docs", 404), ("GET", "/redoc", 404), ("GET", "/openapi.json", 404),
    ("POST", "/api/v1/health", 405), ("DELETE", "/api/v1/health", 405),
    ("POST", "/api/v1/commands", 404), ("POST", "/api/v1/configure", 404),
])
def test_foundation_exposes_no_docs_or_mutation_api(method: str, path: str, status: int) -> None:
    with TestClient(create_app(), base_url=BASE_URL) as client:
        assert client.request(method, path).status_code == status


@pytest.mark.parametrize("arguments", [
    ("doctor", "--offline"), ("install", "--dry-run"), ("configure", "--show-required"),
])
def test_inspection_commands_are_side_effect_free_and_honest(
    arguments: tuple[str, ...], isolated_environment: dict[str, str], tmp_path: Path,
) -> None:
    sentinel = tmp_path / "untouched.txt"
    sentinel.write_bytes(b"existing state\x00\xff")
    before = filesystem_snapshot(tmp_path)
    result = run_cli(isolated_environment, *arguments)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert filesystem_snapshot(tmp_path) == before
    inspected = arguments[0] != "configure"
    assert report.get("status", report.get("state")) == (
        "CONFIGURATION_NOT_PROBED" if inspected else "NOT_CONFIGURED"
    )
    assert report["armed"] is (None if inspected else False)
    setup = report if not inspected else report["setup"]
    assert setup["google"]["installation_index_document_id"] == DRIVE_INDEX_ID
    assert setup["google"]["root_folder_id"] == DRIVE_ROOT_ID
    assert setup["google"]["bootstrap_allowed"] is False
    assert setup["github"]["status"] == ("NOT_PROBED" if inspected else "NOT_CONFIGURED")
    if arguments[0] != "configure":
        assert report["network_calls"] is False
        assert report["sandbox"]["host_execution_fallback"] is False
        assert report["sandbox"]["probe_performed"] is False
        assert report["missing_prerequisites"]
    if arguments[0] == "install":
        assert report["mode"] == "DRY_RUN"
        assert report["runtime_package"]["status"] in {
            "WHEEL", "EDITABLE_SOURCE", "MISSING", "NOT_PROBED",
        }
        assert report["installable"] is (
            report["runtime_package"]["status"] == "WHEEL"
        )
        assert report["service_changes"] is False
        assert report["cloud_writes"] is False
        assert report["would_start_armed"] is None
        assert report["startup_arm_policy"] == "CONFIGURED_RUNTIME"
        assert report["live_start_may_publish_system_state"] is True
        assert report["argv"][4:6] == ["--host", "127.0.0.1"]


@pytest.mark.parametrize("arguments", [
    ("doctor",), ("install",), ("configure",), ("uninstall",),
])
def test_cli_requires_explicit_command_options(
    arguments: tuple[str, ...], isolated_environment: dict[str, str], tmp_path: Path,
) -> None:
    before = filesystem_snapshot(tmp_path)
    result = run_cli(isolated_environment, *arguments)
    assert result.returncode == 2
    assert filesystem_snapshot(tmp_path) == before


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "::1"])
def test_cli_refuses_unsafe_bind_before_startup(
    host: str, isolated_environment: dict[str, str], tmp_path: Path,
) -> None:
    before = filesystem_snapshot(tmp_path)
    result = run_cli(isolated_environment, "serve", "--host", host)
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"] == "INVALID_LOCAL_CONFIGURATION"
    assert "Uvicorn running" not in result.stderr
    assert filesystem_snapshot(tmp_path) == before


@pytest.fixture
def health_process(isolated_environment: dict[str, str]) -> Iterator[int]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "runner_app.cli", "serve", "--health-only", "--host", "127.0.0.1",
         "--port", str(port)],
        env=isolated_environment, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"Health server exited {process.returncode}: {stdout}\n{stderr}")
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.2)
            try:
                connection.request("GET", "/api/v1/health")
                response = connection.getresponse()
                response.read()
                if response.status == 200:
                    break
            except OSError:
                time.sleep(0.025)
            finally:
                connection.close()
        else:
            pytest.fail("Real loopback health process did not start within 10 seconds")
        yield port
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def test_real_loopback_process_health_and_host_boundary(health_process: int, tmp_path: Path) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", health_process, timeout=2)
    try:
        connection.request("GET", "/api/v1/health")
        response = connection.getresponse()
        assert response.status == 200
        body = json.loads(response.read())
        assert body["status"] == "NOT_CONFIGURED"
        assert body["armed"] is False
        connection.request("GET", "/api/v1/health", headers={"Host": "attacker.example"})
        response = connection.getresponse()
        assert response.status == 403
        assert json.loads(response.read()) == {"error": "INVALID_HOST"}
    finally:
        connection.close()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", sorted(SCHEMA_NAMES))
def test_canonical_schemas_are_available_as_package_resources(name: str) -> None:
    resource = files("schemas").joinpath(f"{name}.schema.json")
    schema = load_schema(name)
    assert json.loads(resource.read_text()) == schema
    assert schema == json.loads((ROOT / "schemas" / f"{name}.schema.json").read_text())
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("name", ["../command", "command.schema.json", "missing", "/etc/passwd"])
def test_schema_loader_rejects_nonregistry_names(name: str) -> None:
    with pytest.raises(ValueError, match="Unknown schema"):
        load_schema(name)


def test_reference_and_production_schema_readers_accept_same_command() -> None:
    command = json.loads((ROOT / "examples" / "verify.command.json").read_text())
    Draft202012Validator(load_schema("command")).validate(command)
    validate_schema(command, "command")
