"""Fake-backed EP-06 connection freshness and exact Drive link projections."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from runner_app.adapters.drive_registry import WorkspaceDriveRegistration
from runner_app.api.auth import Sessions
from runner_app.api.runtime import create_runtime_app
from runner_app.config import Settings
from runner_app.engine.connection_checks import (
    CheckBusy,
    ConnectionChecks,
    check_existing_drive,
    check_github_identity,
    check_registered_git_remote,
)
from runner_app.engine.service import Engine
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger

ORIGIN = "http://127.0.0.1:8765"


def test_separate_checks_preserve_last_success_and_sanitize_errors() -> None:
    calls: list[str] = []
    fail = False

    def drive() -> dict[str, str]:
        nonlocal fail
        calls.append("drive")
        if fail:
            raise RuntimeError("sensitive provider token material")
        return {"kind": "EXACT_INSTALLATION_READ"}

    checks = ConnectionChecks(
        drive=drive,
        github_api=lambda: {"kind": "API_IDENTITY_READ"},
        git_remote=lambda project, workspace: {
            "kind": "REMOTE_HEAD_CHECK", "project_id": project, "workspace_id": workspace,
            "last_fetch_at": None,
        },
    )
    assert checks.global_snapshot()["drive"]["status"] == "UNKNOWN"
    first = checks.check_drive()
    assert first["status"] == "OK" and first["last_success_at"] == first["last_checked_at"]
    assert checks.global_snapshot()["github_api"]["status"] == "UNKNOWN"
    remote = checks.check_git_remote("fixture", "area")
    assert remote["status"] == "OK"
    assert checks.remote_snapshot("fixture", "other")["status"] == "UNKNOWN"
    assert checks.remote_snapshot("fixture", "area")["observation"]["last_fetch_at"] is None
    fail = True
    second = checks.check_drive()
    assert second["status"] == "ERROR" and second["code"] == "CONNECTION_CHECK_FAILED"
    assert second["last_success_at"] == first["last_success_at"]
    assert second["observation"] == first["observation"]
    assert "sensitive" not in json.dumps(second)
    assert calls == ["drive", "drive"]


def test_check_busy_is_immediate_and_other_provider_can_check() -> None:
    entered, release = threading.Event(), threading.Event()

    def slow_drive() -> dict[str, str]:
        entered.set()
        assert release.wait(timeout=2)
        return {"kind": "EXACT_INSTALLATION_READ"}

    checks = ConnectionChecks(
        drive=slow_drive, github_api=lambda: {"kind": "API_IDENTITY_READ"},
        git_remote=lambda _project, _workspace: {"kind": "REMOTE_HEAD_CHECK"},
    )
    thread = threading.Thread(target=checks.check_drive)
    thread.start()
    assert entered.wait(timeout=2)
    try:
        with pytest.raises(CheckBusy):
            checks.check_drive()
        assert checks.check_github_api()["status"] == "OK"
    finally:
        release.set()
        thread.join(timeout=2)
    assert not thread.is_alive()


def test_exact_drive_and_github_probes_do_only_provider_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str]] = []
    root_id = "177IEHeBVezHIlbIijZsIvRDSbTJ42kxe"
    index_id = "1x2KIBZCZfHs2w-s_Ah3yMW80elgG4KseTe7OZq7L_hA"

    class Reader:
        def get_file(self, file_id: str, *, fields: str) -> dict[str, Any]:
            seen.append(("get_file", file_id))
            return {"id": root_id, "name": "Chat-Dev Runner",
                    "mimeType": "application/vnd.google-apps.folder", "trashed": False}

    class GitHub:
        def authenticated_identity(self) -> SimpleNamespace:
            seen.append(("authenticated_identity", "owner"))
            return SimpleNamespace(login="ViskyG", id=123)

    attached = SimpleNamespace(config=SimpleNamespace(root_id=root_id, root_name="Chat-Dev Runner",
                                                       index_id=index_id), index_revision="rev-1")
    integrations = SimpleNamespace(attached=attached, provider=Reader(), github=GitHub(),
                                   config=SimpleNamespace(github_login="ViskyG", github_user_id=123))
    monkeypatch.setattr("runner_app.engine.connection_checks.recheck_attached_index",
                        lambda provider, observed: seen.append(("recheck_index", observed.config.index_id)))
    drive = check_existing_drive(integrations)
    github = check_github_identity(integrations)
    assert drive == {"kind": "EXACT_INSTALLATION_READ", "root_id": root_id,
                     "index_id": index_id, "index_revision": "rev-1"}
    assert github == {"kind": "API_IDENTITY_READ", "login": "ViskyG", "user_id": 123,
                      "draft_pr_capability": "NOT_PROBED"}
    assert seen == [("get_file", root_id), ("recheck_index", index_id),
                    ("authenticated_identity", "owner")]


def test_selected_remote_probe_is_ls_remote_not_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    repo_path = tmp_path / "managed/fixture/repo.git"
    worktree = tmp_path / "managed/fixture/workspaces/area"
    observed: list[tuple[str, Any]] = []
    head = "a" * 40
    remote_head = "b" * 40
    repository = SimpleNamespace(private=True, id=42,
                                 clone_url="https://github.com/ViskyG/fixture.git")

    class Registry:
        managed_root = tmp_path / "managed"

        def checked_workspace_path(self, project_id: str, workspace_id: str) -> Path:
            assert (project_id, workspace_id) == ("fixture", "area")
            return worktree

        def get_workspace(self, project_id: str, workspace_id: str) -> dict[str, Any]:
            return {"head": head, "branch": "work/area", "generation": 2}

        def get_project(self, project_id: str) -> dict[str, Any]:
            return {"repo_path": str(repo_path)}

        def get_project_remote(self, project_id: str) -> dict[str, Any]:
            return {"owner": "ViskyG", "name": "fixture", "provider_id": 42,
                    "clone_url": repository.clone_url}

        def _managed_path(self, path: Path, *, directory: bool, expected: Path) -> Path:
            assert path == expected == repo_path and directory
            return path

    class GitHub:
        def get_repository(self, owner: str, name: str, *, expected_repo_id: int) -> Any:
            observed.append(("github_repo", (owner, name, expected_repo_id)))
            return repository

    class Transport:
        def __init__(self, target: Any, token_provider: Any) -> None:
            assert target is repository and callable(token_provider)

        def read_head(self, repo: Path, branch: str) -> str:
            observed.append(("ls_remote", (repo, branch)))
            return remote_head

    monkeypatch.setattr("runner_app.engine.connection_checks.inspect_workspace",
                        lambda repo, path: {"head": head, "branch": "work/area"})
    monkeypatch.setattr("runner_app.engine.connection_checks.GitHubGitTransport", Transport)
    integrations = SimpleNamespace(github=GitHub(), token_provider=lambda: "unused")
    result = check_registered_git_remote(integrations, Registry(), "fixture", "area")
    assert result["kind"] == "REMOTE_HEAD_CHECK"
    assert result["remote_head"] == remote_head and result["last_fetch_at"] is None
    assert result["local_head"] == head and result["source_generation"] == 2
    assert observed == [("github_repo", ("ViskyG", "fixture", 42)),
                        ("ls_remote", (repo_path, "work/area"))]


def test_authenticated_checks_and_exact_drive_links_are_on_demand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    ledger = Ledger(Database(tmp_path / "db/state.sqlite"))
    ledger.startup()
    engine = Engine(ledger)
    sessions = Sessions()
    bootstrap = sessions.issue_bootstrap()
    recorded_head = "a" * 40
    live_head = {"value": recorded_head}
    live_dirty = {"value": False}
    monkeypatch.setattr(
        "runner_app.api.runtime.inspect_registered_workspace",
        lambda _registry, _project, _workspace: {
            "matches_recorded": live_head["value"] == recorded_head,
            "dirty": live_dirty["value"], "observed_branch": "work/area",
            "observed_head": live_head["value"], "recorded_generation": 2,
        },
    )
    checks = ConnectionChecks(
        drive=lambda: calls.append("drive") or {"kind": "EXACT_INSTALLATION_READ"},
        github_api=lambda: calls.append("github") or {"kind": "API_IDENTITY_READ"},
        git_remote=lambda project, workspace: calls.append("remote") or {
            "kind": "REMOTE_HEAD_CHECK", "project_id": project, "workspace_id": workspace,
            "branch": "work/area", "local_head": recorded_head,
            "source_generation": 2, "last_fetch_at": None,
        },
    )

    class Source:
        def get_workspace(self, project_id: str, workspace_id: str) -> dict[str, Any] | None:
            return ({"head": recorded_head, "branch": "work/area", "generation": 2}
                    if (project_id, workspace_id) == ("fixture", "area") else None)

    registration = WorkspaceDriveRegistration(
        installation_id="11111111-1111-4111-8111-111111111111",
        project_id="fixture", workspace_id="area",
        project_folder_id="projectfolder01", workspace_folder_id="workspacefolder01",
        command_doc_id="commanddoc01", changeset_doc_id="changesetdoc01",
        receipt_doc_id="receiptdoc01", latest_result_doc_id="latestresultdoc01",
        project_state_doc_id="projectstatedoc01", context_doc_id="contextdoc01",
    )

    class DriveRegistry:
        def get(self, project_id: str, workspace_id: str) -> WorkspaceDriveRegistration:
            calls.append("drive_registry_get")
            assert (project_id, workspace_id) == ("fixture", "area")
            return registration

    app = create_runtime_app(Settings(), engine, sessions, source_registry=Source(),
                             connection_checks=checks, drive_registry=DriveRegistry(),
                             integration_status={"state": "EXISTING_INSTALLATION_ATTACHED"})
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            assert client.post("/api/v1/local/check-drive", json={},
                               headers={"Origin": ORIGIN}).status_code == 403
            exchange = client.post("/api/v1/local/session", json={"bootstrap": bootstrap},
                                   headers={"Origin": ORIGIN})
            assert exchange.status_code == 200
            csrf = exchange.json()["csrf_token"]
            headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
            system = client.get("/api/v1/system").json()
            assert system["connections"]["drive"]["status"] == "UNKNOWN"
            assert system["connections"]["github_api"]["status"] == "UNKNOWN"
            assert calls == []
            assert client.get("/api/v1/projects/fixture/workspaces/area/drive-docs").status_code == 200
            links = client.get("/api/v1/projects/fixture/workspaces/area/drive-docs").json()
            assert links["documents"]["command"] == {
                "id": "commanddoc01", "url": "https://docs.google.com/document/d/commanddoc01/edit",
            }
            assert links["documents"]["source_context"]["id"] == "contextdoc01"
            assert links["workspace_folder"]["url"] == (
                "https://drive.google.com/drive/folders/workspacefolder01"
            )
            assert calls == ["drive_registry_get", "drive_registry_get"]
            assert client.post("/api/v1/local/check-drive", json={}, headers=headers).json()["status"] == "OK"
            assert client.post("/api/v1/local/check-github", json={}, headers=headers).json()["status"] == "OK"
            checked = client.post("/api/v1/local/check-git-remote",
                                  json={"project_id": "fixture", "workspace_id": "area"},
                                  headers=headers).json()
            assert checked["status"] == "OK" and checked["observation"]["last_fetch_at"] is None
            current = client.get("/api/v1/projects/fixture/workspaces/area/connections").json()
            assert current["git_remote"]["last_checked_at"] == checked["last_checked_at"]
            assert current["git_fetch"]["status"] == "UNKNOWN"
            assert current["git_fetch"]["last_success_at"] is None
            assert current["git_remote"]["workspace_snapshot_current"] is True
            # A local checkout changes outside the runner without changing its registry row.
            live_head["value"] = "b" * 40
            stale = client.get("/api/v1/projects/fixture/workspaces/area/connections").json()
            assert stale["git_remote"]["workspace_snapshot_current"] is False
            assert stale["git_remote"]["last_checked_at"] == checked["last_checked_at"]
            live_head["value"] = recorded_head
            live_dirty["value"] = True
            dirty = client.get("/api/v1/projects/fixture/workspaces/area/connections").json()
            assert dirty["git_remote"]["workspace_snapshot_current"] is False
            assert client.get("/api/v1/projects/fixture/workspaces/unknown/connections").status_code == 404
            assert client.post("/api/v1/local/check-git-remote",
                               json={"project_id": "fixture", "workspace_id": "unknown"},
                               headers=headers).status_code == 404
            assert calls == ["drive_registry_get", "drive_registry_get", "drive", "github", "remote"]
            assert client.post("/api/v1/local/check-drive", json={},
                               headers={"Origin": "http://attacker.example",
                                        "X-CSRF-Token": csrf}).status_code == 403
            assert calls[-1] == "remote"
    finally:
        engine.shutdown()
