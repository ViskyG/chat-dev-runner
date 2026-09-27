"""Offline, exact-ID root CREATE_PROJECT receipt publishing."""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from runner_app.adapters.drive_index import (
    AttachedInstallation,
    ExistingInstallation,
    IndexedFile,
    InstallationIndex,
)
from runner_app.adapters.drive_root_receipt import (
    RootCreateProjectReceiptPublisher,
    RootReceiptError,
)
from runner_app.adapters.drive_writer import DocsCASWriter
from runner_app.domain.commands import freeze
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger

_INSTALLATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_ROOT = "rootfolderid0000001"
_INDEX = "indexdocument000001"
_CONTROL = "controlfolder000001"
_COMMAND = "commanddocument001"
_RECEIPT = "receiptdocument001"
_STATE = "statedocument00001"
_DOC = "application/vnd.google-apps.document"
_FOLDER = "application/vnd.google-apps.folder"
_PROJECT = "fixture"


def _units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _document(file_id: str, text: str, revision: int) -> dict[str, Any]:
    assert text.endswith("\n")
    position = 1
    content: list[dict[str, Any]] = [
        {"sectionBreak": {}, "startIndex": 0, "endIndex": 1},
    ]
    for line in text.splitlines(keepends=True):
        end = position + _units(line)
        content.append({
            "startIndex": position, "endIndex": end,
            "paragraph": {"elements": [{
                "startIndex": position, "endIndex": end,
                "textRun": {"content": line},
            }]},
        })
        position = end
    return {
        "documentId": file_id, "revisionId": f"rev-{revision}",
        "tabs": [{
            "tabProperties": {"tabId": "tab-one"}, "childTabs": [],
            "documentTab": {"body": {"content": content}},
        }],
    }


class FakeRootDocs:
    def __init__(self) -> None:
        self.metadata: dict[str, dict[str, Any]] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.batch_ids: list[str] = []
        self.revision = 1
        self.lose_ack_once = False
        self.fail_before_once = False

    def add(self, file_id: str, name: str, key: str, text: str) -> None:
        self.metadata[file_id] = {
            "id": file_id, "name": name, "mimeType": _DOC,
            "parents": [_CONTROL], "trashed": False, "isAppAuthorized": True,
            "capabilities": {"canEdit": True, "canAddChildren": False},
            "appProperties": {
                "cdr_installation": _INSTALLATION, "cdr_key": key,
            },
        }
        self.documents[file_id] = _document(file_id, text, self.revision)

    def set_document(self, file_id: str, text: str) -> None:
        self.revision += 1
        self.documents[file_id] = _document(file_id, text, self.revision)

    def get_file(self, file_id: str, *, fields: str = "") -> Mapping[str, Any]:
        assert "appProperties" in fields
        self.calls.append(("files.get", file_id))
        return copy.deepcopy(self.metadata[file_id])

    def get_document(self, file_id: str, *, include_tabs_content: bool = True
                     ) -> Mapping[str, Any]:
        assert include_tabs_content
        self.calls.append(("documents.get", file_id))
        return copy.deepcopy(self.documents[file_id])

    def batch_update_document(
        self, file_id: str, *, requests: Sequence[Mapping[str, Any]],
        required_revision_id: str,
    ) -> Mapping[str, Any]:
        self.calls.append(("documents.batchUpdate", file_id))
        self.batch_ids.append(file_id)
        if self.fail_before_once:
            self.fail_before_once = False
            raise OSError("sensitive fake provider error")
        assert file_id == _RECEIPT
        assert self.documents[file_id]["revisionId"] == required_revision_id
        old = self.documents[file_id]
        old_end = old["tabs"][0]["documentTab"]["body"]["content"][-1]["endIndex"]
        cursor = 0
        if "deleteContentRange" in requests[0]:
            assert requests[0]["deleteContentRange"]["range"] == {
                "startIndex": 1, "endIndex": old_end - 1, "tabId": "tab-one",
            }
            cursor = 1
        inserted = requests[cursor]["insertText"]["text"] if cursor < len(requests) else ""
        if cursor < len(requests):
            assert requests[cursor]["insertText"]["location"] == {
                "index": 1, "tabId": "tab-one",
            }
        self.set_document(file_id, inserted + "\n")
        revision = self.documents[file_id]["revisionId"]
        if self.lose_ack_once:
            self.lose_ack_once = False
            raise OSError("sensitive fake lost acknowledgement")
        return {"writeControl": {"requiredRevisionId": revision}}


