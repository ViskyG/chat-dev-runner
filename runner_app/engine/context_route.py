"""Finite owner-local export of one registered committed source snapshot.

The full bounded report, including selected excerpts, stays in the private
operation ledger. The workspace Drive mailbox may receive a status-only
terminal receipt; this route never writes SOURCE CONTEXT or source bytes to a
provider. Publication needs a separate explicit privacy review contract.
"""
from __future__ import annotations

from runner_app.adapters.drive_context import ContextError, ContextExporter
from runner_app.domain.commands import FrozenCommand, validate_shape
from runner_app.engine.service import Context, OperationResult
from runner_app.git.registry import SourceRegistry
from runner_app.storage.ledger import Ledger


class ExportContextRoute:
    def __init__(self, ledger: Ledger, registry: SourceRegistry) -> None:
        self.ledger = ledger
        self.exporter = ContextExporter(ledger, registry)

    def __call__(self, frozen: FrozenCommand, context: Context) -> OperationResult:
        command = frozen.command
        validate_shape(command)
        if command["action"] != "EXPORT_CONTEXT":
            return OperationResult("BLOCKED", "WRONG_WORKFLOW_ACTION")
        if context.ledger.database.path != self.ledger.database.path:
            return OperationResult("FAULT", "WORKFLOW_LEDGER_MISMATCH")
        request_id = command["request_id"]
        operation = self.ledger.get(request_id)
        if (operation is None or operation["status"] != "RUNNING"
                or operation["command"] != command or operation["digest"] != frozen.digest
                or not self.ledger.holds_lock(
                    request_id,
                    f"workspace:{command['project_id']}/{command['workspace_id']}",
                )):
            return OperationResult("BLOCKED", "CONTEXT_ADMISSION_MISMATCH")
        if context.cancel.is_set():
            return OperationResult("CANCELLED", "CANCEL_REQUESTED")
        try:
            report = self.exporter.export(request_id)
        except ContextError as error:
            return OperationResult("BLOCKED", error.code)
        if context.cancel.is_set():
            return OperationResult("CANCELLED", "CANCEL_REQUESTED")
        return OperationResult("COMPLETED", result={"context": report})
