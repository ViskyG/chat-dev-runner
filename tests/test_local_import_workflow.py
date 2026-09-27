"""Offline CREATE_PROJECT LOCAL import, including a disposable Git receiver."""
from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from runner_app.adapters.drive_registry import WorkspaceDriveRegistration
from runner_app.adapters.github import GitHubRepository, RepositoryResult
from runner_app.domain.commands import freeze
from runner_app.engine.service import Context
from runner_app.git.argv import run_git
from runner_app.git.imports import ImportGrants
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.git.service import SourceService
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger
from runner_app.workflows.create_project import (
    CreateProjectWorkflow,
    GitHubLocalImportBootstrap,
)

_INSTALLATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_OWNER = "ViskyG"
_URL = "https://github.com/ViskyG/fixture.git"
_TOKEN = b"fixture-token"
_BRANCH = "feature/imported"


def _original(tmp_path: Path) -> tuple[Path, Path, str, str, dict[str, bytes | str]]:
    approved = tmp_path / "approved"
    approved.mkdir(mode=0o700)
    original = approved / "original"
    original.mkdir(mode=0o700)
    run_git(original, ["init", "-b", "main"])
    tracked = original / "main.py"
    tracked.write_bytes(b"value = 1\n")
    run_git(original, ["add", "main.py"])
    run_git(original, ["-c", "user.name=Owner", "-c", "user.email=owner@local.invalid",
                       "commit", "-m", "First commit"])
    first = run_git(original, ["rev-parse", "HEAD"]).strip()
    run_git(original, ["checkout", "-b", _BRANCH])
    tracked.write_bytes(b"value = 2\n")
    run_git(original, ["add", "main.py"])
    run_git(original, ["-c", "user.name=Owner", "-c", "user.email=owner@local.invalid",
                       "commit", "-m", "Second commit"])
    head = run_git(original, ["rev-parse", "HEAD"]).strip()
    original_remote = tmp_path / "original-remote.git"
    run_git(tmp_path, ["clone", "--bare", "--no-local", str(original), str(original_remote)])
    run_git(original, ["remote", "add", "origin", str(original_remote)])
    # Distinguish the staged index from both the last commit and the dirty worktree.
    tracked.write_bytes(b"value = 3 staged\n")
    run_git(original, ["add", "main.py"])
    tracked.write_bytes(b"value = 4 unstaged\n")
    (original / "draft.txt").write_bytes(b"untracked owner draft\n")
    before: dict[str, bytes | str] = {
        "config": (original / ".git" / "config").read_bytes(),
        "tracked": tracked.read_bytes(),
        "draft": (original / "draft.txt").read_bytes(),
        "status": run_git(original, ["status", "--porcelain=v1", "-z"]),
        "refs": run_git(original, ["show-ref"]),
        "remote_refs": run_git(original_remote, ["show-ref"]),
        "remote_config": (original_remote / "config").read_bytes(),
    }
    # Git status refreshes stat metadata in the index; snapshot it afterwards.
    before["index"] = (original / ".git" / "index").read_bytes()
    return original, original_remote, first, head, before


def _assert_original_unchanged(original: Path, remote: Path,
                               before: dict[str, bytes | str]) -> None:
    assert (original / ".git" / "index").read_bytes() == before["index"]
    assert (original / ".git" / "config").read_bytes() == before["config"]
    assert (original / "main.py").read_bytes() == before["tracked"]
    assert (original / "draft.txt").read_bytes() == before["draft"]
    assert run_git(original, ["status", "--porcelain=v1", "-z"]) == before["status"]
    assert run_git(original, ["show-ref"]) == before["refs"]
    assert run_git(remote, ["show-ref"]) == before["remote_refs"]
    assert (remote / "config").read_bytes() == before["remote_config"]


class FakeGitHub:
    def __init__(self, *, cancel: threading.Event | None = None) -> None:
        self.cancel = cancel
        self.calls = 0
        self.repository: GitHubRepository | None = None

    def create_or_attach_private(self, owner: str, name: str, marker: str, *,
                                 expected_repo_id: int | None = None) -> RepositoryResult:
        self.calls += 1
        assert (owner, name, expected_repo_id) == (_OWNER, "fixture", None)
        assert marker.startswith("cdr:v1:")
        self.repository = GitHubRepository(
            id=421, node_id="R_421", owner=_OWNER, owner_id=99, name="fixture",
            full_name="ViskyG/fixture", html_url="https://github.com/ViskyG/fixture",
            clone_url=_URL, ssh_url="git@github.com:ViskyG/fixture.git",
            private=True, default_branch="main", description=marker,
        )
        if self.cancel is not None:
            self.cancel.set()
        return RepositoryResult(self.repository, "created")

    def get_repository(self, owner: str, name: str, *,
                       expected_repo_id: int | None = None) -> GitHubRepository:
        assert (owner, name, expected_repo_id) == (_OWNER, "fixture", 421)
        assert self.repository is not None
        return self.repository


