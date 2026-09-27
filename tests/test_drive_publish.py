"""Fake-backed publication checks; no OAuth, Google account, or project execution."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from runner_app.adapters.drive_delivery import DeliveryBundle, DeliveryError, prepare_run_bundle
from runner_app.adapters.drive_docs import DriveError
from runner_app.adapters.drive_index import (
    AttachedInstallation,
    ExistingInstallation,
    IndexedFile,
    InstallationIndex,
)
from runner_app.adapters.drive_publish import DriveDeliveryPublisher
from runner_app.adapters.drive_registry import DriveRegistryError
from runner_app.adapters.drive_upload import GoogleUploadProvider
from runner_app.adapters.drive_writer import DocsCASWriter, RegisteredOutputDoc
from runner_app.domain.commands import freeze
from runner_app.storage.ledger import DeliveryPreflight

INSTALLATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RESULTS_ID = "resultsfolder00001"
CONTROL_ID = "controlfolder00001"
ROOT_RECEIPT_ID = "rootreceiptdoc00001"
STATE_ID = "statedocid00000001"
WORKSPACE_ID = "workspacefolder00001"
WORKSPACE_RECEIPT_ID = "workspacereceiptdoc00001"
LATEST_ID = "latestresultdoc00001"
FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"


def _timestamp(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _input_manifest_bytes(run_id: str, run: Mapping[str, Any],
                          command: Mapping[str, Any]) -> bytes:
    return (json.dumps({
        "schema_version": "1.0", "run_id": run_id,
        "project_id": run["project_id"], "workspace_id": run["workspace_id"],
        "base_head": command["expected_head"],
        "commit": run["snapshot"]["commit"], "tree": run["snapshot"]["tree"],
    }, sort_keys=True) + "\n").encode()


def _document(file_id: str, text: str, revision: str) -> dict[str, Any]:
    assert text.endswith("\n")
    position = 1
    content: list[dict[str, Any]] = [{"sectionBreak": {}, "startIndex": 0, "endIndex": 1}]
    for line in text.splitlines(keepends=True):
        end = position + len(line.encode("utf-16-le")) // 2
        content.append({
            "startIndex": position, "endIndex": end,
            "paragraph": {"elements": [{
                "startIndex": position, "endIndex": end,
                "textRun": {"content": line},
            }]},
        })
        position = end
    return {"documentId": file_id, "revisionId": revision, "tabs": [{
        "tabProperties": {"tabId": "tab-one"}, "childTabs": [],
        "documentTab": {"body": {"content": content}},
    }]}


class FakeDocs:
    def __init__(self) -> None:
        self.metadata: dict[str, dict[str, Any]] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        self.writes: list[tuple[str, str]] = []
        self._revision = 1
        self.add(ROOT_RECEIPT_ID, CONTROL_ID, "receipt", "DRAFT\n")
        self.add(STATE_ID, CONTROL_ID, "state", "NOT_CONFIGURED\n")
        self.add(WORKSPACE_RECEIPT_ID, WORKSPACE_ID, "workspace-receipt", "DRAFT\n")
        self.add(LATEST_ID, WORKSPACE_ID, "latest-result", "DRAFT\n")

    def add(self, file_id: str, parent_id: str, app_key: str, text: str) -> None:
        self.metadata[file_id] = {
            "id": file_id, "mimeType": DOC_MIME, "parents": [parent_id],
            "trashed": False, "isAppAuthorized": True,
            "capabilities": {"canEdit": True},
            "appProperties": {"cdr_installation": INSTALLATION, "cdr_key": app_key},
        }
        self.documents[file_id] = _document(file_id, text, "rev-1")

    def text(self, file_id: str) -> str:
        body = self.documents[file_id]["tabs"][0]["documentTab"]["body"]["content"]
        return "".join(
            run["textRun"]["content"]
            for element in body for run in element.get("paragraph", {}).get("elements", [])
        )

    def get_file(self, file_id: str, *, fields: str) -> Mapping[str, Any]:
        assert "appProperties" in fields
        return copy.deepcopy(self.metadata[file_id])

    def get_document(self, file_id: str, *, include_tabs_content: bool = True) -> Mapping[str, Any]:
        assert include_tabs_content
        return copy.deepcopy(self.documents[file_id])

    def batch_update_document(
        self, file_id: str, *, requests: Sequence[Mapping[str, Any]],
        required_revision_id: str,
    ) -> Mapping[str, Any]:
        if self.documents[file_id]["revisionId"] != required_revision_id:
            raise DriveError("STALE_REVISION", "Fake Docs revision changed")
        inserted = "".join(
            str(request["insertText"]["text"])
            for request in requests if "insertText" in request
        )
        text = inserted + "\n"
        self._revision += 1
        revision = f"rev-{self._revision}"
        self.documents[file_id] = _document(file_id, text, revision)
        self.writes.append((file_id, text))
        return {"writeControl": {"requiredRevisionId": revision}}


class FakeRequest:
    def __init__(self, call: Callable[[], dict[str, Any]]) -> None:
        self.call = call

    def execute(self, *, num_retries: int = 0) -> dict[str, Any]:
        assert num_retries == 0
        return self.call()


class FakeFiles:
    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}
        self.creates: list[dict[str, Any]] = []
        self.fault_name: str | None = None
        self.fault_after_write = False
        self.move_results_after_create_name: str | None = None
        self._next_id = 1
        self.entries[RESULTS_ID] = self._entry(
            RESULTS_ID, "02 Results", FOLDER_MIME, "rootfolderid00001", "results",
            None,
        )

    @staticmethod
    def _entry(file_id: str, name: str, mime: str, parent: str,
               key: str, content: bytes | None) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "id": file_id, "name": name, "mimeType": mime, "parents": [parent],
            "trashed": False, "isAppAuthorized": True,
            "appProperties": {"cdr_installation": INSTALLATION, "cdr_key": key},
            "capabilities": {"canEdit": True, "canAddChildren": mime == FOLDER_MIME},
            "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
        }
        if content is not None:
            entry["size"] = str(len(content))
            entry["md5Checksum"] = hashlib.md5(content, usedforsecurity=False).hexdigest()
            entry["sha256Checksum"] = hashlib.sha256(content).hexdigest()
            entry["appProperties"]["cdr_sha256"] = entry["sha256Checksum"]
            entry["appProperties"]["cdr_size"] = entry["size"]
        return entry

    def files(self) -> FakeFiles:
        return self

    def get(self, **kwargs: Any) -> FakeRequest:
        file_id = kwargs["fileId"]
        return FakeRequest(lambda: copy.deepcopy(self.entries[file_id]))

    def list(self, **kwargs: Any) -> FakeRequest:
        query = kwargs["q"]
        properties = dict(re.findall(r"key='([^']+)' and value='([^']+)'", query))
        assert set(properties) == {"cdr_installation", "cdr_key"}
        assert "name" not in query and " in parents" not in query

        def call() -> dict[str, Any]:
            matches = sorted((item for item in self.entries.values()
                              if all(item.get("appProperties", {}).get(k) == v
                                      for k, v in properties.items())),
                             key=lambda item: item["id"])
            offset = int(kwargs.get("pageToken") or "0")
            page = matches[offset:offset + kwargs["pageSize"]]
            next_offset = offset + len(page)
            return {"files": copy.deepcopy(page),
                    "nextPageToken": str(next_offset) if next_offset < len(matches) else None}

        return FakeRequest(call)

    def create(self, **kwargs: Any) -> FakeRequest:
        body = copy.deepcopy(kwargs["body"])
        media = kwargs["media_body"]

        def call() -> dict[str, Any]:
            content = None if media is None else media["content"]
            if self.fault_name == body["name"] and not self.fault_after_write:
                raise OSError("lost upload response")
            file_id = f"uploadedfileid{self._next_id:05d}"
            self._next_id += 1
            entry = self._entry(file_id, body["name"], body["mimeType"],
                                body["parents"][0], body["appProperties"]["cdr_key"], content)
            entry["appProperties"] = body["appProperties"]
            self.entries[file_id] = entry
            self.creates.append({"id": file_id, "body": body, "content": content})
            if self.move_results_after_create_name == body["name"]:
                self.entries[RESULTS_ID]["parents"] = ["movedoutside00000001"]
            if self.fault_name == body["name"] and self.fault_after_write:
                raise OSError("lost upload response")
            return {"id": file_id}

        return FakeRequest(call)


class FakeLedger:
    def __init__(self) -> None:
        self.operations: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.preflight_calls: list[tuple[str, str]] = []
        self.effect_log: list[tuple[str, str]] = []
        self.delivery_records: list[tuple[str, str, str | None]] = []
        self.execute_calls = 0
        self.confirm_fault_step: str | None = None
        self._effect_number = 0
        self._operation_sequence = 0

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.runs.get(run_id)

    def get(self, request_id: str) -> dict[str, Any] | None:
        return self.operations.get(request_id)

    def add_delivery(self, delivery_id: str, run_id: str) -> None:
        self._operation_sequence += 1
        run = self.runs[run_id]
        self.operations[delivery_id] = {
            "request_id": delivery_id, "action": "PUBLISH_REPORT",
            "run_id": run_id, "project_id": run["project_id"],
            "workspace_id": run["workspace_id"],
            "status": "RUNNING", "effects": [],
            "sequence": self._operation_sequence,
        }

    def require_delivery_preflight(
        self, delivery_request_id: str, run_id: str,
    ) -> DeliveryPreflight:
        self.preflight_calls.append((delivery_request_id, run_id))
        operation = self.operations[delivery_request_id]
        assert operation["status"] == "RUNNING"
        assert operation["action"] == "PUBLISH_REPORT"
        assert operation["run_id"] == run_id
        run = self.runs[run_id]
        manifest_bytes = (json.dumps(run, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False) + "\n").encode()
        source = self.operations[run["request_id"]]
        input_bytes = _input_manifest_bytes(run_id, run, source["command"])
        return DeliveryPreflight(
            delivery_request_id, run["request_id"], run_id,
            run["project_id"], run["workspace_id"], "PUBLISH_REPORT", "VERIFY",
            "COMPLETED", "workspace:fixture/area", hashlib.sha256(manifest_bytes).hexdigest(),
            hashlib.sha256(input_bytes).hexdigest(), None,
        )

    def record_delivery(self, request_id: str, status: str, code: str | None = None) -> None:
        operation = self.operations[request_id]
        assert operation["status"] == "RUNNING"
        operation["delivery_status"] = status
        operation["delivery_code"] = code
        self.delivery_records.append((request_id, status, code))

    def begin_effect(self, request_id: str, step_id: str, kind: str,
                     intent: dict[str, Any]) -> str:
        assert kind == "drive_file"
        operation = self.operations[request_id]
        assert operation["status"] == "RUNNING"
        self._effect_number += 1
        effect_id = f"effect-{self._effect_number}"
        operation["effects"].append({
            "effect_id": effect_id, "step_id": step_id, "kind": kind,
            "intent": copy.deepcopy(intent), "state": "INTENDED", "provider_id": None,
        })
        self.effect_log.append((step_id, "INTENDED"))
        return effect_id

    def confirm_effect(self, effect_id: str, provider_id: str) -> None:
        for operation in self.operations.values():
            for effect in operation.get("effects", []):
                if effect["effect_id"] == effect_id:
                    if effect["step_id"] == self.confirm_fault_step:
                        raise OSError("lost confirmation after remote write")
                    effect["state"] = "CONFIRMED"
                    effect["provider_id"] = provider_id
                    self.effect_log.append((effect["step_id"], "CONFIRMED"))
                    return
        raise AssertionError("Unknown fake effect")


class FakeWorkspaceRegistry:
    def __init__(self) -> None:
        self.project_id = "fixture"
        self.workspace_id = "area"
        self.receipt_id = WORKSPACE_RECEIPT_ID
        self.latest_id = LATEST_ID
        self.calls: list[tuple[str, str]] = []

    def require_output_pair(
        self, project_id: str, workspace_id: str,
        receipt: RegisteredOutputDoc, latest: RegisteredOutputDoc,
    ) -> None:
        self.calls.append((project_id, workspace_id))
        if (project_id != self.project_id or workspace_id != self.workspace_id
                or receipt.file_id != self.receipt_id
                or latest.file_id != self.latest_id
                or receipt.parent_id != WORKSPACE_ID
                or latest.parent_id != WORKSPACE_ID):
            raise DriveRegistryError("OUTPUT_SCOPE_MISMATCH", "Fake workspace binding differs")


class FakeEvidence:
    def __init__(self, ledger: FakeLedger) -> None:
        self.ledger = ledger
        self.input_reads = 0
        self.output_reads = 0

    def read_input_manifest(self, run_id: str) -> bytes:
        self.input_reads += 1
        run = self.ledger.runs[run_id]
        command = self.ledger.operations[run["request_id"]]["command"]
        return _input_manifest_bytes(run_id, run, command)

    def open_output(self, run_id: str, output_id: str) -> Any:
        self.output_reads += 1
        raise AssertionError("No project output was approved for publication")


@dataclass
class Fixture:
    ledger: FakeLedger
    evidence: FakeEvidence
    docs: FakeDocs
    files: FakeFiles
    publisher: DriveDeliveryPublisher
    attached: AttachedInstallation
    writer: DocsCASWriter
    registry: FakeWorkspaceRegistry
    bundle_parent: Path

    def add_run(self, *, created_at: str, source_id: str | None = None,
                run_id: str | None = None) -> tuple[str, str, dict[str, Any]]:
        source_id, run_id = source_id or str(uuid4()), run_id or str(uuid4())
        now = datetime.now(timezone.utc)
        command = {
            "schema_version": "1.0", "request_id": source_id, "dispatch": "READY",
            "runner_id": "local-runner", "epoch": str(uuid4()),
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
            "action": "VERIFY", "project_id": "fixture", "workspace_id": "area",
            "expected_head": "a" * 40, "params": {"profile_id": "python-quick"},
            "intent": "Verify committed source",
        }
        run = {
            "schema_version": "1.0", "run_id": run_id, "request_id": source_id,
            "project_id": "fixture", "workspace_id": "area",
            "snapshot": {"commit": "b" * 40, "tree": "c" * 40,
                         "source_dirty_at_capture": False},
            "operation_status": "COMPLETED",
            "execution": {"outcome": "FAILED", "exit_code": 1, "duration_seconds": 0.1},
            "delivery": {"status": "NOT_ATTEMPTED", "reason": None},
            "outputs": [], "created_at": created_at,
        }
        self.ledger.runs[run_id] = run
        self.ledger.operations[source_id] = {
            "request_id": source_id, "command": command,
            "digest": freeze(command).digest, "project_id": "fixture",
            "workspace_id": "area", "status": "COMPLETED", "effects": [],
        }
        operation_manifest = {
            "schema_version": "1.0", "request_id": source_id,
            "project_id": "fixture", "workspace_id": "area",
            "status": "COMPLETED", "run_ids": [run_id],
            "publication": {"status": "NOT_REQUESTED", "repository": None,
                            "branch": None, "commit": None,
                            "pull_request_url": None, "reason": None},
            "delivery": {"status": "PENDING", "reason": None},
            "outputs": [], "effects": [], "created_at": _timestamp(),
        }
        return source_id, run_id, operation_manifest

    def bundle(self, source_id: str, run_id: str,
               operation_manifest: Mapping[str, Any]) -> DeliveryBundle:
        delivery_id = str(uuid4())
        self.ledger.add_delivery(delivery_id, run_id)
        return prepare_run_bundle(
            self.ledger, self.evidence, run_id, delivery_id,
            operation_manifest, self.bundle_parent,
        )

    def publish(self, bundle: DeliveryBundle):
        return self.publisher.publish(
            bundle, receipt_key="workspace-receipt",
            latest_result_key="workspace-result",
        )


def fixture(tmp_path: Path) -> Fixture:
    docs = FakeDocs()
    files = FakeFiles()
    indexed = {
        "root": IndexedFile("root", "rootfolderid00001", "Chat-Dev Runner", FOLDER_MIME,
                            (), {}),
        "results": IndexedFile("results", RESULTS_ID, "02 Results", FOLDER_MIME,
                               ("rootfolderid00001",), {}),
        "control": IndexedFile("control", CONTROL_ID, "00 Control", FOLDER_MIME,
                               ("rootfolderid00001",), {}),
        "receipt": IndexedFile("receipt", ROOT_RECEIPT_ID, "RECEIPT", DOC_MIME,
                               (CONTROL_ID,), {}),
        "state": IndexedFile("state", STATE_ID, "SYSTEM STATE", DOC_MIME,
                             (CONTROL_ID,), {}),
    }
    attached = AttachedInstallation(
        ExistingInstallation("rootfolderid00001", "indexdocid00000001", "Chat-Dev Runner"),
        InstallationIndex(INSTALLATION, indexed), "index-rev", "index-sha", {},
    )
    writer = DocsCASWriter(docs, attached, extra_docs=[
        RegisteredOutputDoc("workspace-receipt", "RECEIPT", WORKSPACE_RECEIPT_ID,
                            WORKSPACE_ID, "workspace-receipt"),
        RegisteredOutputDoc("workspace-result", "LATEST_RESULT", LATEST_ID,
                            WORKSPACE_ID, "latest-result"),
    ])
    ledger = FakeLedger()
    upload = GoogleUploadProvider(
        files, media_factory=lambda content, media_type: {
            "content": content, "media_type": media_type,
        },
    )
    registry = FakeWorkspaceRegistry()
    return Fixture(ledger, FakeEvidence(ledger), docs, files,
                   DriveDeliveryPublisher(ledger, upload, attached, writer, registry),
                   attached, writer, registry, tmp_path / "bundles")


def test_success_uploads_index_commit_marker_last_then_writes_output_docs(tmp_path: Path) -> None:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    bundle = value.bundle(source_id, run_id, manifest)
    result = value.publish(bundle)
    assert result.status == "DELIVERED" and result.code is None
    assert result.request_folder_id and result.run_folder_id
    names = [entry["body"]["name"] for entry in value.files.creates]
    assert names[:2] == [source_id, run_id]
    asset_names = names[2:]
    assert "operation-manifest.json" in asset_names
    assert "input-manifest.json" in asset_names
    assert "report.md" in asset_names
    assert asset_names[-2].startswith("bundle-index-")
    assert asset_names[-1].startswith("delivery-index-")
    published_index = json.loads(value.files.creates[-1]["content"])
    assert published_index["delivery_request_id"] == bundle.delivery_request_id
    assert published_index["run_id"] == run_id
    assets = published_index["assets"]
    assert {entry["drive_file_id"] for entry in assets} == {
        entry["id"] for entry in value.files.creates[2:-1]
    }
    assert {entry["bundle_name"] for entry in assets} == {item.name for item in bundle.items}
    assert [file_id for file_id, _ in value.docs.writes] == [LATEST_ID, WORKSPACE_RECEIPT_ID]
    latest_payload = json.loads(value.docs.text(LATEST_ID).split("\n", 1)[1])
    receipt_payload = json.loads(value.docs.text(WORKSPACE_RECEIPT_ID).split("\n", 1)[1])
    assert latest_payload["index_file_id"] == value.files.creates[-1]["id"]
    assert receipt_payload["index_file_id"] == value.files.creates[-1]["id"]
    assert b"Verify committed source" not in b"".join(
        entry["content"] or b"" for entry in value.files.creates
    )
    assert value.ledger.execute_calls == 0 and value.evidence.output_reads == 0
    effects = value.ledger.operations[bundle.delivery_request_id]["effects"]
    assert effects and all(effect["state"] == "CONFIRMED" for effect in effects)
    assert len(result.confirmed_assets) == len(bundle.items) + 1


def test_publish_under_verified_legacy_results_keeps_new_descendants_app_owned(
    tmp_path: Path,
) -> None:
    value = fixture(tmp_path)
    results = value.files.entries[RESULTS_ID]
    results["isAppAuthorized"] = False
    results.pop("appProperties")
    indexed = dict(value.attached.index.files)
    indexed["installation-index"] = IndexedFile(
        "installation-index", value.attached.config.index_id,
        "INSTALLATION INDEX", DOC_MIME, (value.attached.config.root_id,), {},
    )
    attached = replace(
        value.attached,
        index=InstallationIndex(INSTALLATION, indexed, "owner_manifest"),
        metadata={"results": copy.deepcopy(results)},
    )
    upload = GoogleUploadProvider(
        value.files, attached=attached,
        media_factory=lambda content, media_type: {
            "content": content, "media_type": media_type,
        },
    )
    value.publisher = DriveDeliveryPublisher(
        value.ledger, upload, attached, value.writer, value.registry,
    )
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    result = value.publish(value.bundle(source_id, run_id, manifest))
    assert result.status == "DELIVERED"
    assert value.files.creates
    assert all(item["body"]["appProperties"]["cdr_installation"] == INSTALLATION
               for item in value.files.creates)
    assert all(item["body"]["parents"][0] == RESULTS_ID
               or item["body"]["parents"][0] in {
                   created["id"] for created in value.files.creates
               } for item in value.files.creates)


def test_ambiguous_upload_leaves_intended_effect_and_same_request_never_replays(
    tmp_path: Path,
) -> None:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    bundle = value.bundle(source_id, run_id, manifest)
    value.files.fault_name = "report.md"
    value.files.fault_after_write = True
    first = value.publish(bundle)
    assert first.status == "PARTIAL" and first.code == "UPLOAD_WRITE_UNCERTAIN"
    assert value.ledger.operations[bundle.delivery_request_id]["delivery_status"] == "PARTIAL"
    assert value.ledger.operations[bundle.delivery_request_id]["delivery_code"] == first.code
    assert value.ledger.delivery_records[:2] == [
        (bundle.delivery_request_id, "PENDING", None),
        (bundle.delivery_request_id, "PARTIAL", "UPLOAD_WRITE_UNCERTAIN"),
    ]
    attempts = [entry for entry in value.files.creates if entry["body"]["name"] == "report.md"]
    assert len(attempts) == 1
    unresolved = [effect for effect in value.ledger.operations[bundle.delivery_request_id]["effects"]
                  if effect["state"] == "INTENDED"]
    assert len(unresolved) == 1 and unresolved[0]["intent"]["name"] == "report.md"
    second = value.publish(bundle)
    assert second.status == "PARTIAL" and second.code == "DELIVERY_NEEDS_RECONCILIATION"
    assert len([entry for entry in value.files.creates
                if entry["body"]["name"] == "report.md"]) == 1
    assert value.docs.writes == []


def test_reconciled_partial_upload_is_reused_by_explicit_new_report(tmp_path: Path) -> None:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    old_bundle = value.bundle(source_id, run_id, manifest)
    value.files.fault_name = "report.md"
    value.files.fault_after_write = True
    assert value.publish(old_bundle).status == "PARTIAL"
    unresolved = next(effect for effect in value.ledger.operations[old_bundle.delivery_request_id]["effects"]
                      if effect["state"] == "INTENDED")
    report = next(entry for entry in value.files.creates
                  if entry["body"]["name"] == "report.md")
    # A trusted reconciler has checked the exact remote ID/bytes and closed the
    # old uncertainty. Only a NEW PUBLISH_REPORT request may continue delivery.
    value.ledger.confirm_effect(unresolved["effect_id"], report["id"])
    value.ledger.operations[old_bundle.delivery_request_id]["status"] = "FAULT"
    value.files.fault_name = None
    new_bundle = value.bundle(source_id, run_id, manifest)
    result = value.publish(new_bundle)
    assert result.status == "DELIVERED"
    assert len([entry for entry in value.files.creates
                if entry["body"]["name"] == "report.md"]) == 1
    assert value.ledger.execute_calls == 0 and value.evidence.output_reads == 0


def test_explicit_publish_report_reuses_stored_run_and_confirmed_assets(tmp_path: Path) -> None:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    first_bundle = value.bundle(source_id, run_id, manifest)
    first = value.publish(first_bundle)
    assert first.status == "DELIVERED"
    creates_after_first = len(value.files.creates)
    second_bundle = value.bundle(source_id, run_id, manifest)
    second = value.publish(second_bundle)
    assert second.status == "DELIVERED"
    new_entries = value.files.creates[creates_after_first:]
    assert [entry["body"]["name"] for entry in new_entries] == [
        f"bundle-index-{second_bundle.delivery_request_id}.json",
        f"delivery-index-{second_bundle.delivery_request_id}.json",
    ]
    assert value.ledger.execute_calls == 0
    assert value.ledger.get_run(run_id) is value.ledger.runs[run_id]
    assert value.evidence.input_reads == 2 and value.evidence.output_reads == 0


def test_moved_output_doc_and_duplicate_folder_are_refused_before_upload(tmp_path: Path) -> None:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    bundle = value.bundle(source_id, run_id, manifest)
    value.docs.metadata[LATEST_ID]["parents"] = ["otherfolderid00001"]
    with pytest.raises(DriveError) as moved:
        value.publish(bundle)
    assert moved.value.code == "OUTPUT_IDENTITY_CHANGED"
    assert value.files.creates == []
    value.docs.metadata[LATEST_ID]["parents"] = [WORKSPACE_ID]
    key = f"result-request:{source_id}"
    for index in (1, 2):
        file_id = f"duplicatefolder{index:05d}"
        value.files.entries[file_id] = value.files._entry(
            file_id, source_id, FOLDER_MIME, RESULTS_ID, key, None,
        )
    duplicate = value.publish(bundle)
    assert duplicate.status == "PENDING" and duplicate.code == "UPLOAD_DUPLICATE_KEY"
    assert value.files.creates == []


def test_moved_run_folder_is_found_by_app_key_and_refused(tmp_path: Path) -> None:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    first_bundle = value.bundle(source_id, run_id, manifest)
    first = value.publish(first_bundle)
    assert first.status == "DELIVERED" and first.run_folder_id is not None
    value.files.entries[first.run_folder_id]["parents"] = ["otherfolderid00001"]
    prior_creates = len(value.files.creates)
    second_bundle = value.bundle(source_id, run_id, manifest)
    refused = value.publish(second_bundle)
    assert refused.status == "PARTIAL" and refused.code == "UPLOAD_FILE_MOVED"
    assert len(value.files.creates) == prior_creates
    assert value.ledger.execute_calls == 0


def test_wrong_workspace_output_pair_is_refused_before_remote_write(tmp_path: Path) -> None:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    bundle = value.bundle(source_id, run_id, manifest)
    value.registry.workspace_id = "another-area"
    with pytest.raises(DriveRegistryError) as error:
        value.publish(bundle)
    assert error.value.code == "OUTPUT_SCOPE_MISMATCH"
    assert value.registry.calls == [("fixture", "area")]
    assert value.files.creates == [] and value.docs.writes == []
    assert value.ledger.delivery_records == []


def test_moved_results_folder_is_refused_before_remote_write(tmp_path: Path) -> None:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    bundle = value.bundle(source_id, run_id, manifest)
    value.files.entries[RESULTS_ID]["parents"] = ["otherfolderid00001"]
    with pytest.raises(DriveError) as moved:
        value.publish(bundle)
    assert moved.value.code == "UPLOAD_FILE_MOVED"
    assert value.files.creates == [] and value.docs.writes == []
    assert value.ledger.delivery_records == []


def test_existing_asset_checksum_mismatch_refuses_reuse(tmp_path: Path) -> None:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    first_bundle = value.bundle(source_id, run_id, manifest)
    assert value.publish(first_bundle).status == "DELIVERED"
    old_count = len(value.files.creates)
    report = next(entry for entry in value.files.entries.values()
                  if entry.get("name") == "report.md")
    report["appProperties"]["cdr_sha256"] = "0" * 64
    second_bundle = value.bundle(source_id, run_id, manifest)
    failed = value.publish(second_bundle)
    assert failed.status == "PARTIAL" and failed.code == "UPLOAD_CONTENT_DIGEST_MISMATCH"
    assert len(value.files.creates) == old_count
    assert value.ledger.execute_calls == 0


def test_older_delivery_does_not_overwrite_newer_latest_result(tmp_path: Path) -> None:
    value = fixture(tmp_path)
    old_source, old_run, old_manifest = value.add_run(created_at=_timestamp(-120))
    new_source, new_run, new_manifest = value.add_run(created_at=_timestamp(-20))
    new_bundle = value.bundle(new_source, new_run, new_manifest)
    assert value.publish(new_bundle).status == "DELIVERED"
    latest_new = value.docs.text(LATEST_ID)
    old_bundle = value.bundle(old_source, old_run, old_manifest)
    old_result = value.publish(old_bundle)
    assert old_result.status == "DELIVERED" and old_result.latest_skipped_newer is True
    assert value.docs.text(LATEST_ID) == latest_new
    assert value.docs.writes[-1][0] == WORKSPACE_RECEIPT_ID
    assert json.loads(value.docs.text(WORKSPACE_RECEIPT_ID).split("\n", 1)[1])["latest_skipped_newer"] is True
    assert value.ledger.execute_calls == 0


def test_delayed_older_retry_for_same_run_cannot_replace_newer_latest(tmp_path: Path) -> None:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    old_bundle = value.bundle(source_id, run_id, manifest)
    new_bundle = value.bundle(source_id, run_id, manifest)
    assert value.ledger.get(old_bundle.delivery_request_id)["sequence"] < (
        value.ledger.get(new_bundle.delivery_request_id)["sequence"]
    )
    new_result = value.publish(new_bundle)
    assert new_result.status == "DELIVERED"
    newer_latest = value.docs.text(LATEST_ID)
    old_result = value.publish(old_bundle)
    assert old_result.status == "DELIVERED" and old_result.latest_skipped_newer is True
    assert value.docs.text(LATEST_ID) == newer_latest
    assert value.ledger.execute_calls == 0


def test_equal_sequence_for_distinct_retry_is_ambiguous_before_write(tmp_path: Path) -> None:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    first_bundle = value.bundle(source_id, run_id, manifest)
    second_bundle = value.bundle(source_id, run_id, manifest)
    value.ledger.operations[second_bundle.delivery_request_id]["sequence"] = (
        value.ledger.operations[first_bundle.delivery_request_id]["sequence"]
    )
    assert value.publish(first_bundle).status == "DELIVERED"
    count = len(value.files.creates)
    with pytest.raises(DeliveryError) as ambiguous:
        value.publish(second_bundle)
    assert ambiguous.value.code == "LATEST_ORDER_AMBIGUOUS"
    assert len(value.files.creates) == count


def test_reconcile_lost_folder_ack_confirms_exact_remote_id_without_replay(
    tmp_path: Path,
) -> None:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    bundle = value.bundle(source_id, run_id, manifest)
    value.files.fault_name = source_id
    value.files.fault_after_write = True
    first = value.publish(bundle)
    assert first.status == "PENDING" and first.code == "UPLOAD_WRITE_UNCERTAIN"
    effect = next(item for item in value.ledger.operations[bundle.delivery_request_id]["effects"]
                  if item["step_id"] == "drive-request-folder")
    assert effect["state"] == "INTENDED"
    effect["state"] = "UNKNOWN"
    value.ledger.operations[bundle.delivery_request_id]["status"] = "FAULT"
    created = value.files.creates[0]
    count = len(value.files.creates)
    result = value.publisher.reconcile_effect(
        bundle.delivery_request_id, effect["effect_id"],
        receipt_key="workspace-receipt", latest_result_key="workspace-result",
    )
    assert result.state == "CONFIRMED" and result.provider_id == created["id"]
    assert effect["state"] == "CONFIRMED" and effect["provider_id"] == created["id"]
    assert len(value.files.creates) == count and value.docs.writes == []


def test_reconcile_lost_file_ack_confirms_exact_remote_id_without_replay(
    tmp_path: Path,
) -> None:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    bundle = value.bundle(source_id, run_id, manifest)
    value.files.fault_name = "report.md"
    value.files.fault_after_write = True
    first = value.publish(bundle)
    assert first.status == "PARTIAL" and first.code == "UPLOAD_WRITE_UNCERTAIN"
    effect = next(item for item in value.ledger.operations[bundle.delivery_request_id]["effects"]
                  if item["intent"].get("name") == "report.md")
    assert effect["state"] == "INTENDED"
    effect["state"] = "UNKNOWN"
    value.ledger.operations[bundle.delivery_request_id]["status"] = "FAULT"
    created = next(item for item in value.files.creates
                   if item["body"]["name"] == "report.md")
    count = len(value.files.creates)
    result = value.publisher.reconcile_effect(
        bundle.delivery_request_id, effect["effect_id"],
        receipt_key="workspace-receipt", latest_result_key="workspace-result",
    )
    assert result.state == "CONFIRMED" and result.provider_id == created["id"]
    assert effect["state"] == "CONFIRMED" and effect["provider_id"] == created["id"]
    assert len(value.files.creates) == count and value.docs.writes == []


def test_reconcile_negative_lookup_remains_unresolved_without_create(tmp_path: Path) -> None:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    bundle = value.bundle(source_id, run_id, manifest)
    value.files.fault_name = source_id
    first = value.publish(bundle)
    assert first.status == "PENDING" and first.code == "UPLOAD_WRITE_UNCERTAIN"
    effect = next(item for item in value.ledger.operations[bundle.delivery_request_id]["effects"]
                  if item["step_id"] == "drive-request-folder")
    effect["state"] = "UNKNOWN"
    value.ledger.operations[bundle.delivery_request_id]["status"] = "FAULT"
    result = value.publisher.reconcile_effect(
        bundle.delivery_request_id, effect["effect_id"],
        receipt_key="workspace-receipt", latest_result_key="workspace-result",
    )
    assert result.state == "UNRESOLVED" and result.code == "REMOTE_EFFECT_NOT_OBSERVED"
    assert effect["state"] == "UNKNOWN" and effect["provider_id"] is None
    assert value.files.creates == [] and value.docs.writes == []


def test_reconcile_lost_docs_ack_confirms_desired_digest(tmp_path: Path) -> None:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    bundle = value.bundle(source_id, run_id, manifest)
    value.ledger.confirm_fault_step = "drive-doc:" + hashlib.sha256(
        b"workspace-result",
    ).hexdigest()
    first = value.publish(bundle)
    assert first.status == "PARTIAL" and first.code == "DELIVERY_IO_FAILED"
    assert [file_id for file_id, _ in value.docs.writes] == [LATEST_ID]
    effect = next(item for item in value.ledger.operations[bundle.delivery_request_id]["effects"]
                  if item["step_id"] == value.ledger.confirm_fault_step)
    assert effect["state"] == "INTENDED"
    effect["state"] = "UNKNOWN"
    value.ledger.confirm_fault_step = None
    value.ledger.operations[bundle.delivery_request_id]["status"] = "FAULT"
    count = len(value.files.creates)
    writes = len(value.docs.writes)
    result = value.publisher.reconcile_effect(
        bundle.delivery_request_id, effect["effect_id"],
        receipt_key="workspace-receipt", latest_result_key="workspace-result",
    )
    assert result.state == "CONFIRMED" and result.provider_id == LATEST_ID
    assert effect["state"] == "CONFIRMED" and effect["provider_id"] == LATEST_ID
    assert len(value.files.creates) == count and len(value.docs.writes) == writes


def test_reconcile_changed_doc_remains_unresolved(tmp_path: Path) -> None:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    bundle = value.bundle(source_id, run_id, manifest)
    value.ledger.confirm_fault_step = "drive-doc:" + hashlib.sha256(
        b"workspace-result",
    ).hexdigest()
    assert value.publish(bundle).status == "PARTIAL"
    effect = next(item for item in value.ledger.operations[bundle.delivery_request_id]["effects"]
                  if item["step_id"] == value.ledger.confirm_fault_step)
    effect["state"] = "UNKNOWN"
    value.ledger.confirm_fault_step = None
    value.ledger.operations[bundle.delivery_request_id]["status"] = "FAULT"
    value.docs.documents[LATEST_ID] = _document(LATEST_ID, "TAMPERED\n", "external-rev")
    count = len(value.files.creates)
    writes = len(value.docs.writes)
    result = value.publisher.reconcile_effect(
        bundle.delivery_request_id, effect["effect_id"],
        receipt_key="workspace-receipt", latest_result_key="workspace-result",
    )
    assert result.state == "UNRESOLVED" and result.code == "REMOTE_EFFECT_NOT_OBSERVED"
    assert effect["state"] == "UNKNOWN" and effect["provider_id"] is None
    assert len(value.files.creates) == count and len(value.docs.writes) == writes


def test_results_folder_moved_after_create_never_reports_delivered(tmp_path: Path) -> None:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    bundle = value.bundle(source_id, run_id, manifest)
    value.files.move_results_after_create_name = source_id
    result = value.publish(bundle)
    assert result.status == "PARTIAL"
    assert result.code == "UPLOAD_FILE_MOVED"
    assert value.ledger.operations[bundle.delivery_request_id]["delivery_status"] == "PARTIAL"
    assert value.docs.writes == []
    assert len(value.files.creates) == 1


def test_tampered_input_inventory_with_matching_local_index_fails_durable_hash(
    tmp_path: Path,
) -> None:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    bundle = value.bundle(source_id, run_id, manifest)
    input_item = next(item for item in bundle.items if item.name == "input-manifest.json")
    forged = json.loads(input_item.path.read_bytes())
    forged["files"] = [{"path": "forged.py", "sha256": "f" * 64}]
    raw = (json.dumps(forged, sort_keys=True) + "\n").encode()
    input_item.path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    index_item = next(item for item in bundle.items if item.name == "bundle-index.json")
    index = json.loads(index_item.path.read_bytes())
    indexed_input = next(item for item in index["items"] if item["name"] == "input-manifest.json")
    indexed_input["sha256"] = digest
    indexed_input["size_bytes"] = len(raw)
    index_raw = (json.dumps(index, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")) + "\n").encode()
    index_item.path.write_bytes(index_raw)
    tampered = replace(bundle, items=tuple(
        replace(item, sha256=digest, size_bytes=len(raw))
        if item.name == "input-manifest.json" else
        replace(item, sha256=hashlib.sha256(index_raw).hexdigest(),
                size_bytes=len(index_raw))
        if item.name == "bundle-index.json" else item
        for item in bundle.items
    ))
    with pytest.raises(DeliveryError) as error:
        value.publish(tampered)
    assert error.value.code == "DELIVERY_DURABLE_MANIFEST_MISMATCH"
    assert value.files.creates == [] and value.docs.writes == []
