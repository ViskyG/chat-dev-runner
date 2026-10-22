from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from runner_app.adapters.drive_docs import FILE_FIELDS, DriveError
from runner_app.adapters.drive_google import _bounded_batch
from runner_app.adapters.handoff_docs import TerminalHandoffPublisher

DOCUMENT, TAB, OTHER = "result-document-123", "t.result", "t.command"


def tab(tab_id: str, text: str) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"sectionBreak": {}, "startIndex": 0, "endIndex": 1}]
    position = 1
    for value in (text + "\n").splitlines(keepends=True):
        end = position + len(value.encode("utf-16-le")) // 2
        content.append({"startIndex": position, "endIndex": end,
                        "paragraph": {"elements": [{"startIndex": position, "endIndex": end,
                                                       "textRun": {"content": value}}]}})
        position = end
    return {"tabProperties": {"tabId": tab_id}, "childTabs": [],
            "documentTab": {"body": {"content": content}}}


class FakeDocs:
    def __init__(self, text: str = "{}") -> None:
        self.metadata: dict[str, Any] = {
            "id": DOCUMENT, "mimeType": "application/vnd.google-apps.document", "trashed": False,
            "isAppAuthorized": True, "capabilities": {"canEdit": True},
        }
        outer = tab(OTHER, '{"protocol":"cdr-mailbox/2","dispatch":"ACK"}')
        outer["childTabs"] = [tab(TAB, text)]
        self.document: dict[str, Any] = {"documentId": DOCUMENT, "revisionId": "r1", "tabs": [outer]}
        self.batches: list[Any] = []
        self.lost_response = False
        self.unavailable = False
        self.unavailable_after_write = False
        self.conflict = False

    def selected(self) -> dict[str, Any]:
        return self.document["tabs"][0]["childTabs"][0]

    def payload(self) -> str:
        return "".join(run["textRun"]["content"]
                       for element in self.selected()["documentTab"]["body"]["content"]
                       if "paragraph" in element
                       for run in element["paragraph"]["elements"])[:-1]

    def get_file(self, file_id: str, *, fields: str = FILE_FIELDS) -> Mapping[str, Any]:
        assert file_id == DOCUMENT and fields == FILE_FIELDS
        if self.unavailable:
            raise OSError("Private account response")
        return copy.deepcopy(self.metadata)

    def get_document(self, file_id: str, *, include_tabs_content: bool = True) -> Mapping[str, Any]:
        assert file_id == DOCUMENT and include_tabs_content is True
        if self.unavailable:
            raise OSError("Private account response")
        return copy.deepcopy(self.document)

    def batch_update_document(self, file_id: str, *, requests: Sequence[Mapping[str, Any]],
                              required_revision_id: str) -> Mapping[str, Any]:
        assert file_id == DOCUMENT
        normalized = _bounded_batch(requests)
        self.batches.append((normalized, required_revision_id))
        if self.conflict:
            self.document["revisionId"] = "changed-by-owner"
        if required_revision_id != self.document["revisionId"]:
            raise DriveError("STALE_REVISION", "Private response")
        text = normalized[-1]["insertText"]["text"]
        assert normalized[-1]["insertText"]["location"] == {"index": 1, "tabId": TAB}
        if self.payload():
            assert normalized[0] == {"deleteContentRange": {"range": {
                "startIndex": 1, "endIndex": 1 + len(self.payload().encode("utf-16-le")) // 2,
                "tabId": TAB,
            }}}
        self.document["tabs"][0]["childTabs"][0] = tab(TAB, text)
        self.document["revisionId"] += "-updated"
        if self.unavailable_after_write:
            self.unavailable = True
        if self.lost_response:
            raise TimeoutError("Bearer private-value")
        return {"documentId": DOCUMENT}


def payload(request_id: str = "request-1") -> dict[str, Any]:
    return {"request_id": request_id, "handoff_id": "handoff-1", "project_id": "project-1",
            "target_id": "target-1", "local_delivery_status": "DELIVERED", "files_count": 1,
            "safe_relative_handoff_path": ".runner/handoffs/handoff-1",
            "codex_entrypoint": ".runner/handoffs/handoff-1/START_HERE.md"}


def publisher(api: FakeDocs) -> TerminalHandoffPublisher:
    return TerminalHandoffPublisher(api, document_id=DOCUMENT, tab_id=TAB)


@pytest.mark.parametrize("initial", ["", "{}"])
def test_exact_tab_receipt_is_guarded_and_preserves_command_ack(initial: str) -> None:
    api = FakeDocs(initial)
    other = copy.deepcopy(api.document["tabs"][0])
    result = publisher(api).publish(payload())
    assert result["status"] == "CONFIRMED"
    assert api.batches[0][1] == "r1"
    assert json.loads(api.payload())["results"] == {"request-1": payload()}
    assert {key: value for key, value in api.document["tabs"][0].items() if key != "childTabs"} == {
        key: value for key, value in other.items() if key != "childTabs"}


