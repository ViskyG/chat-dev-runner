"""Fake-backed project COMMAND and exact terminal PROJECT RECEIPT round trips."""
from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from runner_app.adapters.drive_docs import DriveError, read_ready_snapshot
from runner_app.adapters.drive_fake import FakeDrive
from runner_app.adapters.drive_index import (
    AttachedInstallation,
    ExistingInstallation,
    IndexedFile,
    InstallationIndex,
)
from runner_app.adapters.drive_poller import DrivePoller, FrozenSlotStore
from runner_app.adapters.drive_project_receipt import (
    ProjectReceiptError,
    ProjectTerminalReceiptPublisher,
)
from runner_app.adapters.drive_registry import WorkspaceDriveRegistration
from runner_app.domain.commands import LocalPolicy, Provenance, freeze
from runner_app.engine.service import Engine, OperationResult
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger

_PROJECT = "toy"
_PROJECT_FOLDER = "projectfolder001"
_WORKSPACE_FOLDER = "workspacefolder001"
_PROJECT_COMMAND = "projectcommand001"
_PROJECT_RECEIPT = "projectreceipt001"
_HEAD = "a" * 40
_INSTALLATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_CONTROL = "controlfolder001"
_ROOT_RECEIPT = "rootreceipt001"
_ROOT_STATE = "rootstate001"


def _document(file_id: str, text: str, revision: str) -> dict[str, Any]:
    position = 1
    content: list[dict[str, Any]] = [
        {"sectionBreak": {}, "startIndex": 0, "endIndex": 1},
    ]
    for line in text.splitlines(keepends=True):
        length = len(line.encode("utf-16-le")) // 2
        content.append({
            "startIndex": position, "endIndex": position + length,
            "paragraph": {"elements": [{
                "startIndex": position, "endIndex": position + length,
                "textRun": {"content": line},
            }]},
        })
        position += length
    return {
        "documentId": file_id, "revisionId": revision,
        "tabs": [{
            "tabProperties": {"tabId": "tab-one"}, "childTabs": [],
            "documentTab": {"body": {"content": content}},
        }],
    }


class _Docs(FakeDrive):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[str] = []
        self.pre_batch_change: str | None = None
        self.lose_ack = False

    def add(self, file_id: str, parent_id: str, app_key: str, text: str) -> None:
        self.put_file({
            "id": file_id, "name": app_key, "trashed": False,
            "mimeType": "application/vnd.google-apps.document",
            "parents": [parent_id], "isAppAuthorized": True,
            "capabilities": {"canEdit": True},
            "appProperties": {
                "cdr_installation": _INSTALLATION, "cdr_key": app_key,
                "cdr_project": _PROJECT,
            },
        })
        self.put_document(file_id, text)

    def put_document(self, file_id: str, text: str, *, revision: str | None = None,
                     tab_id: str = "tab-one") -> None:
        del tab_id
        with self._lock:
            version = revision or f"rev-{self._next_revision}"
            self._next_revision += 1
            self.documents[file_id] = _document(file_id, text, version)

    def batch_update_document(
        self, file_id: str, *, requests: Sequence[Mapping[str, Any]],
        required_revision_id: str,
    ) -> Mapping[str, Any]:
        self.batches.append(file_id)
        if self.pre_batch_change is not None:
            self.put_document(file_id, self.pre_batch_change)
            self.pre_batch_change = None
        if self.documents[file_id]["revisionId"] != required_revision_id:
            raise DriveError("STALE_REVISION", "Fake required revision failed")
        text = ""
        for request in requests:
            if "insertText" in request:
                text = cast(str, request["insertText"]["text"])
        revision = self.replace_document_text(
            file_id, text + "\n", required_revision_id=required_revision_id,
            lose_ack=self.lose_ack,
        )
        return {"writeControl": {"requiredRevisionId": revision}}


def _attached() -> AttachedInstallation:
    mime = "application/vnd.google-apps.document"
    files = {
        "receipt": IndexedFile("receipt", _ROOT_RECEIPT, "RECEIPT", mime,
                               (_CONTROL,), {}),
        "state": IndexedFile("state", _ROOT_STATE, "SYSTEM STATE", mime,
                             (_CONTROL,), {}),
        "control": IndexedFile("control", _CONTROL, "00 Control",
                               "application/vnd.google-apps.folder", (), {}),
        "command": IndexedFile("command", "rootcommand001", "COMMAND", mime,
                               (_CONTROL,), {}),
    }
    return AttachedInstallation(
        ExistingInstallation("rootfolder001", "indexdocument001", "Chat-Dev Runner"),
        InstallationIndex(_INSTALLATION, files), "index-rev", "index-sha", {},
    )


