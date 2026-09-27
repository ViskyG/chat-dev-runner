"""Offline Docs CAS tests. No OAuth, account access or bootstrap apply occurs."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

import pytest

from runner_app.adapters.drive_docs import DriveError
from runner_app.adapters.drive_index import (
    AttachedInstallation,
    ExistingInstallation,
    IndexedFile,
    InstallationIndex,
)
from runner_app.adapters.drive_writer import DocsCASWriter, RegisteredOutputDoc

DOC_MIME = "application/vnd.google-apps.document"
CONTROL_ID = "control-folder-123"
RECEIPT_ID = "receipt-document-123"
STATE_ID = "state-document-123"
COMMAND_ID = "command-document-123"
RESULT_ID = "latest-result-document-123"
WORKSPACE_ID = "workspace-folder-123"


def _units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _document(file_id: str, text: str, revision: str, *, tab_id: str = "tab-one") -> dict[str, Any]:
    assert text.endswith("\n")
    position = 1
    content: list[dict[str, Any]] = [{"sectionBreak": {}, "startIndex": 0, "endIndex": 1}]
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
        "documentId": file_id, "revisionId": revision,
        "tabs": [{
            "tabProperties": {"tabId": tab_id}, "childTabs": [],
            "documentTab": {"body": {"content": content}},
        }],
    }


class IndexedFake:
    def __init__(self, installation_id: str) -> None:
        self.installation_id = installation_id
        self.metadata: dict[str, dict[str, Any]] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.batches: list[tuple[str, list[Mapping[str, Any]], str]] = []
        self.pre_batch_change: str | None = None
        self.post_batch_change: str | None = None
        self.lose_ack = False
        self.invalid_response = False
        self._next_revision = 10

    def add(self, file_id: str, parent_id: str, app_key: str, text: str) -> None:
        self.metadata[file_id] = {
            "id": file_id, "trashed": False, "mimeType": DOC_MIME,
            "parents": [parent_id], "isAppAuthorized": True,
            "capabilities": {"canEdit": True},
            "appProperties": {
                "cdr_installation": self.installation_id,
                "cdr_key": app_key,
            },
        }
        self.documents[file_id] = _document(file_id, text, "rev-1")

    def set_document(self, file_id: str, text: str) -> None:
        self._next_revision += 1
        self.documents[file_id] = _document(file_id, text, f"rev-{self._next_revision}")

    def get_file(self, file_id: str, *, fields: str) -> Mapping[str, Any]:
        assert "appProperties" in fields and "canEdit" in fields
        self.calls.append(("files.get", file_id))
        return copy.deepcopy(self.metadata[file_id])

    def get_document(self, file_id: str, *, include_tabs_content: bool = True) -> Mapping[str, Any]:
        assert include_tabs_content
        self.calls.append(("documents.get", file_id))
        return copy.deepcopy(self.documents[file_id])

    def batch_update_document(
        self, file_id: str, *, requests: Sequence[Mapping[str, Any]],
        required_revision_id: str,
    ) -> Mapping[str, Any]:
        self.calls.append(("documents.batchUpdate", file_id))
        self.batches.append((file_id, list(requests), required_revision_id))
        if self.pre_batch_change is not None:
            self.set_document(file_id, self.pre_batch_change)
            self.pre_batch_change = None
        if self.documents[file_id]["revisionId"] != required_revision_id:
            raise DriveError("STALE_REVISION", "Fake requiredRevisionId failed")
        current = self.documents[file_id]
        tab_id = current["tabs"][0]["tabProperties"]["tabId"]
        body = current["tabs"][0]["documentTab"]["body"]["content"]
        old_end = body[-1]["endIndex"]
        cursor = 0
        if "deleteContentRange" in requests[0]:
            expected_range = {"startIndex": 1, "endIndex": old_end - 1, "tabId": tab_id}
            assert requests[0] == {"deleteContentRange": {"range": expected_range}}
            cursor = 1
        if cursor < len(requests):
            assert requests[cursor]["insertText"]["location"] == {
                "index": 1, "tabId": tab_id,
            }
            inserted = requests[cursor]["insertText"]["text"]
            assert cursor + 1 == len(requests)
        else:
            inserted = ""
        self.set_document(file_id, inserted + "\n")
        resulting_revision = self.documents[file_id]["revisionId"]
        if self.post_batch_change is not None:
            self.set_document(file_id, self.post_batch_change)
            self.post_batch_change = None
        if self.lose_ack:
            raise OSError("Simulated lost acknowledgement; write was applied")
        if self.invalid_response:
            return {}
        return {"writeControl": {"requiredRevisionId": resulting_revision}}


def _fixture() -> tuple[IndexedFake, AttachedInstallation, DocsCASWriter]:
    installation_id = str(uuid4())
    provider = IndexedFake(installation_id)
    provider.add(RECEIPT_ID, CONTROL_ID, "receipt", "old receipt\n")
    provider.add(STATE_ID, CONTROL_ID, "state", "state\n")
    provider.add(RESULT_ID, WORKSPACE_ID, "latest-result", "old result\n")
    files = {
        "receipt": IndexedFile("receipt", RECEIPT_ID, "RECEIPT", DOC_MIME,
                               (CONTROL_ID,), {}),
        "state": IndexedFile("state", STATE_ID, "SYSTEM STATE", DOC_MIME,
                             (CONTROL_ID,), {}),
        "control": IndexedFile("control", CONTROL_ID, "00 Control", "folder", (), {}),
        "command": IndexedFile("command", COMMAND_ID, "COMMAND", DOC_MIME,
                               (CONTROL_ID,), {}),
    }
    attached = AttachedInstallation(
        ExistingInstallation("root-folder-123", "index-document-123", "Chat-Dev Runner"),
        InstallationIndex(installation_id, files), "index-rev", "index-sha", {},
    )
    extra = RegisteredOutputDoc("workspace-result", "LATEST_RESULT", RESULT_ID,
                                WORKSPACE_ID, "latest-result")
    return provider, attached, DocsCASWriter(provider, attached, extra_docs=[extra])


def _error_code(fn: Any, expected: str) -> None:
    with pytest.raises(DriveError) as error:
        fn()
    assert error.value.code == expected


def test_exact_registered_receipt_uses_single_revision_guarded_batch_and_readback() -> None:
    provider, _, writer = _fixture()
    expected = writer.read("root-receipt")
    result = writer.replace("root-receipt", "new receipt\n", expected=expected)
    assert result.state == "CONFIRMED"
    assert result.base_revision == "rev-1"
    assert result.resulting_revision == provider.documents[RECEIPT_ID]["revisionId"]
    assert result.observed_sha256 == hashlib.sha256(b"new receipt\n").hexdigest()
    assert provider.batches == [(
        RECEIPT_ID,
        [
            {"deleteContentRange": {"range": {
                "startIndex": 1, "endIndex": 12, "tabId": "tab-one",
            }}},
            {"insertText": {
                "location": {"index": 1, "tabId": "tab-one"}, "text": "new receipt",
            }},
        ],
        "rev-1",
    )]
    assert {file_id for _, file_id in provider.calls} == {RECEIPT_ID}


def test_utf16_structural_indices_preserve_non_bmp_text_and_multiple_paragraphs() -> None:
    provider, _, writer = _fixture()
    provider.set_document(RESULT_ID, "rocket 🚀\nsecond 🛰\n")
    observed = writer.read("workspace-result")
    assert observed.body_end_index == 1 + _units("rocket 🚀\nsecond 🛰\n")
    result = writer.replace("workspace-result", "verified ✅\n", expected=observed)
    assert result.state == "CONFIRMED"
    delete = provider.batches[0][1][0]["deleteContentRange"]["range"]
    assert delete["endIndex"] == observed.body_end_index - 1
    assert delete["endIndex"] > len(observed.snapshot.text)


def test_unknown_or_command_id_is_never_a_write_target() -> None:
    provider, attached, writer = _fixture()
    _error_code(lambda: writer.read("command"), "OUTPUT_NOT_REGISTERED")
    command_as_receipt = RegisteredOutputDoc("forged", "RECEIPT", COMMAND_ID,
                                             CONTROL_ID, "receipt")
    _error_code(
        lambda: DocsCASWriter(provider, attached, extra_docs=[command_as_receipt]),
        "OUTPUT_REGISTRATION_INVALID",
    )
    assert provider.batches == []



def test_child_registration_cannot_reuse_root_output_role() -> None:
    provider, attached, _ = _fixture()
    root_role_as_child = RegisteredOutputDoc(
        "child-receipt", "RECEIPT", "child-receipt-document-123",
        WORKSPACE_ID, "receipt",
    )
    _error_code(
        lambda: DocsCASWriter(provider, attached, extra_docs=[root_role_as_child]),
        "OUTPUT_REGISTRATION_INVALID",
    )
    assert provider.batches == []

def test_changed_observation_blocks_write_without_batch() -> None:
    provider, _, writer = _fixture()
    expected = writer.read("root-receipt")
    provider.set_document(RECEIPT_ID, "newer receipt\n")
    _error_code(
        lambda: writer.replace("root-receipt", "older receipt\n", expected=expected),
        "OUTPUT_CHANGED",
    )
    assert provider.batches == []


def test_stale_revision_during_batch_is_uncertain_and_never_replayed() -> None:
    provider, _, writer = _fixture()
    expected = writer.read("root-receipt")
    provider.pre_batch_change = "concurrent receipt\n"
    result = writer.replace("root-receipt", "my receipt\n", expected=expected)
    assert result.state == "UNCERTAIN"
    assert result.reason == "WRITE_ACK_UNCERTAIN"
    assert result.observed_sha256 == hashlib.sha256(b"concurrent receipt\n").hexdigest()
    assert len(provider.batches) == 1


def test_lost_ack_after_applied_write_is_uncertain_even_when_readback_matches() -> None:
    provider, _, writer = _fixture()
    expected = writer.read("root-receipt")
    provider.lose_ack = True
    result = writer.replace("root-receipt", "accepted\n", expected=expected)
    assert result.state == "UNCERTAIN"
    assert result.reason == "WRITE_ACK_UNCERTAIN"
    assert result.observed_sha256 == hashlib.sha256(b"accepted\n").hexdigest()
    assert len(provider.batches) == 1


def test_readback_drift_or_missing_write_control_never_claims_confirmation() -> None:
    provider, _, writer = _fixture()
    expected = writer.read("root-receipt")
    provider.post_batch_change = "newer\n"
    result = writer.replace("root-receipt", "older\n", expected=expected)
    assert result.state == "UNCERTAIN"
    assert result.reason == "READBACK_MISMATCH"
    expected = writer.read("root-receipt")
    provider.invalid_response = True
    result = writer.replace("root-receipt", "another\n", expected=expected)
    assert result.state == "UNCERTAIN"
    assert result.reason == "WRITE_RESPONSE_INVALID"
    assert len(provider.batches) == 2


def test_unchanged_body_has_no_write_side_effect() -> None:
    provider, _, writer = _fixture()
    expected = writer.read("system-state")
    result = writer.replace("system-state", "state\n", expected=expected)
    assert result.state == "UNCHANGED"
    assert result.resulting_revision == "rev-1"
    assert provider.batches == []


def test_moved_unowned_or_nonwritable_document_is_rejected_before_write() -> None:
    for mutation, code in [
        (lambda item: item.update(parents=["different-folder"]), "OUTPUT_IDENTITY_CHANGED"),
        (lambda item: item["appProperties"].update(cdr_key="command"),
         "OUTPUT_OWNERSHIP_CHANGED"),
        (lambda item: item["capabilities"].update(canEdit=False), "OUTPUT_NOT_WRITABLE"),
    ]:
        provider, _, writer = _fixture()
        mutation(provider.metadata[RECEIPT_ID])
        _error_code(lambda writer=writer: writer.read("root-receipt"), code)
        assert provider.batches == []


def test_multitab_or_inconsistent_utf16_indices_fail_closed() -> None:
    provider, _, writer = _fixture()
    document = provider.documents[RECEIPT_ID]
    document["tabs"].append(copy.deepcopy(document["tabs"][0]))
    _error_code(lambda: writer.read("root-receipt"), "UNSUPPORTED_TABS")
    document["tabs"].pop()
    run = document["tabs"][0]["documentTab"]["body"]["content"][-1]["paragraph"]["elements"][0]
    run["endIndex"] -= 1
    _error_code(lambda: writer.read("root-receipt"), "DOC_STRUCTURE_INVALID")
    assert provider.batches == []


def test_invalid_or_oversize_insert_text_is_rejected_without_remote_write() -> None:
    provider, _, writer = _fixture()
    expected = writer.read("root-receipt")
    for text, code in [
        ("missing newline", "OUTPUT_TEXT_INVALID"),
        ("stripped\x00control\n", "OUTPUT_TEXT_INVALID"),
        ("private\ue000use\n", "OUTPUT_TEXT_INVALID"),
        ("A" * 65_536 + "\n", "OUTPUT_TEXT_TOO_LARGE"),
    ]:
        _error_code(lambda text=text: writer.replace("root-receipt", text, expected=expected), code)
    assert provider.batches == []
