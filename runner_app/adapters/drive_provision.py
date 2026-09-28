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
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from runner_app.adapters.drive_index import AttachedInstallation
from runner_app.adapters.drive_registry import WorkspaceDriveRegistration, WorkspaceDriveRegistry
from runner_app.adapters.drive_upload import DOCUMENT_MIME, FOLDER_MIME, GoogleUploadProvider
from runner_app.adapters.drive_writer import DocsCASWriter, ObservedOutputDoc, RegisteredOutputDoc
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


@dataclass(frozen=True)
class _FileSpec:
    step_id: str
    parent_id: str
    installation_id: str
    key: str
    name: str
    mime_type: str
    project_id: str
    workspace_id: str | None

    def intent(self) -> dict[str, Any]:
        return {
            "parent_id": self.parent_id, "installation_id": self.installation_id,
            "key": self.key, "name": self.name, "mime_type": self.mime_type,
            "project_id": self.project_id, "workspace_id": self.workspace_id,
        }

    def expected(self) -> dict[str, Any]:
        return {
            "expected_parent_id": self.parent_id,
            "expected_installation_id": self.installation_id,
            "expected_key": self.key,
            "expected_mime_type": self.mime_type,
            "expected_name": self.name,
            "expected_project_id": self.project_id,
            **({"expected_workspace_id": self.workspace_id}
               if self.workspace_id is not None else {}),
        }

    def lookup_expected(self) -> dict[str, Any]:
        return {
            "expected_mime_type": self.mime_type,
            "expected_name": self.name,
            "expected_project_id": self.project_id,
            **({"expected_workspace_id": self.workspace_id}
               if self.workspace_id is not None else {}),
        }


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
        """Authorize only the original exact CREATE and its adopted Drive folder."""
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

    def _prepare_file(self, request_id: str, spec: _FileSpec) -> str | None:
        """Read one sibling's prior intent before any new write in its batch."""
        operation = self.ledger.get(request_id)
        if operation is None or operation["status"] != "RUNNING":
            raise DriveProvisionError("PROVISION_NOT_RUNNING")
        previous = [effect for effect in operation["effects"]
                    if effect["step_id"] == spec.step_id and effect["kind"] == "drive_file"]
        if len(previous) > 1:
            raise DriveProvisionError("PROVISION_EFFECT_DUPLICATED")
        if previous:
            effect = previous[0]
            if effect["intent"] != spec.intent():
                raise DriveProvisionError("PROVISION_INTENT_CHANGED")
            if effect["state"] == "CONFIRMED":
                found = self.upload.get_file(effect["provider_id"], **spec.expected())
            elif effect["state"] in {"INTENDED", "UNKNOWN"}:
                reconciled = self.upload.find_by_key(
                    spec.parent_id, spec.installation_id, spec.key,
                    **spec.lookup_expected(),
                )
                if reconciled is None:
                    # A negative search cannot prove a timed-out create did not land.
                    raise DriveProvisionError("PROVISION_EFFECT_UNCERTAIN")
                found = reconciled
                self.ledger.confirm_effect(effect["effect_id"], str(found["id"]))
            else:
                raise DriveProvisionError("PROVISION_EFFECT_INVALID")
            if not isinstance(found.get("id"), str) or not found["id"]:
                raise DriveProvisionError("PROVISION_PROVIDER_ID_INVALID")
            return str(found["id"])
        # A matching file without this operation's durable intent is not owned.
        if self.upload.find_by_key(spec.parent_id, spec.installation_id, spec.key,
                                   **spec.lookup_expected()) is not None:
            raise DriveProvisionError("PROVISION_RESOURCE_EXISTS")
        return None

    def _create_file(self, request_id: str, spec: _FileSpec, effect_id: str) -> str:
        if spec.mime_type == FOLDER_MIME:
            created = self.upload.create_folder(
                spec.parent_id, spec.installation_id, spec.key, spec.name,
                project_id=spec.project_id, workspace_id=spec.workspace_id,
            )
        elif spec.mime_type == DOCUMENT_MIME:
            created = self.upload.create_document(
                spec.parent_id, spec.installation_id, spec.key, spec.name,
                project_id=spec.project_id, workspace_id=spec.workspace_id,
            )
        else:
            raise DriveProvisionError("PROVISION_FILE_INVALID")
        # The concrete Google adapter already exact-read and validated its
        # create response. Other ports may return only an ID and need our read.
        found = (created if type(cast(object, self.upload)) is GoogleUploadProvider
                 else self.upload.get_file(str(created["id"]), **spec.expected()))
        if not isinstance(found.get("id"), str) or not found["id"]:
            raise DriveProvisionError("PROVISION_PROVIDER_ID_INVALID")
        self.ledger.confirm_effect(effect_id, str(found["id"]))
        return str(found["id"])

    def _file(self, request_id: str, spec: _FileSpec) -> str:
        existing = self._prepare_file(request_id, spec)
        if existing is not None:
            return existing
        effect_id = self.ledger.begin_effect(
            request_id, spec.step_id, "drive_file", spec.intent(),
        )
        return self._create_file(request_id, spec, effect_id)

    def _start_files(
        self, request_id: str, specs: tuple[_FileSpec, ...],
        pool: ThreadPoolExecutor,
    ) -> tuple[str | Future[str], ...]:
        """Preflight siblings, journal new intents in order, then submit writes."""
        if not specs or len(specs) > 5:
            raise ValueError("A bounded sibling batch is required")
        preflight = [pool.submit(self._prepare_file, request_id, spec)
                     for spec in specs]
        existing: list[str | None] = []
        failures: list[Exception] = []
        for read_future in preflight:
            try:
                existing.append(read_future.result())
            except Exception as error:  # noqa: BLE001 - join all before raising
                failures.append(error)
                existing.append(None)
        if failures:
            raise failures[0]

        # The declared order is stable even when remote calls finish out of order.
        effects: list[str | None] = []
        for spec, provider_id in zip(specs, existing, strict=True):
            effects.append(None if provider_id is not None else self.ledger.begin_effect(
                request_id, spec.step_id, "drive_file", spec.intent(),
            ))
        handles: list[str | Future[str]] = []
        for spec, provider_id, effect_id in zip(specs, existing, effects, strict=True):
            if provider_id is not None:
                handles.append(provider_id)
            else:
                assert effect_id is not None
                handles.append(pool.submit(self._create_file, request_id, spec, effect_id))
        return tuple(handles)

    @staticmethod
    def _collect_files(
        handles: tuple[str | Future[str], ...],
    ) -> tuple[tuple[str, ...], list[Exception]]:
        """Join every submitted sibling before reporting any one failure."""
        result: list[str] = []
        failures: list[Exception] = []
        for handle in handles:
            if isinstance(handle, str):
                result.append(handle)
            else:
                try:
                    result.append(handle.result())
                except Exception as error:  # noqa: BLE001 - collect all futures
                    failures.append(error)
        return tuple(result), failures

    def _files_parallel(self, request_id: str,
                        specs: tuple[_FileSpec, ...]) -> tuple[str, ...]:
        """Run independent siblings within a four-worker bound."""
        with ThreadPoolExecutor(max_workers=min(4, len(specs)),
                                thread_name_prefix="cdr-drive-create") as pool:
            handles = self._start_files(request_id, specs, pool)
            values, failures = self._collect_files(handles)
            if failures:
                raise failures[0]
            return values

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
            request_id, _FileSpec("drive-project-folder", projects_id,
                                  installation_id, f"project:{project_id}",
                                  project_id, FOLDER_MIME, project_id, None),
        )
        # Start each child only after its own parent is confirmed. Project
        # documents do not gate workspace descendants, so their writes may
        # still be running when the workspace folder releases its children.
        project_specs = (
            _FileSpec("drive-project-state-doc", project_folder,
                      installation_id, "project-state", "PROJECT STATE",
                      DOCUMENT_MIME, project_id, None),
            _FileSpec("drive-project-command-doc", project_folder,
                      installation_id, "project-command", "PROJECT COMMAND",
                      DOCUMENT_MIME, project_id, None),
            _FileSpec("drive-project-receipt-doc", project_folder,
                      installation_id, "project-receipt", "PROJECT RECEIPT",
                      DOCUMENT_MIME, project_id, None),
            _FileSpec("drive-workspace-folder", project_folder,
                      installation_id, f"workspace:{project_id}/{workspace_id}",
                      workspace_id, FOLDER_MIME, project_id, workspace_id),
        )
        with ThreadPoolExecutor(max_workers=4,
                                thread_name_prefix="cdr-drive-create") as pool:
            project_handles = self._start_files(request_id, project_specs, pool)
            workspace_handle = project_handles[3]
            try:
                workspace_folder = (
                    workspace_handle if isinstance(workspace_handle, str)
                    else workspace_handle.result()
                )
            except Exception:  # noqa: BLE001 - prefer first declared sibling failure
                _, project_failures = self._collect_files(project_handles)
                assert project_failures
                raise project_failures[0]
            workspace_specs = (
                _FileSpec("drive-source-context-doc", workspace_folder,
                          installation_id, "source-context", "SOURCE CONTEXT",
                          DOCUMENT_MIME, project_id, workspace_id),
                _FileSpec("drive-command-doc", workspace_folder,
                          installation_id, "workspace-command", "COMMAND",
                          DOCUMENT_MIME, project_id, workspace_id),
                _FileSpec("drive-changeset-doc", workspace_folder,
                          installation_id, "workspace-changeset", "CHANGESET",
                          DOCUMENT_MIME, project_id, workspace_id),
                _FileSpec("drive-receipt-doc", workspace_folder,
                          installation_id, "workspace-receipt", "RECEIPT",
                          DOCUMENT_MIME, project_id, workspace_id),
                _FileSpec("drive-latest-result-doc", workspace_folder,
                          installation_id, "latest-result", "LATEST RESULT",
                          DOCUMENT_MIME, project_id, workspace_id),
            )
            try:
                workspace_handles = self._start_files(
                    request_id, workspace_specs, pool,
                )
            except Exception:
                _, project_failures = self._collect_files(project_handles)
                if project_failures:
                    raise project_failures[0] from None
                raise
            project_values, project_failures = self._collect_files(project_handles)
            workspace_values, workspace_failures = self._collect_files(workspace_handles)
            if project_failures:
                raise project_failures[0]
            if workspace_failures:
                raise workspace_failures[0]
        project_state, project_command, project_receipt, _ = project_values
        context_doc, command_doc, changeset_doc, receipt_doc, latest_result_doc = (
            workspace_values
        )
        assert all(isinstance(value, str) for value in (
            project_state, project_command, project_receipt, workspace_folder,
            context_doc, command_doc, changeset_doc, receipt_doc, latest_result_doc,
        ))
        registration = WorkspaceDriveRegistration(
            installation_id, project_id, workspace_id, project_folder,
            workspace_folder, command_doc, changeset_doc,
            receipt_doc, latest_result_doc,
            project_state, context_doc, project_command, project_receipt,
        )
        return self.registry.register(registration)

    def _prepare_doc(self, request_id: str, step_id: str, writer: DocsCASWriter,
                     registration: RegisteredOutputDoc, text: str
                     ) -> tuple[str, ObservedOutputDoc | None]:
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
            return desired_sha256, None
        if observed.snapshot.text.strip("\n") != "":
            raise DriveProvisionError("PROVISION_DOC_NOT_EMPTY")
        return desired_sha256, observed

    def _finish_doc(self, request_id: str, writer: DocsCASWriter,
                    registration: RegisteredOutputDoc, text: str,
                    observed: ObservedOutputDoc, desired_sha256: str,
                    effect_id: str) -> str:
        result = writer.replace(registration.key, text, expected=observed)
        # DocsCASWriter already performed an exact post-CAS read. Its confirmed
        # proof saves a redundant network read; any weaker result stays pending.
        if (result.state not in {"CONFIRMED", "UNCHANGED"}
                or result.key != registration.key
                or result.file_id != registration.file_id
                or result.desired_sha256 != desired_sha256
                or result.observed_sha256 != desired_sha256
                or result.observed_text != text):
            raise DriveProvisionError("PROVISION_DOC_UNCERTAIN")
        self.ledger.confirm_effect(effect_id, registration.file_id)
        return desired_sha256

    def _write_doc(self, request_id: str, step_id: str, writer: DocsCASWriter,
                   registration: RegisteredOutputDoc, text: str) -> str:
        desired_sha256, observed = self._prepare_doc(
            request_id, step_id, writer, registration, text,
        )
        if observed is None:
            return desired_sha256
        intent = {"file_id": registration.file_id, "key": registration.key,
                  "base_revision": observed.snapshot.revision_id,
                  "desired_sha256": desired_sha256}
        effect_id = self.ledger.begin_effect(request_id, step_id, "drive_file", intent)
        return self._finish_doc(
            request_id, writer, registration, text, observed, desired_sha256, effect_id,
        )

    def _write_docs_parallel(
        self, request_id: str, writer: DocsCASWriter,
        docs: tuple[tuple[str, RegisteredOutputDoc, str], ...],
    ) -> tuple[str, ...]:
        """Preflight both independent Docs, then CAS each once after its intent."""
        if not docs or len(docs) > 2:
            raise ValueError("A bounded content batch is required")
        with ThreadPoolExecutor(max_workers=len(docs),
                                thread_name_prefix="cdr-drive-content") as pool:
            preflight = [pool.submit(
                self._prepare_doc, request_id, step_id, writer, registration, text,
            ) for step_id, registration, text in docs]
            prepared: list[tuple[str, ObservedOutputDoc | None]] = []
            failures: list[Exception] = []
            for read_future in preflight:
                try:
                    prepared.append(read_future.result())
                except Exception as error:  # noqa: BLE001 - join both before raising
                    failures.append(error)
            if failures:
                raise failures[0]

            effects: list[str | None] = []
            for (step_id, registration, _), (digest, observed) in zip(
                docs, prepared, strict=True,
            ):
                if observed is None:
                    effects.append(None)
                    continue
                intent = {"file_id": registration.file_id, "key": registration.key,
                          "base_revision": observed.snapshot.revision_id,
                          "desired_sha256": digest}
                effects.append(self.ledger.begin_effect(
                    request_id, step_id, "drive_file", intent,
                ))
            pending: list[Future[str] | None] = []
            for (_, registration, text), (digest, observed), effect_id in zip(
                docs, prepared, effects, strict=True,
            ):
                pending.append(
                    None if observed is None or effect_id is None else pool.submit(
                        self._finish_doc, request_id, writer, registration, text,
                        observed, digest, effect_id,
                    )
                )
            result: list[str] = []
            for (digest, _), write_future in zip(prepared, pending, strict=True):
                if write_future is None:
                    result.append(digest)
                else:
                    try:
                        result.append(write_future.result())
                    except Exception as error:  # noqa: BLE001 - join both before raising
                        failures.append(error)
            if failures:
                raise failures[0]
            return tuple(result)

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
        state_sha, context_sha = self._write_docs_parallel(
            request_id, writer, (
                ("drive-project-state-content", project_doc, state_text),
                ("drive-source-context-content", context_doc, context_text),
            ),
        )
        return {"project_state_sha256": state_sha, "context_sha256": context_sha}

    def provision_workspace(self, request_id: str, project_id: str,
                            workspace_id: str) -> WorkspaceDriveRegistration:
        """Create six descendants of an existing, exact registered project.

        A CREATE_WORKSPACE never creates or adopts a second project folder. Each
        file is intent-first and an uncertain create stays unresolved as in the
        primary provisioning route.
        """
        operation = self.ledger.get(request_id)
        if (operation is None or operation["status"] != "RUNNING"
                or operation["action"] != "CREATE_WORKSPACE"
                or operation["project_id"] != project_id
                or operation["workspace_id"] != workspace_id
                or workspace_id == "primary"
                or not self.ledger.holds_lock(request_id, f"repo:{project_id}")
                or not self.ledger.holds_lock(request_id, "global:drive-installation")):
            raise DriveProvisionError("WORKSPACE_PROVISION_SCOPE_MISMATCH")
        primary = self.registry.get(project_id, "primary")
        if (primary.installation_id != self.attached.index.installation_id
                or primary.project_id != project_id or primary.workspace_id != "primary"
                or None in (primary.project_state_doc_id, primary.project_command_doc_id,
                            primary.project_receipt_doc_id)):
            raise DriveProvisionError("WORKSPACE_PROJECT_REGISTRATION_INVALID")
        project_folder = primary.project_folder_id
        installation_id = primary.installation_id
        workspace_folder = self._file(
            request_id, _FileSpec("drive-workspace-folder", project_folder,
                                  installation_id, f"workspace:{project_id}/{workspace_id}",
                                  workspace_id, FOLDER_MIME, project_id, workspace_id),
        )
        context_doc, command_doc, changeset_doc, receipt_doc, latest_result_doc = (
            self._files_parallel(request_id, (
                _FileSpec("drive-source-context-doc", workspace_folder,
                          installation_id, "source-context", "SOURCE CONTEXT",
                          DOCUMENT_MIME, project_id, workspace_id),
                _FileSpec("drive-command-doc", workspace_folder,
                          installation_id, "workspace-command", "COMMAND",
                          DOCUMENT_MIME, project_id, workspace_id),
                _FileSpec("drive-changeset-doc", workspace_folder,
                          installation_id, "workspace-changeset", "CHANGESET",
                          DOCUMENT_MIME, project_id, workspace_id),
                _FileSpec("drive-receipt-doc", workspace_folder,
                          installation_id, "workspace-receipt", "RECEIPT",
                          DOCUMENT_MIME, project_id, workspace_id),
                _FileSpec("drive-latest-result-doc", workspace_folder,
                          installation_id, "latest-result", "LATEST RESULT",
                          DOCUMENT_MIME, project_id, workspace_id),
            ))
        )
        registration = WorkspaceDriveRegistration(
            installation_id, project_id, workspace_id, project_folder,
            workspace_folder, command_doc, changeset_doc,
            receipt_doc, latest_result_doc,
            primary.project_state_doc_id, context_doc,
            primary.project_command_doc_id, primary.project_receipt_doc_id,
        )
        return self.registry.register(registration)

    def populate_workspace(self, request_id: str, registration: WorkspaceDriveRegistration,
                           *, source_registry: SourceRegistry, writer: DocsCASWriter
                           ) -> dict[str, str]:
        """Publish a new area's context and append its exact IDs to PROJECT STATE.

        The shared project state changes once under revision CAS. A lost response
        can only be confirmed by reading the exact desired hash; it is never
        blindly written again.
        """
        operation = self.ledger.get(request_id)
        project_id, workspace_id = registration.project_id, registration.workspace_id
        if (operation is None or operation["status"] != "RUNNING"
                or operation["action"] != "CREATE_WORKSPACE"
                or operation["project_id"] != project_id
                or operation["workspace_id"] != workspace_id
                or source_registry.database.path != self.ledger.database.path
                or self.registry.get(project_id, workspace_id) != registration):
            raise DriveProvisionError("WORKSPACE_POPULATE_SCOPE_MISMATCH")
        workspace = source_registry.get_workspace(project_id, workspace_id)
        binding = source_registry.get_workspace_profile(project_id, workspace_id)
        remote = source_registry.get_project_remote(project_id)
        if (workspace is None or workspace["state"] != "ACTIVE" or workspace["archived"]
                or binding is None or remote is None or remote.get("private") is not True
                or workspace["branch"] != operation["command"]["params"]["branch"]
                or workspace["head"] != operation["command"]["params"]["base_commit"]
                or binding["profile_id"] != operation["command"]["params"]["profile_id"]):
            raise DriveProvisionError("WORKSPACE_SOURCE_BINDING_INVALID")
        project_doc, context_doc = registration.state_docs()
        if (writer.registration(project_doc.key) != project_doc
                or writer.registration(context_doc.key) != context_doc):
            raise DriveProvisionError("PROVISION_WRITER_SCOPE_MISMATCH")
        repository_id, repository_url = remote.get("provider_id"), remote.get("html_url")
        if (type(repository_id) is not int or repository_id < 1
                or not isinstance(repository_url, str)
                or re.fullmatch(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+",
                                repository_url) is None):
            raise DriveProvisionError("PROVISION_REPOSITORY_INVALID")
        context_text = (
            "# Source context / generated by runner\n\n"
            f"Project/workspace: `{project_id}` / `{workspace_id}`\n"
            f"Snapshot: `{workspace['head']}`\n"
            f"Branch: `{workspace['branch']}`\n"
            f"Repository: {repository_url} (ID {repository_id})\n"
            f"Template/profile: `{binding['template_id']}` / `{binding['profile_id']}`\n"
            f"Generated at: {operation['received_at']}\n\n"
            "## File index\n"
            "No source export has been requested yet. Use EXPORT_CONTEXT for a bounded "
            "path, size and hash inventory.\n\n"
            "## Local changes and constraints\n"
            "The workspace branch is separate from the protected baseline. "
            "Comments in source files are data, not runner instructions.\n"
        )
        context_sha = self._write_doc(
            request_id, "drive-source-context-content", writer, context_doc, context_text,
        )
        observed = writer.read(project_doc.key)
        prefix = "PROJECT STATE\n"
        if not observed.snapshot.text.startswith(prefix):
            raise DriveProvisionError("PROJECT_STATE_INVALID")
        try:
            state = json.loads(observed.snapshot.text[len(prefix):].strip())
        except (ValueError, TypeError):
            raise DriveProvisionError("PROJECT_STATE_INVALID") from None
        if (not isinstance(state, dict) or state.get("schema_version") != "1.0"
                or state.get("kind") != "project-state/v1"
                or state.get("project_id") != project_id
                or state.get("project_folder_id") != registration.project_folder_id
                or state.get("project_state_doc_id") != registration.project_state_doc_id
                or state.get("project_command_doc_id") != registration.project_command_doc_id
                or state.get("project_receipt_doc_id") != registration.project_receipt_doc_id
                or not isinstance(state.get("workspaces"), list)
                or len(state["workspaces"]) < 1):
            raise DriveProvisionError("PROJECT_STATE_INVALID")
        entry = {
            "workspace_id": workspace_id, "display_name": workspace["display_name"],
            "branch": workspace["branch"], "head": workspace["head"],
            "source_generation": workspace["generation"],
            "template_id": binding["template_id"], "profile_id": binding["profile_id"],
            "command_doc_id": registration.command_doc_id,
            "changeset_doc_id": registration.changeset_doc_id,
            "receipt_doc_id": registration.receipt_doc_id,
            "latest_result_doc_id": registration.latest_result_doc_id,
            "context_doc_id": registration.context_doc_id,
        }
        existing = [item for item in state["workspaces"]
                    if isinstance(item, dict) and item.get("workspace_id") == workspace_id]
        if len(existing) > 1 or (existing and existing[0] != entry):
            raise DriveProvisionError("PROJECT_STATE_WORKSPACE_CONFLICT")
        before = self.ledger.get(request_id)
        assert before is not None
        previous = [effect for effect in before["effects"]
                    if effect["step_id"] == "drive-project-state-workspace-add"
                    and effect["kind"] == "drive_file"]
        if len(previous) > 1:
            raise DriveProvisionError("PROVISION_EFFECT_DUPLICATED")
        if not existing:
            if previous:
                raise DriveProvisionError("PROVISION_DOC_UNCERTAIN")
            state["workspaces"].append(entry)
        desired_text = prefix + json.dumps(
            state, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ) + "\n"
        desired_sha = hashlib.sha256(desired_text.encode("utf-8")).hexdigest()
        if previous:
            effect = previous[0]
            intent = effect["intent"]
            if (intent.get("file_id") != project_doc.file_id
                    or intent.get("key") != project_doc.key
                    or intent.get("desired_sha256") != desired_sha
                    or observed.snapshot.sha256 != desired_sha):
                raise DriveProvisionError("PROVISION_DOC_UNCERTAIN")
            if effect["state"] in {"INTENDED", "UNKNOWN"}:
                self.ledger.confirm_effect(effect["effect_id"], project_doc.file_id)
            elif effect["state"] != "CONFIRMED" or effect["provider_id"] != project_doc.file_id:
                raise DriveProvisionError("PROVISION_EFFECT_INVALID")
            return {"context_sha256": context_sha, "project_state_sha256": desired_sha}
        if existing:
            raise DriveProvisionError("PROJECT_STATE_WORKSPACE_CONFLICT")
        intent = {"file_id": project_doc.file_id, "key": project_doc.key,
                  "base_revision": observed.snapshot.revision_id,
                  "base_sha256": observed.snapshot.sha256,
                  "desired_sha256": desired_sha, "workspace_id": workspace_id}
        effect_id = self.ledger.begin_effect(
            request_id, "drive-project-state-workspace-add", "drive_file", intent,
        )
        result = writer.replace(project_doc.key, desired_text, expected=observed)
        if result.state not in {"CONFIRMED", "UNCHANGED"}:
            raise DriveProvisionError("PROVISION_DOC_UNCERTAIN")
        current = writer.read(project_doc.key)
        if current.snapshot.sha256 != desired_sha:
            raise DriveProvisionError("PROVISION_DOC_UNCERTAIN")
        self.ledger.confirm_effect(effect_id, project_doc.file_id)
        return {"context_sha256": context_sha, "project_state_sha256": desired_sha}
