"""Explicit owner-local continuation of a switched, pre-Drive-intent retarget.

The original retarget keeps its repository lock. This route only fills an
association gap for which the original operation has no Drive state intent.
A current or historical unknown Drive write is never submitted again.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from runner_app.adapters.drive_docs import DriveError
from runner_app.domain.commands import FrozenCommand, freeze, validate_shape
from runner_app.engine.service import Context, OperationResult
from runner_app.git import worktrees
from runner_app.git.argv import GitError
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.storage.ledger import Ledger, LedgerError
from runner_app.workflows.retarget_drive_state import RetargetDriveState


class ResumeRetargetWorkflow:
    def __init__(self, ledger: Ledger, sources: SourceRegistry,
                 drive_state: RetargetDriveState) -> None:
        if ledger.database.path != sources.database.path or ledger is not drive_state.ledger:
            raise ValueError("Resume retarget requires one exact admission ledger")
        self.ledger = ledger
        self.sources = sources
        self.drive_state = drive_state

    def _source_generation(self, target: dict[str, Any]) -> int:
        command = target["command"]
        project_id, workspace_id = target["project_id"], target["workspace_id"]
        if (not isinstance(command, dict) or command.get("request_id") != target["request_id"]
                or command.get("action") != "RETARGET_WORKSPACE"
                or command.get("project_id") != project_id
                or command.get("workspace_id") != workspace_id
                or freeze(command).digest != target["digest"]):
            raise SourceError("RETARGET_RESUME_COMMAND_INVALID", "Original command differs")
        validate_shape(command)
        source = [effect for effect in target["effects"]
                  if effect["step_id"] == "retarget-workspace"
                  and effect["kind"] == "local_workspace"]
        if len(source) != 1 or source[0]["state"] != "CONFIRMED":
            raise SourceError("RETARGET_RESUME_SOURCE_UNCONFIRMED", "Source is not reconciled")
        intent = source[0]["intent"]
        params = command["params"]
        if (not isinstance(intent, dict)
                or set(intent) != {
                    "project_id", "workspace_id", "from_branch", "from",
                    "from_generation", "to_branch", "to",
                }
                or intent.get("project_id") != project_id
                or intent.get("workspace_id") != workspace_id
                or intent.get("from") != command["expected_head"]
                or intent.get("from_branch") != params["expected_branch"]
                or intent.get("to") != params["target_commit"]
                or intent.get("to_branch") != params["target_branch"]
                or type(intent.get("from_generation")) is not int
                or intent["from_generation"] < 0
                or source[0]["provider_id"] != params["target_commit"]):
            raise SourceError("RETARGET_RESUME_SOURCE_UNCONFIRMED", "Source proof differs")
        project = self.sources.get_project(project_id)
        record = self.sources.get_workspace(project_id, workspace_id)
        if (project is None or project.get("state") != "READY"
                or record is None or record.get("state") != "ACTIVE"
                or record.get("archived")
                or record.get("generation") != intent["from_generation"] + 1
                or record.get("branch") != params["target_branch"]
                or record.get("head") != params["target_commit"]):
            raise SourceError("RETARGET_RESUME_SOURCE_CHANGED", "Registered source differs")
        repo = self.sources.checked_project_path(project_id)
        path = self.sources.checked_workspace_path(project_id, workspace_id)
        observed = worktrees.inspect_workspace(repo, path)
        if (observed["dirty"] or observed["branch"] != params["target_branch"]
                or observed["head"] != params["target_commit"]):
            raise SourceError("RETARGET_RESUME_SOURCE_CHANGED", "Managed worktree differs")
        return int(intent["from_generation"])

    def __call__(self, frozen: FrozenCommand, context: Context) -> OperationResult:
        command = frozen.command
        validate_shape(command)
        if command["action"] != "RESUME_RETARGET":
            return OperationResult("BLOCKED", "WRONG_WORKFLOW_ACTION")
        if context.ledger.database.path != self.ledger.database.path:
            return OperationResult("FAULT", "WORKFLOW_LEDGER_MISMATCH")
        actor_id = command["request_id"]
        target_id = command["params"]["target_request_id"]
        project_id, workspace_id = command["project_id"], command["workspace_id"]
        actor = self.ledger.get(actor_id)
        if (actor is None or actor["status"] != "RUNNING"
                or actor["action"] != "RESUME_RETARGET"
                or actor["source_id"] != "local-owner"
                or actor["command"] != command or actor["digest"] != frozen.digest
                or command["params"] != {
                    "resume_version": "1.0", "target_request_id": target_id,
                }):
            return OperationResult("BLOCKED", "RETARGET_RESUME_ADMISSION_MISMATCH")
        if context.cancel.is_set():
            return OperationResult("CANCELLED", "CANCEL_REQUESTED")
        try:
            target = self.ledger.get(target_id)
            if (target is None or target_id == actor_id
                    or target["action"] != "RETARGET_WORKSPACE"
                    or target["project_id"] != project_id
                    or target["workspace_id"] != workspace_id
                    or target["status"] not in {"BLOCKED", "FAULT", "INTERRUPTED", "CANCELLED"}
                    or not target["reconcile_required"]
                    or not self.ledger.holds_lock(target_id, f"repo:{project_id}")):
                raise SourceError("RETARGET_RESUME_SCOPE_INVALID", "Retained target differs")
            generation = self._source_generation(target)
            plan = self.drive_state.preflight(
                target["command"], generation=generation, digest=target["digest"],
            )
            self.drive_state.publish_recovery(target_id, actor_id, plan)
            if context.cancel.is_set():
                return OperationResult("CANCELLED", "CANCEL_REQUESTED")
            self._source_generation(self.ledger.get(target_id) or target)
            state_hashes = self.drive_state.verify_desired(plan)
            self.ledger.release_reconciled_retarget(target_id, actor_id=actor_id)
            return OperationResult("COMPLETED", result={
                "target_request_id": target_id,
                "project_id": project_id, "workspace_id": workspace_id,
                "branch": target["command"]["params"]["target_branch"],
                "head": target["command"]["params"]["target_commit"],
                "source_generation": generation + 1,
                "drive_state": state_hashes,
            })
        except (sqlite3.Error, OSError):
            raise
        except (SourceError, GitError, DriveError, LedgerError) as error:
            return OperationResult("BLOCKED", error.code)
        except Exception:  # noqa: BLE001 -- provider details stay out of receipts
            return OperationResult("FAULT", "RETARGET_RESUME_FAILED")
