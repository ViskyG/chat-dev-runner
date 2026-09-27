"""Real disposable Git acceptance tests for EP-02 immutable source snapshots."""
from __future__ import annotations

import hashlib
import json
import stat
import uuid
from pathlib import Path

import pytest

from runner_app.git.argv import run_git
from runner_app.git.changes import ChangeError
from runner_app.git.snapshots import (
    capture_dirty_verify,
    snapshot_is_stale,
    stage_changeset,
)
from runner_app.git.worktrees import create_repository, create_workspace


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def source(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    managed = tmp_path / "managed"
    repo = create_repository(managed, "project")
    start = run_git(repo, ["rev-parse", "HEAD"]).strip()
    workspace = create_workspace(managed, "project", repo, "area", "feature", start)
    (workspace / "notes.txt").write_text("base\n", encoding="utf-8")
    run_git(workspace, ["add", "--", "notes.txt"])
    run_git(workspace, ["-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
                        "commit", "-m", "Base content"])
    base = run_git(workspace, ["rev-parse", "HEAD"]).strip()
    return repo, workspace, managed / "project" / "snapshots", base


def change(base: str, files: list[dict[str, object]]) -> dict[str, object]:
    return {"schema_version": "1.0", "change_id": str(uuid.uuid4()),
            "project_id": "project", "workspace_id": "area", "base_head": base,
            "files": files}


def upsert(path: str, before: bytes | None, after: str) -> dict[str, object]:
    return {"op": "upsert", "path": path,
            "expected_sha256": digest(before) if before is not None else None,
            "content_utf8": after}


def stage(repo: Path, workspace: Path, snapshots: Path, base: str,
          files: list[dict[str, object]]):
    return stage_changeset(repo, workspace, snapshots, change(base, files),
                           project_id="project", workspace_id="area",
                           expected_head=base, source_generation=7)


def test_changeset_retains_exact_immutable_tree_without_editing_workspace(source):
    repo, workspace, snapshots, base = source
    original = (workspace / "notes.txt").read_bytes()
    snap = stage(repo, workspace, snapshots, base,
                 [upsert("notes.txt", original, "changed\n"),
                  upsert("added.txt", None, "new\n")])
    assert run_git(repo, ["rev-parse", f"refs/cdr/snapshots/{snap.snapshot_id}"]).strip() == snap.commit
    assert run_git(repo, ["rev-parse", f"{snap.commit}^{{tree}}"] ).strip() == snap.tree
    assert run_git(repo, ["show", f"{snap.commit}:notes.txt"]) == "changed\n"
    assert run_git(repo, ["show", f"{snap.commit}:added.txt"]) == "new\n"
    assert run_git(workspace, ["rev-parse", "HEAD"]).strip() == base
    assert (workspace / "notes.txt").read_bytes() == original
    assert not (workspace / "added.txt").exists()
    assert not snapshot_is_stale(snap, workspace, current_generation=7)
    manifest = json.loads(snap.manifest_path.read_text())
    assert manifest["commit"] == snap.commit == run_git(repo, ["rev-parse", f"refs/cdr/snapshots/{snap.snapshot_id}"]).strip()
    assert manifest["tree"] == snap.tree
    assert manifest["source_generation"] == 7
    assert digest(snap.manifest_path.read_bytes()) == snap.manifest_sha256
    assert stat.S_IMODE(snap.manifest_path.stat().st_mode) == 0o600


def test_stale_or_invalid_batch_never_allocates_staging(source):
    repo, workspace, snapshots, base = source
    old = (workspace / "notes.txt").read_bytes()
    with pytest.raises(ChangeError) as exc:
        stage(repo, workspace, snapshots, base,
              [upsert("notes.txt", old, "first"), upsert("../escape", None, "bad")])
    assert exc.value.code == "UNSAFE_PATH"
    with pytest.raises(ChangeError) as exc:
        stage(repo, workspace, snapshots, base,
              [upsert("notes.txt", b"wrong", "first"), upsert("new.txt", None, "second")])
    assert exc.value.code == "CONTENT_CONFLICT"
    assert not snapshots.exists()
    assert (workspace / "notes.txt").read_bytes() == old


def test_wrong_workspace_and_full_base_are_required(source):
    repo, workspace, snapshots, base = source
    item = change(base, [upsert("x.txt", None, "x")])
    item["workspace_id"] = "other"
    with pytest.raises(ChangeError) as exc:
        stage_changeset(repo, workspace, snapshots, item,
                        project_id="project", workspace_id="area",
                        expected_head=base, source_generation=7)
    assert exc.value.code == "WRONG_WORKSPACE"
    item["workspace_id"] = "area"
    item["base_head"] = "a" * 40
    with pytest.raises(ChangeError) as exc:
        stage_changeset(repo, workspace, snapshots, item,
                        project_id="project", workspace_id="area",
                        expected_head=base, source_generation=7)
    assert exc.value.code == "STALE_BASE"
    assert not snapshots.exists()


@pytest.mark.parametrize("path", [
    ".git/config", ".env", "a/.env.local", ".github/workflows/build.yml",
    "secrets/token", "a/key.pem", ".gitmodules", "a//b", "a/../b",
])
def test_protected_source_paths_are_refused_before_staging(source, path):
    repo, workspace, snapshots, base = source
    with pytest.raises(ChangeError):
        stage(repo, workspace, snapshots, base, [upsert(path, None, "bad")])
    assert not snapshots.exists()


def test_symlink_parent_in_base_cannot_be_traversed(source):
    repo, workspace, snapshots, _ = source
    (workspace / "link").symlink_to("/tmp")
    run_git(workspace, ["add", "--", "link"])
    run_git(workspace, ["-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
                        "commit", "-m", "Link in base"])
    base = run_git(workspace, ["rev-parse", "HEAD"]).strip()
    with pytest.raises(ChangeError) as exc:
        stage(repo, workspace, snapshots, base, [upsert("link/escape.txt", None, "bad")])
    assert exc.value.code == "SYMLINK"
    assert not snapshots.exists()
    assert not Path("/tmp/escape.txt").exists()


def test_editor_change_after_snapshot_is_stale_but_snapshot_stays_frozen(source):
    repo, workspace, snapshots, base = source
    old = (workspace / "notes.txt").read_bytes()
    snap = stage(repo, workspace, snapshots, base, [upsert("notes.txt", old, "tested\n")])
    (workspace / "notes.txt").write_text("human edit\n", encoding="utf-8")
    assert snapshot_is_stale(snap, workspace, current_generation=7)
    assert run_git(repo, ["show", f"{snap.commit}:notes.txt"]) == "tested\n"
    assert (workspace / "notes.txt").read_text() == "human edit\n"
    assert snapshot_is_stale(snap, workspace, current_generation=8)


def test_dirty_verify_captures_only_exact_approved_tree(source):
    repo, workspace, snapshots, base = source
    (workspace / "notes.txt").write_text("dirty\n", encoding="utf-8")
    (workspace / "new.txt").write_text("explicit\n", encoding="utf-8")
    approved = {"notes.txt": digest(b"dirty\n"), "new.txt": digest(b"explicit\n")}
    with pytest.raises(ChangeError) as exc:
        capture_dirty_verify(repo, workspace, snapshots, project_id="project",
                             workspace_id="area", expected_head=base,
                             source_generation=7, approved_files={"notes.txt": approved["notes.txt"]})
    assert exc.value.code == "DIRTY_SET_CHANGED"
    snap = capture_dirty_verify(repo, workspace, snapshots, project_id="project",
                                workspace_id="area", expected_head=base,
                                source_generation=7, approved_files=approved)
    assert snap.dirty_at_capture and snap.provenance == "LOCAL_DIRTY_VERIFY"
    assert snap.approved_untracked_paths == ("new.txt",)
    assert run_git(repo, ["show", f"{snap.commit}:notes.txt"]) == "dirty\n"
    assert run_git(repo, ["show", f"{snap.commit}:new.txt"]) == "explicit\n"
    assert run_git(workspace, ["rev-parse", "HEAD"]).strip() == base
    assert (workspace / "new.txt").read_text() == "explicit\n"
    assert not snapshot_is_stale(snap, workspace, current_generation=7)
    (workspace / "new.txt").write_text("changed later\n", encoding="utf-8")
    assert snapshot_is_stale(snap, workspace, current_generation=7)


def test_dirty_verify_rejects_secret_untracked_and_symlink(source):
    repo, workspace, snapshots, base = source
    (workspace / ".env").write_text("PRIVATE=1\n")
    with pytest.raises(ChangeError) as exc:
        capture_dirty_verify(repo, workspace, snapshots, project_id="project",
                             workspace_id="area", expected_head=base,
                             source_generation=7,
                             approved_files={".env": digest(b"PRIVATE=1\n")})
    assert exc.value.code == "PROTECTED_PATH"
    (workspace / ".env").unlink()
    (workspace / "linked.txt").symlink_to("notes.txt")
    with pytest.raises(ChangeError) as exc:
        capture_dirty_verify(repo, workspace, snapshots, project_id="project",
                             workspace_id="area", expected_head=base,
                             source_generation=7,
                             approved_files={"linked.txt": digest(b"base\n")})
    assert exc.value.code == "SYMLINK"
    assert not snapshots.exists()


def test_failure_after_commit_leaves_recoverable_ref_and_intent_without_live_edits(
    source, monkeypatch,
):
    import runner_app.git.snapshots as module

    repo, workspace, snapshots, base = source
    original = (workspace / "notes.txt").read_bytes()
    real = module._atomic_json

    def fail_manifest(path, value):
        if path.name == "manifest.json":
            raise OSError("simulated crash")
        return real(path, value)

    monkeypatch.setattr(module, "_atomic_json", fail_manifest)
    with pytest.raises(OSError, match="simulated crash"):
        stage(repo, workspace, snapshots, base, [upsert("notes.txt", original, "staged\n")])
    operations = list(snapshots.iterdir())
    assert len(operations) == 1
    intent = json.loads((operations[0] / "intent.json").read_text())
    assert intent["state"] == "COMMIT_CREATED"
    assert run_git(repo, ["rev-parse", intent["ref"]]).strip() == intent["commit"]
    assert (workspace / "notes.txt").read_bytes() == original
    assert run_git(workspace, ["rev-parse", "HEAD"]).strip() == base


def test_clean_verify_retains_head_and_detects_later_editor_edit(source):
    from runner_app.git.snapshots import capture_clean_verify

    repo, workspace, snapshots, base = source
    snap = capture_clean_verify(repo, workspace, snapshots, project_id="project",
                                workspace_id="area", expected_head=base,
                                source_generation=7)
    assert snap.commit == base
    assert snap.tree == run_git(repo, ["rev-parse", f"{base}^{{tree}}"] ).strip()
    assert snap.provenance == "CLEAN_VERIFY"
    assert run_git(repo, ["rev-parse", f"refs/cdr/snapshots/{snap.snapshot_id}"]).strip() == base
    assert not snapshot_is_stale(snap, workspace, current_generation=7)
    (workspace / "notes.txt").write_text("later edit\n")
    assert snapshot_is_stale(snap, workspace, current_generation=7)
    assert run_git(repo, ["show", f"{snap.commit}:notes.txt"]) == "base\n"


def test_caller_snapshot_id_is_stable_and_cannot_be_reused(source):
    repo, workspace, snapshots, base = source
    reserved = str(uuid.uuid4())
    snap = stage_changeset(repo, workspace, snapshots,
                           change(base, [upsert("new.txt", None, "new")]),
                           project_id="project", workspace_id="area",
                           expected_head=base, source_generation=7,
                           snapshot_id=reserved)
    assert snap.snapshot_id == reserved
    with pytest.raises(ChangeError) as exc:
        stage_changeset(repo, workspace, snapshots,
                        change(base, [upsert("other.txt", None, "other")]),
                        project_id="project", workspace_id="area",
                        expected_head=base, source_generation=7,
                        snapshot_id=reserved)
    assert exc.value.code == "SNAPSHOT_EXISTS"
    assert run_git(repo, ["rev-parse", f"refs/cdr/snapshots/{reserved}"]).strip() == snap.commit


def test_assume_unchanged_does_not_hide_dirty_editor_file(source):
    repo, workspace, snapshots, base = source
    run_git(workspace, ["update-index", "--assume-unchanged", "--", "notes.txt"])
    (workspace / "notes.txt").write_text("hidden dirty edit\n")
    with pytest.raises(ChangeError) as exc:
        stage(repo, workspace, snapshots, base,
              [upsert("new.txt", None, "new")])
    assert exc.value.code == "DIRTY_WORKSPACE"
    assert not snapshots.exists()


def test_nested_change_uses_exact_base_blob_and_path(source):
    repo, workspace, snapshots, _ = source
    (workspace / "src").mkdir()
    (workspace / "src" / "main.py").write_text("x = 1\n")
    run_git(workspace, ["add", "--", "src/main.py"])
    run_git(workspace, ["-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
                        "commit", "-m", "Nested source"])
    base = run_git(workspace, ["rev-parse", "HEAD"]).strip()
    snap = stage(repo, workspace, snapshots, base,
                 [upsert("src/main.py", b"x = 1\n", "x = 2\n")])
    assert run_git(repo, ["show", f"{snap.commit}:src/main.py"]) == "x = 2\n"
    assert (workspace / "src" / "main.py").read_text() == "x = 1\n"


def test_snapshot_manifest_and_directories_remain_owner_only_under_restrictive_umask(
    source,
):
    import os

    repo, workspace, snapshots, base = source
    old_umask = os.umask(0o777)
    try:
        snap = stage(repo, workspace, snapshots, base,
                     [upsert("new.txt", None, "new")])
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(snapshots.stat().st_mode) == 0o700
    assert stat.S_IMODE(snap.manifest_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(snap.manifest_path.stat().st_mode) == 0o600
