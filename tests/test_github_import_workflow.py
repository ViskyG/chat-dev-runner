"""Offline CREATE_PROJECT import through disposable Git and fake provider ports."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from runner_app.adapters.drive_registry import WorkspaceDriveRegistration
from runner_app.adapters.github import GitHubBranch, GitHubRepository
from runner_app.domain.commands import freeze
from runner_app.engine.service import Context
from runner_app.git.argv import run_git
from runner_app.git.github_import import ApprovedGitHubImport, GitHubImportService
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.git.service import SourceService
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger
from runner_app.workflows.create_project import CreateProjectWorkflow

_INSTALLATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_URL = "https://github.com/alice/source.git"
_HEAD_BRANCH = "trunk"


def _repository() -> GitHubRepository:
    return GitHubRepository(
        id=421, node_id="R_421", owner="alice", owner_id=99, name="source",
        full_name="alice/source", html_url="https://github.com/alice/source",
        clone_url=_URL, ssh_url="git@github.com:alice/source.git",
        private=True, default_branch=_HEAD_BRANCH, description=None,
    )


def _original(tmp_path: Path) -> tuple[Path, Path, str]:
    original = tmp_path / "original"
    original.mkdir(mode=0o700)
    run_git(original, ["init", "-b", _HEAD_BRANCH])
    (original / "main.py").write_text("value = 1\n")
    run_git(original, ["add", "."])
    run_git(original, [
        "-c", "user.name=Owner", "-c", "user.email=owner@local.invalid",
        "commit", "-m", "Source",
    ])
    (original / "dirty.txt").write_text("keep me unchanged\n")
    head = run_git(original, ["rev-parse", "HEAD"]).strip()
    remote = tmp_path / "original-bare.git"
    run_git(tmp_path, ["clone", "--bare", "--no-local", str(original), str(remote)])
    return original, remote, head


class FakeREST:
    expected_login = "runner-owner"

    def __init__(self, head: str) -> None:
        self.head = head
        self.moved = False
        self.repository_reads = 0
        self.branch_reads = 0

    def get_repository(self, owner: str, name: str, *,
                       expected_repo_id: int | None = None) -> GitHubRepository:
        self.repository_reads += 1
        assert (owner, name, expected_repo_id) == ("alice", "source", 421)
        return _repository()

    def branch_head(self, repository: GitHubRepository,
                    branch: str) -> GitHubBranch:
        self.branch_reads += 1
        assert repository.full_name == "alice/source" and branch == _HEAD_BRANCH
        head = "b" * 40 if self.moved and self.branch_reads == 2 else self.head
        return GitHubBranch(_HEAD_BRANCH, head, False)


class LocalCloner:
    def __init__(self, source: Path, repository: GitHubRepository) -> None:
        self.source = source
        self.repository = repository
        self.calls = 0

    def clone_bare(self, stage_parent: Path, destination: Path) -> None:
        self.calls += 1
        run_git(stage_parent, [
            "clone", "--bare", "--no-local", "--no-hardlinks", "--no-tags",
            str(self.source), str(destination),
        ])
        run_git(destination, ["remote", "set-url", "origin", self.repository.clone_url])


class NeverCreateGitHub:
    def __init__(self) -> None:
        self.calls = 0

    def create_or_attach_private(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("Import must not create a GitHub repository")


class FakeDrive:
    def __init__(self) -> None:
        self.calls = 0

    def provision(self, request_id: str, project_id: str, workspace_id: str,
                  project_name: str) -> WorkspaceDriveRegistration:
        self.calls += 1
        assert request_id and (project_id, workspace_id, project_name) == (
            "fixture", "primary", "Fixture",
        )
        return WorkspaceDriveRegistration(
            _INSTALLATION, project_id, workspace_id,
            "projectFolder123", "workspaceFolder123",
            "commandDocument123", "changesetDocument123",
            "receiptDocument123", "latestResultDocument123",
            "projectStateDocument123", "sourceContextDocument123",
            "projectCommandDocument123", "projectReceiptDocument123",
        )


class FakeRootReceipt:
    command_id = "root-command"
    receipt_id = "root-receipt-document"

    def __init__(self) -> None:
        self.calls = 0

    def publish(self, request_id: str, *, status: str = "COMPLETED",
                code: str | None = None,
                resources: dict[str, Any] | None = None) -> str:
        self.calls += 1
        assert request_id and status == "COMPLETED" and code is None
        assert resources is not None
        assert resources["github"]["full_name"] == "alice/source"
        assert resources["baseline"]["ref"] == "refs/heads/trunk"
        assert resources["project_state"] == "READY"
        return self.receipt_id


def _setup(tmp_path: Path, remote: Path, head: str, *,
           approved: bool = True, drive_source: bool = True
           ) -> tuple[
               Ledger, SourceRegistry, CreateProjectWorkflow, dict[str, Any],
               FakeREST, list[LocalCloner], NeverCreateGitHub, FakeDrive,
               FakeRootReceipt, list[str],
           ]:
    ledger = Ledger(Database(tmp_path / "state" / "runner.sqlite"))
    ledger.startup()
    ledger.arm()
    registry = SourceRegistry(ledger.database, tmp_path / "managed")
    state = ledger.state()
    now = datetime.now(timezone.utc)
    command = {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "CREATE_PROJECT", "project_id": "fixture",
        "workspace_id": None, "expected_head": None,
        "params": {
            "name": "Fixture", "source": {"kind": "github", "repository": "alice/source"},
            "template_id": "python-basic", "github_identity": "personal",
            "visibility": "private",
        },
        "intent": "Import an approved existing repository",
    }
    source_id = "root-command" if drive_source else "local-owner"
    assert ledger.admit(
        command, freeze(command).digest, source_id,
        ("repo:fixture", "global:personal", "global:drive-installation"),
    ).execute
    rest = FakeREST(head)
    cloners: list[LocalCloner] = []

    def make_cloner(repository: GitHubRepository) -> LocalCloner:
        cloner = LocalCloner(remote, repository)
        cloners.append(cloner)
        return cloner

    approvals = (
        {"alice/source": ApprovedGitHubImport(
            "alice/source", "personal", "runner-owner", 421, 99, "R_421",
        )} if approved else {}
    )
    importer = GitHubImportService(ledger, registry, rest, make_cloner, approvals)
    github = NeverCreateGitHub()
    drive = FakeDrive()
    receipt = FakeRootReceipt()
    bootstrap_calls: list[str] = []
    populate_calls: list[str] = []

    def bootstrap(*_args: Any) -> str:
        bootstrap_calls.append("unexpected")
        raise AssertionError("Import must not push or bootstrap a GitHub repository")

    def populate(request_id: str, docs: WorkspaceDriveRegistration,
                 sources: SourceRegistry, repository: GitHubRepository) -> dict[str, str]:
        populate_calls.append(request_id)
        assert docs.project_id == "fixture" and repository.full_name == "alice/source"
        assert sources.get_workspace_profile("fixture", "primary") == {
            "template_id": "python-basic", "profile_id": "python-quick",
        }
        return {"project_state_sha256": "a" * 64, "context_sha256": "b" * 64}

    workflow = CreateProjectWorkflow(
        ledger, SourceService(ledger, registry), github, drive,
        bootstrap, populate, installation_id=_INSTALLATION,
        github_owner="ViskyG", github_identity="personal",
        template_profiles={"python-basic": "python-quick"},
        github_import=importer, root_receipt=receipt,
    )
    return (ledger, registry, workflow, command, rest, cloners,
            github, drive, receipt, bootstrap_calls)


def test_existing_private_github_import_completes_without_create_or_push(
    tmp_path: Path,
) -> None:
    original, remote, head = _original(tmp_path)
    original_status = run_git(original, ["status", "--porcelain=v1", "-z"])
    remote_refs = run_git(remote, ["show-ref"])
    remote_config = (remote / "config").read_bytes()
    (ledger, registry, workflow, command, rest, cloners, github, drive,
     receipt, bootstrap_calls) = _setup(tmp_path, remote, head)
    result = workflow(freeze(command), Context(threading.Event(), ledger))
    assert result.status == "COMPLETED" and result.code is None
    assert result.result["github"]["disposition"] == "imported"
    assert result.result["github"]["full_name"] == "alice/source"
    assert result.result["baseline"] == {"ref": "refs/heads/trunk", "commit": head}
    assert result.result["root_receipt_doc_id"] == receipt.receipt_id
    assert github.calls == 0 and bootstrap_calls == []
    assert len(cloners) == 1 and cloners[0].calls == 1
    assert rest.repository_reads == rest.branch_reads == 2
    assert drive.calls == receipt.calls == 1
    project = registry.get_project("fixture")
    assert project is not None and project["state"] == "READY"
    assert project["origin_kind"] == "github"
    assert project["origin_reference"] == "alice/source"
    repo = Path(project["repo_path"])
    assert run_git(repo, ["symbolic-ref", "--quiet", "HEAD"]).strip() == "refs/heads/trunk"
    assert run_git(repo, ["rev-parse", "refs/heads/trunk"]).strip() == head
    assert run_git(repo, ["remote", "get-url", "origin"]).strip() == _URL
    workspace = registry.get_workspace("fixture", "primary")
    assert workspace is not None and workspace["head"] == head
    assert workspace["branch"] == "work/fixture"
    binding = registry.get_project_remote("fixture")
    assert binding is not None
    assert binding["origin_reference"] == "alice/source"
    assert binding["provider_id"] == 421 and binding["owner_id"] == 99
    assert binding["default_branch"] == "trunk" and binding["default_head"] == head
    effects = ledger.get(command["request_id"])["effects"]
    assert [item["step_id"] for item in effects] == [
        "local-project", "initial-workspace",
    ]
    assert all(item["state"] == "CONFIRMED" for item in effects)
    assert run_git(original, ["status", "--porcelain=v1", "-z"]) == original_status
    assert (original / "dirty.txt").read_text() == "keep me unchanged\n"
    assert run_git(remote, ["show-ref"]) == remote_refs
    assert (remote / "config").read_bytes() == remote_config


def test_unapproved_import_fails_before_clone_or_drive(tmp_path: Path) -> None:
    _, remote, head = _original(tmp_path)
    (ledger, registry, workflow, command, rest, cloners, github, drive,
     receipt, bootstrap_calls) = _setup(tmp_path, remote, head, approved=False)
    result = workflow(freeze(command), Context(threading.Event(), ledger))
    assert result.status == "BLOCKED" and result.code == "GITHUB_IMPORT_NOT_APPROVED"
    assert registry.get_project("fixture") is None
    assert rest.repository_reads == 0 and cloners == []
    assert github.calls == drive.calls == receipt.calls == 0
    assert bootstrap_calls == []


def test_remote_move_during_clone_keeps_uncertain_stage_without_followup_writes(
    tmp_path: Path,
) -> None:
    _, remote, head = _original(tmp_path)
    (ledger, registry, workflow, command, rest, cloners, github, drive,
     receipt, bootstrap_calls) = _setup(tmp_path, remote, head)
    rest.moved = True
    result = workflow(freeze(command), Context(threading.Event(), ledger))
    assert result.status == "BLOCKED" and result.code == "GITHUB_IMPORT_SOURCE_CHANGED"
    assert registry.get_project("fixture") is None
    assert len(cloners) == 1 and cloners[0].calls == 1
    assert github.calls == drive.calls == receipt.calls == 0
    assert bootstrap_calls == []
    operation = ledger.get(command["request_id"])
    assert operation is not None
    assert operation["effects"][0]["state"] == "INTENDED"
    assert (registry.managed_root / "fixture"
            / f".github-import-{command['request_id']}" / "repo.git").is_dir()
    ledger.finish(command["request_id"], result.status, code=result.code,
                  result=result.result)
    stored = ledger.get(command["request_id"])
    assert stored is not None and stored["reconcile_required"] == 1


def test_imported_remote_binding_is_immutable_and_cannot_use_creation_marker(
    tmp_path: Path,
) -> None:
    _, remote, head = _original(tmp_path)
    ledger, registry, workflow, command, *_ = _setup(tmp_path, remote, head)
    result = workflow(freeze(command), Context(threading.Event(), ledger))
    assert result.status == "COMPLETED"
    binding = registry.get_project_remote("fixture")
    assert binding is not None
    arguments = {
        "origin_reference": "alice/source", "provider_id": 421, "owner_id": 99,
        "node_id": "R_421", "owner": "alice", "name": "source",
        "html_url": "https://github.com/alice/source", "clone_url": _URL,
        "default_branch": "trunk", "default_head": head, "private": True,
    }
    assert registry.bind_imported_project_remote("fixture", **arguments) == binding
    with pytest.raises(SourceError) as changed:
        registry.bind_imported_project_remote(
            "fixture", **{**arguments, "provider_id": 999},
        )
    assert changed.value.code == "PROJECT_REMOTE_CHANGED"
    with pytest.raises(SourceError) as wrong_kind:
        registry.bind_project_remote(
            "fixture", provider_id=421, node_id="R_421", owner="alice",
            name="fixture", html_url="https://github.com/alice/fixture",
            clone_url="https://github.com/alice/fixture.git",
            provision_marker="cdr:v1:" + "a" * 64,
        )
    assert wrong_kind.value.code == "PROJECT_NOT_REGISTERED"
