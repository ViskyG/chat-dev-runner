"""Configuration/startup/API boundary, never reading actual credentials."""
from __future__ import annotations

import json

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


def test_github_only_startup_ignores_broken_disabled_legacy_google_and_reads_no_token(tmp_path):
    paths, _ = paths_and_config(tmp_path)
    legacy = paths.config_dir / "owner-runtime.json"
    legacy.write_text("not valid Google runtime configuration")
    legacy.chmod(0o600)
    runtime = Runtime(Settings(), paths)
    try:
        assert runtime.integrations is None
        assert runtime.v2 is not None and runtime.v2.configured
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
