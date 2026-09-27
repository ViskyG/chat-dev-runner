"""Real Git object materialization for a rootless broker; no project code is executed."""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from runner_app.executors.broker import _snapshot_tree
from runner_app.git.argv import run_git
from runner_app.git.materialize import MaterializeError, materialize_snapshot
from runner_app.git.snapshots import Snapshot, capture_clean_verify
from runner_app.git.worktrees import create_repository, create_workspace


@pytest.fixture
def source(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    managed = tmp_path / "managed"
    repo = create_repository(managed, "project")
    empty = run_git(repo, ["rev-parse", "HEAD"]).strip()
    workspace = create_workspace(managed, "project", repo, "area", "feature/area", empty)
    return repo, workspace, managed / "project" / "snapshots", empty


def _commit(workspace: Path, message: str = "Source") -> str:
    run_git(workspace, ["add", "--all", "--"])
    run_git(workspace, ["-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
                        "commit", "-m", message])
    return run_git(workspace, ["rev-parse", "HEAD"]).strip()


def _snapshot(repo: Path, workspace: Path, snapshots: Path, head: str) -> Snapshot:
    return capture_clean_verify(repo, workspace, snapshots, project_id="project",
                                workspace_id="area", expected_head=head, source_generation=0)


def _destination(tmp_path: Path) -> Path:
    return tmp_path / "sealed-sources" / "run-source"


def _remove_sealed(path: Path) -> None:
    if path.exists():
        for directory, _, _ in os.walk(path, topdown=False, followlinks=False):
            os.chmod(directory, 0o700)
        shutil.rmtree(path)


def test_exact_git_bytes_modes_and_no_checkout_filters(source: tuple[Path, Path, Path, str],
                                                     tmp_path: Path) -> None:
    repo, workspace, snapshots, _ = source
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "data.bin").write_bytes(b"\x00\xff\x1braw\n")
    script = workspace / "run.sh"
    script.write_bytes(b"#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    head = _commit(workspace)
    snap = _snapshot(repo, workspace, snapshots, head)
    destination = _destination(tmp_path)
    materialized = materialize_snapshot(repo, snap.snapshot_id, snap.commit, snap.tree, destination)
    try:
        assert materialized.path == destination
        assert materialized.snapshot_id == snap.snapshot_id
        assert materialized.commit == snap.commit
        assert materialized.tree == snap.tree
        assert (destination / "pkg" / "data.bin").read_bytes() == b"\x00\xff\x1braw\n"
        assert (destination / "run.sh").read_bytes() == b"#!/bin/sh\nexit 0\n"
        assert not (destination / ".git").exists()
        assert stat.S_IMODE(destination.stat().st_mode) == 0o555
        assert stat.S_IMODE((destination / "pkg").stat().st_mode) == 0o555
        assert stat.S_IMODE((destination / "pkg" / "data.bin").stat().st_mode) == 0o444
        assert stat.S_IMODE((destination / "run.sh").stat().st_mode) == 0o555
        assert (destination / "run.sh").stat().st_nlink == 1
        assert _snapshot_tree(destination, snap.tree) == snap.tree
        (workspace / "pkg" / "data.bin").write_bytes(b"human edit\n")
        assert (destination / "pkg" / "data.bin").read_bytes() == b"\x00\xff\x1braw\n"
    finally:
        _remove_sealed(destination)


def test_ref_or_tree_mismatch_refuses_without_replacing_destination(
    source: tuple[Path, Path, Path, str], tmp_path: Path,
) -> None:
    repo, workspace, snapshots, empty = source
    (workspace / "file.txt").write_text("current\n")
    head = _commit(workspace)
    snap = _snapshot(repo, workspace, snapshots, head)
    destination = _destination(tmp_path)
    with pytest.raises(MaterializeError) as error:
        materialize_snapshot(repo, snap.snapshot_id, snap.commit, empty, destination)
    assert error.value.code == "SNAPSHOT_REF_CHANGED"
    assert not destination.exists()
    run_git(repo, ["update-ref", f"refs/cdr/snapshots/{snap.snapshot_id}", empty])
    with pytest.raises(MaterializeError) as error:
        materialize_snapshot(repo, snap.snapshot_id, snap.commit, snap.tree, destination)
    assert error.value.code == "SNAPSHOT_REF_CHANGED"
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


def test_symlink_and_secret_source_names_are_refused(
    source: tuple[Path, Path, Path, str], tmp_path: Path,
) -> None:
    repo, workspace, snapshots, _ = source
    (workspace / "ordinary.txt").write_text("safe\n")
    (workspace / "link").symlink_to("ordinary.txt")
    head = _commit(workspace)
    snap = _snapshot(repo, workspace, snapshots, head)
    destination = _destination(tmp_path)
    with pytest.raises(MaterializeError) as error:
        materialize_snapshot(repo, snap.snapshot_id, snap.commit, snap.tree, destination)
    assert error.value.code == "UNSUPPORTED_SYMLINK"
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []

    (workspace / "link").unlink()
    (workspace / ".env").write_text("TOKEN=private\n")
    head = _commit(workspace, "Protected name")
    snap2 = _snapshot(repo, workspace, snapshots, head)
    with pytest.raises(MaterializeError) as error:
        materialize_snapshot(repo, snap2.snapshot_id, snap2.commit, snap2.tree, destination)
    assert error.value.code == "UNSAFE_SOURCE_PATH"
    assert not destination.exists()


def test_large_blob_refused_before_materialization(
    source: tuple[Path, Path, Path, str], tmp_path: Path,
) -> None:
    repo, workspace, snapshots, _ = source
    (workspace / "large.bin").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    head = _commit(workspace)
    snap = _snapshot(repo, workspace, snapshots, head)
    destination = _destination(tmp_path)
    with pytest.raises(MaterializeError) as error:
        materialize_snapshot(repo, snap.snapshot_id, snap.commit, snap.tree, destination)
    assert error.value.code == "SNAPSHOT_TOO_LARGE"
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


def test_existing_destination_is_retained_without_changing_bytes(
    source: tuple[Path, Path, Path, str], tmp_path: Path,
) -> None:
    repo, workspace, snapshots, _ = source
    (workspace / "file.txt").write_text("source\n")
    head = _commit(workspace)
    snap = _snapshot(repo, workspace, snapshots, head)
    destination = _destination(tmp_path)
    destination.mkdir(parents=True)
    destination.parent.chmod(0o700)
    sentinel = destination / "sentinel"
    sentinel.write_bytes(b"retain me")
    with pytest.raises(MaterializeError) as error:
        materialize_snapshot(repo, snap.snapshot_id, snap.commit, snap.tree, destination)
    assert error.value.code == "MATERIALIZATION_EXISTS"
    assert sentinel.read_bytes() == b"retain me"


def test_invalid_ids_or_repository_refuse_without_creating_source(
    source: tuple[Path, Path, Path, str], tmp_path: Path,
) -> None:
    repo, workspace, snapshots, _ = source
    (workspace / "file.txt").write_text("source\n")
    head = _commit(workspace)
    snap = _snapshot(repo, workspace, snapshots, head)
    destination = _destination(tmp_path)
    with pytest.raises(MaterializeError) as error:
        materialize_snapshot(repo, "not-a-uuid", snap.commit, snap.tree, destination)
    assert error.value.code == "INVALID_SNAPSHOT_ID"
    with pytest.raises(MaterializeError) as error:
        materialize_snapshot(repo, snap.snapshot_id, "bad", snap.tree, destination)
    assert error.value.code == "INVALID_GIT_OBJECT"
    with pytest.raises(MaterializeError) as error:
        materialize_snapshot(repo.parent / "wrong", snap.snapshot_id, snap.commit,
                             snap.tree, destination)
    assert error.value.code == "INVALID_REPOSITORY"
    assert not destination.exists()


def test_atomic_no_replace_handles_destination_created_during_build(
    source: tuple[Path, Path, Path, str], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import runner_app.git.materialize as module

    repo, workspace, snapshots, _ = source
    (workspace / "file.txt").write_text("source\n")
    head = _commit(workspace)
    snap = _snapshot(repo, workspace, snapshots, head)
    destination = _destination(tmp_path)
    real = module._rename_no_replace

    def conflict(parent_fd: int, source_name: str, target_name: str) -> None:
        os.mkdir(target_name, 0o700, dir_fd=parent_fd)
        marker_fd = os.open(f"{target_name}/marker", os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600, dir_fd=parent_fd)
        try:
            os.write(marker_fd, b"concurrent owner bytes")
        finally:
            os.close(marker_fd)
        real(parent_fd, source_name, target_name)

    monkeypatch.setattr(module, "_rename_no_replace", conflict)
    with pytest.raises(MaterializeError) as error:
        materialize_snapshot(repo, snap.snapshot_id, snap.commit, snap.tree, destination)
    assert error.value.code == "MATERIALIZATION_EXISTS"
    assert (destination / "marker").read_bytes() == b"concurrent owner bytes"
    assert sorted(path.name for path in destination.parent.iterdir()) == ["run-source"]


def test_changed_repo_config_and_object_alternates_are_refused(
    source: tuple[Path, Path, Path, str], tmp_path: Path,
) -> None:
    repo, workspace, snapshots, _ = source
    (workspace / "file.txt").write_text("source\n")
    head = _commit(workspace)
    snap = _snapshot(repo, workspace, snapshots, head)
    destination = _destination(tmp_path)

    run_git(repo, ["config", "--local", "filter.inject.smudge", "false"])
    with pytest.raises(MaterializeError) as error:
        materialize_snapshot(repo, snap.snapshot_id, snap.commit, snap.tree, destination)
    assert error.value.code == "UNSAFE_GIT_CONFIG"
    run_git(repo, ["config", "--local", "--unset", "filter.inject.smudge"])

    alternates = repo / "objects" / "info" / "alternates"
    alternates.write_text(str(tmp_path / "outside-objects") + "\n")
    with pytest.raises(MaterializeError) as error:
        materialize_snapshot(repo, snap.snapshot_id, snap.commit, snap.tree, destination)
    assert error.value.code == "OBJECT_ALTERNATES_UNSUPPORTED"
    assert not destination.exists()