class FakeNetwork:
    def __init__(self, receiver: Path) -> None:
        self.receiver = receiver
        self.calls: list[list[str]] = []
        self.push_count = 0

    def __call__(self, cwd: Path, operation: list[str], token: bytes, *,
                 timeout: float) -> bytes:
        assert cwd.name == "repo.git" and token == _TOKEN
        assert _URL in operation and timeout in {30, 120}
        self.calls.append(operation)
        if operation[0] == "ls-remote":
            return run_git(cwd, ["ls-remote", "--refs", str(self.receiver)]).encode()
        assert operation[0] == "push" and timeout == 120
        assert operation[-1].endswith(":refs/heads/main")
        assert all("--force" not in arg and not arg.startswith("+") for arg in operation)
        self.push_count += 1
        run_git(cwd, [*operation[:-2], str(self.receiver), operation[-1]])
        return b""


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
                resources: Mapping[str, Any] | None = None) -> str:
        self.calls += 1
        assert request_id and status == "COMPLETED" and code is None
        assert resources is not None
        assert resources["github"]["full_name"] == "ViskyG/fixture"
        assert resources["github"]["source_branch"] == _BRANCH
        assert resources["baseline"]["ref"] == "refs/heads/main"
        assert resources["baseline"]["source_ref"] == f"refs/heads/{_BRANCH}"
        assert resources["project_state"] == "READY"
        return self.receipt_id


@dataclass
class Case:
    ledger: Ledger
    registry: SourceRegistry
    workflow: CreateProjectWorkflow
    command: dict[str, Any]
    github: FakeGitHub
    network: FakeNetwork
    drive: FakeDrive
    receipt: FakeRootReceipt
    receiver: Path
    handle: str


def _setup(tmp_path: Path, original: Path, *,
           handle_override: str | None = None,
           cancel: threading.Event | None = None,
           local_bootstrap: bool = True) -> Case:
    ledger = Ledger(Database(tmp_path / "state" / "runner.sqlite"))
    ledger.startup()
    ledger.arm()
    registry = SourceRegistry(ledger.database, tmp_path / "managed")
    grants = ImportGrants(tmp_path / "state" / "import-handles",
                          roots=[original.parent])
    handle = grants.grant(str(original))
    now = datetime.now(timezone.utc)
    state = ledger.state()
    command = {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "CREATE_PROJECT", "project_id": "fixture",
        "workspace_id": None, "expected_head": None,
        "params": {
            "name": "Fixture",
            "source": {"kind": "local", "import_handle": handle_override or handle},
            "template_id": "python-basic", "github_identity": "personal",
            "visibility": "private",
        },
        "intent": "Create a project from an owner-approved local source",
    }
    assert ledger.admit(
        command, freeze(command).digest, "root-command",
        ("repo:fixture", "global:personal", "global:drive-installation"),
    ).execute
    github = FakeGitHub(cancel=cancel)
    receiver = tmp_path / "github-receiver.git"
    run_git(tmp_path, ["init", "--bare", "-b", "main", str(receiver)])
    network = FakeNetwork(receiver)
    drive = FakeDrive()
    receipt = FakeRootReceipt()

    def no_new_bootstrap(*_args: Any) -> str:
        raise AssertionError("LOCAL import must not use new-project root bootstrap")

    def populate(request_id: str, docs: WorkspaceDriveRegistration,
                 sources: SourceRegistry, repository: GitHubRepository) -> dict[str, str]:
        assert request_id and docs.project_id == "fixture"
        assert repository.full_name == "ViskyG/fixture"
        assert sources.get_workspace_profile("fixture", "primary") == {
            "template_id": "python-basic", "profile_id": "python-quick",
        }
        return {"project_state_sha256": "a" * 64, "context_sha256": "b" * 64}

    workflow = CreateProjectWorkflow(
        ledger, SourceService(ledger, registry, import_grants=grants),
        github, drive, no_new_bootstrap, populate,
        installation_id=_INSTALLATION, github_owner=_OWNER,
        github_identity="personal",
        template_profiles={"python-basic": "python-quick"},
        local_import_bootstrap=(
            GitHubLocalImportBootstrap(registry, github, lambda: _TOKEN.decode())
            if local_bootstrap else None
        ),
        root_receipt=receipt,
    )
    return Case(ledger, registry, workflow, command, github, network,
                drive, receipt, receiver, handle)


