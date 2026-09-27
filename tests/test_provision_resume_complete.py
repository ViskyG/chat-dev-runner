"""No-write recovery of a complete interrupted create with real managed Git."""
from __future__ import annotations

import json
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from runner_app.adapters.drive_registry import WorkspaceDriveRegistration
from runner_app.adapters.github import (
    GitHubBranch,
    GitHubRepository,
    make_provision_marker,
)
from runner_app.domain.commands import LocalPolicy, Provenance, freeze
from runner_app.engine.service import Engine
from runner_app.git.argv import run_git
from runner_app.git.registry import SourceRegistry
from runner_app.git.service import SourceService
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger
from runner_app.workflows.provision_resume_complete import (
    _CONTENT,
    _DRIVE_CREATES,
    CompleteProvisionRecoveryWorkflow,
)
from runner_app.workflows.reconcile import EffectProof

INSTALLATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROJECTS = "projectsRoot123"
ROOT_COMMAND = "rootCommand123"
OWNER = Provenance("local", "owner-session")


def _command(ledger: Ledger, action: str, target_id: str | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    state = ledger.state()
    params = ({
        "name": "Fixture", "source": {"kind": "new"},
        "template_id": "python-basic", "github_identity": "personal",
        "visibility": "private",
    } if action == "CREATE_PROJECT" else {
        "resume_version": "1.0", "target_request_id": target_id,
    })
    return {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": action, "project_id": "fixture", "workspace_id": None,
        "expected_head": None, "params": params,
        "intent": "Recover a fully provisioned fixture",
    }


class FakeGitHubReader:
    def __init__(self, repository: GitHubRepository, head: str) -> None:
        self.repository = repository
        self.head = head
        self.reads = 0

    def get_repository(self, owner: str, name: str, *,
                       expected_repo_id: int | None = None) -> GitHubRepository:
        self.reads += 1
        assert (owner, name, expected_repo_id) == ("ViskyG", "fixture", 421)
        return self.repository

    def branch_head(self, repository: GitHubRepository, branch: str) -> GitHubBranch:
        self.reads += 1
        assert repository == self.repository and branch == "main"
        return GitHubBranch("main", self.head, True)


class FakeDriveReader:
    def __init__(self) -> None:
        self.remote: dict[str, tuple[str, dict[str, Any]]] = {}
        self.reads = 0

    def __call__(self, target: Mapping[str, Any],
                 effect: Mapping[str, Any]) -> EffectProof | None:
        self.reads += 1
        assert target["action"] == "CREATE_PROJECT" and effect["state"] == "UNKNOWN"
        observed = self.remote.get(effect["step_id"])
        return (None if observed is None else EffectProof(
            effect["effect_id"], observed[0], observed[1],
        ))


class FakeDriveRegistry:
    def __init__(self, registration: WorkspaceDriveRegistration) -> None:
        self.registration = registration
        self.reads = 0

    def get(self, project_id: str, workspace_id: str) -> WorkspaceDriveRegistration:
        self.reads += 1
        assert (project_id, workspace_id) == ("fixture", "primary")
        return self.registration


def _fixture(tmp_path: Path, *, complete: bool = True,
             root_origin: bool = False) -> tuple[
                 Ledger, dict[str, Any], CompleteProvisionRecoveryWorkflow,
                 FakeGitHubReader, FakeDriveReader,
             ]:
    ledger = Ledger(Database(tmp_path / "state" / "ledger.sqlite"))
    ledger.startup()
    ledger.arm()
    registry = SourceRegistry(ledger.database, tmp_path / "managed")
    original = _command(ledger, "CREATE_PROJECT")
    assert ledger.admit(
        original, freeze(original).digest,
        ROOT_COMMAND if root_origin else OWNER.source_id,
        ("repo:fixture", "global:personal", "global:drive-installation"),
    ).execute
    request_id = original["request_id"]
    marker = make_provision_marker(INSTALLATION, "fixture", request_id)
    effect = ledger.begin_effect(
        request_id, "github-repository", "github_repository",
        {"owner": "ViskyG", "name": "fixture", "private": True, "marker": marker},
    )
    ledger.confirm_effect(effect, "421")
    repository = GitHubRepository(
        id=421, node_id="R_421", owner="ViskyG", owner_id=12452296,
        name="fixture", full_name="ViskyG/fixture",
        html_url="https://github.com/ViskyG/fixture",
        clone_url="https://github.com/ViskyG/fixture.git",
        ssh_url="git@github.com:ViskyG/fixture.git", private=True,
        default_branch="main", description=marker,
    )
    github = FakeGitHubReader(repository, "a" * 40)
    drive = FakeDriveReader()
    registration = WorkspaceDriveRegistration(
        INSTALLATION, "fixture", "primary", "projectFolder123", "workspaceFolder123",
        "commandDocument123", "changesetDocument123", "receiptDocument123",
        "latestResultDocument123", "projectStateDocument123",
        "sourceContextDocument123", "projectCommandDocument123",
        "projectReceiptDocument123",
    )
    drive_registry = FakeDriveRegistry(registration)
    workflow = CompleteProvisionRecoveryWorkflow(
        ledger, registry, github, drive, drive_registry,
        installation_id=INSTALLATION, projects_folder_id=PROJECTS,
        github_owner="ViskyG", github_identity="personal",
        template_profiles={"python-basic": "python-quick"},
        owner_source_ids=frozenset({OWNER.source_id}), root_command_id=ROOT_COMMAND,
    )
    if complete:
        source = SourceService(ledger, registry)
        project = source.create_new_source(request_id)
        repo = Path(project["repo_path"])
        registry.bind_project_remote(
            "fixture", provider_id=repository.id, node_id=repository.node_id,
            owner=repository.owner, name=repository.name,
            html_url=repository.html_url, clone_url=repository.clone_url,
            provision_marker=marker,
        )
        run_git(repo, ["remote", "add", "origin", repository.clone_url])
        head = run_git(repo, ["rev-parse", "--verify", "refs/heads/main^{commit}"]).strip()
        github.head = head
        effect = ledger.begin_effect(
            request_id, "baseline-main", "git_ref",
            {"repository_id": 421, "ref": "refs/heads/main", "commit": head},
        )
        ledger.confirm_effect(effect, head)
        source.create_initial_workspace_source(request_id)
        registry.bind_workspace_profile(
            "fixture", "primary", template_id="python-basic", profile_id="python-quick",
        )
        for step, field, parent, key, name, mime, workspace in _DRIVE_CREATES:
            parent_id = PROJECTS if parent == "projects" else getattr(registration, parent)
            file_id = getattr(registration, field)
            intent = {
                "parent_id": parent_id, "installation_id": INSTALLATION,
                "key": key.format(project="fixture"), "name": name.format(project="fixture"),
                "mime_type": mime, "project_id": "fixture", "workspace_id": workspace,
            }
            effect = ledger.begin_effect(request_id, step, "drive_file", intent)
            ledger.confirm_effect(effect, file_id)
            drive.remote[step] = (file_id, intent)
        for step, field, key in _CONTENT:
            file_id = getattr(registration, field)
            intent = {
                "file_id": file_id, "key": key.format(project="fixture"),
                "base_revision": "revision-1", "desired_sha256": "f" * 64,
            }
            effect = ledger.begin_effect(request_id, step, "drive_file", intent)
            ledger.confirm_effect(effect, file_id)
            drive.remote[step] = (file_id, intent)
        registry.mark_project_ready("fixture", "primary")
    ledger.finish(request_id, "BLOCKED", code="LOST_FINAL_RESPONSE")
    return ledger, original, workflow, github, drive


def _run(ledger: Ledger, target_id: str,
         workflow: CompleteProvisionRecoveryWorkflow) -> dict[str, Any]:
    resume = _command(ledger, "RESUME_PROVISION", target_id)
    engine = Engine(ledger, policy=LocalPolicy(), handlers={"RESUME_PROVISION": workflow})
    admission = engine.submit(json.dumps(resume).encode(), OWNER)
    assert admission.execute
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        stored = ledger.get(resume["request_id"])
        assert stored is not None
        if stored["status"] != "RUNNING":
            return stored
        time.sleep(0.01)
    raise AssertionError("Recovery did not finish")


def test_fully_proven_create_completes_with_adopted_effects_and_no_provider_write(
    tmp_path: Path,
) -> None:
    ledger, original, workflow, github, drive = _fixture(tmp_path)
    before = ledger.get(original["request_id"])
    assert before is not None
    result = _run(ledger, original["request_id"], workflow)
    assert result["status"] == "COMPLETED"
    assert result["result"]["recovery"]["target_request_id"] == original["request_id"]
    assert result["result"]["project_state"] == "READY"
    assert result["result"]["github"]["repository_id"] == 421
    assert result["result"]["drive"]["project_state_doc_id"] == "projectStateDocument123"
    assert len(result["effects"]) == 16
    assert all(effect["state"] == "CONFIRMED" and "adoption" in effect
               for effect in result["effects"])
    assert {effect["adoption"]["source_effect_id"] for effect in result["effects"]} == {
        effect["effect_id"] for effect in before["effects"]
    }
    assert ledger.get(original["request_id"]) == before
    assert github.reads == 2 and drive.reads == 12


def test_missing_graph_is_blocked_before_any_read_or_adoption(tmp_path: Path) -> None:
    ledger, original, workflow, github, drive = _fixture(tmp_path, complete=False)
    result = _run(ledger, original["request_id"], workflow)
    assert result["status"] == "BLOCKED"
    assert result["code"] == "RESUME_INCOMPLETE_RESOURCE_GRAPH"
    assert "local-project" in result["result"]["missing_steps"]
    assert result["effects"] == [] and github.reads == drive.reads == 0


def test_changed_remote_head_cannot_be_adopted(tmp_path: Path) -> None:
    ledger, original, workflow, github, drive = _fixture(tmp_path)
    github.head = "b" * 40
    result = _run(ledger, original["request_id"], workflow)
    assert result["status"] == "BLOCKED"
    assert result["code"] == "RESUME_IDENTITY_MISMATCH"
    assert result["effects"] == [] and drive.reads == 0


def test_changed_drive_document_cannot_be_adopted(tmp_path: Path) -> None:
    ledger, original, workflow, _, drive = _fixture(tmp_path)
    file_id, intent = drive.remote["drive-project-state-content"]
    drive.remote["drive-project-state-content"] = (
        file_id, {**intent, "desired_sha256": "0" * 64},
    )
    result = _run(ledger, original["request_id"], workflow)
    assert result["status"] == "BLOCKED"
    assert result["code"] == "RESUME_IDENTITY_MISMATCH"
    assert result["effects"] == []


def test_drive_origin_requires_separate_original_root_receipt_path(tmp_path: Path) -> None:
    ledger, original, workflow, github, drive = _fixture(tmp_path, root_origin=True)
    result = _run(ledger, original["request_id"], workflow)
    assert result["status"] == "BLOCKED"
    assert result["code"] == "RESUME_ROOT_RECEIPT_UNSUPPORTED"
    assert result["effects"] == [] and github.reads == drive.reads == 0


def test_dirty_managed_workspace_cannot_be_adopted(tmp_path: Path) -> None:
    ledger, original, workflow, github, drive = _fixture(tmp_path)
    workspace = workflow.registry.get_workspace("fixture", "primary")
    assert workspace is not None
    Path(workspace["worktree_path"], "unreviewed.txt").write_text("owner edit\n")
    result = _run(ledger, original["request_id"], workflow)
    assert result["status"] == "BLOCKED"
    assert result["code"] == "RESUME_IDENTITY_MISMATCH"
    assert result["effects"] == [] and drive.reads == 0
    assert github.reads == 1
