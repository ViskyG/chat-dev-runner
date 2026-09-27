"""One finite Drive changeset -> contained run -> Git -> Drive result operation.

This handler is inert until an owner registers it with Engine. It reads only the
admitted immutable Drive slot; provider ports and output document keys are local
constructor capabilities, never authority supplied by command JSON.
"""
from __future__ import annotations

import errno
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from runner_app.adapters.drive_delivery import DeliveryBundle, DeliveryError, prepare_run_bundle
from runner_app.adapters.drive_docs import DriveError
from runner_app.adapters.drive_poller import FrozenSlotStore, PollError
from runner_app.adapters.drive_publish import DeliveryResult
from runner_app.domain.commands import (
    DomainError,
    FrozenCommand,
    Provenance,
    parse_json,
    validate_shape,
)
from runner_app.engine.service import Context, OperationResult
from runner_app.executors.broker import BrokerError
from runner_app.executors.preparation_coordinator import PreparationNotReady
from runner_app.executors.preparation_plan import PreparationError
from runner_app.executors.profiles import ProfileError
from runner_app.executors.run_service import (
    RunCompletion,
    RunService,
    RunServiceError,
    TrustedPreparation,
)
from runner_app.executors.verify_handler import (
    PreprovisionedVolumeProvider,
    TrustedPreparationProvider,
    _local_volume,
    _safe_code,
)
from runner_app.git.argv import GitError
from runner_app.git.changes import MAX_CHANGESET_BYTES, ChangeError
from runner_app.git.registry import SourceError
from runner_app.git.service import SourceService
from runner_app.workflows.publish_run import PublishRunWorkflow


class TrustedExportPolicy(Protocol):
    """Owner-local output allowlist; no command or Drive text supplies these IDs."""

    def __call__(self, completion: RunCompletion) -> tuple[Sequence[str], Sequence[str]]: ...


class DeliveryPublisher(Protocol):
    def publish(self, bundle: DeliveryBundle, *, receipt_key: str,
                latest_result_key: str) -> DeliveryResult: ...


def _axes() -> dict[str, Any]:
    return {
        "execution": {"outcome": "NOT_RUN", "exit_code": None},
        "publication": {"status": "NOT_REQUESTED"},
        "delivery": {"status": "NOT_ATTEMPTED"},
        "report_link": {"status": "NOT_REQUESTED"},
    }


def _result(status: str, code: str | None, axes: dict[str, Any]) -> OperationResult:
    return OperationResult(status, code, axes)


def _publication_manifest(
    publication: Mapping[str, Any], *, code: str | None, policy: str,
    repository: str, branch: str | None, commit: str,
) -> dict[str, Any]:
    observed = publication.get("status")
    raw = observed if isinstance(observed, str) else ""
    status = {
        "DRAFT_PUBLISHED": "PR_UPDATED", "PUSHED": "PUSHED",
        "NO_DIFF": "NO_DIFF", "SKIPPED_POLICY": "NOT_REQUESTED",
        "SKIPPED_FAILED_TESTS": "NOT_REQUESTED", "NOT_ATTEMPTED": "NOT_REQUESTED",
        "NOT_REQUESTED": "NOT_REQUESTED",
        "UNKNOWN": "UNKNOWN", "BLOCKED": "BLOCKED",
    }.get(raw, "BLOCKED")
    reason = (
        "POLICY_NEVER" if raw == "SKIPPED_POLICY" else
        "FAILED_TESTS" if raw == "SKIPPED_FAILED_TESTS" else code
    )
    if policy == "never" and raw == "NOT_ATTEMPTED":
        reason = "POLICY_NEVER"
    url = publication.get("pull_request_url")
    return {
        "status": status, "repository": repository, "branch": branch,
        "commit": commit, "pull_request_url": url if isinstance(url, str) else None,
        "reason": reason,
    }


def _effects(operation: Mapping[str, Any]) -> list[dict[str, Any]]:
    # A local_workspace provider ID is a private path, never a Drive report item.
    allowed = {"github_repository", "git_ref", "pull_request", "drive_file"}
    return [
        {"kind": effect["kind"], "provider_id": effect["provider_id"] or "",
         "url": None, "state": "UNKNOWN" if effect["state"] == "INTENDED"
         else effect["state"]}
        for effect in operation["effects"] if effect["kind"] in allowed
    ]


