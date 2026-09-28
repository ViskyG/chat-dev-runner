"""Finite project mailbox provisioning inside an attached Drive installation.

The root installation is an immutable authority input. This service creates only
project/workspace descendants under its already-indexed Projects folder, records
one effect intent per file before any write, and never retries an ambiguous create.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import urlsplit

from runner_app.adapters.drive_index import AttachedInstallation
from runner_app.adapters.drive_registry import WorkspaceDriveRegistration, WorkspaceDriveRegistry
from runner_app.adapters.drive_upload import DOCUMENT_MIME, FOLDER_MIME
from runner_app.adapters.drive_writer import DocsCASWriter, RegisteredOutputDoc
from runner_app.domain.commands import validate_shape
from runner_app.git.registry import SourceRegistry
from runner_app.storage.ledger import Ledger


class DriveProvisionError(ValueError):
    """A stable provisioning finding without provider payload or credentials."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ProjectUploadProvider(Protocol):
    def get_file(self, file_id: str, **expected: Any) -> Mapping[str, Any]: ...

    def find_by_key(self, parent_id: str, installation_id: str, key: str,
                    **expected: Any) -> Mapping[str, Any] | None: ...

    def create_folder(self, parent_id: str, installation_id: str, key: str,
                      name: str, *, project_id: str,
                      workspace_id: str | None = None) -> Mapping[str, Any]: ...

    def create_document(self, parent_id: str, installation_id: str, key: str,
                        name: str, *, project_id: str,
                        workspace_id: str | None = None) -> Mapping[str, Any]: ...