def test_dirty_local_import_creates_separate_target_and_preserves_owner_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, original_remote, first, head, before = _original(tmp_path)
    case = _setup(tmp_path, original)
    monkeypatch.setattr("runner_app.git.github_transport._run_network_git", case.network)
    result = case.workflow(freeze(case.command), Context(threading.Event(), case.ledger))
    assert result.status == "COMPLETED" and result.code is None
    assert case.github.calls == case.drive.calls == case.receipt.calls == 1
    assert case.network.push_count == 1
    assert run_git(case.receiver, ["rev-parse", "refs/heads/main"]).strip() == head
    assert run_git(case.receiver, ["rev-list", "--count", "refs/heads/main"]).strip() == "2"
    assert run_git(case.receiver, ["merge-base", "--is-ancestor", first,
                                   "refs/heads/main"]) == ""
    project = case.registry.get_project("fixture")
    assert project is not None and project["state"] == "READY"
    assert project["origin_kind"] == "local" and project["origin_reference"] == case.handle
    repo = Path(project["repo_path"])
    assert run_git(repo, ["symbolic-ref", "--quiet", "HEAD"]).strip() == f"refs/heads/{_BRANCH}"
    assert run_git(repo, ["rev-parse", "HEAD"]).strip() == head
    assert run_git(repo, ["remote", "get-url", "origin"]).strip() == _URL
    binding = case.registry.get_project_remote("fixture")
    assert binding is not None
    assert binding["origin_kind"] == "local"
    assert binding["origin_reference"] == case.handle
    assert binding["provider_id"] == 421 and binding["owner_id"] == 99
    assert binding["provision_marker"].startswith("cdr:v1:")
    assert binding["source_branch"] == _BRANCH and binding["source_head"] == head
    assert result.result["baseline"] == {
        "ref": "refs/heads/main", "commit": head, "source_ref": f"refs/heads/{_BRANCH}",
    }
    assert result.result["root_receipt_doc_id"] == case.receipt.receipt_id
    workspace = case.registry.get_workspace("fixture", "primary")
    assert workspace is not None and workspace["head"] == head
    assert workspace["branch"] == "work/fixture"
    operation = case.ledger.get(case.command["request_id"])
    assert operation is not None
    effects = operation["effects"]
    assert [item["step_id"] for item in effects] == [
        "github-repository", "local-project", "baseline-main", "initial-workspace",
    ]
    assert all(item["state"] == "CONFIRMED" for item in effects)
    _assert_original_unchanged(original, original_remote, before)
    same = case.registry.bind_local_import_project_remote(
        "fixture", origin_reference=case.handle, provider_id=421, owner_id=99,
        node_id="R_421", owner=_OWNER, name="fixture",
        html_url="https://github.com/ViskyG/fixture", clone_url=_URL,
        provision_marker=binding["provision_marker"], source_branch=_BRANCH,
        source_head=head, private=True,
    )
    assert same == binding
    with pytest.raises(SourceError) as changed:
        case.registry.bind_local_import_project_remote(
            "fixture", origin_reference=case.handle, provider_id=422, owner_id=99,
            node_id="R_421", owner=_OWNER, name="fixture",
            html_url="https://github.com/ViskyG/fixture", clone_url=_URL,
            provision_marker=binding["provision_marker"], source_branch=_BRANCH,
            source_head=head, private=True,
        )
    assert changed.value.code == "PROJECT_REMOTE_CHANGED"


def test_unknown_local_grant_blocks_before_github_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, original_remote, _, _, before = _original(tmp_path)
    case = _setup(tmp_path, original, handle_override="f" * 48)
    monkeypatch.setattr("runner_app.git.github_transport._run_network_git", case.network)
    result = case.workflow(freeze(case.command), Context(threading.Event(), case.ledger))
    assert result.status == "BLOCKED" and result.code == "IMPORT_NOT_APPROVED"
    assert case.github.calls == case.network.push_count == 0
    assert case.drive.calls == case.receipt.calls == 0
    assert case.registry.get_project("fixture") is None
    operation = case.ledger.get(case.command["request_id"])
    assert operation is not None and operation["effects"] == []
    _assert_original_unchanged(original, original_remote, before)


def test_missing_local_baseline_port_blocks_without_effects(tmp_path: Path) -> None:
    original, original_remote, _, _, before = _original(tmp_path)
    case = _setup(tmp_path, original, local_bootstrap=False)
    result = case.workflow(freeze(case.command), Context(threading.Event(), case.ledger))
    assert result.status == "BLOCKED" and result.code == "LOCAL_IMPORT_NOT_CONFIGURED"
    assert case.github.calls == case.network.push_count == 0
    assert case.registry.get_project("fixture") is None
    operation = case.ledger.get(case.command["request_id"])
    assert operation is not None and operation["effects"] == []
    _assert_original_unchanged(original, original_remote, before)


def test_cancel_after_target_creation_retains_confirmed_effect_without_import(
    tmp_path: Path,
) -> None:
    original, original_remote, _, _, before = _original(tmp_path)
    cancel = threading.Event()
    case = _setup(tmp_path, original, cancel=cancel)
    result = case.workflow(freeze(case.command), Context(cancel, case.ledger))
    assert result.status == "CANCELLED" and result.code == "CANCEL_REQUESTED"
    assert case.github.calls == 1
    assert case.network.push_count == case.drive.calls == case.receipt.calls == 0
    assert case.registry.get_project("fixture") is None
    operation = case.ledger.get(case.command["request_id"])
    assert operation is not None
    effects = operation["effects"]
    assert len(effects) == 1
    assert effects[0]["step_id"] == "github-repository"
    assert effects[0]["state"] == "CONFIRMED" and effects[0]["provider_id"] == "421"
    _assert_original_unchanged(original, original_remote, before)