class _Registry:
    def __init__(self, registration: WorkspaceDriveRegistration) -> None:
        self.registration = registration
        self.calls = 0

    def get(self, project_id: str, workspace_id: str) -> WorkspaceDriveRegistration:
        assert (project_id, workspace_id) == (_PROJECT, "primary")
        self.calls += 1
        return self.registration


def _command(ledger: Ledger, *, dispatch: str = "READY") -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    state = ledger.state()
    return {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": dispatch,
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "CREATE_WORKSPACE", "project_id": _PROJECT,
        "workspace_id": "experiment", "expected_head": _HEAD,
        "params": {"name": "Experiment", "branch": "feature/experiment",
                   "base_commit": _HEAD, "profile_id": "python-quick"},
        "intent": "SECRET never publish this text",
    }


def _setup(tmp_path: Path, *, command: dict[str, Any] | None = None,
           on_receipt: bool = True,
           ) -> tuple[Ledger, _Docs, Engine, DrivePoller,
                      ProjectTerminalReceiptPublisher, FrozenSlotStore,
                      dict[str, Any]]:
    ledger = Ledger(Database(tmp_path / "state" / "ledger.sqlite"))
    ledger.startup()
    ledger.arm()
    command = command or _command(ledger)
    provider, attached = _Docs(), _attached()
    registration = WorkspaceDriveRegistration(
        attached.index.installation_id, _PROJECT, "primary",
        _PROJECT_FOLDER, _WORKSPACE_FOLDER,
        "workspacecommand001", "workspacechangeset001",
        "workspacereceipt001", "workspacelatest001",
        "projectstate001", "sourcecontext001",
        _PROJECT_COMMAND, _PROJECT_RECEIPT,
    )
    provider.add(
        _PROJECT_COMMAND, _PROJECT_FOLDER, "project-command",
        json.dumps(command, sort_keys=True) + "\n",
    )
    provider.add(_PROJECT_RECEIPT, _PROJECT_FOLDER, "project-receipt", "previous\n")
    source = Provenance("drive", _PROJECT_COMMAND, _PROJECT)
    policy = LocalPolicy(
        mailboxes={_PROJECT_COMMAND: source},
        approved_profiles=frozenset({"python-quick"}),
        project_ids=frozenset({_PROJECT}),
    )
    handler = lambda _frozen, _context: OperationResult(
        "COMPLETED", result={"private": "SECRET handler output"},
    )
    engine = Engine(ledger, policy=policy, handlers={
        "CREATE_WORKSPACE": handler, "FETCH": handler,
    })
    slots = FrozenSlotStore(tmp_path / "frozen")
    publisher = ProjectTerminalReceiptPublisher(
        ledger, attached, provider, cast(Any, _Registry(registration)),
    )
    def on_observed(snapshot: Any, provenance: Provenance) -> None:
        publisher.publish_if_terminal(snapshot, provenance, slots)

    poller = DrivePoller(
        provider, registration.project_mailbox(), engine, slots,
        on_observed=on_observed if on_receipt else None,
    )
    return ledger, provider, engine, poller, publisher, slots, command


