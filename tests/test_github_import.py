"""Offline approved GitHub source imports; the remote fixture is disposable local Git."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from runner_app.adapters.github import GitHubBranch, GitHubRepository
from runner_app.domain.commands import freeze, validate_shape
from runner_app.git.argv import GitError, run_git
from runner_app.git.github_import import (
    ApprovedGitHubImport,
    GitHubImportService,
)
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.git.service import SourceService
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger

URL = "https://github.com/alice/source.git"


def repository() -> GitHubRepository:
    return GitHubRepository(
        id=421, node_id="R_421", owner="alice", owner_id=99, name="source",
        full_name="alice/source", html_url="https://github.com/alice/source",
        clone_url=URL, ssh_url="git@github.com:alice/source.git",
        private=True, default_branch="main", description=None,
    )


def approval() -> ApprovedGitHubImport:
    return ApprovedGitHubImport("alice/source", "owner", "runner-owner", 421, 99, "R_421")


def effects(ledger: Ledger, request_id: str) -> list[dict[str, Any]]:
    operation = ledger.get(request_id)
    assert operation is not None
    return cast(list[dict[str, Any]], operation["effects"])


def fixture_source(root: Path, *, blocked_file: str | None = None) -> tuple[Path, Path, str]:
    original = root / "original"
    original.mkdir(mode=0o700)
    run_git(original, ["init", "-b", "main"])
    (original / "main.py").write_text("value = 1\n")
    if blocked_file is not None:
        (original / blocked_file).write_text("blocked = 1\n")
    run_git(original, ["add", "."])
    run_git(original, [
        "-c", "user.name=Owner", "-c", "user.email=owner@local.invalid",
        "commit", "-m", "Source",
    ])
    (original / "uncommitted.txt").write_text("leave me alone\n")
    head = run_git(original, ["rev-parse", "HEAD"]).strip()
    source = root / "source.git"
    run_git(root, ["clone", "--bare", "--no-local", str(original), str(source)])
    return original, source, head


def admitted(tmp_path: Path, *, repository_name: str = "alice/source",
             locks: bool = True) -> tuple[Ledger, SourceRegistry, str]:
    database = Database(tmp_path / "state" / "state.sqlite")
    ledger = Ledger(database)
    ledger.startup()
    ledger.arm()
    registry = SourceRegistry(database, tmp_path / "managed")
    state = ledger.state()
    now = datetime.now(timezone.utc)
    command = {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "CREATE_PROJECT", "project_id": "fixture",
        "workspace_id": None, "expected_head": None,
        "params": {"name": "Fixture",
                   "source": {"kind": "github", "repository": repository_name},
                   "template_id": "python-basic", "github_identity": "owner",
                   "visibility": "private"},
        "intent": "Import approved source",
    }
    validate_shape(command)
    scopes = ("repo:fixture", "global:owner", "global:drive-installation") if locks else (
        "repo:fixture", "global:owner",
    )
    assert ledger.admit(command, freeze(command).digest, "local-owner", scopes).execute
    return ledger, registry, command["request_id"]


class FakeREST:
    expected_login = "runner-owner"

    def __init__(self, source: GitHubRepository, head: str) -> None:
        self.source = source
        self.head = head
        self.changed_head: str | None = None
        self.repository_reads = 0
        self.branch_reads = 0

    def get_repository(self, owner: str, name: str, *,
                       expected_repo_id: int | None = None) -> GitHubRepository:
        self.repository_reads += 1
        assert (owner, name, expected_repo_id) == ("alice", "source", 421)
        return self.source

    def branch_head(self, source: GitHubRepository, branch: str) -> GitHubBranch:
        self.branch_reads += 1
        assert source is self.source and branch == "main"
        head = self.changed_head if self.branch_reads > 1 and self.changed_head else self.head
        return GitHubBranch("main", head, False)


class LocalBareCloner:
    def __init__(self, source: Path, repository: GitHubRepository,
                 ledger: Ledger, request_id: str) -> None:
        self.source = source
        self.repository = repository
        self.ledger = ledger
        self.request_id = request_id
        self.calls = 0
        self.fail_after_clone = False

    def clone_bare(self, stage_parent: Path, destination: Path) -> None:
        self.calls += 1
        recorded = effects(self.ledger, self.request_id)
        assert len(recorded) == 1 and recorded[0]["state"] == "INTENDED"
        assert recorded[0]["intent"]["repo_path"] == str(stage_parent.parent / "repo.git")
        run_git(stage_parent, [
            "clone", "--bare", "--no-local", "--no-hardlinks", "--no-tags",
            str(self.source), str(destination),
        ])
        # Model the fixed GitHub URL retained by the production HTTPS cloner.
        run_git(destination, ["remote", "set-url", "origin", self.repository.clone_url])
        if self.fail_after_clone:
            raise GitError("GITHUB_GIT_FAILED", "Simulated lost clone response")


def service(tmp_path: Path, ledger: Ledger, registry: SourceRegistry,
            request_id: str, local_source: Path, head: str,
            *, remote: GitHubRepository | None = None,
            approvals: dict[str, ApprovedGitHubImport] | None = None
            ) -> tuple[GitHubImportService, FakeREST, list[LocalBareCloner]]:
    rest = FakeREST(remote or repository(), head)
    cloners: list[LocalBareCloner] = []

    def make_cloner(observed: GitHubRepository) -> LocalBareCloner:
        cloner = LocalBareCloner(local_source, observed, ledger, request_id)
        cloners.append(cloner)
        return cloner

    importer = GitHubImportService(
        ledger, registry, rest, make_cloner,
        {"alice/source": approval()} if approvals is None else approvals,
    )
    return importer, rest, cloners


def test_import_copies_committed_source_without_changing_original(tmp_path: Path) -> None:
    original, local_source, head = fixture_source(tmp_path)
    original_status = run_git(original, ["status", "--porcelain=v1", "-z"])
    source_refs = run_git(local_source, ["show-ref"])
    source_config = (local_source / "config").read_bytes()
    ledger, registry, request_id = admitted(tmp_path)
    importer, rest, cloners = service(
        tmp_path, ledger, registry, request_id, local_source, head,
    )

    result = importer.import_source(request_id)

    imported = Path(result.project["repo_path"])
    assert result.repository.id == 421 and result.default_head == head
    assert result.project["origin_kind"] == "github"
    assert result.project["origin_reference"] == "alice/source"
    assert run_git(imported, ["rev-parse", "refs/heads/main"]).strip() == head
    assert run_git(imported, ["remote"]).strip() == ""
    assert not (imported / ".git").exists()
    assert rest.repository_reads == 2 and rest.branch_reads == 2
    assert len(cloners) == 1 and cloners[0].calls == 1
    assert effects(ledger, request_id)[0]["state"] == "CONFIRMED"
    registered = registry.get_project("fixture")
    assert registered is not None and registered["repo_path"] == str(imported)
    assert run_git(original, ["status", "--porcelain=v1", "-z"]) == original_status
    assert (original / "uncommitted.txt").read_text() == "leave me alone\n"
    assert run_git(local_source, ["show-ref"]) == source_refs
    assert (local_source / "config").read_bytes() == source_config
    initial = SourceService(ledger, registry).create_initial_workspace_source(request_id)
    assert initial["head"] == head and initial["branch"] == "work/fixture"
    assert Path(initial["worktree_path"]).is_dir()


@pytest.mark.parametrize(
    "remote",
    [
        replace(repository(), id=999),
        replace(repository(), owner_id=999),
        replace(repository(), node_id="R_changed"),
        replace(repository(), private=False),
        replace(repository(), clone_url="https://github.com/evil/source.git"),
    ],
)
def test_import_rejects_remote_identity_before_journal(
    tmp_path: Path, remote: GitHubRepository,
) -> None:
    _, local_source, head = fixture_source(tmp_path)
    ledger, registry, request_id = admitted(tmp_path)
    importer, rest, cloners = service(
        tmp_path, ledger, registry, request_id, local_source, head,
        remote=remote,
    )
    with pytest.raises(SourceError) as error:
        importer.import_source(request_id)
    assert error.value.code == "GITHUB_IMPORT_IDENTITY_CHANGED"
    assert rest.repository_reads == 1
    assert cloners == [] and effects(ledger, request_id) == []
    assert not (registry.managed_root / "fixture").exists()


def test_import_requires_exact_owner_local_approval_and_login(tmp_path: Path) -> None:
    _, local_source, head = fixture_source(tmp_path)
    ledger, registry, request_id = admitted(tmp_path)
    importer, rest, cloners = service(
        tmp_path, ledger, registry, request_id, local_source, head, approvals={},
    )
    with pytest.raises(SourceError) as error:
        importer.import_source(request_id)
    assert error.value.code == "GITHUB_IMPORT_NOT_APPROVED"
    assert rest.repository_reads == 0 and cloners == []
    assert effects(ledger, request_id) == []
    other, _, _ = service(tmp_path, ledger, registry, request_id, local_source, head)
    other.rest.expected_login = "other"
    with pytest.raises(SourceError) as error:
        other.import_source(request_id)
    assert error.value.code == "GITHUB_IMPORT_NOT_APPROVED"


def test_import_refuses_preexisting_project_path_without_adoption(tmp_path: Path) -> None:
    _, local_source, head = fixture_source(tmp_path)
    ledger, registry, request_id = admitted(tmp_path)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir(mode=0o700)
    project_slot = registry.managed_root / "fixture"
    project_slot.symlink_to(unrelated, target_is_directory=True)
    importer, rest, cloners = service(
        tmp_path, ledger, registry, request_id, local_source, head,
    )
    with pytest.raises(SourceError) as error:
        importer.import_source(request_id)
    assert error.value.code == "GITHUB_IMPORT_PATH_EXISTS"
    assert rest.repository_reads == 0 and cloners == []
    assert effects(ledger, request_id) == []
    assert project_slot.is_symlink()


def test_import_keeps_uncertain_stage_when_remote_moves(tmp_path: Path) -> None:
    _, local_source, head = fixture_source(tmp_path)
    ledger, registry, request_id = admitted(tmp_path)
    importer, rest, cloners = service(
        tmp_path, ledger, registry, request_id, local_source, head,
    )
    rest.changed_head = "a" * 40
    with pytest.raises(SourceError) as error:
        importer.import_source(request_id)
    assert error.value.code == "GITHUB_IMPORT_SOURCE_CHANGED"
    stage = registry.managed_root / "fixture" / f".github-import-{request_id}"
    assert (stage / "repo.git").is_dir()
    assert not (registry.managed_root / "fixture" / "repo.git").exists()
    assert registry.get_project("fixture") is None
    assert effects(ledger, request_id)[0]["state"] == "INTENDED"
    with pytest.raises(SourceError) as repeated:
        importer.import_source(request_id)
    assert repeated.value.code == "GITHUB_IMPORT_NEEDS_RECONCILIATION"
    assert len(cloners) == 1 and cloners[0].calls == 1


def test_import_rejects_submodule_tree_and_keeps_evidence(tmp_path: Path) -> None:
    _, local_source, head = fixture_source(tmp_path, blocked_file=".gitmodules")
    ledger, registry, request_id = admitted(tmp_path)
    importer, _, _ = service(
        tmp_path, ledger, registry, request_id, local_source, head,
    )
    with pytest.raises(GitError) as error:
        importer.import_source(request_id)
    assert error.value.code == "SUBMODULE_UNSUPPORTED"
    assert (registry.managed_root / "fixture" / f".github-import-{request_id}" / "repo.git").is_dir()
    assert effects(ledger, request_id)[0]["state"] == "INTENDED"
    assert registry.get_project("fixture") is None


def test_import_requires_drive_and_repository_locks(tmp_path: Path) -> None:
    _, local_source, head = fixture_source(tmp_path)
    ledger, registry, request_id = admitted(tmp_path, locks=False)
    importer, rest, _ = service(tmp_path, ledger, registry, request_id, local_source, head)
    with pytest.raises(SourceError) as error:
        importer.import_source(request_id)
    assert error.value.code == "SOURCE_LOCK_MISSING"
    assert rest.repository_reads == 0 and effects(ledger, request_id) == []


def test_approval_rejects_missing_numeric_identity() -> None:
    with pytest.raises(ValueError):
        replace(approval(), repository_id=0)
    with pytest.raises(ValueError):
        replace(approval(), repository="alice/other/extra")


def test_import_never_replays_a_clone_with_unknown_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, local_source, head = fixture_source(tmp_path)
    ledger, registry, request_id = admitted(tmp_path)
    importer, _, cloners = service(
        tmp_path, ledger, registry, request_id, local_source, head,
    )
    original_clone = LocalBareCloner.clone_bare

    def lose_response(self: LocalBareCloner, stage_parent: Path, destination: Path) -> None:
        original_clone(self, stage_parent, destination)
        raise GitError("GITHUB_GIT_FAILED", "Simulated lost clone result")

    monkeypatch.setattr(LocalBareCloner, "clone_bare", lose_response)
    with pytest.raises(GitError) as error:
        importer.import_source(request_id)
    assert error.value.code == "GITHUB_GIT_FAILED"
    stage = registry.managed_root / "fixture" / f".github-import-{request_id}"
    assert (stage / "repo.git").is_dir()
    assert effects(ledger, request_id)[0]["state"] == "INTENDED"
    assert registry.get_project("fixture") is None
    with pytest.raises(SourceError) as repeated:
        importer.import_source(request_id)
    assert repeated.value.code == "GITHUB_IMPORT_NEEDS_RECONCILIATION"
    assert len(cloners) == 1 and cloners[0].calls == 1
