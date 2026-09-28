"""EP-07 stopped-runner backup and restore on disposable private state only."""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

import runner_app.storage.backup as backup_module
from runner_app.engine.singleton import Singleton
from runner_app.git.argv import run_git
from runner_app.git.worktrees import create_repository, create_workspace
from runner_app.storage.backup import BackupError, _rename_new, create_backup, restore_backup
from runner_app.storage.database import SCHEMA_VERSION, Database
from runner_app.storage.paths import Paths, ensure_private_directory


def _fixture(tmp_path: Path) -> tuple[Paths, Path]:
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    paths.ensure_private()
    Database(paths.database_path).migrate()
    archive_parent = tmp_path / "backups"
    ensure_private_directory(archive_parent)
    return paths, archive_parent / "snapshot"


def _project_row(path: Path) -> tuple[str, str] | None:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT project_id,display_name FROM projects WHERE project_id='demo'"
        ).fetchone()


def test_backup_restores_wal_database_evidence_and_safe_relative_git_link(tmp_path: Path) -> None:
    paths, archive = _fixture(tmp_path)
    fake_token = paths.token_dir / "oauth-fixture"
    fake_token.write_bytes(b"fixture-oauth-secret")
    fake_token.chmod(0o600)
    fake_github = paths.config_dir / "github-token"
    fake_github.write_bytes(b"fixture-github-secret")
    fake_github.chmod(0o600)
    project = paths.state_dir / "managed" / "demo"
    workspace = project / "workspaces" / "primary"
    workspace.mkdir(parents=True)
    (project / "shared.txt").write_text("owner source\n")
    (workspace / "link.txt").symlink_to("../../shared.txt")
    proof = paths.evidence_dir / "run-1" / "stdout.txt"
    proof.parent.mkdir()
    proof.write_bytes(b"contained output\n")
    with Database(paths.database_path).connect() as connection:
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "INSERT INTO projects(project_id,display_name,state,metadata_json,created_at,updated_at) "
            "VALUES('demo','Demo','READY','{}','2026-01-01','2026-01-01')"
        )
        connection.commit()
        assert (paths.database_path.parent / "state.sqlite-wal").exists()
        result = create_backup(paths, archive)
        assert result["database_schema_version"] == SCHEMA_VERSION
        assert result["symlinks"] == 1
    assert archive.stat().st_mode & 0o777 == 0o700
    assert (archive / "manifest.json").stat().st_mode & 0o777 == 0o600
    assert not (archive / "state" / "runner.lock").exists()
    assert not (archive / "state" / "state.sqlite-wal").exists()
    assert not (archive / "config").exists()
    for archived_file in archive.rglob("*"):
        if archived_file.is_file():
            assert b"fixture-oauth-secret" not in archived_file.read_bytes()
            assert b"fixture-github-secret" not in archived_file.read_bytes()
    assert not (archive / "state" / "managed" / "demo" / "workspaces" / "primary" / "link.txt").exists()
    shutil.rmtree(paths.state_dir)  # Disposable fixture only; mimic owner moving damaged state aside.
    restored = restore_backup(archive, paths.state_dir)
    assert restored["manifest_sha256"] == result["manifest_sha256"]
    assert _project_row(paths.database_path) == ("demo", "Demo")
    assert (paths.evidence_dir / "run-1" / "stdout.txt").read_bytes() == b"contained output\n"
    link = paths.state_dir / "managed" / "demo" / "workspaces" / "primary" / "link.txt"
    assert link.is_symlink() and os.readlink(link) == "../../shared.txt"
    assert link.read_text() == "owner source\n"
    assert fake_token.read_bytes() == b"fixture-oauth-secret"
    assert fake_github.read_bytes() == b"fixture-github-secret"


def test_backup_refuses_running_singleton_without_creating_archive(tmp_path: Path) -> None:
    paths, archive = _fixture(tmp_path)
    running = Singleton(paths.state_dir / "runner.lock")
    try:
        with pytest.raises(BackupError) as error:
            create_backup(paths, archive)
        assert error.value.code == "RUNNER_RUNNING"
        assert not archive.exists()
    finally:
        running.close()


@pytest.mark.parametrize("target", ["/etc/passwd", "../../../../../outside"])
def test_backup_refuses_out_of_root_source_link(tmp_path: Path, target: str) -> None:
    paths, archive = _fixture(tmp_path)
    workspace = paths.state_dir / "managed" / "demo" / "workspaces" / "primary"
    workspace.mkdir(parents=True)
    (workspace / "escape").symlink_to(target)
    with pytest.raises(BackupError) as error:
        create_backup(paths, archive)
    assert error.value.code == "UNSAFE_STATE_LINK"
    assert not archive.exists()
    assert not list(archive.parent.glob(".snapshot.partial-*"))


