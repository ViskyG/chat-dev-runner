"""Real disposable Git tests for EP-02 managed source lifecycle."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from runner_app.git.argv import GitError, run_git, run_git_bytes, run_git_input, validate_branch
from runner_app.git.worktrees import (
    archive_workspace,
    create_repository,
    create_workspace,
    fetch,
    import_local_granted,
    inspect_workspace,
    retarget_workspace,
    sync_workspace,
)


def _git(path: Path, *args: str) -> str:
    return run_git(path, list(args)).strip()


def _commit(path: Path, name: str, content: str) -> str:
    (path / name).write_text(content)
    _git(path, "add", "--", name)
    _git(path, "-c", "user.name=Test Owner", "-c", "user.email=owner@example.invalid",
         "commit", "-m", "Fixture commit")
    return _git(path, "rev-parse", "HEAD")


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "human-source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _commit(source, "hello.txt", "committed\n")
    return source


def test_real_worktrees_have_independent_branches_and_private_layout(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    repo = create_repository(root, "project-a")
    base = _git(repo, "rev-parse", "HEAD")
    first = create_workspace(root, "project-a", repo, "first", "feature/first", base)
    second = create_workspace(root, "project-a", repo, "second", "feature/second", base)
    _commit(first, "first.txt", "first\n")
    assert inspect_workspace(repo, first)["head"] != base
    assert inspect_workspace(repo, second)["head"] == base
    assert not (second / "first.txt").exists()
    assert oct(os.stat(root).st_mode & 0o777) == "0o700"
    assert oct(os.stat(repo).st_mode & 0o777) == "0o700"
    assert repo == root / "project-a" / "repo.git"
    assert first == root / "project-a" / "workspaces" / "first"


def test_retarget_requires_exact_clean_idle_head_and_unoccupied_branch(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    repo = create_repository(root, "p")
    base = _git(repo, "rev-parse", "HEAD")
    first = create_workspace(root, "p", repo, "first", "feature/first", base)
    create_workspace(root, "p", repo, "second", "feature/second", base)
    _git(repo, "branch", "feature/free", base)
    arguments = {"expected_head": base, "expected_branch": "feature/first",
                 "target_branch": "feature/free", "target_commit": base}
    with pytest.raises(GitError, match="WORKSPACE_BUSY"):
        retarget_workspace(repo, first, **arguments, busy=True)
    (first / "untracked.txt").write_text("keep me")
    with pytest.raises(GitError, match="DIRTY_WORKSPACE"):
        retarget_workspace(repo, first, **arguments)
    assert (first / "untracked.txt").read_text() == "keep me"
    (first / "untracked.txt").unlink()
    with pytest.raises(GitError, match="BRANCH_IN_USE"):
        retarget_workspace(repo, first, expected_head=base, expected_branch="feature/first",
                           target_branch="feature/second", target_commit=base)
    with pytest.raises(GitError, match="WORKSPACE_CHANGED"):
        retarget_workspace(repo, first, **{**arguments, "expected_head": "0" * 40})
    state = retarget_workspace(repo, first, **arguments)
    assert state["branch"] == "feature/free"
    assert state["head"] == base
    assert _git(repo, "rev-parse", "refs/heads/feature/first") == base


def test_local_import_preserves_dirty_original_and_does_not_run_hooks(tmp_path: Path) -> None:
    source = _source(tmp_path)
    marker = tmp_path / "hook-ran"
    hook = source / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\nprintf ran > '{marker}'\n")
    hook.chmod(0o700)
    (source / "hello.txt").write_text("human unsaved edit\n")
    (source / "private-untracked.txt").write_text("private\n")
    _git(source, "remote", "add", "other", str(tmp_path / "other.git"))
    status_before = _git(source, "status", "--porcelain=v1")
    branch_before = _git(source, "symbolic-ref", "--short", "HEAD")
    index_before = (source / ".git" / "index").read_bytes()
    config_before = (source / ".git" / "config").read_bytes()
    root = tmp_path / "managed"
    repo = import_local_granted(root, "imported", source)
    head = _git(repo, "rev-parse", "HEAD")
    workspace = create_workspace(root, "imported", repo, "first", "feature/imported", head)
    assert (workspace / "hello.txt").read_text() == "committed\n"
    assert not (workspace / "private-untracked.txt").exists()
    assert not marker.exists()
    assert (source / "hello.txt").read_text() == "human unsaved edit\n"
    assert (source / "private-untracked.txt").read_text() == "private\n"
    assert (source / ".git" / "index").read_bytes() == index_before
    assert (source / ".git" / "config").read_bytes() == config_before
    assert _git(source, "status", "--porcelain=v1") == status_before
    assert _git(source, "symbolic-ref", "--short", "HEAD") == branch_before
    assert _git(repo, "remote") == ""


def test_hostile_filter_and_submodule_requirements_are_refused(tmp_path: Path) -> None:
    source = _source(tmp_path)
    marker = tmp_path / "smudge-ran"
    _git(source, "config", "filter.evil.smudge", f"touch {marker}")
    with pytest.raises(GitError, match="UNSAFE_GIT_CONFIG"):
        import_local_granted(tmp_path / "managed", "blocked-filter", source)
    assert not marker.exists()
    _git(source, "config", "--unset", "filter.evil.smudge")
    _commit(source, ".gitmodules", "[submodule \"outside\"]\n\tpath = outside\n")
    with pytest.raises(GitError, match="SUBMODULE_UNSUPPORTED"):
        import_local_granted(tmp_path / "managed", "blocked-submodule", source)


def test_fetch_only_updates_tracking_then_explicit_sync_fast_forwards(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    repo = create_repository(root, "p")
    base = _git(repo, "rev-parse", "HEAD")
    workspace = create_workspace(root, "p", repo, "main-area", "main", base)
    remote = tmp_path / "upstream.git"
    _git(tmp_path, "clone", "--bare", "--no-local", str(repo), str(remote))
    developer = tmp_path / "developer"
    _git(tmp_path, "clone", str(remote), str(developer))
    updated = _commit(developer, "update.txt", "remote update\n")
    _git(developer, "push", "origin", "main")
    _git(repo, "remote", "add", "upstream", str(remote))
    with pytest.raises(GitError, match="REMOTE_NOT_ALLOWED"):
        fetch(repo, "upstream", allowed_urls=[str(tmp_path / "wrong.git")])
    with pytest.raises(GitError, match="REMOTE_NOT_ALLOWED"):
        fetch(repo, "upstream", allowed_urls=str(remote))
    fetch(repo, "upstream", allowed_urls=[str(remote)])
    status = inspect_workspace(repo, workspace, remote="upstream")
    assert status["head"] == base
    assert status["behind"] == 1 and status["ahead"] == 0
    assert not (workspace / "update.txt").exists()
    with pytest.raises(GitError, match="STALE_TARGET"):
        sync_workspace(repo, workspace, expected_head=base, expected_branch="main",
                       remote="upstream", remote_branch="main", expected_target_commit=base)
    synced = sync_workspace(repo, workspace, expected_head=base, expected_branch="main",
                            remote="upstream", remote_branch="main",
                            expected_target_commit=updated)
    assert synced["head"] == updated
    assert synced["behind"] == 0
    assert (workspace / "update.txt").read_text() == "remote update\n"


def test_sync_refuses_divergence_and_dirty_workspace(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    repo = create_repository(root, "p")
    base = _git(repo, "rev-parse", "HEAD")
    workspace = create_workspace(root, "p", repo, "main-area", "main", base)
    remote = tmp_path / "upstream.git"
    _git(tmp_path, "clone", "--bare", "--no-local", str(repo), str(remote))
    developer = tmp_path / "developer"
    _git(tmp_path, "clone", str(remote), str(developer))
    _commit(developer, "remote.txt", "remote\n")
    _git(developer, "push", "origin", "main")
    local_head = _commit(workspace, "local.txt", "local\n")
    _git(repo, "remote", "add", "upstream", str(remote))
    fetch(repo, "upstream", allowed_urls=[str(remote)])
    state = inspect_workspace(repo, workspace, remote="upstream")
    assert state["diverged"] and state["ahead"] == 1 and state["behind"] == 1
    with pytest.raises(GitError, match="SYNC_DIVERGED"):
        sync_workspace(repo, workspace, expected_head=local_head, expected_branch="main",
                       remote="upstream", remote_branch="main")
    assert _git(workspace, "rev-parse", "HEAD") == local_head
    (workspace / "dirty.txt").write_text("keep")
    with pytest.raises(GitError, match="DIRTY_WORKSPACE"):
        sync_workspace(repo, workspace, expected_head=local_head, expected_branch="main",
                       remote="upstream", remote_branch="main")
    assert (workspace / "dirty.txt").read_text() == "keep"


def test_archive_retains_dirty_source_and_refuses_busy(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    repo = create_repository(root, "p")
    base = _git(repo, "rev-parse", "HEAD")
    workspace = create_workspace(root, "p", repo, "first", "feature/first", base)
    (workspace / "draft.txt").write_text("unfinished")
    with pytest.raises(GitError, match="WORKSPACE_BUSY"):
        archive_workspace(repo, workspace, expected_head=base,
                          expected_branch="feature/first", busy=True)
    record = archive_workspace(repo, workspace, expected_head=base,
                               expected_branch="feature/first")
    assert record["inactive"] and record["dirty"]
    assert record["retained_path"] == str(workspace)
    assert (workspace / "draft.txt").read_text() == "unfinished"


def test_git_invocation_is_bounded_redacted_and_ref_validated(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    repo = create_repository(root, "p")
    assert b"\x00" in run_git_bytes(repo, ["for-each-ref", "--format=%(refname)%00"])
    for invalid in ("-evil", "feature/../main", "feature//main", "a.lock", "a b", "a@{1}"):
        with pytest.raises(GitError, match="INVALID_BRANCH"):
            validate_branch(invalid)
    with pytest.raises(GitError) as failure:
        run_git(repo, ["rev-parse", "sensitive-token-should-not-appear"])
    assert "sensitive-token-should-not-appear" not in str(failure.value)
    with pytest.raises(GitError, match="UNSAFE_GIT_CONFIG"):
        run_git(repo, ["-c", "core.hooksPath=/tmp/hostile", "status"])


def test_local_import_accepts_pinned_owner_handle_and_rejects_closed_fd(tmp_path: Path) -> None:
    source = _source(tmp_path)
    original_head = _git(source, "rev-parse", "HEAD")
    descriptor = os.open(source, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
    child_path = f"/proc/self/fd/{descriptor}"
    moved = tmp_path / "moved-original"
    source.rename(moved)
    replacement = _source(tmp_path)
    _commit(replacement, "different.txt", "path swapped")
    try:
        with pytest.raises(GitError, match="INVALID_IMPORT_SOURCE"):
            import_local_granted(tmp_path / "managed", "no-fd", child_path)
        repo = import_local_granted(tmp_path / "managed", "pinned", child_path,
                                    pass_fds=(descriptor,))
        assert _git(repo, "rev-parse", "HEAD") == original_head
        assert _git(repo, "rev-parse", "HEAD") != _git(replacement, "rev-parse", "HEAD")
    finally:
        os.close(descriptor)
    with pytest.raises(GitError, match="INVALID_GIT_FD"):
        run_git(Path(child_path), ["status"], pass_fds=(descriptor,))


def test_managed_checkout_disables_hostile_git_hook(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    repo = create_repository(root, "p")
    marker = tmp_path / "hook-ran"
    hook = repo / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\nprintf ran > '{marker}'\n")
    hook.chmod(0o700)
    base = _git(repo, "rev-parse", "HEAD")
    create_workspace(root, "p", repo, "first", "feature/first", base)
    assert not marker.exists()


def test_tracked_lfs_filter_is_refused_before_checkout(tmp_path: Path) -> None:
    source = _source(tmp_path)
    _commit(source, ".gitattributes", "*.bin filter=lfs diff=lfs merge=lfs -text\n")
    with pytest.raises(GitError, match="FILTER_UNSUPPORTED"):
        import_local_granted(tmp_path / "managed", "lfs", source)
    assert not (tmp_path / "managed" / "lfs" / "repo.git").exists()


def test_binary_git_input_avoids_staging_file_and_has_size_bound(tmp_path: Path) -> None:
    repo = create_repository(tmp_path / "managed", "p")
    blob_id = run_git_input(repo, ["hash-object", "-w", "--stdin", "--no-filters"],
                            b"\x00non-utf8\xff").strip()
    assert run_git_bytes(repo, ["cat-file", "blob", blob_id]) == b"\x00non-utf8\xff"
    with pytest.raises(GitError, match="GIT_INPUT_LIMIT"):
        run_git_input(repo, ["hash-object", "--stdin"], b"x" * (2 * 1024 * 1024 + 1))


def test_local_import_rejects_object_alternates(tmp_path: Path) -> None:
    source = _source(tmp_path)
    alternate = source / ".git" / "objects" / "info" / "alternates"
    alternate.write_text(str(tmp_path / "unapproved-objects") + "\n")
    with pytest.raises(GitError, match="OBJECT_ALTERNATES_UNSUPPORTED"):
        import_local_granted(tmp_path / "managed", "alternates", source)
    assert not (tmp_path / "managed" / "alternates" / "repo.git").exists()


def test_ignored_private_files_survive_retarget_and_sync(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    repo = create_repository(root, "p")
    initial = _git(repo, "rev-parse", "HEAD")
    main = create_workspace(root, "p", repo, "main-area", "main", initial)
    base = _commit(main, ".gitignore", "cache.bin\n")
    target_area = create_workspace(root, "p", repo, "target-area", "feature/target", base)
    (target_area / "cache.bin").write_text("tracked target\n")
    _git(target_area, "add", "-f", "--", "cache.bin")
    _git(target_area, "-c", "user.name=Test Owner", "-c",
         "user.email=owner@example.invalid", "commit", "-m", "Track target fixture")
    target = _git(target_area, "rev-parse", "HEAD")
    old = create_workspace(root, "p", repo, "old-area", "feature/old", base)
    (old / "cache.bin").write_text("private old area\n")
    assert inspect_workspace(repo, old)["dirty"]
    with pytest.raises(GitError, match="DIRTY_WORKSPACE"):
        retarget_workspace(repo, old, expected_head=base, expected_branch="feature/old",
                           target_branch="feature/target", target_commit=target)
    assert (old / "cache.bin").read_text() == "private old area\n"
    assert _git(old, "symbolic-ref", "--short", "HEAD") == "feature/old"

    remote = tmp_path / "upstream.git"
    _git(tmp_path, "clone", "--bare", "--no-local", str(repo), str(remote))
    developer = tmp_path / "developer"
    _git(tmp_path, "clone", str(remote), str(developer))
    (developer / "cache.bin").write_text("tracked remote\n")
    _git(developer, "add", "-f", "--", "cache.bin")
    _git(developer, "-c", "user.name=Test Owner", "-c",
         "user.email=owner@example.invalid", "commit", "-m", "Track remote fixture")
    _git(developer, "push", "origin", "main")
    _git(repo, "remote", "add", "upstream", str(remote))
    fetch(repo, "upstream", allowed_urls=[str(remote)])
    (main / "cache.bin").write_text("private main area\n")
    assert inspect_workspace(repo, main)["dirty"]
    with pytest.raises(GitError, match="DIRTY_WORKSPACE"):
        sync_workspace(repo, main, expected_head=base, expected_branch="main",
                       remote="upstream", remote_branch="main")
    assert (main / "cache.bin").read_text() == "private main area\n"
    assert _git(main, "rev-parse", "HEAD") == base
