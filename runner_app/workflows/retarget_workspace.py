"""Finite clean branch retarget for one registered managed workspace.

Admission owns the repository lock. Only an existing unoccupied branch at its
exact commit can be selected; no remote fetch, branch creation or project code
execution occurs in this route.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from runner_app.adapters.drive_docs import DriveError
from runner_app.domain.commands import FrozenCommand, validate_shape
from runner_app.engine.service import Context, OperationResult
from runner_app.git.argv import GitError
from runner_app.git.registry import SourceError
from runner_app.git.service import SourceService
from runner_app.storage.ledger import Ledger
from runner_app.workflows.retarget_drive_state import RetargetDriveState


class RetargetWorkspaceWorkflow:
    def __init__(self, ledger: Ledger, source: SourceService,
                 drive_state: RetargetDriveState) -> None:
        if ledger.database.path != source.ledger.database.path:
            raise ValueError("Retarget workflow and source need one admission database")
        self.ledger = ledger
        self.source = source
        self.drive_state = drive_state

    def __call__(self, frozen: FrozenCommand, context: Context) -> OperationResult:
        command = frozen.command
        validate_shape(command)
        if command["action"] != "RETARGET_WORKSPACE":
            return OperationResult("BLOCKED", "WRONG_WORKFLOW_ACTION")
        if context.ledger.database.path != self.ledger.database.path:
            return OperationResult("FAULT", "WORKFLOW_LEDGER_MISMATCH")
        request_id = command["request_id"]
        project_id, workspace_id = command["project_id"], command["workspace_id"]
        operation = self.ledger.get(request_id)
        if (operation is None or operation["status"] != "RUNNING"
                or operation["command"] != command or operation["digest"] != frozen.digest
                or not self.ledger.holds_lock(request_id, f"repo:{project_id}")):
            return OperationResult("BLOCKED", "RETARGET_ADMISSION_MISMATCH")
        if context.cancel.is_set():
            return OperationResult("CANCELLED", "CANCEL_REQUESTED")
        try:
            project = self.source.registry.get_project(project_id)
            if project is None or project.get("state") != "READY":
                raise SourceError("PROJECT_NOT_READY", "Project is not ready for branch retarget")
            current = self.source.registry.get_workspace(project_id, workspace_id)
            if (current is None or current.get("state") != "ACTIVE" or current.get("archived")):
                raise SourceError("WORKSPACE_UNAVAILABLE", "Workspace is not active")
            drive_plan = self.drive_state.preflight(
                command, generation=current["generation"], digest=frozen.digest,
            )
            updated = self.source.retarget_workspace_source(
                request_id, expected_generation=current["generation"],
                defer_confirmation=True,
            )
            if (updated["branch"] != command["params"]["target_branch"]
                    or updated["head"] != command["params"]["target_commit"]
                    or updated["generation"] != current["generation"] + 1):
                raise SourceError("WORKSPACE_CHANGED", "Retarget result differs from command")
            state_hashes = self.drive_state.publish(request_id, drive_plan)
            after_drive = self.ledger.get(request_id)
            if after_drive is None or after_drive["status"] != "RUNNING":
                raise SourceError("RETARGET_EFFECT_CHANGED", "Source effect differs")
            git_effects = [effect for effect in after_drive["effects"]
                           if effect["step_id"] == "retarget-workspace"
                           and effect["kind"] == "local_workspace"]
            if len(git_effects) != 1 or git_effects[0]["state"] != "INTENDED":
                raise SourceError("RETARGET_EFFECT_CHANGED", "Source effect differs")
            self.ledger.confirm_effect(git_effects[0]["effect_id"], updated["head"])
            result: dict[str, Any] = {
                "project_id": project_id, "workspace_id": workspace_id,
                "branch": updated["branch"], "head": updated["head"],
                "source_generation": updated["generation"],
                "drive_state": state_hashes,
            }
            return OperationResult("COMPLETED", result=result)
        except (sqlite3.Error, OSError):
            raise
        except (SourceError, GitError, DriveError) as error:
            return OperationResult("BLOCKED", error.code)
        except Exception:  # noqa: BLE001 -- never put local Git detail in a receipt
            return OperationResult("FAULT", "RETARGET_HANDLER_FAILED")
