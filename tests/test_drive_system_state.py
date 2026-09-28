"""Offline publication of the runner's indexed SYSTEM STATE document."""
from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from runner_app.adapters.drive_index import (
    AttachedInstallation,
    ExistingInstallation,
    IndexedFile,
    InstallationIndex,
)
from runner_app.adapters.drive_system_state import SystemStateError, SystemStatePublisher
from runner_app.adapters.drive_writer import DocsCASWriter

_INSTALLATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_ROOT = "rootfolderid0000001"
_INDEX = "indexdocument000001"
_CONTROL = "controlfolder000001"
_STATE = "statedocument00001"
_RECEIPT = "receiptdocument001"
_DOC = "application/vnd.google-apps.document"
_EPOCH = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _document(text: str, revision: int) -> dict[str, Any]:
    assert text.endswith("\n")
    end = 1 + len(text.encode("utf-16-le")) // 2
    return {
        "documentId": _STATE, "revisionId": f"rev-{revision}",
        "tabs": [{
            "tabProperties": {"tabId": "tab-one"}, "childTabs": [],
            "documentTab": {"body": {"content": [
                {"sectionBreak": {}, "startIndex": 0, "endIndex": 1},
                {"startIndex": 1, "endIndex": end,
                 "paragraph": {"elements": [{
                     "startIndex": 1, "endIndex": end,
                     "textRun": {"content": text},
                 }]}},
            ]}},
        }],
    }


class FakeStateDocs:
    def __init__(self) -> None:
        self.metadata: dict[str, Any] = {
            "id": _STATE, "name": "SYSTEM STATE", "mimeType": _DOC,
            "parents": [_CONTROL], "trashed": False, "isAppAuthorized": True,
            "capabilities": {"canEdit": True},
            "appProperties": {"cdr_installation": _INSTALLATION, "cdr_key": "state"},
        }
        self.text = '{"state":"NOT_CONFIGURED"}\n'
        self.revision = 1
        self.calls: list[tuple[str, str]] = []
        self.batches: list[tuple[str, str]] = []
        self.lose_ack = False
        self.readback_drift: str | None = None

    def get_file(self, file_id: str, *, fields: str) -> Mapping[str, Any]:
        assert "appProperties" in fields
        self.calls.append(("files.get", file_id))
        assert file_id == _STATE
        return copy.deepcopy(self.metadata)

    def get_document(self, file_id: str, *,
                     include_tabs_content: bool = True) -> Mapping[str, Any]:
        assert include_tabs_content and file_id == _STATE
        self.calls.append(("documents.get", file_id))
        return _document(self.text, self.revision)

    def batch_update_document(
        self, file_id: str, *, requests: Sequence[Mapping[str, Any]],
        required_revision_id: str,
    ) -> Mapping[str, Any]:
        assert file_id == _STATE
        assert required_revision_id == f"rev-{self.revision}"
        self.calls.append(("documents.batchUpdate", file_id))
        self.batches.append((file_id, required_revision_id))
        assert requests[0]["deleteContentRange"]["range"] == {
            "startIndex": 1,
            "endIndex": 1 + len(self.text.encode("utf-16-le")) // 2 - 1,
            "tabId": "tab-one",
        }
        inserted = requests[1]["insertText"]
        assert inserted["location"] == {"index": 1, "tabId": "tab-one"}
        self.text = inserted["text"] + "\n"
        self.revision += 1
        revision = f"rev-{self.revision}"
        if self.readback_drift is not None:
            self.text = self.readback_drift
            self.revision += 1
        if self.lose_ack:
            raise OSError("SECRET provider response must not escape")
        return {"writeControl": {"requiredRevisionId": revision}}


def _fixture() -> tuple[FakeStateDocs, SystemStatePublisher]:
    docs = FakeStateDocs()
    files = {
        "control": IndexedFile("control", _CONTROL, "00 Control", "folder", (_ROOT,), {}),
        "state": IndexedFile("state", _STATE, "SYSTEM STATE", _DOC, (_CONTROL,), {}),
        "receipt": IndexedFile("receipt", _RECEIPT, "RECEIPT", _DOC, (_CONTROL,), {}),
    }
    attached = AttachedInstallation(
        ExistingInstallation(_ROOT, _INDEX, "Chat-Dev Runner"),
        InstallationIndex(_INSTALLATION, files), "index-rev", "index-sha", {},
    )
    writer = DocsCASWriter(docs, attached)
    publisher = SystemStatePublisher(
        writer, attached, github_owner="ViskyG", github_identity="viskyg",
        approved_templates={"python-basic"}, approved_profiles={"python-quick"},
        available_actions={"CREATE_PROJECT", "RECONCILE", "RESUME_PROVISION"},
    )
    return docs, publisher


