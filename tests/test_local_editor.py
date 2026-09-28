"""The local editor button resolves a registered worktree, never remote text."""
from __future__ import annotations

import errno
from pathlib import Path
from typing import Any

import pytest

from runner_app.api.editor import EditorLaunchError, launch_editor
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.storage import Database


def _registry(tmp_path: Path) -> tuple[SourceRegistry, Path]:
    managed = tmp_path / "managed"
    repo = managed / "demo" / "repo.git"
    worktree = managed / "demo" / "workspaces" / "primary"
    repo.mkdir(parents=True)
    worktree.mkdir(parents=True)
    managed.chmod(0o700)
    registry = SourceRegistry(Database(tmp_path / "state.sqlite"), managed)
    registry.register_project("demo", "Demo", repo, origin_kind="new")
    registry.register_workspace("demo", "primary", "Primary", worktree,
                                branch="work/demo", head="a" * 40)
    return registry, worktree


def test_editor_uses_registered_worktree_and_fixed_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, worktree = _registry(tmp_path)
    launches: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr("runner_app.api.editor.shutil.which", lambda name: "/usr/bin/code")
    monkeypatch.setattr(
        "runner_app.api.editor.subprocess.Popen",
        lambda argv, **kwargs: launches.append((argv, kwargs)),
    )
    result = launch_editor(registry, "demo", "primary")
    assert result == {"status": "OPEN_REQUESTED", "project_id": "demo",
                      "workspace_id": "primary"}
    assert launches[0][0] == ["/usr/bin/code", "--reuse-window", str(worktree)]
    assert launches[0][1]["start_new_session"] is True
    assert launches[0][1]["close_fds"] is True


def test_editor_refuses_unknown_or_replaced_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, worktree = _registry(tmp_path)
    monkeypatch.setattr("runner_app.api.editor.shutil.which", lambda name: "/usr/bin/code")
    monkeypatch.setattr("runner_app.api.editor.subprocess.Popen",
                        lambda *args, **kwargs: pytest.fail("unexpected editor launch"))
    with pytest.raises(EditorLaunchError, match="WORKSPACE_NOT_FOUND"):
        launch_editor(registry, "demo", "unknown")
    outside = tmp_path / "outside"
    outside.mkdir()
    worktree.rmdir()
    worktree.symlink_to(outside, target_is_directory=True)
    with pytest.raises(EditorLaunchError, match="WORKSPACE_PATH_UNSAFE"):
        launch_editor(registry, "demo", "primary")


def test_editor_reports_missing_binary_and_launch_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, _ = _registry(tmp_path)
    monkeypatch.setattr("runner_app.api.editor.shutil.which", lambda name: None)
    with pytest.raises(EditorLaunchError, match="EDITOR_UNAVAILABLE"):
        launch_editor(registry, "demo", "primary")
    monkeypatch.setattr("runner_app.api.editor.shutil.which", lambda name: "/usr/bin/code")

    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno.ENOENT)

    monkeypatch.setattr("runner_app.api.editor.subprocess.Popen", unavailable)
    with pytest.raises(EditorLaunchError, match="EDITOR_LAUNCH_FAILED"):
        launch_editor(registry, "demo", "primary")


def test_checked_workspace_path_rejects_invalid_ids(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    with pytest.raises(SourceError, match="Managed source identity"):
        registry.checked_workspace_path("../demo", "primary")


def test_editor_route_requires_local_session_and_exact_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from runner_app.api.auth import Sessions
    from runner_app.api.runtime import create_runtime_app
    from runner_app.config import Settings
    from runner_app.engine.service import Engine
    from runner_app.storage.ledger import Ledger

    registry, worktree = _registry(tmp_path)
    ledger = Ledger(registry.database)
    ledger.startup()
    engine = Engine(ledger)
    sessions = Sessions()
    bootstrap = sessions.issue_bootstrap()
    launches: list[list[str]] = []
    monkeypatch.setattr("runner_app.api.editor.shutil.which", lambda name: "/usr/bin/code")
    monkeypatch.setattr("runner_app.api.editor.subprocess.Popen",
                        lambda argv, **kwargs: launches.append(argv))
    origin = "http://127.0.0.1:8765"
    app = create_runtime_app(Settings(), engine, sessions, source_registry=registry)
    with TestClient(app, base_url=origin) as client:
        body = {"project_id": "demo", "workspace_id": "primary"}
        assert client.post("/api/v1/local/open-editor", json=body,
                           headers={"Origin": origin}).status_code == 403
        login = client.post("/api/v1/local/session", json={"bootstrap": bootstrap},
                            headers={"Origin": origin})
        csrf = login.json()["csrf_token"]
        headers = {"Origin": origin, "X-CSRF-Token": csrf}
        response = client.post("/api/v1/local/open-editor", json=body, headers=headers)
        assert response.status_code == 202
        assert response.json()["status"] == "OPEN_REQUESTED"
        assert launches == [["/usr/bin/code", "--reuse-window", str(worktree)]]
        rejected = client.post("/api/v1/local/open-editor",
                               json={**body, "path": "/tmp/arbitrary"}, headers=headers)
        assert rejected.status_code == 422
        assert len(launches) == 1
    engine.shutdown()
