"""Explicit delivery of an existing immutable contained run.

PUBLISH_REPORT never starts an executor, writes Git refs, or creates a pull
request. Engine admission and the ledger's run claim select one exact saved run.
A new request prepares its own immutable bundle, while DriveDeliveryPublisher
owns every remote effect intent and reconciliation boundary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator, FormatChecker

from runner_app.adapters.drive_delivery import DeliveryBundle, DeliveryError, prepare_run_bundle
from runner_app.adapters.drive_docs import DriveError
from runner_app.adapters.drive_publish import DeliveryResult, _saved_partial_report_status
from runner_app.domain.commands import FrozenCommand, validate_shape
from runner_app.engine.service import Context, OperationResult
from runner_app.resources import load_schema
from runner_app.storage.ledger import TERMINAL, DeliveryPreflight, Ledger, LedgerError
from runner_app.storage.run_evidence import EvidenceError, RunEvidenceStore

_OPERATION = Draft202012Validator(load_schema("operation-manifest"), format_checker=FormatChecker())

class DeliveryPublisher(Protocol):
    def publish(self, bundle: DeliveryBundle, *, receipt_key: str,
                latest_result_key: str) -> DeliveryResult: ...


class TrustedReportExportPolicy(Protocol):
    """Owner-local output IDs; no command or Drive text supplies export policy."""

    def __call__(self, run: Mapping[str, Any]) -> tuple[Sequence[str], Sequence[str]]: ...


def _manifest_hash(value: Mapping[str, Any]) -> str:
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _verify_manifest(source: Mapping[str, Any], run: Mapping[str, Any],
                     manifest: Mapping[str, Any],
                     preflight: DeliveryPreflight) -> dict[str, Any]:
    value = dict(manifest)
    if (not _OPERATION.is_valid(value) or value["status"] == "RUNNING"
            or value["request_id"] != source["request_id"]
            or value["project_id"] != run["project_id"]
            or value["workspace_id"] != run["workspace_id"]
            or run["run_id"] not in value["run_ids"]
            or value["outputs"] != run["outputs"]
            or (value["status"] != source["status"]
                and not _saved_partial_report_status(preflight, value, source))):
        raise DeliveryError("SOURCE_OPERATION_MANIFEST_MISMATCH",
                            "Source operation manifest differs from the contained run")
    if (preflight.source_operation_manifest_sha256 is not None
            and _manifest_hash(value) != preflight.source_operation_manifest_sha256):
        raise DeliveryError("SOURCE_OPERATION_MANIFEST_MISMATCH",
                            "Stored operation manifest hash differs")
    return value


def _from_terminal_verify(source: Mapping[str, Any], run: Mapping[str, Any]) -> dict[str, Any]:
    """Derive a stable report view only when VERIFY had no stored v1 manifest."""
    effects = [
        {"kind": item["kind"], "provider_id": item["provider_id"] or "",
         "url": None, "state": item["state"]}
        for item in source["effects"]
        if item["kind"] in {"github_repository", "git_ref", "pull_request", "drive_file"}
    ]
    return {
        "schema_version": "1.0", "request_id": source["request_id"],
        "project_id": run["project_id"], "workspace_id": run["workspace_id"],
        "status": source["status"], "run_ids": [run["run_id"]],
        "publication": {
            "status": "NOT_REQUESTED", "repository": None, "branch": None,
            "commit": None, "pull_request_url": None, "reason": None,
        },
        "delivery": {"status": source["delivery_status"], "reason": None},
        "outputs": run["outputs"], "effects": effects,
        "created_at": source["updated_at"],
    }


def _delivery_axes(run_id: str, run: Mapping[str, Any],
                   operation_manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run_id, "source_run_id": run_id,
        "source_request_id": run["request_id"],
        "execution": run["execution"],
        "publication": operation_manifest["publication"],
        "delivery": {"status": "NOT_ATTEMPTED", "reason": None},
    }


class PublishReportWorkflow:
    """Deliver one terminal contained run using its saved evidence and source facts."""

    def __init__(
        self, ledger: Ledger, evidence: RunEvidenceStore, delivery: DeliveryPublisher,
        *, bundle_parent: Path, receipt_key: str, latest_result_key: str,
        export_policy_for_run: TrustedReportExportPolicy | None = None,
    ) -> None:
        if ledger.database.path != evidence.database.path:
            raise ValueError("Report workflow and run evidence must share one admission database")
        if not receipt_key or not latest_result_key or receipt_key == latest_result_key:
            raise ValueError("Two distinct registered output document keys are required")
        self.ledger = ledger
        self.evidence = evidence
        self.delivery = delivery
        self.bundle_parent = Path(bundle_parent)
        self.receipt_key = receipt_key
        self.latest_result_key = latest_result_key
        self.export_policy_for_run = export_policy_for_run

    def __call__(self, frozen: FrozenCommand, context: Context) -> OperationResult:
        command = frozen.command
        validate_shape(command)
        if command["action"] != "PUBLISH_REPORT":
            return OperationResult("BLOCKED", "WRONG_WORKFLOW_ACTION")
        request_id = command["request_id"]
        run_id = command["params"]["source_run_id"]
        if context.ledger.database.path != self.ledger.database.path:
            return OperationResult("FAULT", "WORKFLOW_LEDGER_MISMATCH")
        actor = self.ledger.get(request_id)
        if (actor is None or actor["status"] != "RUNNING"
                or actor["digest"] != frozen.digest or actor["command"] != command
                or not self.ledger.holds_lock(
                    request_id, f"workspace:{command['project_id']}/{command['workspace_id']}"
                )):
            return OperationResult("BLOCKED", "WORKFLOW_ADMISSION_MISMATCH")
        if context.cancel.is_set():
            return OperationResult("CANCELLED", "CANCEL_REQUESTED")
        try:
            preflight = self.ledger.require_delivery_preflight(request_id, run_id)
        except LedgerError as error:
            return OperationResult("BLOCKED", error.code)
        if (preflight.action != "PUBLISH_REPORT"
                or preflight.delivery_request_id != request_id
                or preflight.run_id != run_id
                or preflight.project_id != command["project_id"]
                or preflight.workspace_id != command["workspace_id"]
                or preflight.lock_scope !=
                f"workspace:{command['project_id']}/{command['workspace_id']}"):
            return OperationResult("BLOCKED", "DELIVERY_SOURCE_MISMATCH")
        run = self.ledger.get_run(run_id)
        source = self.ledger.get(preflight.source_request_id)
        if (run is None or source is None or source["status"] not in TERMINAL
                or run.get("run_id") != run_id
                or run.get("request_id") != source["request_id"]
                or run.get("project_id") != command["project_id"]
                or run.get("workspace_id") != command["workspace_id"]
                or source["action"] not in {"VERIFY", "DELIVER_VERIFY_PUBLISH"}
                or source["action"] != preflight.source_action
                or source["status"] != preflight.source_status
                or source["command"].get("action") != source["action"]
                or source["project_id"] != command["project_id"]
                or source["workspace_id"] != command["workspace_id"]):
            return OperationResult("BLOCKED", "DELIVERY_SOURCE_MISMATCH")
        result = source.get("result")
        if isinstance(result, dict) and result.get("run_id", run_id) != run_id:
            return OperationResult("BLOCKED", "DELIVERY_SOURCE_MISMATCH")
        saved = result.get("operation_manifest") if isinstance(result, dict) else None
        if saved is not None and not isinstance(saved, dict):
            return OperationResult("BLOCKED", "SOURCE_OPERATION_MANIFEST_MISMATCH")
        if saved is None and source["action"] != "VERIFY":
            return OperationResult("BLOCKED", "SOURCE_OPERATION_MANIFEST_MISSING")
        if saved is None and preflight.source_operation_manifest_sha256 is not None:
            return OperationResult("BLOCKED", "SOURCE_OPERATION_MANIFEST_MISMATCH")
        try:
            operation_manifest = _verify_manifest(
                source, run, saved if saved is not None else _from_terminal_verify(source, run),
                preflight,
            )
        except (DeliveryError, ValueError, TypeError) as error:
            code = (error.code if isinstance(error, DeliveryError)
                    else "SOURCE_OPERATION_MANIFEST_MISMATCH")
            return OperationResult("BLOCKED", code)
        axes = _delivery_axes(run_id, run, operation_manifest)
        if context.cancel.is_set():
            return OperationResult("CANCELLED", "CANCEL_REQUESTED", axes)
        approved: Sequence[str] = ()
        reviewed: Sequence[str] = ()
        if self.export_policy_for_run is not None:
            try:
                approved, reviewed = self.export_policy_for_run(run)
            except Exception:  # noqa: BLE001 -- owner policy text could contain private paths
                return self._failure(
                    request_id, axes, "EXPORT_POLICY_PROVIDER_FAILED",
                )
        try:
            bundle = prepare_run_bundle(
                self.ledger, self.evidence, run_id, request_id,
                operation_manifest, self.bundle_parent,
                approved_output_ids=approved, reviewed_output_ids=reviewed,
            )
        except (DeliveryError, EvidenceError) as error:
            return self._failure(request_id, axes, error.code)
        except (OSError, TypeError, ValueError):
            return self._failure(
                request_id, axes, "DELIVERY_BUNDLE_IO_FAILED",
            )
        axes["operation_manifest"] = operation_manifest
        if context.cancel.is_set():
            return OperationResult("CANCELLED", "CANCEL_REQUESTED", axes)
        try:
            delivered = self.delivery.publish(
                bundle, receipt_key=self.receipt_key,
                latest_result_key=self.latest_result_key,
            )
        except DeliveryError as error:
            return self._failure(request_id, axes, error.code)
        except (DriveError, OSError):
            return self._failure(
                request_id, axes, "DELIVERY_OUTCOME_UNKNOWN",
            )
        self.ledger.record_delivery(request_id, delivered.status, delivered.code)
        axes["delivery"] = {
            "status": delivered.status, "reason": delivered.code,
            "request_folder_id": delivered.request_folder_id,
            "run_folder_id": delivered.run_folder_id,
            "confirmed_assets": list(delivered.confirmed_assets),
            "latest_skipped_newer": delivered.latest_skipped_newer,
            "index_file_id": delivered.index_file_id,
        }
        if delivered.status != "DELIVERED":
            return OperationResult(
                "BLOCKED", delivered.code or "DELIVERY_INCOMPLETE", axes,
            )
        return OperationResult("COMPLETED", result=axes)

    def _failure(self, request_id: str, axes: dict[str, Any],
                 code: str) -> OperationResult:
        actor = self.ledger.get(request_id)
        effects = actor["effects"] if actor is not None else []
        confirmed = any(item["kind"] == "drive_file" and item["state"] == "CONFIRMED"
                        for item in effects)
        uncertain = any(item["kind"] == "drive_file"
                        and item["state"] in {"INTENDED", "UNKNOWN"} for item in effects)
        previous = actor.get("delivery_status") if actor is not None else None
        if previous == "DELIVERED":
            axes["delivery"] = {"status": "DELIVERED", "reason": None}
            return OperationResult("BLOCKED", "DELIVERY_ALREADY_RECORDED", axes)
        status = (
            "PARTIAL" if confirmed or previous == "PARTIAL" else
            "PENDING" if uncertain or previous == "PENDING" else
            "FAILED"
        )
        self.ledger.record_delivery(request_id, status, code)
        axes["delivery"] = {"status": status, "reason": code}
        return OperationResult("BLOCKED", code, axes)
