"""Current Git facts are read from the exact registered worktree."""
from __future__ import annotations

from pathlib import Path

import pytest

from runner_app.api.source_view import SourceViewError, inspect_registered_workspace
from runner_app.git.argv import run_git
from runner_app.git.registry import SourceRegistry
from runner_app.git.worktrees import create_repository, create_workspace
from runner_app.storage import Database


def _source(tmp_path: Path) -> tuple[SourceRegistry, Path, str]:
    root = tmp_path / "managed"
    repo = create_repository(root, "demo")
    head = run_git(repo, ["rev-parse", "HEAD"]).strip()
    worktree = create_workspace(root, "demo", repo, "primary", "feature/demo", head)
    registry = SourceRegistry(Database(tmp_path / "state.sqlite"), root)
    registry.register_project("demo", "Demo", repo, origin_kind="new")
    registry.register_workspace("demo", "primary", "Primary", worktree,
                                branch="feature/demo", head=head)
    return registry, worktree, head


def test_read_registered_workspace_reports_clean_dirty_and_stale_head(tmp_path: Path) -> None:
    registry, worktree, head = _source(tmp_path)
    clean = inspect_registered_workspace(registry, "demo", "primary")
    assert clean["recorded_head"] == clean["observed_head"] == head
    assert clean["recorded_branch"] == clean["observed_branch"] == "feature/demo"
    assert clean["dirty"] is False and clean["matches_recorded"] is True
    assert clean["checked_at"].endswith("+00:00")

    (worktree / "unsaved.txt").write_text("owner edit\n")
    dirty = inspect_registered_workspace(registry, "demo", "primary")
    assert dirty["dirty"] is True and dirty["matches_recorded"] is True
    run_git(worktree, ["add", "--", "unsaved.txt"])
    run_git(worktree, ["-c", "user.name=Owner", "-c", "user.email=owner@example.invalid",
                       "commit", "-m", "Owner edit"])
    changed = inspect_registered_workspace(registry, "demo", "primary")
    assert changed["dirty"] is False and changed["matches_recorded"] is False
    assert changed["observed_head"] != head


def test_read_refuses_missing_and_replaced_worktree(tmp_path: Path) -> None:
    registry, worktree, _ = _source(tmp_path)
    with pytest.raises(SourceViewError, match="SOURCE_NOT_FOUND"):
        inspect_registered_workspace(registry, "demo", "unknown")
    outside = tmp_path / "outside"
    outside.mkdir()
    worktree.rename(worktree.with_name("moved"))
    worktree.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SourceViewError, match="SOURCE_UNSAFE"):
        inspect_registered_workspace(registry, "demo", "primary")


def test_source_endpoint_is_authenticated_and_reads_current_git(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from runner_app.api.auth import Sessions
    from runner_app.api.runtime import create_runtime_app
    from runner_app.config import Settings
    from runner_app.engine.service import Engine
    from runner_app.storage.ledger import Ledger

    registry, worktree, _ = _source(tmp_path)
    ledger = Ledger(registry.database)
    ledger.startup()
    engine = Engine(ledger)
    sessions = Sessions()
    bootstrap = sessions.issue_bootstrap()
    origin = "http://127.0.0.1:8765"
    path = "/api/v1/projects/demo/workspaces/primary/source"
    with TestClient(create_runtime_app(Settings(), engine, sessions,
                                       source_registry=registry), base_url=origin) as client:
        assert client.get(path).status_code == 403
        login = client.post("/api/v1/local/session", json={"bootstrap": bootstrap},
                            headers={"Origin": origin})
        assert login.status_code == 200
        response = client.get(path)
        assert response.status_code == 200
        assert response.json()["dirty"] is False
        (worktree / "owner-note.txt").write_text("not committed\n")
        changed = client.get(path)
        assert changed.status_code == 200
        assert changed.json()["dirty"] is True
        assert client.get("/api/v1/projects/demo/workspaces/unknown/source").status_code == 404
    engine.shutdown()
