"""Owner-configured Runtime attaches existing identities without provider writes."""
from __future__ import annotations

import json
import os
import threading
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from runner_app.adapters.github import GitHubHTTPResponse, GitHubREST
from runner_app.config import DRIVE_INDEX_ID, DRIVE_ROOT_ID, Settings
from runner_app.engine import integrations
from runner_app.engine.execution_assembly import OwnerExecutionAssembly
from runner_app.engine.integrations import (
    IntegrationConfigError,
    OwnerRuntimeConfig,
    ProviderConnections,
    load_owner_runtime_config,
)
from runner_app.engine.runtime import Runtime
from runner_app.storage.ledger import LedgerError
from runner_app.storage.paths import Paths


def _paths(tmp_path: Path) -> Paths:
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    paths.ensure_private()
    return paths


def _config() -> dict[str, Any]:
    return {
        "schema_version": "1.0", "mode": "attach_existing",
        "drive_root_id": DRIVE_ROOT_ID, "drive_index_id": DRIVE_INDEX_ID,
        "github_login": "ViskyG", "github_user_id": 12452296,
        "github_identity": "viskyg", "template_profiles": {"python-basic": "python-quick"},
        "import_roots": [], "github_imports": [],
    }


def _owner_file(paths: Paths, name: str, content: dict[str, Any]) -> Path:
    path = paths.config_dir / name
    path.write_text(json.dumps(content))
    path.chmod(0o600)
    return path


class _GitHubUser:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str, **_: Any) -> GitHubHTTPResponse:
        self.calls.append((method, path))
        if (method, path) != ("GET", "/user"):
            raise AssertionError("Startup may read only the authenticated identity")
        return GitHubHTTPResponse(200, {"login": "ViskyG", "id": 12452296})


