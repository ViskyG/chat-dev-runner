"""Fake-backed Docs mailbox tests; no account access, cloud writes or project code."""

from __future__ import annotations

import copy
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from runner_app.adapters.drive_docs import (
    DriveError,
    RegisteredMailbox,
    extract_document,
    read_ready_snapshot,
)
from runner_app.adapters.drive_fake import FakeDrive
from runner_app.domain.commands import LocalPolicy, Provenance
from runner_app.engine.service import Engine, OperationResult
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger

COMMAND_ID = "fake-workspace-command-123456"
CHANGESET_ID = "fake-workspace-changeset-123"
PARENT_ID = "fake-workspace-folder-123456"
HEAD = "a" * 40


def _metadata(file_id: str, *, parent_id: str = PARENT_ID) -> dict:
    return {
        "id": file_id, "name": "COMMAND", "mimeType": "application/vnd.google-apps.document",
        "parents": [parent_id], "trashed": False, "isAppAuthorized": True,
        "capabilities": {"canEdit": True, "canAddChildren": False}, "version": "1",
    }


def _command(*, action: str = "DELIVER_VERIFY_PUBLISH", epoch: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    params = (
        {"changeset_doc_id": CHANGESET_ID, "changeset_revision": "change-rev-1",
         "profile_id": "python-quick", "publication": "never"}
        if action == "DELIVER_VERIFY_PUBLISH" else {"profile_id": "python-quick"}
    )
    return {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": "local-runner", "epoch": epoch or str(uuid4()),
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": action, "project_id": "project", "workspace_id": "workspace",
        "expected_head": HEAD, "params": params, "intent": "One explicit offline fixture request",
    }


def _changeset() -> dict:
    return {
        "schema_version": "1.0", "change_id": str(uuid4()),
        "project_id": "project", "workspace_id": "workspace", "base_head": HEAD,
        "files": [{
            "op": "upsert", "path": "src/hello.py", "expected_sha256": None,
            "content_utf8": "print('hi')\n",
        }],
    }


def workspace_fixture(
    *, command: dict | None = None, changeset: dict | None = None,
) -> tuple[FakeDrive, RegisteredMailbox]:
    provider = FakeDrive()
    provider.put_file(_metadata(COMMAND_ID))
    change_meta = _metadata(CHANGESET_ID)
    change_meta["name"] = "CHANGESET"
    provider.put_file(change_meta)
    provider.put_document(COMMAND_ID, json.dumps(command or _command()), revision="command-rev-1")
    provider.put_document(
        CHANGESET_ID, json.dumps(changeset or _changeset()), revision="change-rev-1",
    )
    return provider, RegisteredMailbox(
        command_id=COMMAND_ID, changeset_id=CHANGESET_ID, parent_id=PARENT_ID,
        project_id="project", workspace_id="workspace",
    )


def test_extracts_complete_single_tab_plain_text() -> None:
    provider, _ = workspace_fixture()
    snapshot = extract_document(
        provider.get_document(COMMAND_ID), COMMAND_ID, max_bytes=262_144,
    )
    assert snapshot.revision_id == "command-rev-1"
    assert snapshot.tab_id == "tab-one"
    assert snapshot.raw_bytes == snapshot.text.encode("utf-8")


@pytest.mark.parametrize("mutation,code", [
    (lambda d: d["tabs"].append(copy.deepcopy(d["tabs"][0])), "UNSUPPORTED_TABS"),
    (lambda d: d["tabs"][0].update({"childTabs": [{"tabProperties": {"tabId": "two"}}]}),
     "UNSUPPORTED_TABS"),
    (lambda d: d["tabs"][0]["documentTab"]["body"]["content"].append({"table": {}}),
     "UNSUPPORTED_DOC_CONTENT"),
    (lambda d: d.pop("revisionId"), "REVISION_MISSING"),
])
def test_unsupported_tabs_tables_and_revision_are_explicit(mutation, code: str) -> None:
    provider, _ = workspace_fixture()
    document = provider.documents[COMMAND_ID]
    mutation(document)
    with pytest.raises(DriveError) as error:
        extract_document(document, COMMAND_ID, max_bytes=262_144)
    assert error.value.code == code


def test_ready_command_and_changeset_freeze_exact_bytes_and_revisions() -> None:
    provider, mailbox = workspace_fixture()
    frozen = read_ready_snapshot(provider, mailbox)
    assert frozen is not None
    assert frozen.command.command["action"] == "DELIVER_VERIFY_PUBLISH"
    assert frozen.command_revision == "command-rev-1"
    assert frozen.changeset_revision == "change-rev-1"
    assert frozen.change_plan is not None
    assert frozen.change_plan.files[0].path == "src/hello.py"
    assert provider.calls.count(("documents.get", COMMAND_ID)) == 3
    assert provider.calls.count(("documents.get", CHANGESET_ID)) == 2
    assert not any(call[0] == "files.list" for call in provider.calls)


def test_draft_is_inert_and_never_fetches_changeset() -> None:
    command = _command()
    command["dispatch"] = "DRAFT"
    provider, mailbox = workspace_fixture(command=command)
    assert read_ready_snapshot(provider, mailbox) is None
    assert ("documents.get", CHANGESET_ID) not in provider.calls


def test_unregistered_changeset_reference_never_gets_fetched() -> None:
    command = _command()
    command["params"]["changeset_doc_id"] = "arbitrary-unregistered-document"
    provider, mailbox = workspace_fixture(command=command)
    with pytest.raises(DriveError) as error:
        read_ready_snapshot(provider, mailbox)
    assert error.value.code == "UNREGISTERED_CHANGESET"
    assert ("documents.get", "arbitrary-unregistered-document") not in provider.calls


def test_changed_changeset_revision_before_first_read_never_freezes() -> None:
    command = _command()
    command["params"]["changeset_revision"] = "obsolete-revision"
    provider, mailbox = workspace_fixture(command=command)
    with pytest.raises(DriveError) as error:
        read_ready_snapshot(provider, mailbox)
    assert error.value.code == "INPUT_CHANGED"


def test_command_change_during_read_is_rejected_before_admission() -> None:
    provider, mailbox = workspace_fixture()

    def mutate(fake: FakeDrive, count: int) -> None:
        if count == 2:
            changed = _command()
            fake.put_document(COMMAND_ID, json.dumps(changed), revision="command-rev-2")

    provider.on_document_read[COMMAND_ID] = mutate
    with pytest.raises(DriveError) as error:
        read_ready_snapshot(provider, mailbox)
    assert error.value.code == "INPUT_CHANGED"


def test_changeset_change_during_final_read_is_rejected() -> None:
    provider, mailbox = workspace_fixture()

    def mutate(fake: FakeDrive, count: int) -> None:
        if count == 2:
            changed = _changeset()
            fake.put_document(CHANGESET_ID, json.dumps(changed), revision="change-rev-2")

    provider.on_document_read[CHANGESET_ID] = mutate
    with pytest.raises(DriveError) as error:
        read_ready_snapshot(provider, mailbox)
    assert error.value.code == "INPUT_CHANGED"


def test_command_change_after_second_read_is_caught_by_final_recheck() -> None:
    provider, mailbox = workspace_fixture()

    def mutate(fake: FakeDrive, count: int) -> None:
        if count == 3:
            changed = _command()
            fake.put_document(COMMAND_ID, json.dumps(changed), revision="command-rev-3")

    provider.on_document_read[COMMAND_ID] = mutate
    with pytest.raises(DriveError) as error:
        read_ready_snapshot(provider, mailbox)
    assert error.value.code == "INPUT_CHANGED"


def test_moved_mailbox_is_refused_even_when_id_still_resolves() -> None:
    provider, mailbox = workspace_fixture()
    provider.files[COMMAND_ID]["parents"] = ["fake-other-parent-123"]
    with pytest.raises(DriveError) as error:
        read_ready_snapshot(provider, mailbox)
    assert error.value.code == "FILE_MOVED"


def test_fake_docs_cas_stale_revision_and_lost_ack_are_observable() -> None:
    provider, _ = workspace_fixture()
    with pytest.raises(DriveError) as error:
        provider.replace_document_text(
            COMMAND_ID, "new", required_revision_id="wrong",
        )
    assert error.value.code == "STALE_REVISION"
    with pytest.raises(OSError):
        provider.replace_document_text(
            COMMAND_ID, "new", required_revision_id="command-rev-1", lose_ack=True,
        )
    assert "new" in provider.get_document(COMMAND_ID)["tabs"][0]["documentTab"]["body"][
        "content"
    ][0]["paragraph"]["elements"][0]["textRun"]["content"]


def test_two_same_polls_admit_once_and_changed_id_body_is_refused(tmp_path: Path) -> None:
    ledger = Ledger(Database(tmp_path / "state.sqlite"), max_parallel=1)
    ledger.startup()
    ledger.arm()
    command = _command(action="VERIFY", epoch=ledger.state()["epoch"])
    provider, mailbox = workspace_fixture(command=command)
    provenance = Provenance(
        kind="drive", source_id=COMMAND_ID, project_id="project", workspace_id="workspace",
    )
    calls: list[str] = []

    def handler(frozen, _context):
        calls.append(frozen.digest)
        return OperationResult()

    policy = LocalPolicy(
        mailboxes={COMMAND_ID: provenance},
        approved_profiles=frozenset({"python-quick"}),
        project_ids=frozenset({"project"}),
        workspace_pairs=frozenset({("project", "workspace")}),
    )
    engine = Engine(ledger, policy=policy, handlers={"VERIFY": handler})
    try:
        first = read_ready_snapshot(provider, mailbox)
        assert first is not None
        admitted = engine.submit(first.raw_command, provenance)
        assert admitted.execute
        second = read_ready_snapshot(provider, mailbox)
        assert second is not None
        duplicate = engine.submit(second.raw_command, provenance)
        assert duplicate.duplicate and not duplicate.execute
        deadline = time.monotonic() + 3
        while ledger.get(command["request_id"])["status"] == "RUNNING" and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(calls) == 1

        changed = copy.deepcopy(command)
        changed["intent"] = "Changed body for same request ID"
        provider.put_document(COMMAND_ID, json.dumps(changed), revision="command-rev-2")
        changed_snapshot = read_ready_snapshot(provider, mailbox)
        assert changed_snapshot is not None
        refusal = engine.submit(changed_snapshot.raw_command, provenance)
        assert refusal.receipt["code"] == "REQUEST_ID_REUSED"
        assert len(calls) == 1
    finally:
        engine.shutdown()


def test_old_epoch_after_restart_is_terminal_refusal(tmp_path: Path) -> None:
    ledger = Ledger(Database(tmp_path / "state.sqlite"), max_parallel=1)
    first_state = ledger.startup()
    ledger.arm()
    command = _command(action="VERIFY", epoch=first_state["epoch"])
    provider, mailbox = workspace_fixture(command=command)
    provenance = Provenance(
        kind="drive", source_id=COMMAND_ID, project_id="project", workspace_id="workspace",
    )
    calls: list[str] = []
    policy = LocalPolicy(
        mailboxes={COMMAND_ID: provenance},
        approved_profiles=frozenset({"python-quick"}),
        project_ids=frozenset({"project"}),
        workspace_pairs=frozenset({("project", "workspace")}),
    )
    engine = Engine(
        ledger, policy=policy,
        handlers={"VERIFY": lambda frozen, _context: (
            calls.append(frozen.digest) or OperationResult()
        )},
    )
    frozen = read_ready_snapshot(provider, mailbox)
    assert frozen is not None
    receipt = engine.submit(frozen.raw_command, provenance)
    assert receipt.receipt["code"] == "WRONG_RUNNER_EPOCH"
    assert not calls
    engine.shutdown()
