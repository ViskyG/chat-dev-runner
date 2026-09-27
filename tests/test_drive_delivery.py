"""Offline Drive bundle tests: frozen run evidence, explicit privacy approval, no provider calls."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from runner_app.adapters.drive_delivery import DeliveryError, prepare_run_bundle
from runner_app.domain.commands import freeze
from runner_app.storage.run_evidence import OutputHandle


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _output(output_id: str, kind: str, media_type: str, content: bytes, path: str) -> dict:
    return {
        "output_id": output_id, "kind": kind, "format": "text/plain" if kind == "report" else "raw-bytes-v1",
        "media_type": media_type, "relative_path": path, "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(), "complete": True,
        "truncated_reason": None, "publication": "local_only",
    }


@dataclass
class Fixture:
    ledger: object
    evidence: object
    run_id: str
    source_request_id: str
    operation_manifest: dict
    content: bytes


class FakeLedger:
    def __init__(self, run: dict, operation: dict) -> None:
        self.run = run
        self.operation = operation
        self.calls: list[str] = []

    def get_run(self, run_id: str) -> dict | None:
        self.calls.append("get_run")
        return self.run if run_id == self.run["run_id"] else None

    def get(self, request_id: str) -> dict | None:
        self.calls.append("get")
        return self.operation if request_id == self.operation["request_id"] else None


class FakeEvidence:
    def __init__(self, run_id: str, content: bytes, input_manifest: bytes) -> None:
        self.run_id = run_id
        self.content = content
        self.input_manifest = input_manifest
        self.calls = 0
        self.input_calls = 0

    def read_input_manifest(self, run_id: str) -> bytes:
        assert run_id == self.run_id
        self.input_calls += 1
        return self.input_manifest

    def open_output(self, run_id: str, output_id: str) -> OutputHandle:
        assert run_id == self.run_id and output_id == "report-log"
        self.calls += 1
        descriptor = os.memfd_create("fake-registered-output", os.MFD_CLOEXEC)
        os.write(descriptor, self.content)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return OutputHandle(descriptor, len(self.content), hashlib.sha256(self.content).hexdigest(),
                            output_id)


def _fixture(content: bytes) -> Fixture:
    source_request_id, run_id = str(uuid4()), str(uuid4())
    issued = datetime.now(timezone.utc)
    command = {
        "schema_version": "1.0", "request_id": source_request_id, "dispatch": "READY",
        "runner_id": "local-runner", "epoch": str(uuid4()),
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(minutes=5)).isoformat(),
        "action": "VERIFY", "project_id": "fixture", "workspace_id": "area",
        "expected_head": "a" * 40, "params": {"profile_id": "python-quick"},
        "intent": "Verify committed source",
    }
    run = {
        "schema_version": "1.0", "run_id": run_id, "request_id": source_request_id,
        "project_id": "fixture", "workspace_id": "area",
        "snapshot": {"commit": "b" * 40, "tree": "c" * 40,
                     "source_dirty_at_capture": False},
        "operation_status": "COMPLETED",
        "execution": {"outcome": "FAILED", "exit_code": 1, "duration_seconds": 0.1},
        "delivery": {"status": "NOT_ATTEMPTED", "reason": None},
        "outputs": [
            _output("stdout-raw", "console", "application/octet-stream", b"raw secret", "capture/stdout.bin"),
            _output("stderr-raw", "console", "application/octet-stream", b"", "capture/stderr.bin"),
            _output("report-log", "report", "text/plain; charset=utf-8", content, "collected/report-log.txt"),
        ],
        "created_at": _now(),
    }
    source_operation = {"request_id": source_request_id, "command": command,
                        "digest": freeze(command).digest}
    operation_manifest = {
        "schema_version": "1.0", "request_id": source_request_id,
        "project_id": "fixture", "workspace_id": "area",
        "status": "COMPLETED", "run_ids": [run_id],
        "publication": {"status": "NOT_REQUESTED", "repository": None, "branch": None,
                        "commit": None, "pull_request_url": None, "reason": None},
        "delivery": {"status": "PENDING", "reason": None},
        "outputs": [], "effects": [], "created_at": _now(),
    }
    input_manifest = {
        "schema_version": "1.0", "snapshot_id": str(uuid4()),
        "project_id": "fixture", "workspace_id": "area", "base_head": "a" * 40,
        "commit": "b" * 40, "tree": "c" * 40,
        "source_generation": 0, "capture_fingerprint": "d" * 64,
        "provenance": "CLEAN_VERIFY", "dirty_at_capture": False,
        "included_paths": [], "approved_untracked_paths": [], "files": [],
    }
    input_manifest["retained_ref"] = f"refs/cdr/snapshots/{input_manifest['snapshot_id']}"
    input_bytes = (json.dumps(input_manifest, sort_keys=True) + "\n").encode()
    return Fixture(FakeLedger(run, source_operation), FakeEvidence(run_id, content, input_bytes),
                   run_id, source_request_id, operation_manifest, content)


def _prepare(value: Fixture, tmp_path: Path, *, approved: tuple[str, ...] = (),
             reviewed: tuple[str, ...] = ()):
    return prepare_run_bundle(
        value.ledger, value.evidence, value.run_id, str(uuid4()),
        value.operation_manifest, tmp_path / "bundles", approved_output_ids=approved,
        reviewed_output_ids=reviewed,
    )


def test_only_explicit_sanitized_export_enters_immutable_bundle(tmp_path: Path) -> None:
    fixture = _fixture(b"ordinary checked report\n")
    bundle = _prepare(fixture, tmp_path, approved=("report-log",))
    names = {item.name for item in bundle.items}
    assert names == {
        "operation-manifest.json", "run-manifest.json", "input-manifest.json",
        "frozen-command.json",
        "report.md", "bundle-index.json", "exports/report-log-export.txt",
    }
    export = next(item for item in bundle.items if item.source_output_id == "report-log")
    assert export.path.read_bytes().startswith(b"Sanitized export")
    assert export.sha256 != hashlib.sha256(fixture.content).hexdigest()
    assert all("stdout" not in item.name for item in bundle.items)
    index = json.loads((bundle.root / "bundle-index.json").read_text())
    assert index["raw_outputs_local_only"] == ["stdout-raw", "stderr-raw", "report-log"]
    assert index["tested_commit"] == "b" * 40
    assert fixture.evidence.calls == 1
    assert fixture.evidence.input_calls == 1
    assert fixture.ledger.calls == ["get_run", "get"]
    assert all(item.path.is_relative_to(bundle.root) for item in bundle.items)


def test_suspicious_output_is_quarantined_until_local_review(tmp_path: Path) -> None:
    secret = b"token=ghp_" + b"A" * 24 + b"\n"
    fixture = _fixture(secret)
    first = _prepare(fixture, tmp_path, approved=("report-log",))
    assert first.quarantined_output_ids == ("report-log",)
    assert not any(item.source_output_id for item in first.items)
    assert secret not in b"".join(item.path.read_bytes() for item in first.items)
    reviewed = _prepare(fixture, tmp_path, approved=("report-log",), reviewed=("report-log",))
    export = next(item for item in reviewed.items if item.source_output_id == "report-log")
    assert b"[REDACTED]" in export.path.read_bytes()
    assert secret not in export.path.read_bytes()
    assert reviewed.quarantined_output_ids == ()
    assert fixture.evidence.calls == 2


def test_retry_uses_saved_run_and_new_bundle_identity_without_execution(tmp_path: Path) -> None:
    fixture = _fixture(b"report\n")
    first = _prepare(fixture, tmp_path)
    second = _prepare(fixture, tmp_path)
    assert first.root != second.root and first.run_id == second.run_id
    assert first.source_request_id == fixture.source_request_id
    stable = lambda bundle: {item.name: item.sha256 for item in bundle.items
                             if item.name != "bundle-index.json"}
    assert stable(first) == stable(second)
    assert (first.root / "bundle-index.json").read_bytes() != (
        second.root / "bundle-index.json"
    ).read_bytes()
    assert fixture.evidence.calls == 0


def test_mismatched_operation_or_tampered_registered_output_is_refused(tmp_path: Path) -> None:
    fixture = _fixture(b"report\n")
    wrong = {**fixture.operation_manifest, "project_id": "other"}
    with pytest.raises(DeliveryError) as error:
        prepare_run_bundle(fixture.ledger, fixture.evidence, fixture.run_id, str(uuid4()),
                           wrong, tmp_path / "bundles")
    assert error.value.code == "OPERATION_MANIFEST_MISMATCH"
    fixture.evidence.content = b"tampered\n"
    with pytest.raises(DeliveryError) as error:
        _prepare(fixture, tmp_path, approved=("report-log",))
    assert error.value.code == "EXPORT_SOURCE_CHANGED"
    assert not list((tmp_path / "bundles").iterdir())


def test_binary_raw_stream_cannot_be_allowlisted_for_text_upload(tmp_path: Path) -> None:
    fixture = _fixture(b"report\n")
    with pytest.raises(DeliveryError) as error:
        _prepare(fixture, tmp_path, approved=("stdout-raw",))
    assert error.value.code == "EXPORT_FORMAT_UNSUPPORTED"


def test_input_manifest_mismatch_refuses_before_bundle_creation(tmp_path: Path) -> None:
    fixture = _fixture(b"report\n")
    input_value = json.loads(fixture.evidence.input_manifest)
    input_value["commit"] = "f" * 40
    fixture.evidence.input_manifest = json.dumps(input_value).encode()
    with pytest.raises(DeliveryError) as error:
        _prepare(fixture, tmp_path)
    assert error.value.code == "INPUT_MANIFEST_MISMATCH"
    assert not (tmp_path / "bundles").exists()


def test_free_form_command_intent_is_redacted_from_every_bundle_item(tmp_path: Path) -> None:
    fixture = _fixture(b"report\n")
    secret = "token=ghp_" + "A" * 24
    fixture.ledger.operation["command"]["intent"] = f"Review source {secret}"
    fixture.ledger.operation["digest"] = freeze(fixture.ledger.operation["command"]).digest
    bundle = _prepare(fixture, tmp_path)
    assert all(secret.encode() not in item.path.read_bytes() for item in bundle.items)
    view = json.loads((bundle.root / "frozen-command.json").read_bytes())
    assert view["kind"] == "redacted-command/v1"
    assert view["request_digest"] == fixture.ledger.operation["digest"]
    assert view["redacted_fields"] == ["intent"]
    assert view["command"]["intent"] == "[REDACTED FOR DELIVERY]"
    assert fixture.evidence.input_calls == 1