def _terminal(ledger: Ledger, request_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        operation = ledger.get(request_id)
        assert operation is not None
        if operation["status"] != "RUNNING":
            return operation
        time.sleep(0.01)
    raise AssertionError("fake worker did not finish")


def _published(provider: _Docs, ledger: Ledger, request_id: str) -> dict[str, Any]:
    operation = ledger.get(request_id)
    assert operation is not None
    text = provider.documents[_PROJECT_RECEIPT]["tabs"][0]["documentTab"]["body"]["content"]
    observed = "".join(
        run["textRun"]["content"]
        for paragraph in text if "paragraph" in paragraph
        for run in paragraph["paragraph"]["elements"]
    )
    published = json.loads(observed)
    assert published == operation["receipt"]
    assert "SECRET" not in observed
    return operation


def test_terminal_receipt_matches_committed_success_and_is_idempotent(tmp_path: Path) -> None:
    ledger, provider, engine, poller, _, _, command = _setup(tmp_path)
    try:
        assert poller.poll_once().kind == "SUBMITTED"
        assert _terminal(ledger, command["request_id"])["status"] == "COMPLETED"
        assert poller.poll_once().kind == "ALREADY_OBSERVED"
        operation = _published(provider, ledger, command["request_id"])
        assert operation["reconcile_required"] == 0
        effects = [item for item in operation["effects"]
                   if item["step_id"] == "drive-project-terminal-receipt"]
        assert len(effects) == 1 and effects[0]["state"] == "CONFIRMED"
        assert provider.batches == [_PROJECT_RECEIPT]
        assert poller.poll_once().kind == "ALREADY_OBSERVED"
        assert len(provider.batches) == 1
    finally:
        engine.shutdown()


@pytest.mark.parametrize("reason", ["WRONG_RUNNER_EPOCH", "BUSY"])
def test_admission_refusal_receives_matching_terminal_project_receipt(
    tmp_path: Path, reason: str,
) -> None:
    ledger, provider, engine, poller, _, _, command = _setup(tmp_path)
    try:
        if reason == "WRONG_RUNNER_EPOCH":
            command["epoch"] = str(uuid4())
            provider.put_document(_PROJECT_COMMAND, json.dumps(command) + "\n")
        else:
            blocker = _command(ledger)
            assert ledger.admit(
                blocker, freeze(blocker).digest, "local-owner",
                ("repo:toy",),
            ).execute
        assert poller.poll_once().kind == "SUBMITTED"
        operation = _published(provider, ledger, command["request_id"])
        assert operation["status"] == "REJECTED"
        assert operation["code"] == reason
        assert operation["reconcile_required"] == 0
        assert poller.poll_once().kind == "ALREADY_OBSERVED"
        assert len(provider.batches) == 1
    finally:
        engine.shutdown()


def test_draft_is_inert_and_does_not_create_a_receipt_intent(tmp_path: Path) -> None:
    ledger, provider, engine, poller, _, _, command = _setup(tmp_path)
    try:
        command["dispatch"] = "DRAFT"
        provider.put_document(_PROJECT_COMMAND, json.dumps(command) + "\n")
        assert poller.poll_once().kind == "IDLE"
        assert ledger.get(command["request_id"]) is None
        assert provider.batches == []
    finally:
        engine.shutdown()


def test_lost_ack_is_confirmed_by_exact_readback_without_replay(tmp_path: Path) -> None:
    ledger, provider, engine, poller, _, _, command = _setup(tmp_path)
    try:
        provider.lose_ack = True
        assert poller.poll_once().kind == "SUBMITTED"
        _terminal(ledger, command["request_id"])
        assert poller.poll_once().kind == "ALREADY_OBSERVED"
        operation = _published(provider, ledger, command["request_id"])
        assert operation["reconcile_required"] == 0
        assert len(provider.batches) == 1
    finally:
        engine.shutdown()


def test_uncertain_cas_is_not_replayed(tmp_path: Path) -> None:
    ledger, provider, engine, poller, _, _, command = _setup(tmp_path)
    try:
        provider.pre_batch_change = "external owner change\n"
        assert poller.poll_once().kind == "SUBMITTED"
        _terminal(ledger, command["request_id"])
        with pytest.raises(ProjectReceiptError) as first:
            poller.poll_once()
        assert first.value.code == "PROJECT_RECEIPT_WRITE_UNCERTAIN"
        assert len(provider.batches) == 1
        with pytest.raises(ProjectReceiptError) as second:
            poller.poll_once()
        assert second.value.code == "PROJECT_RECEIPT_WRITE_UNCERTAIN"
        assert len(provider.batches) == 1
        operation = ledger.get(command["request_id"])
        assert operation is not None
        assert operation["reconcile_required"] == 1
    finally:
        engine.shutdown()


def test_changed_command_revision_prevents_receipt_write(tmp_path: Path) -> None:
    ledger, provider, engine, poller, publisher, slots, command = _setup(
        tmp_path, on_receipt=False,
    )
    try:
        assert poller.poll_once().kind == "SUBMITTED"
        _terminal(ledger, command["request_id"])
        original = read_ready_snapshot(provider, poller.mailbox)
        assert original is not None
        provider.put_document(_PROJECT_COMMAND, json.dumps(command) + "\n")
        with pytest.raises(ProjectReceiptError) as error:
            publisher.publish_if_terminal(original, poller.provenance, slots)
        assert error.value.code == "PROJECT_RECEIPT_INPUT_CHANGED"
        assert provider.batches == []
    finally:
        engine.shutdown()


def test_project_fetch_uses_same_registered_terminal_receipt(tmp_path: Path) -> None:
    ledger, provider, engine, poller, _, _, command = _setup(tmp_path)
    try:
        command.update(action="FETCH", workspace_id=None, expected_head=None, params={})
        provider.put_document(_PROJECT_COMMAND, json.dumps(command) + "\n")
        assert poller.poll_once().kind == "SUBMITTED"
        _terminal(ledger, command["request_id"])
        assert poller.poll_once().kind == "ALREADY_OBSERVED"
        operation = _published(provider, ledger, command["request_id"])
        assert operation["status"] == "COMPLETED"
        assert operation["action"] == "FETCH"
    finally:
        engine.shutdown()
