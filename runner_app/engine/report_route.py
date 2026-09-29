"""Exact registered-workspace dispatch for an explicit report delivery retry.

This route is independent of rootless execution readiness: it reads one frozen
run and may publish its saved evidence, but cannot start tests or mutate Git.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from runner_app.adapters.drive_docs import DriveError
from runner_app.adapters.drive_publish import DriveDeliveryPublisher
from runner_app.adapters.drive_writer import DocsCASWriter
from runner_app.domain.commands import FrozenCommand
from runner_app.engine.service import Context, OperationResult
from runner_app.git.registry import SourceRegistry
from runner_app.storage.ledger import Ledger
from runner_app.storage.paths import Paths
from runner_app.storage.run_evidence import RunEvidenceStore
from runner_app.workflows.publish_report import PublishReportWorkflow

if TYPE_CHECKING:
    from runner_app.engine.integrations import RuntimeIntegrations


class PublishReportRoute:
    """Resolve each request against current exact local and Drive registrations."""

    def __init__(self, integrations: RuntimeIntegrations, ledger: Ledger,
                 registry: SourceRegistry, evidence: RunEvidenceStore,
                 paths: Paths) -> None:
        if (ledger.database.path != registry.database.path
                or ledger.database.path != evidence.database.path):
            raise ValueError("Report route requires one admission database")
        self.integrations = integrations
        self.ledger = ledger
        self.registry = registry
        self.evidence = evidence
        self.paths = paths

    def __call__(self, frozen: FrozenCommand, context: Context) -> OperationResult:
        command = frozen.command
        if command.get("action") != "PUBLISH_REPORT":
            return OperationResult("BLOCKED", "WRONG_WORKFLOW_ACTION")
        project_id = command.get("project_id")
        workspace_id = command.get("workspace_id")
        if not isinstance(project_id, str) or not isinstance(workspace_id, str):
            return OperationResult("BLOCKED", "WORKSPACE_ROUTE_NOT_REGISTERED")
        workspace = self.registry.get_workspace(project_id, workspace_id)
        if workspace is None:
            return OperationResult("BLOCKED", "WORKSPACE_ROUTE_NOT_REGISTERED")
        try:
            registration = self.integrations.drive_registry.get(project_id, workspace_id)
        except DriveError as error:
            return OperationResult("BLOCKED", error.code)
        if (registration.project_id != project_id
                or registration.workspace_id != workspace_id
                or registration.installation_id !=
                self.integrations.attached.index.installation_id):
            return OperationResult("BLOCKED", "WORKSPACE_DRIVE_IDENTITY_CHANGED")
        receipt_key = f"workspace-receipt:{project_id}/{workspace_id}"
        latest_key = f"latest-result:{project_id}/{workspace_id}"
        writer = DocsCASWriter(
            cast(Any, self.integrations.provider), self.integrations.attached,
            extra_docs=registration.output_docs(
                receipt_key=receipt_key, latest_result_key=latest_key,
            ),
        )
        delivery = DriveDeliveryPublisher(
            self.ledger, cast(Any, self.integrations.upload),
            self.integrations.attached, writer, self.integrations.drive_registry,
        )
        workflow = PublishReportWorkflow(
            self.ledger, self.evidence, delivery,
            bundle_parent=self.paths.state_dir / "delivery-bundles",
            receipt_key=receipt_key, latest_result_key=latest_key,
        )
        return workflow(frozen, context)
