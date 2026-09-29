"""Fake Drive mailbox proof that delivery commands get exact terminal receipts."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from test_ep07_mailbox_preflight import preflight
from test_project_mailbox_receipt import _attached
from test_retarget_route import (
    COMMAND_ID,
    PROJECT,
    RECEIPT_ID,
    WORKSPACE,
    _command,
    _Registration,
    _setup,
    _terminal,
    _text,
)

from runner_app.adapters.drive_docs import read_document
from runner_app.adapters.drive_workspace_receipt import (
    WorkspaceReceiptError,
    WorkspaceTerminalReceiptPublisher,
)
from runner_app.engine.service import Context, OperationResult
from runner_app.engine.workspace_mailboxes import WorkspaceMailboxManager
from runner_app.storage.ledger import Ledger


def _dvp_command(ledger: Ledger, head: str, changeset_id: str) -> dict:
    command = _command(ledger, head)
    command["action"] = "DELIVER_VERIFY_PUBLISH"
    command["params"] = {
        "changeset_doc_id": changeset_id, "changeset_revision": "change-rev-1",
        "profile_id": "python-quick", "publication": "never",
    }
    return command


def _manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: Ledger,
             registry, provider, registration, engine) -> WorkspaceMailboxManager:
    monkeypatch.setattr(ledger, "completed_provision_project_ids", lambda: [PROJECT])
    monkeypatch.setattr(ledger, "completed_workspace_pairs", lambda: [(PROJECT, WORKSPACE)])
    manager = WorkspaceMailboxManager(
        ledger, registry, _Registration(registration), provider, engine,
        tmp_path / "frozen-delivery", {"python-basic": "python-quick"},
        workspace_receipt=WorkspaceTerminalReceiptPublisher(
            ledger, _attached(), provider, _Registration(registration),
        ),
    )
    assert manager.refresh().activated == 1
    return manager


def test_full_delivery_replaces_interim_receipt_with_digest_matched_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, registry, _, _, _, head, provider, registration, engine = _setup(tmp_path)
    changeset = {
        "schema_version": "1.0", "change_id": str(uuid4()),
        "project_id": PROJECT, "workspace_id": WORKSPACE, "base_head": head,
        "files": [{"op": "upsert", "path": "src/hello.py",
                   "expected_sha256": None, "content_utf8": "print('hello')\n"}],
    }
    provider.add(registration.changeset_doc_id, registration.workspace_folder_id,
                 "workspace-changeset", json.dumps(changeset))
    provider.put_document(registration.changeset_doc_id, json.dumps(changeset),
                          revision="change-rev-1")
    command = _dvp_command(ledger, head, registration.changeset_doc_id)
    provider.put_document(COMMAND_ID, json.dumps(command) + "\n")
    key = f"workspace-receipt:{PROJECT}/{WORKSPACE}"

    def deliver(frozen, context: Context) -> OperationResult:
        interim = "CHAT-DEV RUNNER / RECEIPT\n" + json.dumps({
            "request_id": frozen.command["request_id"],
            "delivery_status": "DELIVERED",
        }) + "\n"
        observed = read_document(provider, RECEIPT_ID, max_bytes=65536)
        effect_id = context.ledger.begin_effect(
            frozen.command["request_id"],
            "drive-doc:" + hashlib.sha256(key.encode()).hexdigest(),
            "drive_file", {"file_id": RECEIPT_ID, "key": key,
                           "base_revision": observed.revision_id,
                           "desired_sha256": hashlib.sha256(interim.encode()).hexdigest()},
        )
        provider.put_document(RECEIPT_ID, interim)
        context.ledger.confirm_effect(effect_id, RECEIPT_ID)
        return OperationResult("COMPLETED")

    engine.handlers["DELIVER_VERIFY_PUBLISH"] = deliver
    manager = _manager(tmp_path, monkeypatch, ledger, registry, provider,
                       registration, engine)
    try:
        poller = manager._bridges[(PROJECT, WORKSPACE)].poller
        assert poller.poll_once().kind == "SUBMITTED"
        operation = _terminal(ledger, command["request_id"])
        assert operation["status"] == "COMPLETED"
        assert poller.poll_once().kind == "ALREADY_OBSERVED"
        assert json.loads(_text(provider, RECEIPT_ID)) == operation["receipt"]
        assert operation["reconcile_required"] == 0
        maintenance = preflight.summarize_mailbox(
            provider, registration.mailbox(), scope="workspace",
            now=datetime.now(timezone.utc),
        )
        assert maintenance["matching_receipt_status"] == "COMPLETED"
        assert maintenance["maintenance_pending"] is False
        writes = list(provider.batches)
        assert poller.poll_once().kind == "ALREADY_OBSERVED"
        assert provider.batches == writes
    finally:
        manager.close()
        engine.shutdown()


def test_report_retry_without_source_run_still_gets_terminal_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, registry, _, _, _, head, provider, registration, engine = _setup(tmp_path)
    command = _command(ledger, head)
    command.update({"action": "PUBLISH_REPORT", "expected_head": None,
                    "params": {"source_run_id": str(uuid4())}})
    provider.put_document(COMMAND_ID, json.dumps(command) + "\n")
    engine.handlers["PUBLISH_REPORT"] = lambda *_: OperationResult("COMPLETED")
    manager = _manager(tmp_path, monkeypatch, ledger, registry, provider,
                       registration, engine)
    try:
        poller = manager._bridges[(PROJECT, WORKSPACE)].poller
        assert poller.poll_once().kind == "SUBMITTED"
        operation = ledger.get(command["request_id"])
        assert operation is not None
        assert operation["status"] == "REJECTED"
        assert operation["code"] == "SOURCE_RUN_MISSING"
        assert json.loads(_text(provider, RECEIPT_ID)) == operation["receipt"]
        assert operation["effects"][0]["step_id"] == "drive-workspace-terminal-receipt"
    finally:
        manager.close()
        engine.shutdown()


def test_uncertain_interim_receipt_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, registry, _, _, _, head, provider, registration, engine = _setup(tmp_path)
    changeset = {
        "schema_version": "1.0", "change_id": str(uuid4()),
        "project_id": PROJECT, "workspace_id": WORKSPACE, "base_head": head,
        "files": [{"op": "upsert", "path": "src/hello.py",
                   "expected_sha256": None, "content_utf8": "print('hello')\n"}],
    }
    provider.add(registration.changeset_doc_id, registration.workspace_folder_id,
                 "workspace-changeset", json.dumps(changeset))
    provider.put_document(registration.changeset_doc_id, json.dumps(changeset),
                          revision="change-rev-1")
    command = _dvp_command(ledger, head, registration.changeset_doc_id)
    provider.put_document(COMMAND_ID, json.dumps(command) + "\n")
    before = _text(provider, RECEIPT_ID)
    key = f"workspace-receipt:{PROJECT}/{WORKSPACE}"

    def uncertain(frozen, context: Context) -> OperationResult:
        context.ledger.begin_effect(
            frozen.command["request_id"],
            "drive-doc:" + hashlib.sha256(key.encode()).hexdigest(),
            "drive_file", {"file_id": RECEIPT_ID, "key": key,
                           "base_revision": "old-revision",
                           "desired_sha256": "a" * 64},
        )
        return OperationResult("BLOCKED", "DELIVERY_DOC_UNCERTAIN")

    engine.handlers["DELIVER_VERIFY_PUBLISH"] = uncertain
    manager = _manager(tmp_path, monkeypatch, ledger, registry, provider,
                       registration, engine)
    try:
        poller = manager._bridges[(PROJECT, WORKSPACE)].poller
        assert poller.poll_once().kind == "SUBMITTED"
        operation = _terminal(ledger, command["request_id"])
        assert operation["reconcile_required"] == 1
        with pytest.raises(WorkspaceReceiptError, match="WORKSPACE_RECEIPT_PRIOR_WRITE_UNCERTAIN"):
            poller.poll_once()
        assert _text(provider, RECEIPT_ID) == before
        assert provider.batches.count(RECEIPT_ID) == 0
    finally:
        manager.close()
        engine.shutdown()
