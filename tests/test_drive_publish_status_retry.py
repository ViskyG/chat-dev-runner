"""Regression: retry exactly a saved pre-delivery manifest after partial Drive delivery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_drive_publish import _timestamp, fixture

from runner_app.adapters.drive_delivery import DeliveryError
from runner_app.domain.commands import freeze


def _partial_full_route(tmp_path: Path) -> tuple[Any, str, str, dict[str, Any]]:
    value = fixture(tmp_path)
    source_id, run_id, manifest = value.add_run(created_at=_timestamp(-30))
    source = value.ledger.operations[source_id]
    command = source["command"]
    command["action"] = "DELIVER_VERIFY_PUBLISH"
    command["params"] = {
        "changeset_doc_id": "changeset-document-123",
        "changeset_revision": "rev-1",
        "profile_id": "python-quick",
        "publication": "never",
    }
    source["digest"] = freeze(command).digest
    source["action"] = "DELIVER_VERIFY_PUBLISH"
    source["status"] = "BLOCKED"
    source["delivery_status"] = "PARTIAL"
    source["result"] = {
        "run_id": run_id,
        "delivery": {"status": "PARTIAL", "reason": "UPLOAD_WRITE_UNCERTAIN"},
        "operation_manifest": manifest,
    }
    saved_hash = hashlib.sha256(
        (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False) + "\n").encode(),
    ).hexdigest()
    original_preflight = value.ledger.require_delivery_preflight

    def preflight(delivery_id: str, selected_run_id: str) -> Any:
        base = original_preflight(delivery_id, selected_run_id)
        return replace(
            base, source_action="DELIVER_VERIFY_PUBLISH", source_status="BLOCKED",
            source_operation_manifest_sha256=saved_hash,
        )

    value.ledger.require_delivery_preflight = preflight
    return value, source_id, run_id, manifest


def test_saved_partial_full_route_manifest_is_delivered_without_rewriting_status(
    tmp_path: Path,
) -> None:
    value, source_id, run_id, manifest = _partial_full_route(tmp_path)
    bundle = value.bundle(source_id, run_id, manifest)
    result = value.publish(bundle)
    assert result.status == "DELIVERED"
    uploaded = next(
        entry for entry in value.files.creates
        if entry["body"]["name"] == "operation-manifest.json"
    )
    assert json.loads(uploaded["content"]) == manifest
    assert uploaded["content"] == (json.dumps(
        manifest, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ) + "\n").encode()
    assert manifest["status"] == "COMPLETED"


def test_cancelled_after_partial_delivery_retries_exact_saved_manifest(tmp_path: Path) -> None:
    value, source_id, run_id, manifest = _partial_full_route(tmp_path)
    value.ledger.operations[source_id]["status"] = "CANCELLED"
    original = value.ledger.require_delivery_preflight

    def cancelled_preflight(delivery_id: str, selected_run_id: str) -> Any:
        return replace(original(delivery_id, selected_run_id), source_status="CANCELLED")

    value.ledger.require_delivery_preflight = cancelled_preflight
    bundle = value.bundle(source_id, run_id, manifest)
    result = value.publish(bundle)
    assert result.status == "DELIVERED"
    assert manifest["status"] == "COMPLETED"


def test_changed_saved_manifest_is_refused_before_any_remote_write(tmp_path: Path) -> None:
    value, source_id, run_id, manifest = _partial_full_route(tmp_path)
    bundle = value.bundle(source_id, run_id, manifest)
    value.ledger.operations[source_id]["result"]["operation_manifest"] = {
        **manifest, "delivery": {"status": "FAILED", "reason": None},
    }
    with pytest.raises(DeliveryError) as error:
        value.publish(bundle)
    assert error.value.code == "DELIVERY_SOURCE_CHANGED"
    assert value.files.creates == []