def _command(ledger: Ledger, *, source_kind: str = "new") -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    state = ledger.state()
    return {
        "schema_version": "1.0", "request_id": str(uuid4()),
        "dispatch": "READY", "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "CREATE_PROJECT", "project_id": _PROJECT,
        "workspace_id": None, "expected_head": None,
        "params": {
            "name": "Fixture",
            "source": ({"kind": "github", "repository": "alice/source"}
                       if source_kind == "github" else
                       {"kind": "local", "import_handle": "f" * 48}
                       if source_kind == "local" else {"kind": "new"}),
            "template_id": "python-basic", "github_identity": "personal",
            "visibility": "private",
        },
        "intent": "SECRET command intent must never reach the receipt",
    }


def _resources() -> dict[str, Any]:
    head = "a" * 40
    drive = {
        key: f"resourceid{index:03d}"
        for index, key in enumerate((
            "project_folder_id", "workspace_folder_id", "project_state_doc_id",
            "project_command_doc_id", "project_receipt_doc_id", "context_doc_id",
            "command_doc_id", "changeset_doc_id", "receipt_doc_id",
            "latest_result_doc_id",
        ))
    }
    return {
        "project_id": _PROJECT, "workspace_id": "primary",
        "template_id": "python-basic", "profile_id": "python-quick",
        "github": {
            "repository_id": 42, "owner": "ViskyG", "name": "fixture",
            "full_name": "ViskyG/fixture",
            "html_url": "https://github.com/ViskyG/fixture",
            "private": True, "clone_url": "https://token-secret@github.com/ViskyG/fixture",
        },
        "local": {"repo_path": "/private/secret/local/path"},
        "baseline": {"ref": "refs/heads/main", "commit": head},
        "workspace": {"path": "/private/secret/worktree", "branch": "work/fixture",
                      "head": head},
        "drive": drive, "project_state": "READY",
        "github_for_chat": "SECRET unexpected field",
    }


def _setup(tmp_path: Path, *, drive_source: bool = True,
           source_kind: str = "new"
           ) -> tuple[Ledger, FakeRootDocs, RootCreateProjectReceiptPublisher, dict[str, Any]]:
    ledger = Ledger(Database(tmp_path / "state" / "ledger.sqlite"))
    ledger.startup()
    ledger.arm()
    command = _command(ledger, source_kind=source_kind)
    api = FakeRootDocs()
    api.add(_COMMAND, "COMMAND", "command", json.dumps(command) + "\n")
    api.add(_RECEIPT, "RECEIPT", "receipt", "old receipt\n")
    api.add(_STATE, "SYSTEM STATE", "state", "old state\n")
    files = {
        "command": IndexedFile("command", _COMMAND, "COMMAND", _DOC, (_CONTROL,), {}),
        "receipt": IndexedFile("receipt", _RECEIPT, "RECEIPT", _DOC, (_CONTROL,), {}),
        "state": IndexedFile("state", _STATE, "SYSTEM STATE", _DOC, (_CONTROL,), {}),
        "control": IndexedFile("control", _CONTROL, "00 Control", _FOLDER, (_ROOT,), {}),
    }
    attached = AttachedInstallation(
        ExistingInstallation(_ROOT, _INDEX, "Chat-Dev Runner"),
        InstallationIndex(_INSTALLATION, files), "rev-1", "a" * 64, {},
    )
    writer = DocsCASWriter(api, attached)
    assert ledger.admit(
        command, freeze(command).digest,
        _COMMAND if drive_source else "local-ui",
        (f"repo:{_PROJECT}", "global:owner", "global:drive-installation"),
    ).execute
    return ledger, api, RootCreateProjectReceiptPublisher(
        ledger, attached, api, writer, trusted_github_owner="ViskyG",
    ), command


def _effects(ledger: Ledger, request_id: str) -> list[dict[str, Any]]:
    operation = ledger.get(request_id)
    assert operation is not None
    return cast(list[dict[str, Any]], operation["effects"])


def _code(call: Any, code: str) -> None:
    with pytest.raises(RootReceiptError) as error:
        call()
    assert error.value.code == code