def _state(*, armed: bool = False, updated_at: str = "first") -> dict[str, Any]:
    return {
        "runner_id": "local-runner", "epoch": _EPOCH, "armed": armed,
        "updated_at": updated_at, "secret": "do not publish",
    }


def test_publishes_exact_indexed_doc_and_repeating_state_does_not_write() -> None:
    docs, publisher = _fixture()
    publisher.publish(_state(armed=True))
    value = json.loads(docs.text)
    assert value == {
        "schema_version": "1.0", "state": "ARMED", "runner_armed": True,
        "runner_id": "local-runner", "epoch": _EPOCH,
        "installation_id": _INSTALLATION,
        "installation_index_document_id": _INDEX,
        "root_folder_id": _ROOT,
        "available_actions": ["CREATE_PROJECT"],
        "approved_templates": ["python-basic"],
        "approved_profiles": ["python-quick"],
        "github_owner": "ViskyG", "github_identity": "viskyg",
        "chat_repository_access": "MANUAL_REQUIRED",
    }
    assert docs.batches == [(_STATE, "rev-1")]
    assert {file_id for _, file_id in docs.calls} == {_STATE}
    publisher.publish(_state(armed=True, updated_at="later"))
    assert docs.batches == [(_STATE, "rev-1")]


def test_lost_ack_is_confirmed_only_by_exact_desired_text_readback() -> None:
    docs, publisher = _fixture()
    docs.lose_ack = True
    publisher.publish(_state())
    assert json.loads(docs.text)["state"] == "DISARMED"
    assert len(docs.batches) == 1


def test_ambiguous_write_with_different_readback_is_sanitized_and_not_retried() -> None:
    docs, publisher = _fixture()
    docs.lose_ack = True
    docs.readback_drift = '{"state":"OTHER"}\n'
    with pytest.raises(SystemStateError) as error:
        publisher.publish(_state())
    assert error.value.code == "SYSTEM_STATE_READBACK_MISMATCH"
    assert "SECRET" not in str(error.value)
    assert len(docs.batches) == 1


def test_invalid_ledger_state_or_changed_target_fails_before_write() -> None:
    docs, publisher = _fixture()
    with pytest.raises(SystemStateError) as invalid:
        publisher.publish({**_state(), "epoch": "not-a-uuid"})
    assert invalid.value.code == "SYSTEM_STATE_INPUT_INVALID"
    assert docs.calls == []
    docs.metadata["parents"] = ["other-folder"]
    with pytest.raises(SystemStateError) as moved:
        publisher.publish(_state())
    assert moved.value.code == "SYSTEM_STATE_READ_FAILED"
    assert docs.batches == []


def test_dynamic_actions_and_profiles_refresh_same_ledger_state() -> None:
    docs, static = _fixture()
    actions = ["CREATE_PROJECT"]
    profiles: list[str] = []
    publisher = SystemStatePublisher(
        static.writer, static.attached, github_owner="ViskyG",
        github_identity="viskyg", approved_templates={"python-basic"},
        approved_profiles=lambda: profiles, available_actions=lambda: actions,
    )
    state = _state(armed=True)
    publisher.publish(state)
    assert json.loads(docs.text)["available_actions"] == ["CREATE_PROJECT"]
    assert json.loads(docs.text)["approved_profiles"] == []
    actions.append("DELIVER_VERIFY_PUBLISH")
    profiles.append("python-quick")
    publisher.publish(state)
    value = json.loads(docs.text)
    assert value["available_actions"] == [
        "CREATE_PROJECT", "DELIVER_VERIFY_PUBLISH",
    ]
    assert value["approved_profiles"] == ["python-quick"]
    assert len(docs.batches) == 2
    publisher.publish(state)
    assert len(docs.batches) == 2
