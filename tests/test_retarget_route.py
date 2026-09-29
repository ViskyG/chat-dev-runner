"""Finite v1 retarget through real disposable Git, local API and exact fake Drive Docs."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_project_mailbox_receipt import _attached, _Docs

from runner_app.adapters.drive_docs import extract_document
from runner_app.adapters.drive_poller import DrivePoller, FrozenSlotStore
from runner_app.adapters.drive_registry import WorkspaceDriveRegistration
from runner_app.adapters.drive_workspace_receipt import WorkspaceTerminalReceiptPublisher
from runner_app.api.auth import Sessions
from runner_app.api.runtime import create_runtime_app
from runner_app.config import Settings
from runner_app.domain.commands import LocalPolicy, Provenance
from runner_app.engine.service import Engine, OperationResult
from runner_app.git.argv import run_git
from runner_app.git.registry import SourceRegistry
from runner_app.git.service import SourceService
from runner_app.git.worktrees import create_repository, create_workspace, inspect_workspace
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger
from runner_app.workflows.retarget_drive_state import RetargetDriveState
from runner_app.workflows.retarget_workspace import RetargetWorkspaceWorkflow

PROJECT = "fixture"
WORKSPACE = "area"
COMMAND_ID = "workspacecommand001"
RECEIPT_ID = "workspacereceipt001"
STATE_ID = "projectstate001"
CONTEXT_ID = "sourcecontext001"
ORIGIN = "http://127.0.0.1:8765"


class _Registration:
    def __init__(self, registration: WorkspaceDriveRegistration) -> None:
        self.registration = registration

    def get(self, project_id: str, workspace_id: str) -> WorkspaceDriveRegistration:
        assert (project_id, workspace_id) == (PROJECT, WORKSPACE)
        return self.registration


def _text(provider: _Docs, file_id: str) -> str:
    return extract_document(provider.get_document(file_id), file_id,
                            max_bytes=65536).text


def _command(ledger: Ledger, head: str, *, target_branch: str = "feature/free") -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    state = ledger.state()
    return {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "RETARGET_WORKSPACE", "project_id": PROJECT,
        "workspace_id": WORKSPACE, "expected_head": head,
        "params": {"expected_branch": "feature/area", "target_branch": target_branch,
                   "target_commit": head},
        "intent": "Switch one approved clean fixture workspace",
    }


def _setup(tmp_path: Path) -> tuple[Ledger, SourceRegistry, Path, Path, Path, str,
                                     _Docs, WorkspaceDriveRegistration, Engine]:
    ledger = Ledger(Database(tmp_path / "state.sqlite"))
    ledger.startup()
    ledger.arm()
    managed = tmp_path / "managed"
    repo = create_repository(managed, PROJECT)
    head = run_git(repo, ["rev-parse", "HEAD"]).strip()
    area = create_workspace(managed, PROJECT, repo, WORKSPACE, "feature/area", head)
    other = create_workspace(managed, PROJECT, repo, "other", "feature/other", head)
    run_git(repo, ["branch", "feature/free", head])
    registry = SourceRegistry(ledger.database, managed)
    registry.register_project(PROJECT, "Fixture", repo, origin_kind="new")
    registry.register_workspace(PROJECT, WORKSPACE, "Area", area,
                                branch="feature/area", head=head)
    registry.register_workspace(PROJECT, "other", "Other", other,
                                branch="feature/other", head=head)
    registry.bind_project_remote(
        PROJECT, provider_id=42, node_id="R_42", owner="alice", name=PROJECT,
        html_url=f"https://github.com/alice/{PROJECT}",
        clone_url=f"https://github.com/alice/{PROJECT}.git",
        provision_marker="cdr:v1:" + "a" * 64,
    )
    registry.bind_workspace_profile(PROJECT, WORKSPACE,
                                    template_id="python-basic", profile_id="python-quick")
    registry.mark_project_ready(PROJECT, WORKSPACE)
    provider, attached = _Docs(), _attached()
    registration = WorkspaceDriveRegistration(
        attached.index.installation_id, PROJECT, WORKSPACE,
        "projectfolder001", "workspacefolder001", COMMAND_ID,
        "workspacechangeset001", RECEIPT_ID, "workspacelatest001",
        STATE_ID, CONTEXT_ID, "projectcommand001", "projectreceipt001",
    )
    state = {
        "schema_version": "1.0", "kind": "project-state/v1",
        "project_id": PROJECT, "project_folder_id": registration.project_folder_id,
        "project_state_doc_id": STATE_ID,
        "workspaces": [
            {"workspace_id": WORKSPACE, "branch": "feature/area", "head": head,
             "source_generation": 0, "command_doc_id": COMMAND_ID,
             "changeset_doc_id": registration.changeset_doc_id,
             "receipt_doc_id": RECEIPT_ID,
             "latest_result_doc_id": registration.latest_result_doc_id,
             "context_doc_id": CONTEXT_ID},
            {"workspace_id": "other", "branch": "feature/other", "head": head,
             "source_generation": 0},
        ],
    }
    provider.add(STATE_ID, registration.project_folder_id, "project-state",
                 "PROJECT STATE\n" + json.dumps(state, sort_keys=True) + "\n")
    provider.add(CONTEXT_ID, registration.workspace_folder_id, "source-context",
                 "# Source context / generated by runner\n\n"
                 f"Project/workspace: `{PROJECT}` / `{WORKSPACE}`\n"
                 f"Snapshot: `{head}`\nBranch: `feature/area`\n"
                 "## Later export\nkeep this independent context section\n")
    provider.add(COMMAND_ID, registration.workspace_folder_id, "workspace-command", "\n")
    provider.add(RECEIPT_ID, registration.workspace_folder_id, "workspace-receipt",
                 "previous delivery report\n")
    registration_port = _Registration(registration)
    workflow = RetargetWorkspaceWorkflow(
        ledger, SourceService(ledger, registry),
        RetargetDriveState(ledger, attached, provider, registration_port),
    )
    provenance = Provenance("drive", COMMAND_ID, PROJECT, WORKSPACE)
    engine = Engine(
        ledger, policy=LocalPolicy(
            mailboxes={COMMAND_ID: provenance}, project_ids=frozenset({PROJECT}),
            workspace_pairs=frozenset({(PROJECT, WORKSPACE)}),
        ), handlers={"RETARGET_WORKSPACE": workflow},
    )
    return ledger, registry, repo, area, other, head, provider, registration, engine


def _terminal(ledger: Ledger, request_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        operation = ledger.get(request_id)
        if operation is not None and operation["status"] != "RUNNING":
            return operation
        time.sleep(0.01)
    pytest.fail("Retarget worker did not finish")


def test_local_api_retarget_updates_registered_drive_association_without_other_area(
    tmp_path: Path,
) -> None:
    ledger, registry, repo, area, other, head, provider, _, engine = _setup(tmp_path)
    sessions = Sessions()
    bootstrap = sessions.issue_bootstrap()
    command = _command(ledger, head)
    before_other = inspect_workspace(repo, other)
    try:
        with TestClient(create_runtime_app(Settings(), engine, sessions,
                                           source_registry=registry), base_url=ORIGIN) as client:
            assert client.post("/api/v1/operations", json=command,
                               headers={"Origin": ORIGIN}).status_code == 403
            login = client.post("/api/v1/local/session", json={"bootstrap": bootstrap},
                                headers={"Origin": ORIGIN})
            assert login.status_code == 200
            headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
            admitted = client.post("/api/v1/operations", json=command, headers=headers)
            assert admitted.status_code == 202
            operation = _terminal(ledger, command["request_id"])
            assert operation["status"] == "COMPLETED"
            duplicate = client.post("/api/v1/operations", json=command, headers=headers)
            assert duplicate.status_code == 200
            assert duplicate.json() == operation["receipt"]
        assert inspect_workspace(repo, area)["branch"] == "feature/free"
        assert inspect_workspace(repo, other) == before_other
        assert registry.get_workspace(PROJECT, WORKSPACE)["generation"] == 1
        assert [effect["state"] for effect in operation["effects"]] == [
            "CONFIRMED", "CONFIRMED", "CONFIRMED",
        ]
        state = json.loads(_text(provider, STATE_ID).removeprefix("PROJECT STATE\n"))
        assert state["workspaces"][0]["branch"] == "feature/free"
        assert state["workspaces"][0]["source_generation"] == 1
        assert state["workspaces"][1]["branch"] == "feature/other"
        context = _text(provider, CONTEXT_ID)
        assert "Branch: `feature/free`\n" in context
        assert "keep this independent context section" in context
    finally:
        engine.shutdown()


@pytest.mark.parametrize("refusal", ["DIRTY_WORKSPACE", "BRANCH_IN_USE", "BUSY"])
def test_retarget_refuses_dirty_occupied_or_busy_without_mutation(
    tmp_path: Path, refusal: str,
) -> None:
    ledger, registry, repo, area, other, head, provider, _, engine = _setup(tmp_path)
    command = _command(ledger, head, target_branch=(
        "feature/other" if refusal == "BRANCH_IN_USE" else "feature/free"
    ))
    before_state = _text(provider, STATE_ID)
    if refusal == "DIRTY_WORKSPACE":
        (area / "human-note.txt").write_text("keep me")
    if refusal == "BUSY":
        blocker = _command(ledger, head)
        from runner_app.domain.commands import freeze
        assert ledger.admit(blocker, freeze(blocker).digest, "local-owner",
                            (f"repo:{PROJECT}",)).execute
    try:
        admission = engine.submit(json.dumps(command).encode(),
                                  Provenance("local", "local-owner"))
        operation = _terminal(ledger, command["request_id"])
        assert operation["code"] == refusal
        if refusal == "BUSY":
            assert not admission.execute and operation["status"] == "REJECTED"
        else:
            assert operation["status"] == "BLOCKED"
            assert operation["effects"] == []
        assert inspect_workspace(repo, area)["branch"] == "feature/area"
        assert inspect_workspace(repo, other)["branch"] == "feature/other"
        assert _text(provider, STATE_ID) == before_state
        assert registry.get_workspace(PROJECT, WORKSPACE)["generation"] == 0
    finally:
        engine.shutdown()


def test_drive_retarget_publishes_exact_terminal_receipt_without_replay(tmp_path: Path) -> None:
    ledger, _, repo, area, other, head, provider, registration, engine = _setup(tmp_path)
    command = _command(ledger, head)
    provider.put_document(COMMAND_ID, json.dumps(command, sort_keys=True) + "\n")
    slots = FrozenSlotStore(tmp_path / "frozen")
    publisher = WorkspaceTerminalReceiptPublisher(
        ledger, _attached(), provider, _Registration(registration),
    )
    poller = DrivePoller(
        provider, registration.mailbox(), engine, slots,
        on_observed=lambda observed, source: publisher.publish_if_terminal(
            observed, source, slots,
        ),
    )
    try:
        assert poller.poll_once().kind == "SUBMITTED"
        operation = _terminal(ledger, command["request_id"])
        assert operation["status"] == "COMPLETED"
        assert poller.poll_once().kind == "ALREADY_OBSERVED"
        operation = ledger.get(command["request_id"])
        assert json.loads(_text(provider, RECEIPT_ID)) == operation["receipt"]
        assert operation["reconcile_required"] == 0
        assert len([e for e in operation["effects"] if e["step_id"] ==
                    "drive-workspace-terminal-receipt"]) == 1
        writes = list(provider.batches)
        assert poller.poll_once().kind == "ALREADY_OBSERVED"
        assert provider.batches == writes
        assert inspect_workspace(repo, area)["branch"] == "feature/free"
        assert inspect_workspace(repo, other)["branch"] == "feature/other"
    finally:
        engine.shutdown()


def test_workspace_mailbox_activates_retarget_without_execution_route(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from runner_app.engine.workspace_mailboxes import WorkspaceMailboxManager

    ledger, registry, _, _, _, _, provider, registration, engine = _setup(tmp_path)
    monkeypatch.setattr(ledger, "completed_provision_project_ids", lambda: [PROJECT])
    monkeypatch.setattr(ledger, "completed_workspace_pairs", lambda: [(PROJECT, WORKSPACE)])
    publisher = WorkspaceTerminalReceiptPublisher(
        ledger, _attached(), provider, _Registration(registration),
    )
    with pytest.raises(ValueError, match="terminal receipt publisher"):
        WorkspaceMailboxManager(
            ledger, registry, _Registration(registration), provider, engine,
            tmp_path / "no-receipt-slots", {"python-basic": "python-quick"},
        )
    manager = WorkspaceMailboxManager(
        ledger, registry, _Registration(registration), provider, engine,
        tmp_path / "manager-slots", {"python-basic": "python-quick"},
        workspace_receipt=publisher,
    )
    try:
        refresh = manager.refresh()
        assert COMMAND_ID in refresh.new_mailboxes
        assert manager.active_count() == 1
        assert manager.full_route_count() == 0
        provenance = Provenance("drive", COMMAND_ID, PROJECT, WORKSPACE)
        assert engine.policy.mailboxes[COMMAND_ID] == provenance
        assert "DELIVER_VERIFY_PUBLISH" not in engine.capabilities()
        from runner_app.engine.runtime import Runtime
        local_runtime = object.__new__(Runtime)
        local_runtime.drive_bridge = object()  # type: ignore[assignment]
        local_runtime.integrations = object()  # type: ignore[assignment]
        local_runtime.workspace_mailboxes = manager
        local_runtime.engine = engine
        engine.handlers["PUBLISH_REPORT"] = lambda *_: OperationResult()
        assert local_runtime.drive_actions() == [
            "CREATE_PROJECT", "PUBLISH_REPORT", "RETARGET_WORKSPACE",
        ]
    finally:
        manager.close()
        engine.shutdown()


def test_drive_retarget_lost_ack_has_exact_readback_and_no_extra_write(tmp_path: Path) -> None:
    ledger, _, _, _, _, head, provider, registration, engine = _setup(tmp_path)
    command = _command(ledger, head)
    provider.put_document(COMMAND_ID, json.dumps(command) + "\n")
    provider.lose_ack = True
    slots = FrozenSlotStore(tmp_path / "frozen")
    publisher = WorkspaceTerminalReceiptPublisher(
        ledger, _attached(), provider, _Registration(registration),
    )
    poller = DrivePoller(
        provider, registration.mailbox(), engine, slots,
        on_observed=lambda observed, source: publisher.publish_if_terminal(
            observed, source, slots,
        ),
    )
    try:
        assert poller.poll_once().kind == "SUBMITTED"
        assert _terminal(ledger, command["request_id"])["status"] == "COMPLETED"
        assert poller.poll_once().kind == "ALREADY_OBSERVED"
        operation = ledger.get(command["request_id"])
        assert operation["reconcile_required"] == 0
        assert json.loads(_text(provider, RECEIPT_ID)) == operation["receipt"]
        assert sorted(provider.batches) == sorted([STATE_ID, CONTEXT_ID, RECEIPT_ID])
        poller.poll_once()
        assert len(provider.batches) == 3
    finally:
        engine.shutdown()


def test_uncertain_state_cas_retains_repo_lock_and_publishes_blocked_receipt(tmp_path: Path) -> None:
    ledger, registry, repo, area, _, head, provider, registration, engine = _setup(tmp_path)
    command = _command(ledger, head)
    provider.put_document(COMMAND_ID, json.dumps(command) + "\n")
    provider.pre_batch_change = "external owner change\n"
    slots = FrozenSlotStore(tmp_path / "frozen")
    publisher = WorkspaceTerminalReceiptPublisher(
        ledger, _attached(), provider, _Registration(registration),
    )
    poller = DrivePoller(
        provider, registration.mailbox(), engine, slots,
        on_observed=lambda observed, source: publisher.publish_if_terminal(
            observed, source, slots,
        ),
    )
    try:
        assert poller.poll_once().kind == "SUBMITTED"
        operation = _terminal(ledger, command["request_id"])
        assert operation["status"] == "BLOCKED"
        assert operation["code"] == "NEEDS_RECONCILIATION"
        assert operation["reconcile_required"] == 1
        assert ledger.holds_lock(command["request_id"], f"repo:{PROJECT}")
        assert inspect_workspace(repo, area)["branch"] == "feature/free"
        assert registry.get_workspace(PROJECT, WORKSPACE)["generation"] == 1
        assert poller.poll_once().kind == "ALREADY_OBSERVED"
        assert json.loads(_text(provider, RECEIPT_ID)) == ledger.get(command["request_id"])["receipt"]
        assert ledger.get(command["request_id"])["reconcile_required"] == 1
        assert provider.batches.count(STATE_ID) == 1
        poller.poll_once()
        assert provider.batches.count(STATE_ID) == 1
    finally:
        engine.shutdown()


@pytest.mark.parametrize("phase", ["before_switch", "after_registry"])
def test_uncertain_git_retarget_has_only_exact_positive_reconcile_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    from runner_app.git.argv import GitError
    from runner_app.workflows.reconcile_local import ManagedGitEffectReader

    ledger, registry, repo, area, _, head, provider, _, engine = _setup(tmp_path)
    command = _command(ledger, head)
    if phase == "before_switch":
        def fail_switch(*_: Any, **__: Any) -> None:
            raise GitError("GIT_SWITCH_FAILED", "Disposable switch failed")
        monkeypatch.setattr("runner_app.git.worktrees.retarget_workspace", fail_switch)
    else:
        workflow = engine.handlers["RETARGET_WORKSPACE"]

        def fail_before_drive(*_: Any, **__: Any) -> None:
            raise RuntimeError("Simulated crash after Git, before Drive intents")

        monkeypatch.setattr(workflow.drive_state, "publish", fail_before_drive)
    try:
        engine.submit(json.dumps(command).encode(), Provenance("local", "local-owner"))
        operation = _terminal(ledger, command["request_id"])
        assert operation["code"] == "NEEDS_RECONCILIATION"
        assert operation["reconcile_required"] == 1
        effects = [item for item in operation["effects"]
                   if item["step_id"] == "retarget-workspace"]
        assert len(effects) == 1 and effects[0]["state"] == "UNKNOWN"
        proof = ManagedGitEffectReader(registry)(operation, effects[0])
        assert proof is not None
        assert proof.provider_id == ("UNCHANGED" if phase == "before_switch" else head)
        assert inspect_workspace(repo, area)["branch"] == (
            "feature/area" if phase == "before_switch" else "feature/free"
        )
        assert provider.batches == []
        from runner_app.workflows.reconcile import ReconcileWorkflow

        reader = ManagedGitEffectReader(registry)
        engine.handlers["RECONCILE"] = ReconcileWorkflow(
            ledger, owner_source_ids=frozenset({"local-owner"}), ports={
                "github_repository": lambda *_: None,
                "git_ref": lambda *_: None,
                "pull_request": lambda *_: None,
                "drive_file": lambda *_: None,
                "local_workspace": reader,
            },
        )
        actor = _command(ledger, head)
        actor.update(
            request_id=str(uuid4()), action="RECONCILE", expected_head=None,
            params={"reconcile_version": "1.0", "target_request_id": command["request_id"]},
        )
        engine.submit(json.dumps(actor).encode(), Provenance("local", "local-owner"))
        reconciled = _terminal(ledger, actor["request_id"])
        assert reconciled["status"] == "COMPLETED"
        expected_release = phase == "before_switch"
        assert reconciled["result"]["target_lock_retained"] is not expected_release
        if expected_release:
            assert ledger.get(command["request_id"])["reconcile_required"] == 0
        else:
            assert reconciled["result"]["release_code"] == "RETARGET_STATE_INCOMPLETE"
            assert ledger.get(command["request_id"])["reconcile_required"] == 1
    finally:
        engine.shutdown()


def test_reconcile_exact_drive_readback_releases_completed_retarget_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import cast

    from runner_app.adapters.drive_writer import DocsCASWriter
    from runner_app.workflows.reconcile import ReconcileWorkflow
    from runner_app.workflows.reconcile_drive import DriveEffectReader
    from runner_app.workflows.reconcile_local import ManagedGitEffectReader

    ledger, registry, repo, area, _, head, provider, registration, engine = _setup(tmp_path)
    command = _command(ledger, head)
    original_confirm = ledger.confirm_effect

    def lose_context_confirmation(effect_id: str, provider_id: str) -> None:
        effect = next((item for item in ledger.get(command["request_id"])["effects"]
                       if item["effect_id"] == effect_id), None)
        if effect is not None and effect["step_id"] == "drive-retarget-source-context":
            raise RuntimeError("Simulated lost local context confirmation")
        original_confirm(effect_id, provider_id)

    monkeypatch.setattr(ledger, "confirm_effect", lose_context_confirmation)
    try:
        engine.submit(json.dumps(command).encode(), Provenance("local", "local-owner"))
        interrupted = _terminal(ledger, command["request_id"])
        assert interrupted["code"] == "NEEDS_RECONCILIATION"
        assert interrupted["reconcile_required"] == 1
        assert ledger.holds_lock(command["request_id"], f"repo:{PROJECT}")
        assert inspect_workspace(repo, area)["branch"] == "feature/free"
        monkeypatch.setattr(ledger, "confirm_effect", original_confirm)
        writer = DocsCASWriter(cast(Any, provider), _attached(),
                               extra_docs=registration.state_docs())
        drive_reader = DriveEffectReader(_attached(), cast(Any, object()), writer)
        engine.handlers["RECONCILE"] = ReconcileWorkflow(
            ledger, owner_source_ids=frozenset({"local-owner"}), ports={
                "github_repository": lambda *_: None,
                "git_ref": lambda *_: None,
                "pull_request": lambda *_: None,
                "drive_file": drive_reader,
                "local_workspace": ManagedGitEffectReader(registry),
            },
        )
        actor = _command(ledger, head)
        actor.update(
            request_id=str(uuid4()), action="RECONCILE", expected_head=None,
            params={"reconcile_version": "1.0", "target_request_id": command["request_id"]},
        )
        engine.submit(json.dumps(actor).encode(), Provenance("local", "local-owner"))
        reconciled = _terminal(ledger, actor["request_id"])
        assert reconciled["status"] == "COMPLETED"
        assert reconciled["result"]["remaining_effect_ids"] == []
        assert reconciled["result"]["target_lock_retained"] is False
        assert ledger.get(command["request_id"])["reconcile_required"] == 0
        assert not ledger.holds_lock(command["request_id"], f"repo:{PROJECT}")
    finally:
        engine.shutdown()


def test_drive_dirty_refusal_gets_terminal_receipt_without_source_effect(tmp_path: Path) -> None:
    ledger, registry, repo, area, _, head, provider, registration, engine = _setup(tmp_path)
    (area / "human-note.txt").write_text("leave this untouched")
    command = _command(ledger, head)
    provider.put_document(COMMAND_ID, json.dumps(command) + "\n")
    slots = FrozenSlotStore(tmp_path / "frozen")
    publisher = WorkspaceTerminalReceiptPublisher(
        ledger, _attached(), provider, _Registration(registration),
    )
    poller = DrivePoller(
        provider, registration.mailbox(), engine, slots,
        on_observed=lambda observed, source: publisher.publish_if_terminal(
            observed, source, slots,
        ),
    )
    try:
        assert poller.poll_once().kind == "SUBMITTED"
        operation = _terminal(ledger, command["request_id"])
        assert operation["status"] == "BLOCKED" and operation["code"] == "DIRTY_WORKSPACE"
        poller.poll_once()
        assert json.loads(_text(provider, RECEIPT_ID)) == operation["receipt"]
        assert [effect["step_id"] for effect in ledger.get(command["request_id"])["effects"]] == [
            "drive-workspace-terminal-receipt",
        ]
        assert (area / "human-note.txt").read_text() == "leave this untouched"
        assert inspect_workspace(repo, area)["branch"] == "feature/area"
        assert registry.get_workspace(PROJECT, WORKSPACE)["generation"] == 0
    finally:
        engine.shutdown()


def test_uncertain_workspace_receipt_cas_is_never_replayed(tmp_path: Path) -> None:
    ledger, _, _, _, _, head, provider, registration, engine = _setup(tmp_path)
    command = _command(ledger, head)
    provider.put_document(COMMAND_ID, json.dumps(command) + "\n")
    slots = FrozenSlotStore(tmp_path / "frozen")
    publisher = WorkspaceTerminalReceiptPublisher(
        ledger, _attached(), provider, _Registration(registration),
    )
    poller = DrivePoller(
        provider, registration.mailbox(), engine, slots,
        on_observed=lambda observed, source: publisher.publish_if_terminal(
            observed, source, slots,
        ),
    )
    try:
        assert poller.poll_once().kind == "SUBMITTED"
        assert _terminal(ledger, command["request_id"])["status"] == "COMPLETED"
        provider.pre_batch_change = "concurrent owner receipt edit\n"
        from runner_app.adapters.drive_workspace_receipt import WorkspaceReceiptError

        with pytest.raises(WorkspaceReceiptError, match="WORKSPACE_RECEIPT_WRITE_UNCERTAIN"):
            poller.poll_once()
        assert ledger.get(command["request_id"])["reconcile_required"] == 1
        writes = provider.batches.count(RECEIPT_ID)
        with pytest.raises(WorkspaceReceiptError, match="WORKSPACE_RECEIPT_WRITE_UNCERTAIN"):
            poller.poll_once()
        assert provider.batches.count(RECEIPT_ID) == writes == 1
    finally:
        engine.shutdown()


def test_drive_context_export_has_status_receipt_and_no_source_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runner_app.engine.context_route import ExportContextRoute
    from runner_app.engine.runtime import Runtime
    from runner_app.engine.workspace_mailboxes import WorkspaceMailboxManager

    ledger, registry, repo, area, other, head, provider, registration, engine = _setup(tmp_path)
    command = _command(ledger, head)
    command.update(
        action="EXPORT_CONTEXT",
        params={"paths": ["README.md"], "include_diff": False},
        intent="Collect a committed local context fixture",
    )
    before_context = _text(provider, CONTEXT_ID)
    before_state = _text(provider, STATE_ID)
    before_area = inspect_workspace(repo, area)
    before_other = inspect_workspace(repo, other)
    engine.handlers["EXPORT_CONTEXT"] = ExportContextRoute(ledger, registry)
    provider.put_document(COMMAND_ID, json.dumps(command, sort_keys=True) + "\n")
    monkeypatch.setattr(ledger, "completed_provision_project_ids", lambda: [PROJECT])
    monkeypatch.setattr(ledger, "completed_workspace_pairs", lambda: [(PROJECT, WORKSPACE)])
    publisher = WorkspaceTerminalReceiptPublisher(
        ledger, _attached(), provider, _Registration(registration),
    )
    manager = WorkspaceMailboxManager(
        ledger, registry, _Registration(registration), provider, engine,
        tmp_path / "context-frozen", {"python-basic": "python-quick"},
        workspace_receipt=publisher,
    )
    try:
        assert manager.refresh().activated == 1
        local_runtime = object.__new__(Runtime)
        local_runtime.drive_bridge = object()  # type: ignore[assignment]
        local_runtime.integrations = object()  # type: ignore[assignment]
        local_runtime.workspace_mailboxes = manager
        local_runtime.engine = engine
        assert local_runtime.drive_actions() == [
            "CREATE_PROJECT", "EXPORT_CONTEXT", "RETARGET_WORKSPACE",
        ]
        poller = manager._bridges[(PROJECT, WORKSPACE)].poller
        assert poller.poll_once().kind == "SUBMITTED"
        operation = _terminal(ledger, command["request_id"])
        assert operation["status"] == "COMPLETED"
        assert operation["result"]["context"]["delivery_status"] == "LOCAL_ONLY"
        assert poller.poll_once().kind == "ALREADY_OBSERVED"
        receipt = _text(provider, RECEIPT_ID)
        assert json.loads(receipt) == ledger.get(command["request_id"])["receipt"]
        assert "inventory" not in receipt and "selection" not in receipt
        assert _text(provider, CONTEXT_ID) == before_context
        assert _text(provider, STATE_ID) == before_state
        assert inspect_workspace(repo, area) == before_area
        assert inspect_workspace(repo, other) == before_other
        assert len(provider.batches) == 1
        assert len([effect for effect in ledger.get(command["request_id"])["effects"]
                    if effect["step_id"] == "drive-workspace-terminal-receipt"]) == 1
    finally:
        manager.close()
        engine.shutdown()
