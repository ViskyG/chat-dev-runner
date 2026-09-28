"""Finite creation of an independent area in an already provisioned project.

The admitted command pins the base commit and branch. The existing project and
Drive registration are rechecked before local or cloud effects. There is no
implicit replay of a partial workspace creation.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from runner_app.adapters.drive_provision import DriveProvisionError
from runner_app.adapters.drive_registry import WorkspaceDriveRegistration
from runner_app.adapters.github import GitHubError, GitHubREST
from runner_app.domain.commands import FrozenCommand, validate_shape
from runner_app.engine.service import Context, OperationResult
from runner_app.git.argv import GitError
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.git.service import SourceService
from runner_app.storage.ledger import Ledger


class WorkspaceDrivePort(Protocol):
    def provision_workspace(self, request_id: str, project_id: str,
                            workspace_id: str) -> WorkspaceDriveRegistration: ...


PopulateWorkspacePort = Callable[
    [str, WorkspaceDriveRegistration, SourceRegistry], Mapping[str, str],
]


class CreateWorkspaceWorkflow:
    """One owner-approved workspace from a full SHA, using registered ports."""

    def __init__(self, ledger: Ledger, source: SourceService, github: GitHubREST,
                 drive: WorkspaceDrivePort, populate: PopulateWorkspacePort, *,
                 template_profiles: Mapping[str, str]) -> None:
        if ledger.database.path != source.ledger.database.path:
            raise ValueError("Workspace workflow and source need one admission database")
        self.ledger = ledger
        self.source = source
        self.github = github
        self.drive = drive
        self.populate = populate
        self.template_profiles = dict(template_profiles)

    def _template(self, project_id: str, profile_id: str) -> str:
        primary = self.source.registry.get_workspace_profile(project_id, "primary")
        if primary is None:
            raise SourceError("PRIMARY_PROFILE_MISSING", "Primary profile is not registered")
        if (self.template_profiles.get(primary["template_id"]) == profile_id
                and primary["profile_id"] == profile_id):
            return primary["template_id"]
        candidates = [template for template, profile in self.template_profiles.items()
                      if profile == profile_id]
        if len(candidates) != 1:
            raise SourceError("PROFILE_TEMPLATE_AMBIGUOUS", "Profile has no unique approved template")
        return candidates[0]

    def __call__(self, frozen: FrozenCommand, context: Context) -> OperationResult:
        command = frozen.command
        validate_shape(command)
        if (command["action"] != "CREATE_WORKSPACE"
                or not isinstance(command["workspace_id"], str)
                or command["workspace_id"] == "primary"
                or command["expected_head"] != command["params"]["base_commit"]):
            return OperationResult("BLOCKED", "WORKSPACE_COMMAND_SCOPE_INVALID")
        if context.ledger.database.path != self.ledger.database.path:
            return OperationResult("FAULT", "WORKFLOW_LEDGER_MISMATCH")
        request_id = command["request_id"]
        project_id, workspace_id = command["project_id"], command["workspace_id"]
        operation = self.ledger.get(request_id)
        if (operation is None or operation["status"] != "RUNNING"
                or operation["command"] != command or operation["digest"] != frozen.digest
                or not self.ledger.holds_lock(request_id, f"repo:{project_id}")
                or not self.ledger.holds_lock(request_id, "global:drive-installation")):
            return OperationResult("BLOCKED", "WORKSPACE_ADMISSION_MISMATCH")
        if context.cancel.is_set():
            return OperationResult("CANCELLED", "CANCEL_REQUESTED")
        result: dict[str, Any] = {
            "project_id": project_id, "workspace_id": workspace_id,
            "branch": command["params"]["branch"],
            "base_commit": command["params"]["base_commit"],
        }
        try:
            project = self.source.registry.get_project(project_id)
            remote = self.source.registry.get_project_remote(project_id)
            if (project is None or project.get("state") != "READY"
                    or remote is None or remote.get("private") is not True):
                raise SourceError("PROJECT_NOT_READY", "Project is not ready for a new workspace")
            if self.source.registry.get_workspace(project_id, workspace_id) is not None:
                raise SourceError("WORKSPACE_ID_REUSED", "Workspace identity is already registered")
            repository = self.github.get_repository(
                remote["owner"], remote["name"], expected_repo_id=remote["provider_id"],
            )
            if (repository.private is not True or repository.id != remote["provider_id"]
                    or repository.node_id != remote["node_id"]
                    or repository.owner != remote["owner"]
                    or repository.name != remote["name"]
                    or repository.html_url != remote["html_url"]
                    or repository.clone_url != remote["clone_url"]
                    or ("owner_id" in remote
                        and repository.owner_id != remote["owner_id"])):
                raise GitHubError("GITHUB_IDENTITY_MISMATCH", "Private project remote changed")
            template_id = self._template(project_id, command["params"]["profile_id"])
            result["template_id"] = template_id
            result["profile_id"] = command["params"]["profile_id"]
            if context.cancel.is_set():
                return OperationResult("CANCELLED", "CANCEL_REQUESTED", result)
            workspace = self.source.create_workspace_source(request_id)
            if (workspace["branch"] != command["params"]["branch"]
                    or workspace["head"] != command["params"]["base_commit"]):
                raise SourceError("WORKSPACE_SOURCE_CHANGED", "New workspace identity differs")
            self.source.registry.bind_workspace_profile(
                project_id, workspace_id, template_id=template_id,
                profile_id=command["params"]["profile_id"],
            )
            result["local_workspace"] = "REGISTERED"
            if context.cancel.is_set():
                return OperationResult("CANCELLED", "CANCEL_REQUESTED", result)
            registration = self.drive.provision_workspace(request_id, project_id, workspace_id)
            result["drive"] = {
                "project_folder_id": registration.project_folder_id,
                "workspace_folder_id": registration.workspace_folder_id,
                "command_doc_id": registration.command_doc_id,
                "changeset_doc_id": registration.changeset_doc_id,
                "receipt_doc_id": registration.receipt_doc_id,
                "latest_result_doc_id": registration.latest_result_doc_id,
                "context_doc_id": registration.context_doc_id,
            }
            if context.cancel.is_set():
                return OperationResult("CANCELLED", "CANCEL_REQUESTED", result)
            content = dict(self.populate(request_id, registration, self.source.registry))
            if (set(content) != {"context_sha256", "project_state_sha256"}
                    or any(not isinstance(value, str) or len(value) != 64
                           or any(char not in "0123456789abcdef" for char in value)
                           for value in content.values())):
                raise DriveProvisionError("WORKSPACE_CONTENT_PROOF_INVALID")
            result["drive_content"] = content
            result["workspace_state"] = "ACTIVE"
            return OperationResult("COMPLETED", result=result)
        except (sqlite3.Error, OSError):
            raise
        except (SourceError, GitHubError, GitError, DriveProvisionError, ValueError) as error:
            return OperationResult("BLOCKED", getattr(error, "code", "WORKSPACE_PROVISION_INVALID"),
                                   result)
        except Exception:  # noqa: BLE001 -- never expose provider text in operation
            return OperationResult("FAULT", "WORKSPACE_HANDLER_FAILED", result)
