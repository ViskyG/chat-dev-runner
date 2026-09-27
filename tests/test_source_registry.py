"""EP-02 durable source identities and immutable snapshot records on local fixtures."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest

import runner_app.storage.database as database_module
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.storage import Database


def fixture_registry(tmp_path: Path) -> tuple[SourceRegistry, Path, Path]:
    root = tmp_path / "managed"
    project = root / "fixture"
    repo = project / "repo.git"
    workspaces = project / "workspaces"
    workspace = workspaces / "area"
    for path in (root, project, repo, workspaces, workspace):
        path.mkdir(mode=0o700)
    registry = SourceRegistry(Database(tmp_path / "state" / "state.sqlite"), root)
    return registry, repo, workspace


def record_source(registry: SourceRegistry, repo: Path, workspace: Path) -> None:
    registry.register_project("fixture", "Fixture", repo, origin_kind="local",
                              origin_reference="owner-grant")
    registry.register_workspace("fixture", "area", "Area", workspace,
                                branch="feature/area", head="a" * 40)


def manifest(registry: SourceRegistry, snapshot_id: str) -> Path:
    path = registry.managed_root / "fixture" / "snapshots" / snapshot_id / "manifest.json"
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text(json.dumps({"commit": "b" * 40, "tree": "c" * 40}) + "\n")
    path.chmod(0o600)
    return path


def register_snapshot(registry: SourceRegistry, snapshot_id: str, path: Path) -> dict:
    return registry.register_snapshot(
        snapshot_id, "fixture", "area", base_head="a" * 40, commit="b" * 40,
        tree="c" * 40, source_generation=0, capture_fingerprint="d" * 64,
        provenance="CHANGESET_V1", dirty_at_capture=False, included_paths=("src/main.py",),
        manifest_path=path,
    )


def test_v1_to_latest_migration_preserves_metadata_and_is_atomic(tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "state" / "state.sqlite")
    with monkeypatch.context() as scope:
        scope.setattr(database_module, "SCHEMA_VERSION", 1)
        assert database.migrate() == 1
    assert database.migrate() == 8
    connection = database.connect()
    try:
        assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
            (1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,)
        ]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        connection.close()


def test_source_identity_and_generation_cas_survive_restart(tmp_path: Path) -> None:
    registry, repo, workspace = fixture_registry(tmp_path)
    record_source(registry, repo, workspace)
    assert registry.register_project("fixture", "Fixture", repo, origin_kind="local",
                                     origin_reference="owner-grant")["repo_path"] == str(repo)
    assert registry.register_workspace("fixture", "area", "Area", workspace,
                                       branch="feature/area", head="a" * 40)["generation"] == 0
    with pytest.raises(SourceError, match="different source") as error:
        registry.register_project("fixture", "Changed", repo, origin_kind="local",
                                  origin_reference="owner-grant")
    assert error.value.code == "PROJECT_ID_REUSED"
    with pytest.raises(SourceError) as error:
        registry.register_workspace("fixture", "area", "Area", workspace,
                                    branch="feature/other", head="a" * 40)
    assert error.value.code == "WORKSPACE_ID_REUSED"

    restarted = SourceRegistry(registry.database, registry.managed_root)
    assert restarted.get_workspace("fixture", "area")["head"] == "a" * 40
    changed = restarted.transition_workspace("fixture", "area", expected_generation=0,
                                               expected_head="a" * 40, branch="feature/area",
                                               head="b" * 40)
    assert changed["generation"] == 1
    with pytest.raises(SourceError) as error:
        restarted.transition_workspace("fixture", "area", expected_generation=0,
                                         expected_head="a" * 40, branch="feature/area",
                                         head="c" * 40)
    assert error.value.code == "WORKSPACE_CHANGED"
    archived = restarted.transition_workspace("fixture", "area", expected_generation=1,
                                                expected_head="b" * 40, branch="feature/area",
                                                head="b" * 40, archive=True)
    assert archived["state"] == "ARCHIVED"
    with pytest.raises(SourceError) as error:
        restarted.transition_workspace("fixture", "area", expected_generation=2,
                                         expected_head="b" * 40, branch="feature/area",
                                         head="c" * 40)
    assert error.value.code == "WORKSPACE_ARCHIVED"


def test_managed_path_symlink_and_outside_root_are_rejected(tmp_path: Path) -> None:
    registry, repo, workspace = fixture_registry(tmp_path)
    external = tmp_path / "outside"
    external.mkdir(mode=0o700)
    with pytest.raises(SourceError) as error:
        registry.register_project("fixture", "Fixture", external, origin_kind="new")
    assert error.value.code == "OUTSIDE_MANAGED_ROOT"
    workspace.rmdir()
    workspace.symlink_to(external, target_is_directory=True)
    registry.register_project("fixture", "Fixture", repo, origin_kind="new")
    with pytest.raises(SourceError) as error:
        registry.register_workspace("fixture", "area", "Area", workspace,
                                    branch="feature/area", head="a" * 40)
    assert error.value.code == "UNSAFE_SOURCE_PATH"
    assert registry.get_workspace("fixture", "area") is None


def test_snapshot_is_immutable_idempotent_and_manifest_tamper_detected(tmp_path: Path) -> None:
    registry, repo, workspace = fixture_registry(tmp_path)
    record_source(registry, repo, workspace)
    snapshot_id = str(uuid4())
    path = manifest(registry, snapshot_id)
    stored = register_snapshot(registry, snapshot_id, path)
    assert stored["snapshot_commit"] == "b" * 40
    assert stored["included_paths"] == ["src/main.py"]
    assert registry.get_snapshot(snapshot_id) == stored
    with pytest.raises(SourceError) as error:
        registry.publication_target(str(uuid4()), project_id="fixture", workspace_id="area")
    assert error.value.code == "RUN_NOT_TESTED"
    registry.transition_workspace("fixture", "area", expected_generation=0,
                                  expected_head="a" * 40, branch="feature/area", head="b" * 40)
    assert register_snapshot(registry, snapshot_id, path) == stored
    with pytest.raises(SourceError) as error:
        registry.register_snapshot(snapshot_id, "fixture", "area", base_head="a" * 40,
                                   commit="f" * 40, tree="c" * 40, source_generation=0,
                                   capture_fingerprint="d" * 64, provenance="CHANGESET_V1",
                                   dirty_at_capture=False, included_paths=("src/main.py",),
                                   manifest_path=path)
    assert error.value.code == "SNAPSHOT_ID_REUSED"
    with closing(registry.database.connect()) as connection, pytest.raises(
        sqlite3.IntegrityError, match="immutable"
    ):
        connection.execute("UPDATE source_snapshots SET snapshot_commit=? WHERE snapshot_id=?",
                           ("e" * 40, snapshot_id))
    path.write_text("tampered\n")
    with pytest.raises(SourceError) as error:
        registry.get_snapshot(snapshot_id)
    assert error.value.code == "SNAPSHOT_MANIFEST_CHANGED"


def test_snapshot_requires_current_generation_private_manifest_and_safe_paths(tmp_path: Path) -> None:
    registry, repo, workspace = fixture_registry(tmp_path)
    record_source(registry, repo, workspace)
    snapshot_id = str(uuid4())
    path = manifest(registry, snapshot_id)
    path.chmod(0o644)
    with pytest.raises(SourceError) as error:
        register_snapshot(registry, snapshot_id, path)
    assert error.value.code == "UNSAFE_SOURCE_PATH"
    path.chmod(0o600)
    with pytest.raises(SourceError) as error:
        registry.register_snapshot(snapshot_id, "fixture", "area", base_head="a" * 40,
                                   commit="b" * 40, tree="c" * 40, source_generation=1,
                                   capture_fingerprint="d" * 64, provenance="CHANGESET_V1",
                                   dirty_at_capture=False, included_paths=("src/main.py",),
                                   manifest_path=path)
    assert error.value.code == "WORKSPACE_CHANGED"
    with pytest.raises(SourceError) as error:
        registry.register_snapshot(snapshot_id, "fixture", "area", base_head="a" * 40,
                                   commit="b" * 40, tree="c" * 40, source_generation=0,
                                   capture_fingerprint="d" * 64, provenance="CHANGESET_V1",
                                   dirty_at_capture=False, included_paths=("../escape",),
                                   manifest_path=path)
    assert error.value.code == "INVALID_SNAPSHOT"
    assert registry.get_snapshot(snapshot_id) is None


def test_failed_v3_upgrade_leaves_v2_database_unchanged(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "state" / "state.sqlite")
    with monkeypatch.context() as scope:
        scope.setattr(database_module, "SCHEMA_VERSION", 2)
        assert database.migrate() == 2
    monkeypatch.setitem(database_module._MIGRATIONS, 3,
                        database_module._MIGRATIONS[3] + ("NOT VALID SQL",))
    with closing(database.connect()) as connection:
        with pytest.raises(sqlite3.OperationalError):
            database.migrate(connection)
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
            (1,), (2,)
        ]
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='source_projects'").fetchone() is None
        assert connection.execute("PRAGMA table_info(workspaces)").fetchall()[-1][1] != "display_name"