def test_absent_owner_config_keeps_runtime_unarmed_and_without_handlers(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    runtime = Runtime(Settings(), paths)
    try:
        assert runtime.integrations is None
        assert runtime.workspace_mailboxes is None
        assert runtime.engine.capabilities() == []
        assert runtime.ledger.state()["armed"] is False
        assert runtime.arm_on_startup() is False
    finally:
        runtime.close()


def test_owner_config_requires_exact_ids_and_private_file(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    value = _config()
    value["drive_root_id"] = "another-root"
    path = _owner_file(paths, integrations.CONFIG_FILENAME, value)
    with pytest.raises(IntegrationConfigError) as error:
        load_owner_runtime_config(paths)
    assert error.value.code == "RUNTIME_CONFIG_INVALID"
    value = _config()
    value["github_token"] = "credential-must-not-be-in-this-file"
    _owner_file(paths, integrations.CONFIG_FILENAME, value)
    with pytest.raises(IntegrationConfigError) as error:
        load_owner_runtime_config(paths)
    assert error.value.code == "RUNTIME_CONFIG_INVALID"
    _owner_file(paths, integrations.CONFIG_FILENAME, _config())
    path.chmod(0o644)
    with pytest.raises(IntegrationConfigError) as error:
        load_owner_runtime_config(paths)
    assert error.value.code == "RUNTIME_CONFIG_INVALID"


def test_configured_runtime_attaches_existing_without_provider_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    fake, _, records, manifest = import_module("test_drive_index").legacy_fixture()
    _owner_file(paths, integrations.CONFIG_FILENAME, _config())
    _owner_file(paths, "installation-legacy.json", manifest)
    transport = _GitHubUser()
    github = GitHubREST(transport, expected_login="ViskyG", expected_user_id=12452296)
    connections = ProviderConnections(
        reader=fake,
        upload_factory=lambda _attached: cast(Any, object()),
        github=github,
        token_provider=lambda: "unused-in-read-only-startup",
    )
    monkeypatch.setattr(integrations, "_real_connections", lambda *_: connections)
    runtime = Runtime(Settings(), paths)
    try:
        assert runtime.integrations is not None
        assert runtime.workspace_mailboxes is not None
        assert runtime.workspace_mailboxes.mailboxes() == {}
        assert runtime.workspace_mailboxes.active_count() == 0
        assert runtime.integrations.attached.index.identity_mode == "owner_manifest"
        assert runtime.integrations.attached.root_mailbox().command_id == records["command"]["id"]
        assert runtime.integrations.root_receipt.command_id == records["command"]["id"]
        assert set(runtime.engine.capabilities()) == {
            "CREATE_PROJECT", "CREATE_WORKSPACE", "FETCH", "PUBLISH_REPORT",
            "RECONCILE", "RESUME_PROVISION", "RESUME_RETARGET", "RETARGET_WORKSPACE",
            "EXPORT_CONTEXT",
        }
        assert records["command"]["id"] in runtime.engine.policy.mailboxes
        assert runtime.ledger.state()["armed"] is False
        assert runtime.arm_on_startup() is False  # polling is not initialized yet
        runtime.drive_bridge = cast(Any, SimpleNamespace(request_stop=lambda: None, close=lambda **_: None))
        initial_epoch = runtime.ledger.state()["epoch"]
        assert runtime.arm_on_startup() is True
        armed = runtime.ledger.state()
        assert armed["armed"] is True and armed["epoch"] != initial_epoch
        assert runtime.arm_on_startup() is True
        assert runtime.ledger.state()["epoch"] == armed["epoch"]
        runtime.ledger.disarm()

        def needs_reconciliation() -> dict[str, Any]:
            raise LedgerError("NEEDS_RECONCILIATION", "uncertain effect")

        monkeypatch.setattr(runtime.ledger, "arm", needs_reconciliation)
        assert runtime.arm_on_startup() is False
        assert runtime.ledger.state()["armed"] is False
        assert transport.calls == [("GET", "/user")]
        assert all(method in {"files.get", "documents.get"} for method, _ in fake.calls)
    finally:
        runtime.close()
    assert os.stat(paths.config_dir / integrations.CONFIG_FILENAME).st_mode & 0o777 == 0o600



def test_runtime_shutdown_signals_all_pollers_before_bounded_joins(tmp_path: Path) -> None:
    events: list[str] = []
    deadlines: list[float] = []
    runtime = cast(Any, object.__new__(Runtime))
    runtime._bootstrap_stop = threading.Event()
    runtime._bootstrap_refresh = threading.Event()
    runtime._bootstrap_thread = None
    runtime._bootstrap_path = tmp_path / "unused-bootstrap.json"

    def stop(name: str) -> None:
        events.append(f"stop:{name}")

    def join(name: str, *, deadline: float) -> None:
        events.append(f"join:{name}")
        deadlines.append(deadline)
        assert events[:2] == ["stop:manager", "stop:root"]

    runtime.workspace_mailboxes = SimpleNamespace(
        request_stop=lambda: stop("manager"),
        close=lambda *, deadline: join("manager", deadline=deadline),
    )
    runtime.drive_bridge = SimpleNamespace(
        request_stop=lambda: stop("root"),
        close=lambda *, deadline: join("root", deadline=deadline),
    )
    runtime.engine = SimpleNamespace(
        shutdown=lambda *, timeout: events.append(f"engine:{timeout <= 2.0}"),
    )
    runtime._singleton = SimpleNamespace(close=lambda: events.append("singleton"))

    runtime.close()
    assert events == ["stop:manager", "stop:root", "join:manager", "join:root",
                      "engine:True", "singleton"]
    assert len(deadlines) == 2 and deadlines[0] == deadlines[1]
    assert runtime._bootstrap_stop.is_set()


def test_owner_config_has_no_implicit_import_or_github_approval() -> None:
    config = OwnerRuntimeConfig.from_mapping(_config())
    assert config.import_roots == ()
    assert config.github_imports == {}


def test_configured_profile_dir_keeps_workspace_inert_when_quota_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    fake, _, _, manifest = import_module("test_drive_index").legacy_fixture()
    config = _config()
    config["execution_profile_dir"] = str(tmp_path / "owner-profiles")
    _owner_file(paths, integrations.CONFIG_FILENAME, config)
    _owner_file(paths, "installation-legacy.json", manifest)
    github = GitHubREST(_GitHubUser(), expected_login="ViskyG",
                        expected_user_id=12452296)
    monkeypatch.setattr(integrations, "_real_connections", lambda *_: ProviderConnections(
        fake, lambda _attached: cast(Any, object()), github, lambda: "unused",
    ))
    called: list[Path] = []

    def unavailable(_paths: Paths, _ledger: Any, _registry: Any, _evidence: Any,
                    profile_dir: Path) -> OwnerExecutionAssembly:
        called.append(profile_dir)
        return OwnerExecutionAssembly(None, "VOLUME_HELPER_UNAVAILABLE")

    monkeypatch.setattr("runner_app.engine.runtime.build_owner_execution_ports", unavailable)
    runtime = Runtime(Settings(), paths)
    try:
        assert called == [tmp_path / "owner-profiles"]
        assert runtime.execution_readiness_code == "VOLUME_HELPER_UNAVAILABLE"
        assert runtime.workspace_mailboxes is not None
        assert runtime.workspace_mailboxes.active_count() == 0
        assert "DELIVER_VERIFY_PUBLISH" not in runtime.engine.capabilities()
        assert runtime.ledger.state()["armed"] is False
    finally:
        runtime.close()


def test_real_connection_builder_isolates_both_google_service_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    credentials = object()
    request_builder = object()
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake_build(name: str, version: str, **kwargs: Any) -> object:
        calls.append((name, version, kwargs))
        return object()

    monkeypatch.setattr(integrations, "load_authorized_credentials",
                        lambda *_args, **_kwargs: credentials)
    monkeypatch.setattr(integrations, "isolated_request_builder",
                        lambda selected: request_builder if selected is credentials else pytest.fail())
    monkeypatch.setattr(integrations, "import_module",
                        lambda _name: SimpleNamespace(build=fake_build))
    connections = integrations._real_connections(
        paths, OwnerRuntimeConfig.from_mapping(_config()),
    )
    assert connections.reader is not None
    assert [(name, version) for name, version, _ in calls] == [
        ("drive", "v3"), ("docs", "v1"),
    ]
    assert all(options["credentials"] is credentials
               and options["requestBuilder"] is request_builder
               and options["cache_discovery"] is False
               for _, _, options in calls)