def test_whole_json_preserves_previous_requests_and_terminal_is_immutable() -> None:
    api = FakeDocs()
    port = publisher(api)
    assert port.publish(payload())["status"] == "CONFIRMED"
    assert port.publish(payload())["status"] == "CONFIRMED"
    assert len(api.batches) == 1
    changed = {**payload(), "local_delivery_status": "FAILED"}
    assert port.publish(changed)["code"] == "DELIVERY_RESULT_CONFLICT"
    assert len(api.batches) == 1
    assert port.publish(payload("request-2"))["status"] == "CONFIRMED"
    assert json.loads(api.payload())["results"] == {"request-1": payload(),
                                                    "request-2": payload("request-2")}


def test_lost_write_response_exact_readback_proves_success_without_retry() -> None:
    api = FakeDocs()
    api.lost_response = True
    result = publisher(api).publish(payload())
    assert result["status"] == "CONFIRMED"
    assert len(api.batches) == 1
    assert "private-value" not in json.dumps(result)


def test_unknown_write_remains_separate_and_later_readback_avoids_rewrite() -> None:
    api = FakeDocs()
    api.lost_response = True
    api.unavailable_after_write = True
    port = publisher(api)
    result = port.publish(payload())
    assert result["status"] == "UNCONFIRMED"
    assert result["code"] == "DELIVERY_RESULT_UNCONFIRMED"
    assert len(api.batches) == 1
    api.unavailable = False
    assert port.publish(payload())["status"] == "CONFIRMED"
    assert len(api.batches) == 1


def test_revision_conflict_does_not_retry_or_mutate_tab() -> None:
    api = FakeDocs()
    api.conflict = True
    result = publisher(api).publish(payload())
    assert result["code"] == "DOCS_RESULT_REVISION_CONFLICT"
    assert api.payload() == "{}"
    assert len(api.batches) == 1


@pytest.mark.parametrize("change", [
    {"id": "other-document-id"}, {"trashed": True}, {"capabilities": {"canEdit": False}},
    {"mimeType": "application/zip"}, {"isAppAuthorized": False},
])
def test_result_metadata_identity_change_never_writes(change: dict[str, Any]) -> None:
    api = FakeDocs()
    api.metadata.update(change)
    assert publisher(api).publish(payload())["status"] == "FAILED"
    assert api.batches == []


@pytest.mark.parametrize("text", [
    '{"protocol":"cdr-mailbox/2","dispatch":"ACK"}',
    '{"protocol":"cdr-handoff-results/1","results":{"other":{"request_id":"wrong"}}}',
    '{"protocol":"cdr-handoff-results/1","results":{},"unexpected":1}',
    '{"results":{},"results":{}}',
])
def test_command_or_unexpected_json_is_not_overwritten(text: str) -> None:
    api = FakeDocs(text)
    assert publisher(api).publish(payload())["status"] == "FAILED"
    assert api.payload() == text and not api.batches


@pytest.mark.parametrize("private", [
    {"path": "/home/owner/project"}, {"nested": {"path": "C:\\Users\\owner"}},
    {"text": "Read /tmp/private/project"}, {"access_token": "private-value"},
    {"credentials": {}},
])
def test_no_absolute_paths_or_credentials_remote(private: dict[str, Any]) -> None:
    api = FakeDocs()
    assert publisher(api).publish({**payload(), **private})["code"] == "PRIVATE_DELIVERY_RECEIPT"
    assert api.batches == []


def test_missing_tab_and_large_result_fail_before_write() -> None:
    api = FakeDocs()
    port = TerminalHandoffPublisher(api, document_id=DOCUMENT, tab_id="not-enrolled-tab")
    assert port.publish(payload())["code"] == "DOCS_TAB_MISSING"
    port = TerminalHandoffPublisher(api, document_id=DOCUMENT, tab_id=TAB, max_bytes=16)
    assert port.publish(payload())["code"] == "DOCS_RESULT_TOO_LARGE"
    assert not api.batches


def test_verified_same_request_recovery_advances_once_and_delivered_is_immutable() -> None:
    api = FakeDocs()
    port = publisher(api)
    handoff = {"handoff_id": "handoff-1", "project_id": "project-1", "target_id": "target-1",
               "manifest_sha256": "b" * 64, "local_delivery_status": "NOT_DELIVERED"}
    failed = {"request_id": "request-1", "request_digest": "a" * 64, "project_id": "project-1",
              "runner_id": "runner-1", "status": "INTERRUPTED", "handoff": handoff}
    delivered = {**failed, "status": "DELIVERED",
                 "handoff": {**handoff, "local_delivery_status": "DELIVERED", "files_count": 1}}
    assert port.publish(failed)["status"] == "CONFIRMED"
    assert port.publish({**delivered, "request_digest": "c" * 64})["code"] == (
        "DELIVERY_RESULT_CONFLICT")
    changed_manifest = {**delivered, "handoff": {**delivered["handoff"],
                                               "manifest_sha256": "d" * 64}}
    assert port.publish(changed_manifest)["code"] == "DELIVERY_RESULT_CONFLICT"
    assert len(api.batches) == 1
    assert port.publish(delivered)["status"] == "CONFIRMED"
    assert json.loads(api.payload())["results"]["request-1"] == delivered
    assert port.publish(delivered)["status"] == "CONFIRMED"
    assert port.publish(failed)["code"] == "DELIVERY_RESULT_CONFLICT"
    assert len(api.batches) == 2
