"""Current-at-start source capture against disposable real Git repositories."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

import runner_app.git.source_v2 as source_module
from runner_app.executors.broker import _snapshot_tree
from runner_app.git.argv import GitError, run_git
from runner_app.git.results_v2 import LocalBareResultTransport
from runner_app.git.source_v2 import (
    DEFAULT_SOURCE_LIMITS,
    LocalWorkspace,
    SourceError,
    SourceExclusion,
    SourceLimits,
    SourceProject,
    SourceResolver,
)


def git(path: Path, *arguments: str) -> str:
    return run_git(path, list(arguments)).strip()


def commit(path: Path, message: str) -> str:
    git(path, "add", "--all")
    git(path, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
        "commit", "--quiet", "-m", message)
    return git(path, "rev-parse", "HEAD")


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        git(self.workspace, "init", "--initial-branch=tested")
        for name, content in {
            "a.txt": b"original\n", "delete.txt": b"delete me\n",
            ".runner/project.json": b'{"project_id":"demo"}\n',
            ".runner/tasks/test.sh": b"#!/bin/sh\nexit 0\n",
            ".runner/commands/slot.json": b'{"untrusted":"command"}\n',
            ".runner/results/old/result.json": b'{"old":"result"}\n',
            ".runner/state/local.json": b'{"controller":"state"}\n',
            ".github/workflows/test.yml": b"name: existing workflow\n",
            ".env": b"SECRET=must-not-copy\n",
        }.items():
            target = self.workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self.initial = commit(self.workspace, "Initial source")
        self.receiver = root / "receiver.git"
        self.repo = root / "controller.git"
        git(root, "clone", "--bare", "--no-local", str(self.workspace), str(self.receiver))
        git(root, "clone", "--bare", "--no-local", str(self.receiver), str(self.repo))
        git(self.workspace, "remote", "add", "receiver", str(self.receiver))
        self.runs = root / "runs"
        self.transport = LocalBareResultTransport(self.receiver)

    def resolver(self, *, local: LocalWorkspace | None = None,
                 limits: SourceLimits = DEFAULT_SOURCE_LIMITS,
                 fetch: object | None = None) -> SourceResolver:
        return SourceResolver({"demo": SourceProject(
            self.repo, self.runs, {"editor": local or LocalWorkspace(self.workspace)},
            fetch_head=self.transport.fetch_head if fetch is None else fetch,  # type: ignore[arg-type]
        )}, limits=limits)

    def capture(self, *, local: LocalWorkspace | None = None,
                limits: SourceLimits = DEFAULT_SOURCE_LIMITS) -> source_module.SourceSnapshot:
        return self.resolver(local=local, limits=limits).resolve_current("demo", str(uuid4()), {
            "kind": "local_workspace" if local else "github_branch", "branch": "tested",
            **({"workspace_id": "editor"} if local else {}),
        })


@pytest.fixture
def fixture(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def test_branch_is_fetched_when_execution_starts_and_copied_once(fixture: Fixture) -> None:
    resolver = fixture.resolver()  # Request may sit queued while this resolver exists.
    (fixture.workspace / "a.txt").write_bytes(b"advanced while queued\n")
    current = commit(fixture.workspace, "Advance queued source")
    git(fixture.workspace, "push", "receiver", "HEAD:refs/heads/tested")
    assert git(fixture.repo, "rev-parse", "refs/heads/tested") == fixture.initial
    refs = git(fixture.repo, "for-each-ref", "--format=%(refname) %(objectname)")
    index = (fixture.workspace / ".git/index").read_bytes()
    snapshot = resolver.resolve_current("demo", str(uuid4()), {
        "kind": "github_branch", "branch": "tested"})
    assert snapshot.source_commit == current
    assert snapshot.source_tree == git(fixture.workspace, "rev-parse", "HEAD^{tree}")
    assert snapshot.path.joinpath("a.txt").read_bytes() == b"advanced while queued\n"
    assert snapshot.source_tree != snapshot.workload_tree
    assert _snapshot_tree(snapshot.path, snapshot.tree) == snapshot.workload_tree
    assert not snapshot.dirty and snapshot.overlay_sha256 is None
    assert git(fixture.repo, "for-each-ref", "--format=%(refname) %(objectname)") == refs
    assert (fixture.workspace / ".git/index").read_bytes() == index
    (fixture.workspace / "a.txt").write_bytes(b"changed after start\n")
    assert snapshot.path.joinpath("a.txt").read_bytes() == b"advanced while queued\n"
    assert all(not item.stat().st_mode & 0o222 for item in snapshot.path.rglob("*"))
    manifest = snapshot.manifest_path.read_bytes()
    assert hashlib.sha256(manifest).hexdigest() == snapshot.manifest_sha256
    assert json.loads(manifest)["workload_snapshot_sha256"] == snapshot.workload_snapshot_sha256
    assert snapshot.path.parent == fixture.runs / snapshot.request_id


def test_reserved_and_credentials_excluded_but_manifest_tasks_ci_retained(fixture: Fixture) -> None:
    snapshot = fixture.capture()
    paths = {item.path for item in snapshot.files}
    assert paths == {"a.txt", "delete.txt", ".runner/project.json",
                     ".runner/tasks/test.sh", ".github/workflows/test.yml"}
    excluded = {item.path: item.reason for item in snapshot.excluded_paths}
    assert excluded[".env"] == "credential_material"
    assert excluded[".runner/results/old/result.json"] == "controller_owned"
    assert excluded[".runner/commands/slot.json"] == "controller_owned"
    assert excluded[".runner/state/local.json"] == "controller_owned"
    assert not snapshot.path.joinpath(".git").exists()
    assert all(item.source_identity is None for item in snapshot.files)


def test_local_dirty_untracked_and_deletion_capture_without_touching_originals(fixture: Fixture) -> None:
    (fixture.workspace / "a.txt").write_bytes(b"dirty current content\n")
    (fixture.workspace / "a.txt").chmod(0o755)
    (fixture.workspace / "delete.txt").unlink()
    (fixture.workspace / "new.txt").write_bytes(b"approved untracked\n")
    (fixture.workspace / "unknown.txt").write_bytes(b"not approved\n")
    before_index = (fixture.workspace / ".git/index").read_bytes()
    before_refs = git(fixture.workspace, "for-each-ref", "--format=%(refname) %(objectname)")
    snapshot = fixture.capture(local=LocalWorkspace(
        fixture.workspace, include_dirty=True, include_untracked=("new.txt",)))
    assert snapshot.source_commit == fixture.initial
    assert snapshot.dirty and snapshot.overlay_sha256
    assert snapshot.dirty_paths == ("a.txt", "delete.txt")
    assert snapshot.untracked_paths == ("new.txt",)
    assert snapshot.path.joinpath("a.txt").read_bytes() == b"dirty current content\n"
    assert snapshot.path.joinpath("new.txt").read_bytes() == b"approved untracked\n"
    assert not snapshot.path.joinpath("delete.txt").exists()
    assert not snapshot.path.joinpath("unknown.txt").exists()
    copied = next(item for item in snapshot.files if item.path == "a.txt")
    original_info = (fixture.workspace / "a.txt").stat()
    assert copied.source_identity and copied.source_identity[:2] == (
        original_info.st_dev, original_info.st_ino)
    assert copied.sha256 == hashlib.sha256(b"dirty current content\n").hexdigest()
    assert _snapshot_tree(snapshot.path, snapshot.tree) == snapshot.workload_tree
    assert before_index == (fixture.workspace / ".git/index").read_bytes()
    assert before_refs == git(fixture.workspace, "for-each-ref", "--format=%(refname) %(objectname)")
    assert (fixture.workspace / "unknown.txt").read_bytes() == b"not approved\n"
    assert (fixture.workspace / "a.txt").stat().st_mode & 0o200


def test_staged_add_is_dirty_and_not_misreported_as_untracked(fixture: Fixture) -> None:
    (fixture.workspace / "new.txt").write_bytes(b"staged content\n")
    git(fixture.workspace, "add", "new.txt")
    snapshot = fixture.capture(local=LocalWorkspace(fixture.workspace, include_dirty=True))
    assert snapshot.dirty_paths == ("new.txt",)
    assert not snapshot.untracked_paths


def test_only_approved_untracked_subtrees_are_traversed(fixture: Fixture) -> None:
    (fixture.workspace / "approved").mkdir()
    (fixture.workspace / "approved/new.txt").write_bytes(b"approved subtree\n")
    (fixture.workspace / "node_modules").mkdir()
    (fixture.workspace / "node_modules/unsafe-link").symlink_to("/outside")
    snapshot = fixture.capture(local=LocalWorkspace(
        fixture.workspace, include_untracked=("approved/",)))
    assert snapshot.untracked_paths == ("approved/new.txt",)
    assert SourceExclusion("node_modules", "unapproved_untracked") in snapshot.excluded_paths


def test_local_mutation_during_copy_fails_without_publishing_snapshot(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_blob = source_module._write_blob

    def mutate(root_fd: int, path: str, payload: bytes, *, executable: bool) -> None:
        write_blob(root_fd, path, payload, executable=executable)
        if path == "a.txt":
            (fixture.workspace / path).write_bytes(b"concurrent editor mutation\n")

    monkeypatch.setattr(source_module, "_write_blob", mutate)
    request = str(uuid4())
    with pytest.raises(SourceError, match="INPUT_CHANGED"):
        fixture.resolver().resolve_current("demo", request, {
            "kind": "local_workspace", "workspace_id": "editor", "branch": "tested"})
    assert not (fixture.runs / request).exists()
    assert (fixture.workspace / "a.txt").read_bytes() == b"concurrent editor mutation\n"


@pytest.mark.parametrize("missing", [False, True])
def test_fetch_failure_or_missing_branch_never_uses_cached_head(fixture: Fixture, missing: bool) -> None:
    calls: list[tuple[Path, str]] = []

    def fetch(repo: Path, branch: str) -> str | None:
        calls.append((repo, branch))
        if missing:
            return None
        raise RuntimeError("provider detail containing a hypothetical token")

    request = str(uuid4())
    with pytest.raises(SourceError) as error:
        fixture.resolver(fetch=fetch).resolve_current("demo", request, {
            "kind": "github_branch", "branch": "tested"})
    assert error.value.code == ("SOURCE_BRANCH_MISSING" if missing else "SOURCE_FETCH_FAILED")
    assert "token" not in str(error.value)
    assert calls == [(fixture.repo, "tested")]
    assert not (fixture.runs / request).exists()
    assert git(fixture.repo, "rev-parse", "refs/heads/tested") == fixture.initial


@pytest.mark.parametrize("case", ["symlink", "hardlink", "collision", "filter", "submodule", "alternate"])
def test_unsafe_local_features_refused(fixture: Fixture, case: str) -> None:
    expected = "UNSAFE_SOURCE_FILE"
    if case == "symlink":
        (fixture.workspace / "linked").symlink_to("a.txt")
    elif case == "hardlink":
        os.link(fixture.workspace / "a.txt", fixture.workspace / "linked")
    elif case == "collision":
        (fixture.workspace / "A.txt").write_bytes(b"collision")
        expected = "SOURCE_PATH_COLLISION"
    elif case == "filter":
        (fixture.workspace / ".gitattributes").write_bytes(b"*.txt filter=arbitrary\n")
        git(fixture.workspace, "add", ".gitattributes")
        expected = "FILTER_UNSUPPORTED"
    elif case == "submodule":
        git(fixture.workspace, "update-index", "--add", "--cacheinfo",
            f"160000,{fixture.initial},submodule")
        expected = "SUBMODULE_UNSUPPORTED"
    else:
        (fixture.workspace / ".git/objects/info/alternates").write_text("/unapproved/objects\n")
        with pytest.raises(GitError, match="OBJECT_ALTERNATES_UNSUPPORTED"):
            fixture.capture(local=LocalWorkspace(fixture.workspace, include_dirty=True))
        return
    with pytest.raises(SourceError, match=expected):
        fixture.capture(local=LocalWorkspace(fixture.workspace, include_dirty=True))


@pytest.mark.parametrize("limits", [SourceLimits(per_file_bytes=1), SourceLimits(source_bytes=1),
                                  SourceLimits(max_files=1)])
def test_source_copy_is_bounded(fixture: Fixture, limits: SourceLimits) -> None:
    with pytest.raises(SourceError, match="SOURCE_(BYTE|FILE_COUNT)_LIMIT"):
        fixture.capture(limits=limits)


def test_dirty_requires_controller_approval_and_detached_is_not_guessed(fixture: Fixture) -> None:
    (fixture.workspace / "a.txt").write_bytes(b"unapproved dirty\n")
    with pytest.raises(SourceError, match="DIRTY_SOURCE_NOT_APPROVED"):
        fixture.capture(local=LocalWorkspace(fixture.workspace))
    git(fixture.workspace, "checkout", "--detach", "HEAD")
    with pytest.raises(SourceError, match="BRANCH_REQUIRED"):
        fixture.capture(local=LocalWorkspace(fixture.workspace, include_dirty=True))


def test_exact_scope_and_registered_paths_only(fixture: Fixture) -> None:
    resolver = fixture.resolver()
    with pytest.raises(SourceError, match="WORKSPACE_NOT_REGISTERED"):
        resolver.resolve_current("demo", str(uuid4()), {
            "kind": "local_workspace", "branch": "tested", "workspace_id": "/tmp"})
    with pytest.raises(SourceError, match="INVALID_SOURCE_SELECTOR"):
        resolver.resolve_current("demo", str(uuid4()), {
            "kind": "github_branch", "branch": "tested", "path": "/tmp"})
    request = str(uuid4())
    selector = {"kind": "github_branch", "branch": "tested"}
    resolver.resolve_current("demo", request, selector)
    with pytest.raises(SourceError, match="SOURCE_SNAPSHOT_EXISTS"):
        resolver.resolve_current("demo", request, selector)
    unsafe = SourceResolver({"demo": SourceProject(
        fixture.repo, fixture.workspace / "runs", {"editor": LocalWorkspace(fixture.workspace)})})
    with pytest.raises(SourceError, match="UNSAFE_RUNS_ROOT"):
        unsafe.resolve_current("demo", str(uuid4()), selector)
    assert not (fixture.workspace / "runs").exists()
