"""Offline source-run PUBLISH_REPORT: real ledger/evidence, fake Drive delivery."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Any, cast
from uuid import uuid4

from test_run_evidence import _evidence, _fixture

from runner_app.adapters.drive_delivery import DeliveryBundle
from runner_app.adapters.drive_docs import DriveError
from runner_app.adapters.drive_publish import DeliveryResult, _saved_partial_report_status
from runner_app.domain.commands import freeze, validate_shape
from runner_app.engine.service import Context
from runner_app.storage.ledger import Ledger
from runner_app.storage.run_evidence import RunEvidenceStore
from runner_app.workflows.publish_report import PublishReportWorkflow


class FakePublisher:
    def __init__(self, ledger: Ledger, mode: str = "delivered") -> None:
        self.ledger = ledger
        self.mode = mode
        self.calls: list[DeliveryBundle] = []

    def publish(self, bundle: DeliveryBundle, *, receipt_key: str,
                latest_result_key: str) -> DeliveryResult:
        assert (receipt_key, latest_result_key) == ("receipt", "latest-result")
        preflight = self.ledger.require_delivery_preflight(
            bundle.delivery_request_id, bundle.run_id,
        )
        assert preflight.action == "PUBLISH_REPORT"
        assert bundle.source_request_id == preflight.source_request_id
        assert bundle.delivery_request_id != bundle.source_request_id
        assert (bundle.root / "run-manifest.json").is_file()
        assert (bundle.root / "input-manifest.json").is_file()
        assert (bundle.root / "bundle-index.json").is_file()
        self.calls.append(bundle)
        effect = self.ledger.begin_effect(
            bundle.delivery_request_id, "fake-drive-asset", "drive_file",
            {"run_id": bundle.run_id, "name": "report.md"},
        )
        if self.mode == "ambiguous":
            raise DriveError("UPLOAD_PROVIDER_WRITE_UNKNOWN", "Synthetic response loss")
        self.ledger.confirm_effect(effect, "fake-report-file-id")
        if self.mode == "partial":
            return DeliveryResult(
                "PARTIAL", "FAKE_LATER_UPLOAD_FAILED", "request-folder", "run-folder",
                ("report.md",),
            )
        return DeliveryResult(
            "DELIVERED", None, "request-folder", "run-folder",
            ("report.md",), index_file_id="delivery-index",
        )


def report_command(ledger: Ledger, head: str, run_id: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    state = ledger.state()
    command = {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "PUBLISH_REPORT", "project_id": "fixture", "workspace_id": "area",
        "expected_head": head, "params": {"source_run_id": run_id},
        "intent": "Deliver the saved run",
    }
    validate_shape(command)
    assert ledger.admit(
        command, freeze(command).digest, "local-owner", ("workspace:fixture/area",),
    ).execute
    return command


def recorded_case(
    tmp_path: Path, *, source_action: str = "VERIFY",
    prior_delivery: str | None = None,
) -> tuple[RunEvidenceStore, Ledger, dict[str, Any], str, str]:
    store, registry, ledger, source_id, snapshot_id, head, _ = _fixture(
        tmp_path, action=source_action,
    )
    run, provenance, files = _evidence(store, source_id, snapshot_id, registry)
    store.record_contained_run(run, snapshot_id=snapshot_id,
                               provenance=provenance, files=files)
    result: dict[str, Any] = {
        "run_id": run["run_id"], "execution": run["execution"],
        "publication": {"status": "NOT_REQUESTED"},
        "delivery": {"status": prior_delivery or "NOT_ATTEMPTED", "reason": None},
    }
    if source_action == "DELIVER_VERIFY_PUBLISH":
        result["operation_manifest"] = {
            "schema_version": "1.0", "request_id": source_id,
            "project_id": "fixture", "workspace_id": "area",
            "status": "COMPLETED", "run_ids": [run["run_id"]],
            "publication": {
                "status": "NOT_REQUESTED", "repository": None, "branch": None,
                "commit": None, "pull_request_url": None, "reason": "POLICY_NEVER",
            },
            "delivery": {"status": "PENDING", "reason": None},
            "outputs": run["outputs"], "effects": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    if prior_delivery is not None:
        ledger.record_delivery(source_id, prior_delivery, "FAKE_DELIVERY_FAILURE")
    source_status = "BLOCKED" if prior_delivery is not None else "COMPLETED"
    ledger.finish(source_id, source_status,
                  code="FAKE_DELIVERY_FAILURE" if prior_delivery else None,
                  result=result)
    return store, ledger, run, head, source_id


def actor(ledger: Ledger, request_id: str) -> dict[str, Any]:
    operation = ledger.get(request_id)
    assert operation is not None
    return cast(dict[str, Any], operation)


def workflow(ledger: Ledger, store: RunEvidenceStore, publisher: FakePublisher,
             tmp_path: Path) -> PublishReportWorkflow:
    return PublishReportWorkflow(
        ledger, store, publisher, bundle_parent=tmp_path / "bundles",
        receipt_key="receipt", latest_result_key="latest-result",
    )


def test_publish_report_delivers_saved_verify_run_without_reexecution(tmp_path: Path) -> None:
    store, ledger, run, head, source_id = recorded_case(tmp_path)
    command = report_command(ledger, head, run["run_id"])
    publisher = FakePublisher(ledger)
    handler = workflow(ledger, store, publisher, tmp_path)

    result = handler(freeze(command), Context(Event(), ledger))

    assert result.status == "COMPLETED" and result.code is None
    assert result.result["run_id"] == run["run_id"]
    assert result.result["source_request_id"] == source_id
    assert result.result["execution"] == run["execution"]
    assert result.result["publication"]["status"] == "NOT_REQUESTED"
    assert result.result["delivery"]["status"] == "DELIVERED"
    assert len(publisher.calls) == 1
    bundle = publisher.calls[0]
    saved = json.loads((bundle.root / "run-manifest.json").read_text())
    assert saved == run
    manifest = json.loads((bundle.root / "operation-manifest.json").read_text())
    assert manifest["request_id"] == source_id
    assert manifest["status"] == "COMPLETED"
    assert manifest["outputs"] == run["outputs"]
    assert actor(ledger, command["request_id"])["delivery_status"] == "DELIVERED"
    ledger.finish(command["request_id"], result.status, result=result.result)
    with ledger._connection() as connection:
        released = connection.execute(
            "SELECT 1 FROM run_delivery_claim_releases WHERE delivery_request_id=?",
            (command["request_id"],),
        ).fetchone()
    assert released is not None


def test_publish_report_keeps_partial_delivery_separate_from_saved_test_result(
    tmp_path: Path,
) -> None:
    store, ledger, run, head, _ = recorded_case(tmp_path)
    command = report_command(ledger, head, run["run_id"])
    publisher = FakePublisher(ledger, "partial")
    result = workflow(ledger, store, publisher, tmp_path)(
        freeze(command), Context(Event(), ledger),
    )
    assert result.status == "BLOCKED" and result.code == "FAKE_LATER_UPLOAD_FAILED"
    assert result.result["execution"]["outcome"] == run["execution"]["outcome"]
    assert result.result["delivery"]["status"] == "PARTIAL"
    assert actor(ledger, command["request_id"])["delivery_status"] == "PARTIAL"
    assert len(publisher.calls) == 1


def test_publish_report_reuses_exact_saved_full_route_manifest_after_partial_delivery(
    tmp_path: Path,
) -> None:
    store, ledger, run, head, source_id = recorded_case(
        tmp_path, source_action="DELIVER_VERIFY_PUBLISH", prior_delivery="PARTIAL",
    )
    source = ledger.get(source_id)
    assert source is not None and source["status"] == "BLOCKED"
    saved_manifest = source["result"]["operation_manifest"]
    assert saved_manifest["status"] == "COMPLETED"
    command = report_command(ledger, head, run["run_id"])
    publisher = FakePublisher(ledger)
    result = workflow(ledger, store, publisher, tmp_path)(
        freeze(command), Context(Event(), ledger),
    )
    assert result.status == "COMPLETED"
    assert result.result["operation_manifest"] == saved_manifest
    assert result.result["publication"] == saved_manifest["publication"]
    assert len(publisher.calls) == 1
    uploaded = json.loads(
        (publisher.calls[0].root / "operation-manifest.json").read_text(),
    )
    assert uploaded == saved_manifest


def test_publish_report_preserves_ambiguous_write_and_does_not_replay(
    tmp_path: Path,
) -> None:
    store, ledger, run, head, _ = recorded_case(tmp_path)
    command = report_command(ledger, head, run["run_id"])
    publisher = FakePublisher(ledger, "ambiguous")
    handler = workflow(ledger, store, publisher, tmp_path)
    first = handler(freeze(command), Context(Event(), ledger))
    assert first.status == "BLOCKED" and first.code == "DELIVERY_OUTCOME_UNKNOWN"
    assert first.result["delivery"]["status"] == "PENDING"
    assert actor(ledger, command["request_id"])["delivery_status"] == "PENDING"
    second = handler(freeze(command), Context(Event(), ledger))
    assert second.status == "BLOCKED" and second.code == "BUNDLE_EXISTS"
    assert second.result["delivery"]["status"] == "PENDING"
    assert len(publisher.calls) == 1
    terminal = ledger.finish(command["request_id"], second.status,
                             code=second.code, result=second.result)
    assert terminal["code"] == "NEEDS_RECONCILIATION"


def test_publish_report_rejects_wrong_frozen_identity_before_bundle(tmp_path: Path) -> None:
    store, ledger, run, head, _ = recorded_case(tmp_path)
    command = report_command(ledger, head, run["run_id"])
    publisher = FakePublisher(ledger)
    changed = {**command, "intent": "changed after admission"}
    result = workflow(ledger, store, publisher, tmp_path)(
        freeze(changed), Context(Event(), ledger),
    )
    assert result.status == "BLOCKED" and result.code == "WORKFLOW_ADMISSION_MISMATCH"
    assert publisher.calls == []
    assert not (tmp_path / "bundles").exists()


def test_publish_report_rejects_mismatched_saved_manifest_without_delivery(
    tmp_path: Path,
) -> None:
    store, ledger, run, head, source_id = recorded_case(
        tmp_path, source_action="DELIVER_VERIFY_PUBLISH", prior_delivery="PARTIAL",
    )
    source = ledger.get(source_id)
    assert source is not None
    # Durable source result claims another contained run. The ledger's read-only
    # preflight checks this before any local bundle or Drive write.
    with ledger._transaction() as connection:
        altered = source["result"]
        altered["operation_manifest"]["run_ids"] = [str(uuid4())]
        connection.execute(
            "UPDATE operations SET result_json=? WHERE request_id=?",
            (json.dumps(altered), source_id),
        )
    command = report_command(ledger, head, run["run_id"])
    publisher = FakePublisher(ledger)
    result = workflow(ledger, store, publisher, tmp_path)(
        freeze(command), Context(Event(), ledger),
    )
    assert result.status == "BLOCKED"
    assert result.code in {"DELIVERY_SOURCE_MISMATCH", "SOURCE_OPERATION_MANIFEST_MISMATCH"}
    assert publisher.calls == []


def test_saved_partial_status_exception_is_exact_and_rejects_mismatch(tmp_path: Path) -> None:
    store, ledger, run, head, source_id = recorded_case(
        tmp_path, source_action="DELIVER_VERIFY_PUBLISH", prior_delivery="PARTIAL",
    )
    command = report_command(ledger, head, run["run_id"])
    preflight = ledger.require_delivery_preflight(command["request_id"], run["run_id"])
    source = actor(ledger, source_id)
    saved = source["result"]["operation_manifest"]
    assert _saved_partial_report_status(preflight, saved, source)
    assert not _saved_partial_report_status(
        replace(preflight, source_operation_manifest_sha256=None), saved, source,
    )
    changed = {**saved, "delivery": {"status": "FAILED", "reason": None}}
    assert not _saved_partial_report_status(preflight, changed, source)
    wrong_source = {**source, "result": {
        **source["result"], "delivery": {"status": "DELIVERED", "reason": None},
    }}
    assert not _saved_partial_report_status(preflight, saved, wrong_source)
    assert store.read_input_manifest(run["run_id"])