class DeliverVerifyPublishWorkflow:
    """Run the admitted full route once, preserving each completed axis on failure."""

    def __init__(
        self, sources: SourceService, runs: RunService, frozen_slots: FrozenSlotStore,
        publication: PublishRunWorkflow, delivery: DeliveryPublisher, *,
        volume_for_run: PreprovisionedVolumeProvider, bundle_parent: Path,
        receipt_key: str, latest_result_key: str,
        preparation_for_run: TrustedPreparationProvider | None = None,
        export_policy_for_run: TrustedExportPolicy | None = None,
    ) -> None:
        db = sources.ledger.database.path
        if runs.ledger.database.path != db or publication.ledger.database.path != db:
            raise ValueError("Full route services must share one admission database")
        if not receipt_key or not latest_result_key or receipt_key == latest_result_key:
            raise ValueError("Two distinct registered output document keys are required")
        self.sources = sources
        self.runs = runs
        self.frozen_slots = frozen_slots
        self.publication = publication
        self.delivery = delivery
        self.volume_for_run = volume_for_run
        self.bundle_parent = Path(bundle_parent)
        self.receipt_key = receipt_key
        self.latest_result_key = latest_result_key
        self.preparation_for_run = preparation_for_run
        self.export_policy_for_run = export_policy_for_run

    def __call__(self, frozen: FrozenCommand, context: Context) -> OperationResult:
        axes = _axes()
        command = frozen.command
        validate_shape(command)
        if command.get("action") != "DELIVER_VERIFY_PUBLISH":
            return _result("BLOCKED", "WRONG_WORKFLOW_ACTION", axes)
        request_id = command["request_id"]
        if context.ledger.database.path != self.sources.ledger.database.path:
            return _result("FAULT", "WORKFLOW_LEDGER_MISMATCH", axes)
        operation = context.ledger.get(request_id)
        if (operation is None or operation["status"] != "RUNNING"
                or operation["digest"] != frozen.digest
                or operation["command"] != command
                or not context.ledger.holds_lock(request_id, f"repo:{command['project_id']}")):
            return _result("BLOCKED", "WORKFLOW_ADMISSION_MISMATCH", axes)
        if context.cancel.is_set():
            return _result("CANCELLED", "CANCEL_REQUESTED", axes)
        provenance = Provenance(
            kind="drive", source_id=operation["source_id"],
            project_id=command["project_id"], workspace_id=command["workspace_id"],
        )
        try:
            slot = self.frozen_slots.load_frozen(request_id, provenance)
            params = command["params"]
            if (slot.command.digest != frozen.digest
                    or slot.command_doc_id != operation["source_id"]
                    or slot.changeset_doc_id != params["changeset_doc_id"]
                    or slot.changeset_revision != params["changeset_revision"]
                    or slot.raw_changeset is None or slot.change_plan is None):
                return _result("BLOCKED", "FROZEN_INPUT_MISMATCH", axes)
            change = parse_json(slot.raw_changeset, max_bytes=MAX_CHANGESET_BYTES)
        except (PollError, DomainError) as error:
            return _result("BLOCKED", _safe_code(error.code, "FROZEN_INPUT_INVALID"), axes)
        if context.cancel.is_set():
            return _result("CANCELLED", "CANCEL_REQUESTED", axes)
        try:
            offered = self.volume_for_run(request_id)
        except Exception:  # noqa: BLE001 -- local provider text may reveal private paths
            return _result("FAULT", "VOLUME_PROVIDER_FAILED", axes)
        volume = _local_volume(offered, request_id)
        if volume is None:
            return _result("BLOCKED", "DISK_QUOTA_UNAVAILABLE", axes)
        preparation: TrustedPreparation | None = None
        if self.preparation_for_run is not None:
            try:
                preparation = self.preparation_for_run(request_id, params["profile_id"])
            except Exception:  # noqa: BLE001 -- local provider text may reveal private paths
                return _result("FAULT", "PREPARATION_PROVIDER_FAILED", axes)
            if preparation is not None and not isinstance(preparation, TrustedPreparation):
                return _result("FAULT", "PREPARATION_POLICY_INVALID", axes)
        if context.cancel.is_set():
            return _result("CANCELLED", "CANCEL_REQUESTED", axes)
        try:
            snapshot = self.sources.stage_changeset_source(request_id, change)
            axes.update({"snapshot_id": snapshot.snapshot_id,
                         "tested_commit": snapshot.commit, "tested_tree": snapshot.tree})
            if context.cancel.is_set():
                return _result("CANCELLED", "CANCEL_REQUESTED", axes)
            completion = self.runs.execute(
                request_id, snapshot.snapshot_id, run_id=request_id,
                writable_volume=volume, cancel=context.cancel, preparation=preparation,
            )
        except (SourceError, ChangeError, GitError) as error:
            return _result("BLOCKED", _safe_code(error.code, "SOURCE_STEP_FAILED"), axes)
        except ProfileError as error:
            return _result("BLOCKED", _safe_code(error.code, "PROFILE_STEP_FAILED"), axes)
        except PreparationNotReady as error:
            code = _safe_code(error.code, "PREPARATION_NOT_READY")
            axes["preparation"] = {
                "run_id": error.run_id, "outcome": error.outcome,
                "evidence_sha256": error.evidence_sha256,
            }
            status = ("CANCELLED" if error.outcome == "CANCELLED"
                      else "BLOCKED" if error.outcome in {"FAILED", "TIMED_OUT"}
                      else "FAULT")
            return _result(status, code, axes)
        except PreparationError as error:
            code = _safe_code(error.code, "PREPARATION_FAILED")
            status = "CANCELLED" if code == "CANCELLED_BEFORE_START" else "BLOCKED"
            return _result(status, code, axes)
        except BrokerError as error:
            code = _safe_code(error.code, "BROKER_STEP_FAILED")
            status = "CANCELLED" if code == "CANCELLED_BEFORE_START" else "FAULT"
            axes["execution"] = {"outcome": "NOT_RUN" if status == "CANCELLED" else "UNKNOWN",
                                 "exit_code": None}
            return _result(status, code, axes)
        except RunServiceError as error:
            axes["execution"] = {"outcome": "UNKNOWN", "exit_code": None}
            return _result("FAULT", _safe_code(error.code, "RUN_STEP_FAILED"), axes)
        except OSError as error:
            code = ("EVIDENCE_STORAGE_FULL" if error.errno in {errno.ENOSPC, errno.EDQUOT}
                    else "LOCAL_IO_FAILED")
            axes["execution"] = {"outcome": "UNKNOWN", "exit_code": None}
            return _result("FAULT", code, axes)
        axes["run_id"] = completion.run_id
        axes["execution"] = completion.manifest["execution"]
        if completion.preparation is not None:
            axes["preparation"] = completion.preparation
        if completion.operation_status != "COMPLETED":
            return _result(completion.operation_status, completion.operation_code, axes)
        if context.cancel.is_set():
            self._record_operation_manifest(
                command, completion, axes, context,
                status="CANCELLED", code="CANCEL_REQUESTED",
            )
            return _result("CANCELLED", "CANCEL_REQUESTED", axes)
        published = self.publication.publish_run(
            request_id, completion.run_id, params["publication"], context,
        )
        axes["publication"] = published.result.get("publication", {"status": "BLOCKED"})
        for key in ("editor_head", "editor_matches_tested", "remote_ref"):
            if key in published.result:
                axes[key] = published.result[key]
        if published.status == "CANCELLED" or context.cancel.is_set():
            self._record_operation_manifest(
                command, completion, axes, context,
                status="CANCELLED", code="CANCEL_REQUESTED",
            )
            return _result("CANCELLED", "CANCEL_REQUESTED", axes)
        # Git refusal does not suppress an honest test report. The publication
        # axis and its reason remain in the immutable operation manifest.
        return self._deliver(command, completion, published, axes, context)

    def _record_operation_manifest(
        self, command: Mapping[str, Any], completion: RunCompletion,
        axes: dict[str, Any], context: Context, *, status: str,
        code: str | None,
    ) -> bool:
        """Retain a bounded report view once a contained run exists, even on cancel."""
        operation = context.ledger.get(command["request_id"])
        if operation is None:
            return False
        repo = self.publication.repository
        branch = axes.get("remote_ref")
        if isinstance(branch, str) and branch.startswith("refs/heads/"):
            branch = branch.removeprefix("refs/heads/")
        elif not isinstance(branch, str):
            branch = None
        axes["operation_manifest"] = {
            "schema_version": "1.0", "request_id": command["request_id"],
            "project_id": command["project_id"], "workspace_id": command["workspace_id"],
            "status": status, "run_ids": [completion.run_id],
            "publication": _publication_manifest(
                axes["publication"], code=code,
                policy=command["params"]["publication"],
                repository=f"{repo.owner}/{repo.name}", branch=branch,
                commit=completion.manifest["snapshot"]["commit"],
            ),
            "delivery": {
                "status": "NOT_ATTEMPTED" if status == "CANCELLED" else "PENDING",
                "reason": None,
            },
            "outputs": completion.manifest["outputs"], "effects": _effects(operation),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return True

    def _deliver(self, command: Mapping[str, Any], completion: RunCompletion,
                 published: OperationResult, axes: dict[str, Any],
                 context: Context) -> OperationResult:
        request_id = command["request_id"]
        if context.cancel.is_set():
            self._record_operation_manifest(
                command, completion, axes, context,
                status="CANCELLED", code="CANCEL_REQUESTED",
            )
            return _result("CANCELLED", "CANCEL_REQUESTED", axes)
        status = "COMPLETED" if published.status == "COMPLETED" else "BLOCKED"
        if not self._record_operation_manifest(
            command, completion, axes, context, status=status, code=published.code,
        ):
            return _result("FAULT", "OPERATION_NOT_REGISTERED", axes)
        operation_manifest = axes["operation_manifest"]
        approved: Sequence[str] = ()
        reviewed: Sequence[str] = ()
        if self.export_policy_for_run is not None:
            try:
                approved, reviewed = self.export_policy_for_run(completion)
            except Exception:  # noqa: BLE001 -- local policy text may contain private paths
                code = "EXPORT_POLICY_PROVIDER_FAILED"
                context.ledger.record_delivery(request_id, "FAILED", code)
                axes["delivery"] = {"status": "FAILED", "reason": code}
                return _result("BLOCKED", code, axes)
        try:
            bundle = prepare_run_bundle(
                context.ledger, self.runs.evidence, completion.run_id, request_id,
                operation_manifest, self.bundle_parent,
                approved_output_ids=approved, reviewed_output_ids=reviewed,
            )
        except DeliveryError as error:
            code = _safe_code(error.code, "DELIVERY_PREPARATION_FAILED")
            context.ledger.record_delivery(request_id, "FAILED", code)
            axes["delivery"] = {"status": "FAILED", "reason": code}
            return _result("BLOCKED", code, axes)
        except (OSError, TypeError):
            code = "DELIVERY_BUNDLE_IO_FAILED"
            context.ledger.record_delivery(request_id, "FAILED", code)
            axes["delivery"] = {"status": "FAILED", "reason": code}
            return _result("FAULT", code, axes)
        if context.cancel.is_set():
            return _result("CANCELLED", "CANCEL_REQUESTED", axes)
        try:
            delivered = self.delivery.publish(
                bundle, receipt_key=self.receipt_key,
                latest_result_key=self.latest_result_key,
            )
        except DeliveryError as error:
            code = _safe_code(error.code, "DELIVERY_PREFLIGHT_FAILED")
            context.ledger.record_delivery(request_id, "FAILED", code)
            axes["delivery"] = {"status": "FAILED", "reason": code}
            return _result("BLOCKED", code, axes)
        except (DriveError, OSError):
            # External write may have committed; never replay it in this operation.
            context.ledger.record_delivery(request_id, "PENDING", "DELIVERY_OUTCOME_UNKNOWN")
            axes["delivery"] = {"status": "PENDING", "reason": "DELIVERY_OUTCOME_UNKNOWN"}
            return _result("BLOCKED", "DELIVERY_OUTCOME_UNKNOWN", axes)
        context.ledger.record_delivery(request_id, delivered.status, delivered.code)
        axes["delivery"] = {
            "status": delivered.status, "reason": delivered.code,
            "request_folder_id": delivered.request_folder_id,
            "run_folder_id": delivered.run_folder_id,
            "confirmed_assets": list(delivered.confirmed_assets),
            "latest_skipped_newer": delivered.latest_skipped_newer,
            "index_file_id": delivered.index_file_id,
        }
        if context.cancel.is_set():
            return _result("CANCELLED", "CANCEL_REQUESTED", axes)
        if published.status != "COMPLETED":
            return _result("BLOCKED", published.code, axes)
        if delivered.status != "DELIVERED":
            return _result("BLOCKED", delivered.code or "DELIVERY_INCOMPLETE", axes)
        if axes["publication"].get("status") == "DRAFT_PUBLISHED":
            if not isinstance(delivered.run_folder_id, str):
                axes["report_link"] = {"status": "BLOCKED"}
                return _result("BLOCKED", "REPORT_FOLDER_UNCONFIRMED", axes)
            linked = self.publication.attach_report_link(
                request_id, completion.run_id, delivered.run_folder_id, context,
            )
            axes["report_link"] = linked.result.get(
                "report_link", {"status": "UNKNOWN"},
            )
            if linked.status != "COMPLETED":
                return _result(linked.status, linked.code or "REPORT_LINK_INCOMPLETE", axes)
        return _result("COMPLETED", None, axes)