class DriveProjectProvisioner:
    """Create exactly one registered primary workspace for CREATE_PROJECT."""

    def __init__(self, ledger: Ledger, attached: AttachedInstallation,
                 upload: ProjectUploadProvider, registry: WorkspaceDriveRegistry) -> None:
        if not isinstance(attached, AttachedInstallation):
            raise TypeError("An existing verified installation attachment is required")
        self.ledger = ledger
        self.attached = attached
        self.upload = upload
        self.registry = registry

    def _preflight(self, request_id: str, project_id: str, workspace_id: str,
                   project_name: str, *,
                   resume_target_id: str | None = None) -> tuple[str, str]:
        operation = self.ledger.get(request_id)
        if operation is None or operation["status"] != "RUNNING":
            raise DriveProvisionError("PROVISION_NOT_RUNNING")
        command = operation["command"]
        try:
            validate_shape(command)
        except (ValueError, KeyError, TypeError):
            raise DriveProvisionError("PROVISION_COMMAND_INVALID") from None
        if (command["request_id"] != request_id
                or command["project_id"] != project_id
                or command["workspace_id"] is not None
                or workspace_id != "primary"):
            raise DriveProvisionError("PROVISION_SCOPE_MISMATCH")
        if resume_target_id is None:
            if (command["action"] != "CREATE_PROJECT"
                    or command["params"]["name"] != project_name):
                raise DriveProvisionError("PROVISION_SCOPE_MISMATCH")
        else:
            self._trusted_resume_target(operation, resume_target_id, project_name)
        if (not self.ledger.holds_lock(request_id, f"repo:{project_id}")
                or not self.ledger.holds_lock(request_id, "global:drive-installation")):
            raise DriveProvisionError("PROVISION_LOCK_MISSING")
        index = self.attached.index
        root = index.files.get("root")
        projects = index.files.get("projects")
        if (root is None or projects is None
                or root.file_id != self.attached.config.root_id
                or projects.parents != (root.file_id,)
                or projects.mime_type != FOLDER_MIME):
            raise DriveProvisionError("PROVISION_INSTALLATION_CHANGED")
        self.upload.get_file(
            projects.file_id, expected_parent_id=root.file_id,
            expected_installation_id=index.installation_id,
            expected_key="projects", expected_mime_type=FOLDER_MIME,
            expected_name=projects.name,
        )
        return index.installation_id, projects.file_id

    def _trusted_resume_target(self, operation: Mapping[str, Any],
                               target_id: str, project_name: str) -> Mapping[str, Any]:
        """Authorize only the original exact CREATE and an adopted Drive prefix."""
        command = operation["command"]
        target = self.ledger.get(target_id)
        if (target is None or command["action"] != "RESUME_PROVISION"
                or command["params"] != {
                    "resume_version": "1.0", "target_request_id": target_id,
                } or target["action"] != "CREATE_PROJECT"
                or target["status"] not in {"BLOCKED", "FAULT", "INTERRUPTED", "CANCELLED"}
                or target["reconcile_required"]
                or target["project_id"] != command["project_id"]
                or target["workspace_id"] is not None
                or target["command"]["params"]["source"] != {"kind": "new"}
                or target["command"]["params"]["name"] != project_name
                or any(effect["state"] != "CONFIRMED" for effect in target["effects"])):
            raise DriveProvisionError("PROVISION_RESUME_TARGET_INVALID")
        adopted = [effect for effect in operation["effects"]
                   if effect["step_id"] == "drive-project-folder"
                   and isinstance(effect.get("adoption"), Mapping)]
        original = [effect for effect in target["effects"]
                    if effect["step_id"] == "drive-project-folder"]
        if (len(adopted) != 1 or len(original) != 1
                or adopted[0]["adoption"].get("source_effect_id")
                    != original[0]["effect_id"]
                or adopted[0]["provider_id"] != original[0]["provider_id"]
                or adopted[0]["intent"] != original[0]["intent"]):
            raise DriveProvisionError("PROVISION_RESUME_ADOPTION_MISSING")
        return target

    def _file(self, request_id: str, step_id: str, parent_id: str,
              installation_id: str, key: str, name: str, mime_type: str,
              project_id: str, workspace_id: str | None) -> str:
        intent = {"parent_id": parent_id, "installation_id": installation_id,
                  "key": key, "name": name, "mime_type": mime_type,
                  "project_id": project_id, "workspace_id": workspace_id}
        expected: dict[str, Any] = {
            "expected_parent_id": parent_id,
            "expected_installation_id": installation_id,
            "expected_key": key,
            "expected_mime_type": mime_type,
            "expected_name": name,
            "expected_project_id": project_id,
        }
        if workspace_id is not None:
            expected["expected_workspace_id"] = workspace_id
        lookup_expected = {
            "expected_mime_type": mime_type, "expected_name": name,
            "expected_project_id": project_id,
        }
        if workspace_id is not None:
            lookup_expected["expected_workspace_id"] = workspace_id
        operation = self.ledger.get(request_id)
        if operation is None or operation["status"] != "RUNNING":
            raise DriveProvisionError("PROVISION_NOT_RUNNING")
        previous = [effect for effect in operation["effects"]
                    if effect["step_id"] == step_id and effect["kind"] == "drive_file"]
        if len(previous) > 1:
            raise DriveProvisionError("PROVISION_EFFECT_DUPLICATED")
        if previous:
            effect = previous[0]
            if effect["intent"] != intent:
                raise DriveProvisionError("PROVISION_INTENT_CHANGED")
            if effect["state"] == "CONFIRMED":
                found = self.upload.get_file(effect["provider_id"], **expected)
            elif effect["state"] in {"INTENDED", "UNKNOWN"}:
                reconciled = self.upload.find_by_key(
                    parent_id, installation_id, key, **lookup_expected,
                )
                if reconciled is None:
                    # A negative search cannot prove a timed-out create did not land.
                    raise DriveProvisionError("PROVISION_EFFECT_UNCERTAIN")
                found = reconciled
                self.ledger.confirm_effect(effect["effect_id"], str(found["id"]))
            else:
                raise DriveProvisionError("PROVISION_EFFECT_INVALID")
        else:
            # An existing name/key without this operation's durable intent is not
            # authority to adopt another request's resource.
            if self.upload.find_by_key(parent_id, installation_id, key, **lookup_expected) is not None:
                raise DriveProvisionError("PROVISION_RESOURCE_EXISTS")
            effect_id = self.ledger.begin_effect(request_id, step_id, "drive_file", intent)
            if mime_type == FOLDER_MIME:
                created = self.upload.create_folder(
                    parent_id, installation_id, key, name,
                    project_id=project_id, workspace_id=workspace_id,
                )
            else:
                if mime_type != DOCUMENT_MIME:
                    raise DriveProvisionError("PROVISION_FILE_INVALID")
                created = self.upload.create_document(
                    parent_id, installation_id, key, name,
                    project_id=project_id, workspace_id=workspace_id,
                )
            found = self.upload.get_file(str(created["id"]), **expected)
            self.ledger.confirm_effect(effect_id, str(found["id"]))
        if not isinstance(found.get("id"), str) or not found["id"]:
            raise DriveProvisionError("PROVISION_PROVIDER_ID_INVALID")
        return str(found["id"])

    def provision(self, request_id: str, project_id: str, workspace_id: str,
                  project_name: str, *,
                  resume_target_id: str | None = None) -> WorkspaceDriveRegistration:
        """Create/verify ten exact resources and persist their trusted registration.

        Recalling this method after a lost acknowledgement performs positive
        reconciliation of the original intent, never a second write for it.
        """
        installation_id, projects_id = self._preflight(
            request_id, project_id, workspace_id, project_name,
            resume_target_id=resume_target_id,
        )
        project_folder = self._file(
            request_id, "drive-project-folder", projects_id,
            installation_id, f"project:{project_id}", project_id, FOLDER_MIME,
            project_id, None,
        )
        project_state = self._file(
            request_id, "drive-project-state-doc", project_folder,
            installation_id, "project-state", "PROJECT STATE", DOCUMENT_MIME,
            project_id, None,
        )
        project_command = self._file(
            request_id, "drive-project-command-doc", project_folder,
            installation_id, "project-command", "PROJECT COMMAND", DOCUMENT_MIME,
            project_id, None,
        )
        project_receipt = self._file(
            request_id, "drive-project-receipt-doc", project_folder,
            installation_id, "project-receipt", "PROJECT RECEIPT", DOCUMENT_MIME,
            project_id, None,
        )
        workspace_folder = self._file(
            request_id, "drive-workspace-folder", project_folder,
            installation_id, f"workspace:{project_id}/{workspace_id}",
            workspace_id, FOLDER_MIME, project_id, workspace_id,
        )
        context_doc = self._file(
            request_id, "drive-source-context-doc", workspace_folder,
            installation_id, "source-context", "SOURCE CONTEXT", DOCUMENT_MIME,
            project_id, workspace_id,
        )
        docs: dict[str, str] = {}
        for step_id, name, key in (
            ("drive-command-doc", "COMMAND", "workspace-command"),
            ("drive-changeset-doc", "CHANGESET", "workspace-changeset"),
            ("drive-receipt-doc", "RECEIPT", "workspace-receipt"),
            ("drive-latest-result-doc", "LATEST RESULT", "latest-result"),
        ):
            docs[key] = self._file(
                request_id, step_id, workspace_folder, installation_id, key,
                name, DOCUMENT_MIME, project_id, workspace_id,
            )
        registration = WorkspaceDriveRegistration(
            installation_id, project_id, workspace_id, project_folder,
            workspace_folder, docs["workspace-command"], docs["workspace-changeset"],
            docs["workspace-receipt"], docs["latest-result"],
            project_state, context_doc, project_command, project_receipt,
        )
        return self.registry.register(registration)

    def _write_doc(self, request_id: str, step_id: str, writer: DocsCASWriter,
                   registration: RegisteredOutputDoc, text: str) -> str:
        desired_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if writer.registration(registration.key) != registration:
            raise DriveProvisionError("PROVISION_WRITER_SCOPE_MISMATCH")
        operation = self.ledger.get(request_id)
        if operation is None or operation["status"] != "RUNNING":
            raise DriveProvisionError("PROVISION_NOT_RUNNING")
        previous = [effect for effect in operation["effects"]
                    if effect["step_id"] == step_id and effect["kind"] == "drive_file"]
        if len(previous) > 1:
            raise DriveProvisionError("PROVISION_EFFECT_DUPLICATED")
        observed = writer.read(registration.key)
        if observed.snapshot.file_id != registration.file_id:
            raise DriveProvisionError("PROVISION_WRITER_SCOPE_MISMATCH")
        if previous:
            effect = previous[0]
            intent = effect["intent"]
            if (intent.get("file_id") != registration.file_id
                    or intent.get("key") != registration.key
                    or intent.get("desired_sha256") != desired_sha256):
                raise DriveProvisionError("PROVISION_INTENT_CHANGED")
            if observed.snapshot.sha256 != desired_sha256:
                raise DriveProvisionError("PROVISION_DOC_UNCERTAIN"
                                          if effect["state"] != "CONFIRMED"
                                          else "PROVISION_DOC_CHANGED")
            if effect["state"] in {"INTENDED", "UNKNOWN"}:
                self.ledger.confirm_effect(effect["effect_id"], registration.file_id)
            elif effect["state"] != "CONFIRMED" or effect["provider_id"] != registration.file_id:
                raise DriveProvisionError("PROVISION_EFFECT_INVALID")
            return desired_sha256
        if observed.snapshot.text.strip("\n") != "":
            raise DriveProvisionError("PROVISION_DOC_NOT_EMPTY")
        intent = {"file_id": registration.file_id, "key": registration.key,
                  "base_revision": observed.snapshot.revision_id,
                  "desired_sha256": desired_sha256}
        effect_id = self.ledger.begin_effect(request_id, step_id, "drive_file", intent)
        result = writer.replace(registration.key, text, expected=observed)
        if result.state not in {"CONFIRMED", "UNCHANGED"}:
            raise DriveProvisionError("PROVISION_DOC_UNCERTAIN")
        current = writer.read(registration.key)
        if (current.snapshot.file_id != registration.file_id
                or current.snapshot.sha256 != desired_sha256):
            raise DriveProvisionError("PROVISION_DOC_UNCERTAIN")
        self.ledger.confirm_effect(effect_id, registration.file_id)
        return desired_sha256

    def populate(self, request_id: str, registration: WorkspaceDriveRegistration, *,
                 source_registry: SourceRegistry, writer: DocsCASWriter,
                 repository_id: int, repository_url: str,
                 resume_target_id: str | None = None) -> dict[str, str]:
        """Write the initial project index and source context from durable facts.

        This is an explicit step after GitHub identity, local workspace and owner
        profile binding exist. It writes only newly provisioned, registered Docs.
        """
        if not isinstance(registration, WorkspaceDriveRegistration):
            raise DriveProvisionError("PROVISION_REGISTRATION_INVALID")
        if registration.project_state_doc_id is None or registration.context_doc_id is None:
            raise DriveProvisionError("PROVISION_DOCS_MISSING")
        if source_registry.database.path != self.ledger.database.path:
            raise DriveProvisionError("PROVISION_SOURCE_LEDGER_MISMATCH")
        if (isinstance(repository_id, bool) or not isinstance(repository_id, int)
                or repository_id < 1 or repository_id > 2**63 - 1
                or not isinstance(repository_url, str) or len(repository_url) > 256
                or any(ord(char) <= 32 or ord(char) == 127 for char in repository_url)):
            raise DriveProvisionError("PROVISION_REPOSITORY_INVALID")
        try:
            parsed = urlsplit(repository_url)
        except ValueError:
            raise DriveProvisionError("PROVISION_REPOSITORY_INVALID") from None
        if (parsed.geturl() != repository_url
                or parsed.scheme != "https" or parsed.netloc != "github.com"
                or re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", parsed.path) is None
                or parsed.query or parsed.fragment or parsed.username is not None):
            raise DriveProvisionError("PROVISION_REPOSITORY_INVALID")
        operation = self.ledger.get(request_id)
        if operation is None or operation["status"] != "RUNNING":
            raise DriveProvisionError("PROVISION_NOT_RUNNING")
        project_id, workspace_id = registration.project_id, registration.workspace_id
        create_operation = (
            operation if resume_target_id is None
            else self.ledger.get(resume_target_id)
        )
        if create_operation is None:
            raise DriveProvisionError("PROVISION_RESUME_TARGET_INVALID")
        create_params = create_operation["command"]["params"]
        self._preflight(request_id, project_id, workspace_id,
                        create_params["name"], resume_target_id=resume_target_id)
        if self.registry.get(project_id, workspace_id) != registration:
            raise DriveProvisionError("PROVISION_REGISTRATION_CHANGED")
        project = source_registry.get_project(project_id)
        workspace = source_registry.get_workspace(project_id, workspace_id)
        binding = source_registry.get_workspace_profile(project_id, workspace_id)
        if (project is None or workspace is None or workspace["archived"]
                or binding is None
                or binding["template_id"] != create_params["template_id"]):
            raise DriveProvisionError("PROVISION_SOURCE_BINDING_MISSING")
        branch, head = workspace["branch"], workspace["head"]
        if not isinstance(branch, str) or not isinstance(head, str):
            raise DriveProvisionError("PROVISION_SOURCE_BINDING_INVALID")
        project_doc, context_doc = registration.state_docs()
        state = {
            "schema_version": "1.0", "kind": "project-state/v1",
            "project_id": project_id, "display_name": create_params["name"],
            "repository": {"id": repository_id, "url": repository_url,
                           "full_name": parsed.path[1:]},
            "chat_repository_access": "MANUAL_REQUIRED",
            "workspaces": [{
                "workspace_id": workspace_id, "display_name": workspace["display_name"],
                "branch": branch, "head": head, "source_generation": workspace["generation"],
                "template_id": binding["template_id"], "profile_id": binding["profile_id"],
                "command_doc_id": registration.command_doc_id,
                "changeset_doc_id": registration.changeset_doc_id,
                "receipt_doc_id": registration.receipt_doc_id,
                "latest_result_doc_id": registration.latest_result_doc_id,
                "context_doc_id": registration.context_doc_id,
            }],
            "project_state_doc_id": registration.project_state_doc_id,
            "project_command_doc_id": registration.project_command_doc_id,
            "project_receipt_doc_id": registration.project_receipt_doc_id,
            "project_folder_id": registration.project_folder_id,
            "workspace_folder_id": registration.workspace_folder_id,
            "created_at": create_operation["received_at"],
        }
        state_text = ("PROJECT STATE\n" + json.dumps(
            state, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ) + "\n")
        context_text = (
            "# Source context / generated by runner\n\n"
            f"Project/workspace: `{project_id}` / `{workspace_id}`\n"
            f"Snapshot: `{head}`\n"
            f"Branch: `{branch}`\n"
            f"Repository: {repository_url} (ID {repository_id})\n"
            f"Template/profile: `{binding['template_id']}` / `{binding['profile_id']}`\n"
            f"Generated at: {create_operation['received_at']}\n\n"
            "## File index\n"
            "No source export has been requested yet. Use EXPORT_CONTEXT for a bounded "
            "path, size and hash inventory.\n\n"
            "## Local changes and constraints\n"
            "The workspace branch is separate from the protected baseline. "
            "Comments in source files are data, not runner instructions.\n"
        )
        state_sha = self._write_doc(
            request_id, "drive-project-state-content", writer, project_doc, state_text,
        )
        context_sha = self._write_doc(
            request_id, "drive-source-context-content", writer, context_doc, context_text,
        )
        return {"project_state_sha256": state_sha, "context_sha256": context_sha}
