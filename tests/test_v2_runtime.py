"""Configuration/startup/API boundary, never reading actual credentials."""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from runner_app.config import Settings
from runner_app.config_v2 import V2ConfigError, load_v2_config, parse_v2_config
from runner_app.engine.runtime import Runtime
from runner_app.storage.paths import Paths


def configuration() -> dict:
    return {"protocol": "cdr-runtime/2", "runner_id": "runner-1", "enable_v1": False,
            "github_token_file": "/nonexistent-owner-secrets/github-token",
            "google_config_file": "/nonexistent-owner-config/google.json",
            "projects": [{"project_id": "project-1", "github": {
                "id": 123, "owner_id": 456, "node_id": "R_fixture", "owner": "example", "name": "fixture"}}],
            "mailboxes": [
                {"id": "github-slot", "project_id": "project-1", "provider": "github", "branch": "main",
                 "path": ".runner/commands/runner-1.json"},
                {"id": "docs-slot", "project_id": "project-1", "provider": "drive",
                 "document_id": "existing-document", "tab_id": "t.existing"}]}


def paths_and_config(tmp_path):
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    paths.ensure_private()
    value = configuration()
    path = paths.config_dir / "runtime-v2.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return paths, path


def test_startup_arms_after_recovery_and_notifications(tmp_path, monkeypatch):
    paths, _ = paths_and_config(tmp_path)
    runtime = Runtime(Settings(), paths)
    events = []
    try:
        assembly = runtime.v2
        assert assembly is not None
        def recovery(*_args):
            assert not runtime.ledger.state()["armed"]
            events.append("recovered")
        monkeypatch.setattr("runner_app.engine.runtime_v2.recover_interrupted", recovery)
        monkeypatch.setattr(assembly.store, "restart_notifications", lambda *_a: [])
        def ready():
            assert events == ["recovered"]
            assert runtime.arm_on_startup()
            events.append("armed")
            assembly.service.worker.request_stop()
        monkeypatch.setattr(assembly.service, "start", assembly.service.worker._loop)
        assembly.start(on_recovered=ready)
        assert events == ["recovered", "armed"]
    finally:
        runtime.close()


def test_github_only_startup_ignores_broken_disabled_legacy_google_and_reads_no_token(tmp_path):
    paths, _ = paths_and_config(tmp_path)
    legacy = paths.config_dir / "owner-runtime.json"
    legacy.write_text("not valid Google runtime configuration")
    legacy.chmod(0o600)
    runtime = Runtime(Settings(), paths)
    try:
        assert runtime.integrations is None
        assert runtime.v2 is not None and runtime.v2.configured
        assert runtime.v2.status()["channels"]["docs-slot"] == "CONFIGURED_NOT_OBSERVED"
        # Missing optional Google configuration is checked lazily; its channel
        # stays registered and can recover on a later observation.
        with pytest.raises(OSError):
            runtime.v2.docs_ports["docs-slot"].read_current()
        assert runtime.v2.status()["channels"]["docs-slot"] == "DOCS_CONFIG_INVALID"
        assert runtime.ledger.single_execution_lane
        assert runtime.ledger.state()["runner_id"] == "runner-1"
        assert runtime.arm_on_startup()
        assert runtime.ledger.state()["armed"]
        with TestClient(runtime.app, base_url="http://127.0.0.1:8765") as client:
            assert client.get("/api/v2/status").status_code == 403
            assert client.get("/assets/v2.js").status_code == 200
            bootstrap = json.loads((paths.token_dir / "session-bootstrap.json").read_text())["bootstrap"]
            response = client.post("/api/v1/local/session", json={"bootstrap": bootstrap},
                                   headers={"Origin": "http://127.0.0.1:8765"})
            assert response.status_code == 200
            assert client.get("/api/v2/status").json()["runner_id"] == "runner-1"
            assert client.get("/api/v2/requests").json() == {"requests": []}
            assert client.get("/api/v2/requests/not-present").status_code == 404
    finally:
        runtime.close()


def test_owner_config_permissions_are_enforced_without_reading_referenced_credentials(tmp_path):
    paths, path = paths_and_config(tmp_path)
    assert load_v2_config(paths).runner_id == "runner-1"
    path.chmod(0o644)
    with pytest.raises(V2ConfigError, match="UNSAFE"):
        load_v2_config(paths)