def test_restore_detects_tamper_before_touching_state(tmp_path: Path) -> None:
    paths, archive = _fixture(tmp_path)
    (paths.evidence_dir / "proof").write_bytes(b"original")
    create_backup(paths, archive)
    shutil.rmtree(paths.state_dir)
    (archive / "state" / "outputs" / "proof").write_bytes(b"changed!")
    with pytest.raises(BackupError) as error:
        restore_backup(archive, paths.state_dir)
    assert error.value.code == "BACKUP_HASH_MISMATCH"
    assert not paths.state_dir.exists()


def test_restore_refuses_existing_or_different_state_path(tmp_path: Path) -> None:
    paths, archive = _fixture(tmp_path)
    create_backup(paths, archive)
    with pytest.raises(BackupError) as existing:
        restore_backup(archive, paths.state_dir)
    assert existing.value.code == "RESTORE_TARGET_EXISTS"
    assert paths.database_path.exists()
    different = tmp_path / "different"
    with pytest.raises(BackupError) as mismatch:
        restore_backup(archive, different)
    assert mismatch.value.code == "TARGET_PATH_MISMATCH"
    assert not different.exists()


def test_backup_refuses_unclassified_state_entry(tmp_path: Path) -> None:
    paths, archive = _fixture(tmp_path)
    (paths.state_dir / "unexpected-secret").write_text("private")
    with pytest.raises(BackupError) as error:
        create_backup(paths, archive)
    assert error.value.code == "UNKNOWN_STATE_ENTRY"
    assert not archive.exists()


def test_real_managed_git_worktree_survives_same_path_restore(tmp_path: Path) -> None:
    paths, archive = _fixture(tmp_path)
    managed = paths.state_dir / "managed"
    ensure_private_directory(managed)
    repo = create_repository(managed, "actual")
    head = run_git(repo, ["rev-parse", "HEAD"]).strip()
    workspace = create_workspace(managed, "actual", repo, "primary", "feature", head)
    (workspace / "note.txt").write_text("review me\n")
    create_backup(paths, archive)
    shutil.rmtree(paths.state_dir)
    restore_backup(archive, paths.state_dir)
    assert run_git(workspace, ["rev-parse", "HEAD"]).strip() == head
    assert run_git(workspace, ["status", "--porcelain"]).strip() == "?? note.txt"
    assert (workspace / "note.txt").read_text() == "review me\n"


def test_atomic_promotion_never_replaces_existing_archive(tmp_path: Path) -> None:
    source = tmp_path / "new"
    target = tmp_path / "existing"
    ensure_private_directory(source)
    ensure_private_directory(target)
    (target / "sentinel").write_text("preserved")
    with pytest.raises(BackupError) as error:
        _rename_new(source, target)
    assert error.value.code == "BACKUP_TARGET_EXISTS"
    assert (target / "sentinel").read_text() == "preserved"
    assert source.exists()


def test_restore_refuses_archive_symlink_without_following_it(tmp_path: Path) -> None:
    paths, archive = _fixture(tmp_path)
    (paths.evidence_dir / "proof").write_bytes(b"evidence")
    create_backup(paths, archive)
    shutil.rmtree(paths.state_dir)
    outside = tmp_path / "outside"
    outside.write_bytes(b"external")
    member = archive / "state" / "outputs" / "proof"
    member.unlink()
    member.symlink_to(outside)
    with pytest.raises(BackupError) as error:
        restore_backup(archive, paths.state_dir)
    assert error.value.code == "BACKUP_MANIFEST_INVALID"
    assert outside.read_bytes() == b"external"
    assert not paths.state_dir.exists()


def test_backup_refuses_configuration_root_overlapping_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    paths = Paths(state / "managed", state, tmp_path / "cache")
    paths.ensure_private()
    Database(paths.database_path).migrate()
    parent = tmp_path / "backups"
    ensure_private_directory(parent)
    with pytest.raises(BackupError) as error:
        create_backup(paths, parent / "snapshot")
    assert error.value.code == "BACKUP_PATH_OVERLAP"
    assert not (parent / "snapshot").exists()


def test_backup_detects_new_top_level_entry_during_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, archive = _fixture(tmp_path)
    (paths.evidence_dir / "proof").write_bytes(b"evidence")
    original = backup_module._copy_state_tree

    def mutate_after_scan(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)  # type: ignore[arg-type]
        (paths.state_dir / "new-entry").write_bytes(b"concurrent")

    monkeypatch.setattr(backup_module, "_copy_state_tree", mutate_after_scan)
    with pytest.raises(BackupError) as error:
        create_backup(paths, archive)
    assert error.value.code == "STATE_CHANGED_DURING_BACKUP"
    assert not archive.exists()