def test_drive_create_receipt_is_sanitized_journaled_exact_and_idempotent(
    tmp_path: Path,
) -> None:
    ledger, api, publisher, command = _setup(tmp_path)
    before_command = copy.deepcopy(api.documents[_COMMAND])
    assert publisher.publish(command["request_id"], resources=_resources()) == _RECEIPT
    assert api.documents[_COMMAND] == before_command
    assert api.batch_ids == [_RECEIPT]
    text = "".join(
        run["textRun"]["content"]
        for item in api.documents[_RECEIPT]["tabs"][0]["documentTab"]["body"]["content"]
        for run in item.get("paragraph", {}).get("elements", [])
    )
    receipt = json.loads(text)
    assert receipt["request_id"] == command["request_id"]
    assert receipt["request_digest"] == freeze(command).digest
    assert receipt["status"] == "COMPLETED" and receipt["code"] is None
    assert receipt["run_id"] is None
    assert "https://github.com/ViskyG/fixture" in receipt["message"]
    assert "project_command_doc_id" in receipt["message"]
    for secret in ("SECRET", "token-secret", "/private/"):
        assert secret not in text
    effects = _effects(ledger, command["request_id"])
    assert len(effects) == 1
    assert effects[0]["step_id"] == "drive-root-create-receipt"
    assert effects[0]["state"] == "CONFIRMED"
    assert effects[0]["provider_id"] == _RECEIPT
    assert effects[0]["intent"]["desired_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert publisher.publish(command["request_id"], resources=_resources()) == _RECEIPT
    assert api.batch_ids == [_RECEIPT]




def test_new_receipt_rejects_alias_as_repository_owner(tmp_path: Path) -> None:
    ledger, api, publisher, command = _setup(tmp_path)
    resources = _resources()
    resources["github"].update({
        "owner": "personal", "full_name": "personal/fixture",
        "html_url": "https://github.com/personal/fixture",
    })
    _code(lambda: publisher.publish(command["request_id"], resources=resources),
          "ROOT_RECEIPT_RESOURCES_INVALID")
    assert api.batch_ids == []
    assert _effects(ledger, command["request_id"]) == []

def test_import_receipt_uses_exact_approved_source_and_default_ref(tmp_path: Path) -> None:
    ledger, api, publisher, command = _setup(tmp_path, source_kind="github")
    resources = _resources()
    resources["github"].update({
        "owner": "alice", "name": "source", "full_name": "alice/source",
        "html_url": "https://github.com/alice/source",
        "default_branch": "trunk",
    })
    resources["baseline"]["ref"] = "refs/heads/trunk"
    assert publisher.publish(command["request_id"], resources=resources) == _RECEIPT
    text = str(api.documents[_RECEIPT])
    assert "https://github.com/alice/source" in text
    assert "refs/heads/trunk" in text
    assert "https://github.com/ViskyG/fixture" not in text
    assert _effects(ledger, command["request_id"])[0]["state"] == "CONFIRMED"


def test_import_receipt_rejects_alias_or_project_substitution_for_source(
    tmp_path: Path,
) -> None:
    ledger, api, publisher, command = _setup(tmp_path, source_kind="github")
    resources = _resources()
    resources["github"]["default_branch"] = "trunk"
    resources["baseline"]["ref"] = "refs/heads/trunk"
    _code(lambda: publisher.publish(command["request_id"], resources=resources),
          "ROOT_RECEIPT_RESOURCES_INVALID")
    assert _effects(ledger, command["request_id"]) == []
    assert api.batch_ids == []

def test_local_import_receipt_uses_target_and_source_ref_without_handle_or_paths(
    tmp_path: Path,
) -> None:
    ledger, api, publisher, command = _setup(tmp_path, source_kind="local")
    resources = _resources()
    resources["github"]["source_branch"] = "feature/imported"
    resources["baseline"]["source_ref"] = "refs/heads/feature/imported"
    assert publisher.publish(command["request_id"], resources=resources) == _RECEIPT
    text = str(api.documents[_RECEIPT])
    assert "https://github.com/ViskyG/fixture" in text
    assert "refs/heads/feature/imported" in text
    assert "f" * 48 not in text
    assert "/private/" not in text and "SECRET" not in text
    assert _effects(ledger, command["request_id"])[0]["state"] == "CONFIRMED"


def test_local_import_receipt_rejects_wrong_source_ref(tmp_path: Path) -> None:
    ledger, api, publisher, command = _setup(tmp_path, source_kind="local")
    resources = _resources()
    resources["github"]["source_branch"] = "feature/imported"
    resources["baseline"]["source_ref"] = "refs/heads/other"
    _code(lambda: publisher.publish(command["request_id"], resources=resources),
          "ROOT_RECEIPT_RESOURCES_INVALID")
    assert _effects(ledger, command["request_id"]) == []
    assert api.batch_ids == []


def test_local_create_returns_none_without_any_drive_access(tmp_path: Path) -> None:
    ledger, api, publisher, command = _setup(tmp_path, drive_source=False)
    assert publisher.publish(command["request_id"]) is None
    assert api.calls == []
    assert _effects(ledger, command["request_id"]) == []


def test_changed_ready_command_digest_prevents_root_receipt_write(tmp_path: Path) -> None:
    ledger, api, publisher, command = _setup(tmp_path)
    changed = {**command, "intent": "changed content"}
    api.set_document(_COMMAND, json.dumps(changed) + "\n")
    _code(lambda: publisher.publish(command["request_id"], resources=_resources()),
          "ROOT_COMMAND_MISMATCH")
    assert api.batch_ids == []
    assert _effects(ledger, command["request_id"]) == []


def test_lost_ack_after_applied_write_confirms_by_exact_readback(tmp_path: Path) -> None:
    ledger, api, publisher, command = _setup(tmp_path)
    api.lose_ack_once = True
    assert publisher.publish(command["request_id"], resources=_resources()) == _RECEIPT
    assert api.batch_ids == [_RECEIPT]
    effect = _effects(ledger, command["request_id"])[0]
    assert effect["state"] == "CONFIRMED"
    assert publisher.publish(command["request_id"], resources=_resources()) == _RECEIPT
    assert api.batch_ids == [_RECEIPT]


def test_unapplied_uncertain_write_stays_pending_and_never_retries(tmp_path: Path) -> None:
    ledger, api, publisher, command = _setup(tmp_path)
    api.fail_before_once = True
    _code(lambda: publisher.publish(command["request_id"], resources=_resources()),
          "ROOT_RECEIPT_WRITE_UNCERTAIN")
    effect = _effects(ledger, command["request_id"])[0]
    assert effect["state"] == "INTENDED"
    assert api.batch_ids == [_RECEIPT]
    _code(lambda: publisher.publish(command["request_id"], resources=_resources()),
          "ROOT_RECEIPT_WRITE_UNCERTAIN")
    assert api.batch_ids == [_RECEIPT]



def test_completed_receipt_refuses_unresolved_provisioning_effect(tmp_path: Path) -> None:
    ledger, api, publisher, command = _setup(tmp_path)
    ledger.begin_effect(
        command["request_id"], "github-repository", "github_repository",
        {"marker": "durable-create-intent"},
    )
    _code(lambda: publisher.publish(command["request_id"], resources=_resources()),
          "ROOT_RECEIPT_EFFECTS_UNRESOLVED")
    assert api.calls == []
    assert api.batch_ids == []


def test_existing_same_request_receipt_without_journal_is_not_adopted(
    tmp_path: Path,
) -> None:
    ledger, api, publisher, command = _setup(tmp_path)
    api.set_document(
        _RECEIPT,
        json.dumps({"request_id": command["request_id"], "status": "COMPLETED"}) + "\n",
    )
    _code(lambda: publisher.publish(command["request_id"], resources=_resources()),
          "ROOT_RECEIPT_UNJOURNALED")
    assert api.batch_ids == []
    assert _effects(ledger, command["request_id"]) == []


def test_root_command_scope_drift_prevents_receipt_write(tmp_path: Path) -> None:
    ledger, api, publisher, command = _setup(tmp_path)
    api.metadata[_COMMAND]["appProperties"]["cdr_workspace"] = "primary"
    _code(lambda: publisher.publish(command["request_id"], resources=_resources()),
          "ROOT_COMMAND_IDENTITY_CHANGED")
    assert api.batch_ids == []
    assert _effects(ledger, command["request_id"]) == []

def test_partial_status_uses_only_allowlisted_safe_resources(tmp_path: Path) -> None:
    _, api, publisher, command = _setup(tmp_path)
    resources = {"project_id": _PROJECT, "workspace_id": "primary",
                 "local": {"repo_path": "/private/secret/path"},
                 "github_for_chat": "SECRET"}
    assert publisher.publish(
        command["request_id"], status="BLOCKED", code="GITHUB_PROVISION_UNKNOWN",
        resources=resources,
    ) == _RECEIPT
    text = str(api.documents[_RECEIPT])
    assert "GITHUB_PROVISION_UNKNOWN" in text
    assert "/private/" not in text and "SECRET" not in text
