"""Offline CREATE_PROJECT path through real Ledger and local Git source."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from runner_app.adapters.drive_docs import DriveError
from runner_app.adapters.drive_registry import WorkspaceDriveRegistration
from runner_app.adapters.drive_root_receipt import RootReceiptError
from runner_app.adapters.github import GitHubError, GitHubRepository, RepositoryResult
from runner_app.domain.commands import freeze, validate_shape
from runner_app.engine.service import Context
from runner_app.git.argv import run_git
from runner_app.git.registry import SourceRegistry
from runner_app.git.service import SourceService
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger
from runner_app.workflows.create_project import CreateProjectWorkflow

INSTALLATION = "37c39a84-3856-4da2-8d4f-d09416e2ea5e"


def _command(ledger: Ledger) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    state = ledger.state()
    command = {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "CREATE_PROJECT", "project_id": "notes-lab",
        "workspace_id": None, "expected_head": None,
        "params": {
            "name": "Notes Lab", "source": {"kind": "new"},
            "template_id": "python-basic", "github_identity": "owner",
            "visibility": "private",
        },
        "intent": "Create a managed notes project",
    }
    validate_shape(command)
    return command


class FakeGitHub:
    def __init__(self, cancel: threading.Event | None = None,
                 *, fail_unknown: bool = False, owner: str = "owner") -> None:
        self.calls = 0
        self.cancel = cancel
        self.fail_unknown = fail_unknown
        self.owner = owner

    def create_or_attach_private(self, owner: str, name: str, marker: str, *,
                                 expected_repo_id: int | None = None) -> RepositoryResult:
        self.calls += 1
        assert owner == self.owner and name == "notes-lab"
        assert marker.startswith("cdr:v1:") and expected_repo_id is None
        if self.fail_unknown:
            raise GitHubError("REPOSITORY_CREATE_UNKNOWN", "Provider response was lost")
        if self.cancel is not None:
            self.cancel.set()
        return RepositoryResult(GitHubRepository(
            id=42, node_id="R_42", owner=owner, owner_id=9, name=name,
            full_name=f"{owner}/{name}",
            html_url=f"https://github.com/{owner}/{name}",
            clone_url=f"https://github.com/{owner}/{name}.git",
            ssh_url=f"git@github.com:{owner}/{name}.git",
            private=True, default_branch=None, description=marker,
        ), "created")


class FakeDrive:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def provision(self, request_id: str, project_id: str, workspace_id: str,
                  project_name: str) -> WorkspaceDriveRegistration:
        self.calls += 1
        assert project_id == "notes-lab" and workspace_id == "primary"
        assert project_name == "Notes Lab"
        if self.fail:
            raise DriveError("DRIVE_QUOTA_EXCEEDED", "No Drive space")
        return WorkspaceDriveRegistration(
            INSTALLATION, project_id, workspace_id,
            "projectFolder123", "workspaceFolder123",
            "commandDocument123", "changesetDocument123",
            "receiptDocument123", "latestResultDocument123",
            "projectStateDocument123", "sourceContextDocument123",
            "projectCommandDocument123", "projectReceiptDocument123",
        )



class FakeRootReceipt:
    command_id = "root-command"
    receipt_id = "root-receipt-document"

    def __init__(self, *, failure: str | None = None,
                 response: str | None = receipt_id) -> None:
        self.calls = 0
        self.failure = failure
        self.response = response

    def publish(self, request_id: str, *, status: str = "COMPLETED",
                code: str | None = None,
                resources: dict[str, Any] | None = None) -> str | None:
        self.calls += 1
        assert request_id and status == "COMPLETED" and code is None
        assert resources is not None and resources["project_state"] == "READY"
        assert resources["drive"]["project_command_doc_id"] == "projectCommandDocument123"
        if self.failure is not None:
            raise RootReceiptError(self.failure)
        return self.response

def _setup(tmp_path: Path, *, cancel: threading.Event | None = None,
           github_unknown: bool = False, drive_fail: bool = False,
           drive_source: bool = False, root_receipt: FakeRootReceipt | None = None,
           root_command_id: str | None = None,
           github_owner: str = "owner", github_identity: str | None = None
           ) -> tuple[Ledger, SourceRegistry, CreateProjectWorkflow, dict[str, Any],
                      FakeGitHub, FakeDrive, threading.Event, list[str]]:
    ledger = Ledger(Database(tmp_path / "state" / "runner.sqlite"))
    ledger.startup()
    ledger.arm()
    registry = SourceRegistry(ledger.database, tmp_path / "managed")
    source = SourceService(ledger, registry)
    command = _command(ledger)
    identity_alias = github_identity or github_owner
    command["params"]["github_identity"] = identity_alias
    validate_shape(command)
    scopes = ("repo:notes-lab", f"global:{identity_alias}", "global:drive-installation")
    source_id = FakeRootReceipt.command_id if drive_source else "owner-session"
    assert ledger.admit(command, freeze(command).digest, source_id, scopes).execute
    event = cancel or threading.Event()
    github = FakeGitHub(event if cancel is not None else None,
                        fail_unknown=github_unknown, owner=github_owner)
    drive = FakeDrive(fail=drive_fail)
    pushed: list[str] = []

    def bootstrap(project_id: str, expected: str, repository: GitHubRepository) -> str:
        assert project_id == "notes-lab" and repository.id == 42
        assert registry.get_project(project_id) is not None
        pushed.append(expected)
        return expected

    def populate(request_id: str, docs: WorkspaceDriveRegistration,
                 sources: SourceRegistry, repository: GitHubRepository) -> dict[str, str]:
        assert request_id == command["request_id"]
        assert docs.project_state_doc_id == "projectStateDocument123"
        assert docs.context_doc_id == "sourceContextDocument123"
        assert sources.get_workspace_profile("notes-lab", "primary") == {
            "template_id": "python-basic", "profile_id": "python-quick",
        }
        assert repository.id == 42
        return {"project_state_sha256": "a" * 64, "context_sha256": "b" * 64}

    workflow = CreateProjectWorkflow(
        ledger, source, github, drive, bootstrap, populate,
        installation_id=INSTALLATION, github_owner=github_owner,
        github_identity=identity_alias,
        template_profiles={"python-basic": "python-quick"},
        root_receipt=root_receipt, root_command_id=root_command_id,
    )
    return ledger, registry, workflow, command, github, drive, event, pushed


def test_new_project_links_private_repo_local_source_workspace_and_drive(tmp_path: Path) -> None:
    ledger, registry, workflow, command, github, drive, event, pushed = _setup(tmp_path)
    result = workflow(freeze(command), Context(event, ledger))
    assert result.status == "COMPLETED" and result.code is None
    assert github.calls == drive.calls == 1
    assert result.result["github"]["repository_id"] == 42
    assert result.result["github"]["private"] is True
    remote_binding = registry.get_project_remote("notes-lab")
    assert remote_binding is not None and remote_binding["provider_id"] == 42
    assert remote_binding["private"] is True
    assert result.result["github_for_chat"] == "MANUAL_REQUIRED"
    assert result.result["profile_id"] == "python-quick"
    assert result.result["project_state"] == "READY"
    project = registry.get_project("notes-lab")
    assert project is not None and project["state"] == "READY"
    workspace = registry.get_workspace("notes-lab", "primary")
    assert workspace is not None
    assert workspace["branch"] == "work/notes-lab"
    assert workspace["head"] == pushed[0]
    with registry.database.connect() as connection:
        import json
        binding = json.loads(connection.execute(
            "SELECT metadata_json FROM workspaces WHERE project_id=? AND workspace_id=?",
            ("notes-lab", "primary"),
        ).fetchone()[0])
    assert binding["template_id"] == "python-basic"
    assert binding["profile_id"] == "python-quick"
    assert run_git(Path(workspace["worktree_path"]), ["status", "--porcelain=v1", "-z"]) == ""
    operation = ledger.get(command["request_id"])
    assert operation is not None
    effects = operation["effects"]
    assert [(item["step_id"], item["state"]) for item in effects] == [
        ("github-repository", "CONFIRMED"), ("local-project", "CONFIRMED"),
        ("baseline-main", "CONFIRMED"), ("initial-workspace", "CONFIRMED"),
    ]
    ledger.finish(command["request_id"], result.status, code=result.code,
                  result=result.result)
    stored = ledger.get(command["request_id"])
    assert stored is not None and stored["status"] == "COMPLETED"




def test_local_alias_can_differ_from_mixed_case_github_owner(tmp_path: Path) -> None:
    ledger, registry, workflow, command, github, _, event, _ = _setup(
        tmp_path, github_owner="ViskyG", github_identity="personal",
    )
    result = workflow(freeze(command), Context(event, ledger))
    assert result.status == "COMPLETED" and result.code is None
    assert github.calls == 1
    assert result.result["github"]["owner"] == "ViskyG"
    assert result.result["github"]["html_url"] == "https://github.com/ViskyG/notes-lab"
    assert ledger.holds_lock(command["request_id"], "global:personal")
    with ledger.database.connect() as connection:
        scopes = {
            row[0] for row in connection.execute(
                "SELECT scope FROM locks WHERE request_id=?", (command["request_id"],),
            )
        }
    assert "global:ViskyG" not in scopes
    binding = registry.get_project_remote("notes-lab")
    assert binding is not None and binding["owner"] == "ViskyG"

def test_drive_origin_requires_confirmed_root_receipt_before_completed(
    tmp_path: Path,
) -> None:
    receipt = FakeRootReceipt()
    ledger, registry, workflow, command, _, drive, event, _ = _setup(
        tmp_path, drive_source=True, root_receipt=receipt,
    )
    result = workflow(freeze(command), Context(event, ledger))
    assert result.status == "COMPLETED" and result.code is None
    assert result.result["root_receipt"] == "CONFIRMED"
    assert result.result["root_receipt_doc_id"] == receipt.receipt_id
    assert receipt.calls == drive.calls == 1
    assert registry.get_project("notes-lab")["state"] == "READY"


def test_drive_root_receipt_uncertain_returns_truthful_partial_result(
    tmp_path: Path,
) -> None:
    receipt = FakeRootReceipt(failure="ROOT_RECEIPT_WRITE_UNCERTAIN")
    ledger, registry, workflow, command, _, drive, event, _ = _setup(
        tmp_path, drive_source=True, root_receipt=receipt,
    )
    result = workflow(freeze(command), Context(event, ledger))
    assert result.status == "BLOCKED"
    assert result.code == "ROOT_RECEIPT_WRITE_UNCERTAIN"
    assert result.result["project_state"] == "READY"
    assert result.result["root_receipt"] == "UNCONFIRMED"
    assert "root_receipt_doc_id" not in result.result
    assert registry.get_project("notes-lab")["state"] == "READY"
    assert receipt.calls == drive.calls == 1


def test_drive_unconfirmed_root_receipt_id_blocks_completion(tmp_path: Path) -> None:
    receipt = FakeRootReceipt(response=None)
    ledger, _, workflow, command, _, _, event, _ = _setup(
        tmp_path, drive_source=True, root_receipt=receipt,
    )
    result = workflow(freeze(command), Context(event, ledger))
    assert result.status == "BLOCKED" and result.code == "ROOT_RECEIPT_NOT_CONFIRMED"
    assert result.result["root_receipt"] == "UNCONFIRMED"
    assert receipt.calls == 1



def test_unexpected_receipt_port_failure_reports_fault_with_partial_state(
    tmp_path: Path,
) -> None:
    receipt = FakeRootReceipt()
    def fail_publish(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("sensitive provider payload")
    receipt.publish = fail_publish  # type: ignore[method-assign]
    ledger, registry, workflow, command, _, _, event, _ = _setup(
        tmp_path, drive_source=True, root_receipt=receipt,
    )
    result = workflow(freeze(command), Context(event, ledger))
    assert result.status == "FAULT" and result.code == "ROOT_RECEIPT_FAILED"
    assert result.result["project_state"] == "READY"
    assert result.result["root_receipt"] == "UNCONFIRMED"
    assert "sensitive" not in str(result.result)
    assert registry.get_project("notes-lab")["state"] == "READY"

def test_drive_source_without_configured_receipt_blocks_before_effects(
    tmp_path: Path,
) -> None:
    ledger, registry, workflow, command, github, drive, event, _ = _setup(
        tmp_path, drive_source=True, root_command_id=FakeRootReceipt.command_id,
    )
    result = workflow(freeze(command), Context(event, ledger))
    assert result.status == "BLOCKED" and result.code == "ROOT_RECEIPT_NOT_CONFIGURED"
    assert github.calls == drive.calls == 0
    assert registry.get_project("notes-lab") is None
    assert ledger.get(command["request_id"])["effects"] == []


def test_local_ui_skips_configured_root_receipt_port(tmp_path: Path) -> None:
    receipt = FakeRootReceipt()
    ledger, _, workflow, command, _, _, event, _ = _setup(
        tmp_path, root_receipt=receipt,
    )
    result = workflow(freeze(command), Context(event, ledger))
    assert result.status == "COMPLETED"
    assert receipt.calls == 0
    assert "root_receipt" not in result.result

def test_cancel_after_repo_creation_preserves_confirmed_identity(tmp_path: Path) -> None:
    event = threading.Event()
    ledger, registry, workflow, command, _, drive, _, _ = _setup(tmp_path, cancel=event)
    result = workflow(freeze(command), Context(event, ledger))
    assert result.status == "CANCELLED"
    assert result.result["github"]["repository_id"] == 42
    assert registry.get_project("notes-lab") is None and drive.calls == 0
    ledger.finish(command["request_id"], result.status, code=result.code,
                  result=result.result)
    stored = ledger.get(command["request_id"])
    assert stored is not None
    effect = stored["effects"][0]
    assert effect["provider_id"] == "42" and effect["state"] == "CONFIRMED"


def test_lost_repo_creation_response_retains_unknown_effect_and_lock(tmp_path: Path) -> None:
    ledger, registry, workflow, command, _, drive, event, _ = _setup(
        tmp_path, github_unknown=True,
    )
    result = workflow(freeze(command), Context(event, ledger))
    assert result.status == "BLOCKED" and result.code == "REPOSITORY_CREATE_UNKNOWN"
    assert registry.get_project("notes-lab") is None and drive.calls == 0
    ledger.finish(command["request_id"], result.status, code=result.code,
                  result=result.result)
    stored = ledger.get(command["request_id"])
    assert stored is not None
    assert stored["code"] == "NEEDS_RECONCILIATION"
    assert stored["reconcile_required"] == 1
    assert stored["effects"][0]["state"] == "UNKNOWN"


def test_drive_failure_keeps_confirmed_repo_baseline_and_local_worktree(tmp_path: Path) -> None:
    ledger, registry, workflow, command, _, drive, event, pushed = _setup(
        tmp_path, drive_fail=True,
    )
    result = workflow(freeze(command), Context(event, ledger))
    assert result.status == "BLOCKED" and result.code == "DRIVE_QUOTA_EXCEEDED"
    assert drive.calls == 1 and len(pushed) == 1
    assert registry.get_workspace("notes-lab", "primary") is not None
    project = registry.get_project("notes-lab")
    assert project is not None and project["state"] == "LOCAL_READY"
    ledger.finish(command["request_id"], result.status, code=result.code,
                  result=result.result)
    stored = ledger.get(command["request_id"])
    assert stored is not None
    assert stored["reconcile_required"] == 0
    assert all(item["state"] == "CONFIRMED" for item in stored["effects"])
    assert stored["result"]["github"]["repository_id"] == 42



def test_new_project_baseline_is_pushed_to_real_disposable_bare_remote(
    tmp_path: Path,
) -> None:
    from runner_app.git.publish import bootstrap_new_project_main

    ledger, registry, _, command, github, drive, event, _ = _setup(tmp_path)
    remote = tmp_path / "approved-remote.git"
    run_git(tmp_path, ["init", "--bare", "-b", "main", str(remote)])

    def bootstrap(project_id: str, expected: str, repository: GitHubRepository) -> str:
        assert repository.id == 42
        managed = registry.get_project(project_id)
        assert managed is not None
        repo = Path(managed["repo_path"])
        assert run_git(repo, ["remote", "get-url", "origin"]).strip() == repository.clone_url
        run_git(repo, ["remote", "add", "offline", str(remote)])
        outcome = bootstrap_new_project_main(
            registry, project_id=project_id, remote_name="offline",
            remote_url=remote, expected_initial_commit=expected,
        )
        assert outcome.status == "BOOTSTRAPPED"
        return outcome.commit

    workflow = CreateProjectWorkflow(
        ledger, SourceService(ledger, registry), github, drive, bootstrap,
        lambda _request, _docs, _sources, _repo: {
            "project_state_sha256": "a" * 64, "context_sha256": "b" * 64,
        },
        installation_id=INSTALLATION, github_owner="owner",
        template_profiles={"python-basic": "python-quick"},
    )
    result = workflow(freeze(command), Context(event, ledger))
    assert result.status == "COMPLETED", result.code
    assert run_git(remote, ["rev-parse", "refs/heads/main"]).strip() == (
        result.result["local"]["baseline_commit"]
    )
    ledger.finish(command["request_id"], result.status, code=result.code,
                  result=result.result)


def test_local_and_drive_root_admission_produce_same_resource_shape(tmp_path: Path) -> None:
    import json
    import time

    from runner_app.domain.commands import LocalPolicy, Provenance
    from runner_app.engine.service import Engine

    observed: list[dict[str, Any]] = []
    for origin in ("local", "drive"):
        root = tmp_path / origin
        ledger = Ledger(Database(root / "state" / "runner.sqlite"))
        ledger.startup()
        ledger.arm()
        registry = SourceRegistry(ledger.database, root / "managed")
        receipt = FakeRootReceipt()
        workflow = CreateProjectWorkflow(
            ledger, SourceService(ledger, registry), FakeGitHub(), FakeDrive(),
            lambda _project, commit, _repository: commit,
            lambda _request, _docs, _sources, _repo: {
                "project_state_sha256": "a" * 64, "context_sha256": "b" * 64,
            },
            installation_id=INSTALLATION, github_owner="owner",
            template_profiles={"python-basic": "python-quick"},
            root_receipt=receipt,
        )
        mailbox = Provenance("drive", "root-command")
        policy = LocalPolicy(
            mailboxes={"root-command": mailbox},
            approved_templates=frozenset({"python-basic"}),
            github_identities=frozenset({"owner"}),
        )
        engine = Engine(ledger, policy=policy, handlers={"CREATE_PROJECT": workflow})
        command = _command(ledger)
        provenance = (Provenance("local", "local-owner") if origin == "local"
                      else mailbox)
        accepted = engine.submit(json.dumps(command).encode(), provenance)
        assert accepted.execute
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            operation = ledger.get(command["request_id"])
            assert operation is not None
            if operation["status"] != "RUNNING":
                break
            time.sleep(0.01)
        assert operation is not None
        assert operation["status"] == "COMPLETED", operation["code"]
        result = operation["result"]
        assert result is not None
        assert receipt.calls == (1 if origin == "drive" else 0)
        assert ("root_receipt_doc_id" in result) is (origin == "drive")
        observed.append({
            "github": result["github"], "drive": result["drive"],
            "template_id": result["template_id"],
            "profile_id": result["profile_id"],
            "workspace_branch": result["workspace"]["branch"],
            "effects": [item["step_id"] for item in operation["effects"]],
        })
        engine.shutdown()
    assert observed[0] == observed[1]


def test_cancel_after_baseline_push_preserves_remote_commit_in_partial_result(
    tmp_path: Path,
) -> None:
    ledger, registry, _, command, github, drive, event, _ = _setup(tmp_path)

    def bootstrap(_project: str, commit: str, _repository: GitHubRepository) -> str:
        event.set()
        return commit

    workflow = CreateProjectWorkflow(
        ledger, SourceService(ledger, registry), github, drive, bootstrap,
        lambda _request, _docs, _sources, _repo: {
            "project_state_sha256": "a" * 64, "context_sha256": "b" * 64,
        },
        installation_id=INSTALLATION, github_owner="owner",
        template_profiles={"python-basic": "python-quick"},
    )
    result = workflow(freeze(command), Context(event, ledger))
    assert result.status == "CANCELLED" and result.code == "CANCEL_REQUESTED"
    assert result.result["baseline"]["commit"] == (
        result.result["local"]["baseline_commit"]
    )
    assert drive.calls == 0
    ledger.finish(command["request_id"], result.status, code=result.code,
                  result=result.result)
    stored = ledger.get(command["request_id"])
    assert stored is not None
    assert all(effect["state"] == "CONFIRMED" for effect in stored["effects"])
    assert stored["result"]["baseline"]["commit"] == result.result["baseline"]["commit"]


def test_project_and_profile_bindings_are_immutable_across_source_transition(
    tmp_path: Path,
) -> None:
    import pytest

    from runner_app.git.registry import SourceError

    ledger, registry, workflow, command, _, _, event, _ = _setup(tmp_path)
    result = workflow(freeze(command), Context(event, ledger))
    assert result.status == "COMPLETED"
    remote = registry.get_project_remote("notes-lab")
    assert remote is not None
    assert registry.bind_project_remote(
        "notes-lab", provider_id=remote["provider_id"], node_id=remote["node_id"],
        owner=remote["owner"], name=remote["name"],
        html_url=remote["html_url"], clone_url=remote["clone_url"],
        provision_marker=remote["provision_marker"],
    ) == remote
    with pytest.raises(SourceError) as wrong_remote:
        registry.bind_project_remote(
            "notes-lab", provider_id=43, node_id=remote["node_id"],
            owner=remote["owner"], name=remote["name"],
            html_url=remote["html_url"], clone_url=remote["clone_url"],
            provision_marker=remote["provision_marker"],
        )
    assert wrong_remote.value.code == "PROJECT_REMOTE_CHANGED"
    assert registry.bind_workspace_profile(
        "notes-lab", "primary", template_id="python-basic", profile_id="python-quick",
    ) == {"template_id": "python-basic", "profile_id": "python-quick"}
    with pytest.raises(SourceError) as wrong_profile:
        registry.bind_workspace_profile(
            "notes-lab", "primary", template_id="python-basic", profile_id="host-shell",
        )
    assert wrong_profile.value.code == "PROFILE_BINDING_CHANGED"
    workspace = registry.get_workspace("notes-lab", "primary")
    assert workspace is not None
    registry.transition_workspace(
        "notes-lab", "primary", expected_generation=0,
        expected_head=workspace["head"], branch=workspace["branch"],
        head=workspace["head"],
    )
    assert registry.get_workspace_profile("notes-lab", "primary") == {
        "template_id": "python-basic", "profile_id": "python-quick",
    }