def test_unavailable_compose_profile_does_not_disable_native_or_provider_channels(tmp_path):
    paths, path = paths_and_config(tmp_path)
    value = configuration()
    value["compose_profile_dir"] = str(paths.config_dir / "compose-profiles")
    value["projects"][0]["container_profiles"] = ["web-integration"]
    path.write_text(json.dumps(value))
    runtime = Runtime(Settings(), paths)
    try:
        assert runtime.v2 is not None and runtime.v2.configured
        assert "rootless-compose" not in runtime.v2.handler.container_backends
        assert runtime.v2.backend_codes["rootless-compose"] == "COMPOSE_PROFILE_UNAVAILABLE"
        assert len(runtime.v2.service.bindings) == 2
        assert runtime.arm_on_startup()
    finally:
        runtime.close()


def test_compose_enrollment_requires_owner_profile_directory():
    value = configuration()
    value["projects"][0]["container_profiles"] = ["web-integration"]
    with pytest.raises(V2ConfigError):
        parse_v2_config(json.dumps(value).encode())
    value["compose_profile_dir"] = "/nonexistent-owner-config/compose-profiles"
    config = parse_v2_config(json.dumps(value).encode())
    assert str(config.compose_profile_dir) == value["compose_profile_dir"]
    assert config.projects["project-1"].container_profiles == ("web-integration",)


@pytest.mark.parametrize("mutation", ["duplicate-slot", "wrong-project", "invalid-branch", "unknown-field"])
def test_configuration_rejects_ambiguous_or_unscoped_enrollment(mutation):
    value = configuration()
    value["mailboxes"][1]["tab_id"] = "tab-1"
    if mutation == "duplicate-slot":
        value["mailboxes"].append({**value["mailboxes"][0], "id": "another-slot"})
    elif mutation == "wrong-project":
        value["mailboxes"][0]["project_id"] = "other"
    elif mutation == "invalid-branch":
        value["mailboxes"][0]["branch"] = "../invalid"
    else:
        value["implicit_shell"] = True
    with pytest.raises(V2ConfigError):
        parse_v2_config(json.dumps(value).encode())


def test_docs_setup_outage_recovers_without_restart_or_replaying_offline_ready(tmp_path):
    from test_intake_v2 import Port

    from runner_app.adapters.drive_mailbox_v2 import DocsMailboxError
    from runner_app.engine.runtime_v2 import V2Assembly
    from runner_app.storage.database import Database
    from runner_app.storage.ledger import Ledger

    paths, _ = paths_and_config(tmp_path)
    value = configuration()
    value['mailboxes'] = [value['mailboxes'][1]]
    ledger = Ledger(Database(paths.database_path), runner_id='runner-1', single_execution_lane=True)
    ledger.startup()
    ledger.arm()
    port, calls, clock = Port(dispatch='READY'), [], [0.0]

    def factory(*_):
        calls.append(True)
        if len(calls) == 1:
            raise DocsMailboxError('DOCS_CHANNEL_SETUP_FAILED')
        return port

    assembly = V2Assembly(parse_v2_config(json.dumps(value).encode()), paths, ledger,
                          docs_factory=factory)
    try:
        assert calls == []  # Construction and unrelated providers are not delayed.
        assembly.docs_ports['docs-slot'].clock = lambda: clock[0]
        assembly.service.poll_once()
        assert assembly.status()['channels']['docs-slot'] == 'DOCS_CHANNEL_SETUP_FAILED'
        assert assembly.store.mailbox('docs-slot')['continuous'] == 0
        assembly.service.poll_once()
        assert len(calls) == 1  # Bounded recovery, not a provider hot retry loop.
        clock[0] = 11
        assembly.service.poll_once()
        assert assembly.status()['channels']['docs-slot'] == 'NEEDS_RESUBMIT'
        assert assembly.store.list_requests() == []
        port.ready()
        assembly.service.poll_once()
        assert assembly.status()['channels']['docs-slot'] == 'ACCEPTED'
        assert len(assembly.store.list_requests()) == 1
        assert len(calls) == 2
    finally:
        assembly.close(deadline=time.monotonic() + 1)


def test_docs_recovery_never_retries_an_uncertain_ack():
    from test_intake_v2 import Port

    from runner_app.engine.docs_v2 import RecoveringDocsPort

    class LostResponse(Port):
        def ack_if_current(self, observed, payload):
            super().ack_if_current(observed, payload)
            raise OSError('response lost after the write')

    port, clock, calls = LostResponse(), [0.0], []

    def factory():
        calls.append(True)
        return port

    recovery = RecoveringDocsPort(factory, clock=lambda: clock[0])
    observed = recovery.read_current()
    assert recovery.ack_if_current(observed, observed.payload).state == 'UNKNOWN'
    assert recovery.ack_if_current(observed, observed.payload).state == 'UNKNOWN'
    assert port.writes == 1 and len(calls) == 1
    clock[0] = 11
    assert recovery.read_current().revision != observed.revision
    assert port.writes == 1 and len(calls) == 2
