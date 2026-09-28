"""No-write recovery of a complete interrupted create with real managed Git."""
from __future__ import annotations

import json
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from runner_app.adapters.drive_registry import WorkspaceDriveRegistration
from runner_app.adapters.drive_root_receipt import RootPartialReceiptProof
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
from runner_app.storage.ledger import Ledger, confirmed_create_effects_proof
from runner_app.workflows.provision_resume_complete import (
    _CONTENT,
    _DRIVE_CREATES,
    CompleteProvisionRecoveryWorkflow,
)
from runner_app.workflows.provision_resume_partial import PartialProvisionRecoveryWorkflow
from runner_app.workflows.reconcile import EffectProof, ReconcileWorkflow

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
             root_origin: bool = False, partial_workspace_folder: bool = False,
             root_interrupted_full: bool = False) -> tuple[
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
    if root_origin and (partial_workspace_folder or root_interrupted_full):
        ledger.journal(request_id, "dispatch", "STARTED", {"action": "CREATE_PROJECT"})
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
    if complete or partial_workspace_folder:
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
            drive.remote[step] = (file_id, intent)
            if partial_workspace_folder and step == "drive-workspace-folder":
                break
            ledger.confirm_effect(effect, file_id)
        if not partial_workspace_folder:
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
    interrupted = partial_workspace_folder or root_interrupted_full
    ledger.finish(request_id, "INTERRUPTED" if interrupted else "BLOCKED",
                  code="NEEDS_RECONCILIATION" if interrupted
                       else "LOST_FINAL_RESPONSE")
    return ledger, original, workflow, github, drive


def _run(ledger: Ledger, target_id: str,
         workflow: Any) -> dict[str, Any]:
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


class FakePartialProvisioner:
    """Count only the missing Drive writes after exact old-effect adoption."""

    def __init__(self, complete: CompleteProvisionRecoveryWorkflow) -> None:
        self.ledger = complete.ledger
        self.registry = complete.drive_registry
        self.attached = SimpleNamespace(index=SimpleNamespace(installation_id=INSTALLATION))
        self.created_steps: list[str] = []
        self.registration = cast(FakeDriveRegistry, self.registry).registration

    def provision(self, request_id: str, project_id: str, workspace_id: str,
                  project_name: str, *, resume_target_id: str | None = None
                  ) -> WorkspaceDriveRegistration:
        operation = self.ledger.get(request_id)
        assert operation is not None
        assert (project_id, workspace_id, project_name, resume_target_id) == (
            "fixture", "primary", "Fixture",
            operation["command"]["params"]["target_request_id"],
        )
        effects = {item["step_id"]: item for item in operation["effects"]}
        assert all(step in effects and "adoption" in effects[step]
                   for step in ("github-repository", "local-project", "baseline-main",
                                "initial-workspace", *[item[0] for item in _DRIVE_CREATES[:5]]))
        for step, field, parent, key, name, mime, workspace in _DRIVE_CREATES:
            if step in effects:
                continue
            intent = {
                "parent_id": PROJECTS if parent == "projects"
                             else getattr(self.registration, parent),
                "installation_id": INSTALLATION, "key": key.format(project="fixture"),
                "name": name.format(project="fixture"), "mime_type": mime,
                "project_id": "fixture", "workspace_id": workspace,
            }
            effect_id = self.ledger.begin_effect(request_id, step, "drive_file", intent)
            self.ledger.confirm_effect(effect_id, getattr(self.registration, field))
            self.created_steps.append(step)
        return self.registration

    def populate(self, request_id: str, _registration: WorkspaceDriveRegistration,
                 **kwargs: Any) -> dict[str, str]:
        operation = self.ledger.get(request_id)
        assert operation is not None
        assert kwargs["resume_target_id"] == operation["command"]["params"][
            "target_request_id"]
        for step, field, key in _CONTENT:
            file_id = getattr(self.registration, field)
            intent = {"file_id": file_id, "key": key.format(project="fixture"),
                      "base_revision": "revision-1", "desired_sha256": "f" * 64}
            effect_id = self.ledger.begin_effect(request_id, step, "drive_file", intent)
            self.ledger.confirm_effect(effect_id, file_id)
            self.created_steps.append(step)
        return {"project_state_sha256": "f" * 64, "context_sha256": "f" * 64}


def _reconcile_partial_folder(ledger: Ledger, target_id: str,
                              drive: FakeDriveReader) -> None:
    reconcile = ReconcileWorkflow(
        ledger, owner_source_ids=frozenset({OWNER.source_id}), ports={
            "github_repository": lambda _target, _effect: None,
            "git_ref": lambda _target, _effect: None,
            "pull_request": lambda _target, _effect: None,
            "drive_file": drive,
        },
    )
    command = _command(ledger, "RESUME_PROVISION", target_id)
    command["action"] = "RECONCILE"
    command["params"] = {"reconcile_version": "1.0", "target_request_id": target_id}
    engine = Engine(ledger, policy=LocalPolicy(), handlers={"RECONCILE": reconcile})
    admitted = engine.submit(json.dumps(command).encode(), OWNER)
    assert admitted.execute
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = ledger.get(command["request_id"])
        assert result is not None
        if result["status"] != "RUNNING":
            assert result["status"] == "COMPLETED"
            assert result["result"]["ready_for_resume"] is True
            break
        time.sleep(0.01)
    else:
        raise AssertionError("Reconciliation did not finish")
    ledger.confirm_stopped(target_id)
    original = ledger.get(target_id)
    assert original is not None and original["reconcile_required"] == 0
    assert len(original["effects"]) == 9
    assert all(item["state"] == "CONFIRMED" for item in original["effects"])


def test_partial_ui_create_adopts_exact_prefix_then_creates_only_missing_docs(
    tmp_path: Path,
) -> None:
    ledger, original, complete, github, drive = _fixture(
        tmp_path, partial_workspace_folder=True,
    )
    target_id = original["request_id"]
    _reconcile_partial_folder(ledger, target_id, drive)
    original_before = ledger.get(target_id)
    assert original_before is not None
    provisioner = FakePartialProvisioner(complete)
    partial = PartialProvisionRecoveryWorkflow(
        complete, cast(Any, provisioner), lambda _registration: cast(Any, object()),
    )
    result = _run(ledger, target_id, partial)
    assert result["status"] == "COMPLETED", result["code"]
    assert result["result"]["project_state"] == "READY"
    assert len(result["effects"]) == 16
    assert len([effect for effect in result["effects"] if "adoption" in effect]) == 9
    assert provisioner.created_steps == [item[0] for item in _DRIVE_CREATES[5:]] + [
        item[0] for item in _CONTENT
    ]
    assert ledger.get(target_id) == original_before
    assert github.reads == 2 and drive.reads == 6


def test_partial_root_create_stays_blocked_after_positive_reconciliation(
    tmp_path: Path,
) -> None:
    ledger, original, complete, github, drive = _fixture(
        tmp_path, root_origin=True, partial_workspace_folder=True,
    )
    _reconcile_partial_folder(ledger, original["request_id"], drive)
    provisioner = FakePartialProvisioner(complete)
    partial = PartialProvisionRecoveryWorkflow(
        complete, cast(Any, provisioner), lambda _registration: cast(Any, object()),
    )
    result = _run(ledger, original["request_id"], partial)
    assert result["status"] == "BLOCKED"
    assert result["code"] == "RESUME_ROOT_RECEIPT_UNCONFIRMED"
    assert result["effects"] == []
    assert provisioner.created_steps == [] and github.reads == 0


def test_partial_ui_create_refuses_changed_confirmed_drive_id_before_adoption(
    tmp_path: Path,
) -> None:
    ledger, original, complete, _github, drive = _fixture(
        tmp_path, partial_workspace_folder=True,
    )
    _reconcile_partial_folder(ledger, original["request_id"], drive)
    file_id, intent = drive.remote["drive-project-folder"]
    drive.remote["drive-project-folder"] = ("wrongProjectFolder123", intent)
    provisioner = FakePartialProvisioner(complete)
    partial = PartialProvisionRecoveryWorkflow(
        complete, cast(Any, provisioner), lambda _registration: cast(Any, object()),
    )
    result = _run(ledger, original["request_id"], partial)
    assert result["status"] == "BLOCKED"
    assert result["code"] == "RESUME_IDENTITY_MISMATCH"
    assert result["effects"] == [] and provisioner.created_steps == []
    assert file_id == "projectFolder123"


ROOT_RECEIPT = "rootReceipt123"

def _terminal_marker(ledger: Ledger, target_id: str) -> RootPartialReceiptProof:
    target = ledger.get(target_id)
    assert target is not None and target["source_id"] == ROOT_COMMAND
    count, effects_sha = confirmed_create_effects_proof(target["effects"])
    intent = {
        "command_doc_id": ROOT_COMMAND, "receipt_doc_id": ROOT_RECEIPT,
        "request_digest": target["digest"], "status": "INTERRUPTED",
        "code": "PROVISIONING_PARTIAL",
        "summary": {"project_id": "fixture", "workspace_id": "primary",
                    "template_id": "python-basic",
                    "confirmed_effect_count": str(count)},
        "frozen_command_revision": "revision-1",
        "frozen_command_sha256": "b" * 64,
        "confirmed_effect_count": count,
        "confirmed_effects_sha256": effects_sha,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "base_revision": "receipt-revision-1",
        "desired_sha256": "c" * 64,
    }
    effect_id = ledger.begin_terminal_root_receipt(
        target_id, source_id=ROOT_COMMAND, digest=target["digest"], intent=intent,
    )
    ledger.confirm_effect(effect_id, ROOT_RECEIPT)
    ledger.complete_terminal_root_receipt(
        target_id, source_id=ROOT_COMMAND, digest=target["digest"],
        receipt_doc_id=ROOT_RECEIPT, effect_id=effect_id,
    )
    return RootPartialReceiptProof(
        target_id, target["digest"], ROOT_RECEIPT, "c" * 64, count, effects_sha,
    )


def test_partial_root_resume_requires_durable_terminal_marker_and_exact_proof(
    tmp_path: Path,
) -> None:
    ledger, original, complete, _github, drive = _fixture(
        tmp_path, root_origin=True, partial_workspace_folder=True,
    )
    target_id = original["request_id"]
    _reconcile_partial_folder(ledger, target_id, drive)
    proof = _terminal_marker(ledger, target_id)
    provisioner = FakePartialProvisioner(complete)
    failed = PartialProvisionRecoveryWorkflow(
        complete, cast(Any, provisioner), lambda _registration: cast(Any, object()),
        root_proof=lambda _request_id: RootPartialReceiptProof(
            proof.request_id, proof.request_digest, proof.receipt_doc_id,
            "0" * 64, proof.confirmed_effect_count, proof.confirmed_effects_sha256,
        ),
        root_receipt_id=ROOT_RECEIPT,
    )
    rejected = _run(ledger, target_id, failed)
    assert rejected["status"] == "BLOCKED"
    assert rejected["code"] == "RESUME_ROOT_RECEIPT_UNCONFIRMED"
    assert rejected["effects"] == [] and provisioner.created_steps == []
    # A fresh new request ID may be submitted after a rejected preflight.
    partial = PartialProvisionRecoveryWorkflow(
        complete, cast(Any, provisioner), lambda _registration: cast(Any, object()),
        root_proof=lambda request_id: proof if request_id == target_id else pytest.fail(),
        root_receipt_id=ROOT_RECEIPT,
    )
    result = _run(ledger, target_id, partial)
    assert result["status"] == "COMPLETED", result["code"]
    assert len(result["effects"]) == 16
    assert len([effect for effect in result["effects"] if "adoption" in effect]) == 9
    assert "drive-root-create-receipt" not in {
        effect["step_id"] for effect in result["effects"]
    }
    assert result["result"]["project_state"] == "READY"


def test_full_graph_root_resume_after_terminal_receipt_needs_no_new_resource(
    tmp_path: Path,
) -> None:
    ledger, original, complete, _github, _drive = _fixture(
        tmp_path, root_origin=True, root_interrupted_full=True,
    )
    target_id = original["request_id"]
    ledger.confirm_stopped(target_id)
    proof = _terminal_marker(ledger, target_id)
    provisioner = FakePartialProvisioner(complete)
    partial = PartialProvisionRecoveryWorkflow(
        complete, cast(Any, provisioner), lambda _registration: cast(Any, object()),
        root_proof=lambda request_id: proof if request_id == target_id else pytest.fail(),
        root_receipt_id=ROOT_RECEIPT,
    )
    result = _run(ledger, target_id, partial)
    assert result["status"] == "COMPLETED", result["code"]
    assert len(result["effects"]) == 16
    assert all("adoption" in effect for effect in result["effects"])
    assert provisioner.created_steps == []
