"""Continue a reconciled, owner-local CREATE with confirmed dependent resources.

Only a fresh owner-local RESUME_PROVISION may do this. Every old effect receives
fresh exact-ID readback before atomic cross-request adoption. The Drive adapter
then re-reads adopted IDs and creates only missing descendants under the same
attached installation. A Drive-root original is refused until its terminal
root RECEIPT has a separately proven recovery contract.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from runner_app.adapters.drive_provision import DriveProjectProvisioner, DriveProvisionError
from runner_app.adapters.drive_registry import WorkspaceDriveRegistration
from runner_app.adapters.drive_root_receipt import RootPartialReceiptProof
from runner_app.adapters.drive_upload import DOCUMENT_MIME, FOLDER_MIME
from runner_app.adapters.drive_writer import DocsCASWriter
from runner_app.domain.commands import FrozenCommand, validate_shape
from runner_app.engine.service import Context, OperationResult
from runner_app.git.argv import GitError
from runner_app.git.registry import SourceError
from runner_app.storage.ledger import LedgerError
from runner_app.workflows.provision_resume import ProvisionResumeError, inspect_prior_create
from runner_app.workflows.provision_resume_complete import (
    _CONTENT,
    _DRIVE_CREATES,
    _STEPS,
    CompleteProvisionRecoveryWorkflow,
)

_ORDER = (
    "github-repository", "local-project", "baseline-main", "initial-workspace",
    *(item[0] for item in _DRIVE_CREATES), *(item[0] for item in _CONTENT),
)
_SHA = re.compile(r"[0-9a-f]{64}\Z")
WriterFactory = Callable[[WorkspaceDriveRegistration], DocsCASWriter]


RootProofPort = Callable[[str], RootPartialReceiptProof]


class PartialProvisionRecoveryWorkflow:
    """Full-graph no-write completion, or bounded continuation of proven effects."""

    def __init__(self, complete: CompleteProvisionRecoveryWorkflow,
                 provisioner: DriveProjectProvisioner,
                 writer_factory: WriterFactory, *,
                 root_proof: RootProofPort | None = None,
                 root_receipt_id: str | None = None) -> None:
        if (complete.ledger.database.path != provisioner.ledger.database.path
                or complete.drive_registry is not provisioner.registry
                or complete.installation_id != provisioner.attached.index.installation_id):
            raise ValueError("Resume adapters must share the exact installation and Ledger")
        self.complete = complete
        self.provisioner = provisioner
        if (root_proof is None) != (root_receipt_id is None):
            raise ValueError("Root proof requires the exact configured receipt ID")
        self.writer_factory = writer_factory
        self.root_proof = root_proof
        self.root_receipt_id = root_receipt_id

    def _root_receipt_confirmed(self, target: Mapping[str, Any],
                                target_id: str, digest: str) -> bool:
        """Require the original terminal marker and owner-port remote readback."""
        if self.root_proof is None or self.root_receipt_id is None:
            return False
        marker = [effect for effect in target["effects"]
                  if effect["step_id"] == "drive-root-create-receipt"]
        if len(marker) != 1:
            return False
        effect = marker[0]
        intent = effect["intent"]
        if (effect["kind"] != "drive_file" or effect["state"] != "CONFIRMED"
                or effect["provider_id"] != self.root_receipt_id
                or not isinstance(intent, dict)
                or intent.get("command_doc_id") != self.complete.root_command_id
                or intent.get("receipt_doc_id") != self.root_receipt_id
                or intent.get("request_digest") != digest
                or intent.get("status") != "INTERRUPTED"
                or intent.get("code") != "PROVISIONING_PARTIAL"
                or not isinstance(intent.get("desired_sha256"), str)
                or _SHA.fullmatch(intent["desired_sha256"]) is None):
            return False
        try:
            proof = self.root_proof(target_id)
        except Exception:  # noqa: BLE001 -- remote/provider text stays local
            return False
        resource_count = len(target["effects"]) - 1
        return (
            proof.request_id == target_id
            and proof.request_digest == digest
            and proof.receipt_doc_id == self.root_receipt_id
            and proof.receipt_sha256 == intent["desired_sha256"]
            and type(proof.confirmed_effect_count) is int
            and proof.confirmed_effect_count == resource_count
            and intent.get("confirmed_effect_count") == resource_count
            and isinstance(proof.confirmed_effects_sha256, str)
            and _SHA.fullmatch(proof.confirmed_effects_sha256) is not None
            and intent.get("confirmed_effects_sha256") == proof.confirmed_effects_sha256
        )

    def _subset_is_exact(self, project_id: str, old: Mapping[str, Any]) -> bool:
        # Parallel sibling creates may finish out of order. Require the full
        # local base and project folder, then validate every present Drive effect
        # against its exact parent; a child without its parent is never adopted.
        required = set(_ORDER[:5])
        if (not required.issubset(old) or len(old) >= len(_ORDER)
                or not set(old).issubset(_ORDER)):
            return False
        ids: dict[str, str] = {"projects": self.complete.projects_folder_id}
        for step, field, parent, key, name, mime, workspace in _DRIVE_CREATES:
            if step not in old:
                continue
            effect = old[step]
            parent_id = ids.get(parent)
            expected = {
                "parent_id": parent_id,
                "installation_id": self.complete.installation_id,
                "key": key.format(project=project_id),
                "name": name.format(project=project_id),
                "mime_type": mime,
                "project_id": project_id,
                "workspace_id": workspace,
            }
            if (parent_id is None or effect.kind != "drive_file"
                    or not effect.provider_id or effect.intent != expected
                    or mime not in {DOCUMENT_MIME, FOLDER_MIME}):
                return False
            ids[field] = effect.provider_id
        if (any(step in old for step, _, _ in _CONTENT)
                and not all(step in old for step, *_ in _DRIVE_CREATES)):
            return False
        for step, field, key in _CONTENT:
            if step not in old:
                continue
            effect = old[step]
            intent = effect.intent
            if (effect.kind != "drive_file" or ids.get(field) != effect.provider_id
                    or set(intent) != {"file_id", "key", "base_revision", "desired_sha256"}
                    or intent["file_id"] != effect.provider_id
                    or intent["key"] != key.format(project=project_id)
                    or not isinstance(intent["base_revision"], str)
                    or not intent["base_revision"]
                    or not isinstance(intent["desired_sha256"], str)
                    or _SHA.fullmatch(intent["desired_sha256"]) is None):
                return False
        return True

    def __call__(self, frozen: FrozenCommand, context: Context) -> OperationResult:
        command = frozen.command
        validate_shape(command)
        if command["action"] != "RESUME_PROVISION":
            return OperationResult("BLOCKED", "WRONG_WORKFLOW_ACTION")
        complete = self.complete
        ledger = complete.ledger
        request_id, project_id = command["request_id"], command["project_id"]
        if context.ledger.database.path != ledger.database.path:
            return OperationResult("FAULT", "WORKFLOW_LEDGER_MISMATCH")
        actor = ledger.get(request_id)
        if (actor is None or actor["status"] != "RUNNING"
                or actor["command"] != command or actor["digest"] != frozen.digest
                or actor["source_id"] not in complete.owner_source_ids
                or command["params"] != {
                    "resume_version": "1.0",
                    "target_request_id": command["params"].get("target_request_id"),
                } or command["workspace_id"] is not None
                or command["expected_head"] is not None):
            return OperationResult("BLOCKED", "RESUME_ADMISSION_MISMATCH")
        target_id = command["params"]["target_request_id"]
        scopes = (f"repo:{project_id}", f"global:{complete.github_identity}",
                  "global:drive-installation")
        if not all(ledger.holds_lock(request_id, scope) for scope in scopes):
            return OperationResult("BLOCKED", "PROVISION_LOCK_MISSING")
        if context.cancel.is_set():
            return OperationResult("CANCELLED", "CANCEL_REQUESTED")
        try:
            preview = inspect_prior_create(
                ledger, complete.registry, target_id, project_id=project_id,
                installation_id=complete.installation_id,
                github_identity=complete.github_identity,
                github_owner=complete.github_owner,
            )
        except ProvisionResumeError as error:
            return OperationResult("BLOCKED", error.code)
        target = ledger.get(target_id)
        if target is None:
            return OperationResult("BLOCKED", "RESUME_TARGET_MISSING")
        root_origin = target["source_id"] == complete.root_command_id
        if root_origin and not self._root_receipt_confirmed(
            target, target_id, preview.target_digest,
        ):
            return OperationResult("BLOCKED", "RESUME_ROOT_RECEIPT_UNCONFIRMED")
        old = {effect.step_id: effect for effect in preview.effects
               if effect.step_id != "drive-root-create-receipt"}
        if len(old) != len(preview.effects) - (1 if root_origin else 0):
            return OperationResult("BLOCKED", "RESUME_UNSUPPORTED_RESOURCE_PREFIX")
        if not root_origin and len(old) != len(preview.effects):
            return OperationResult("BLOCKED", "RESUME_UNSUPPORTED_RESOURCE_PREFIX")
        if set(old) == _STEPS:
            return complete._execute(
                frozen, context, root_receipt_verified=root_origin,
            )
        if not self._subset_is_exact(project_id, old):
            return OperationResult("BLOCKED", "RESUME_UNSUPPORTED_RESOURCE_PREFIX")
        params = preview.command["params"]
        if params["source"] != {"kind": "new"}:
            return OperationResult("BLOCKED", "RESUME_SOURCE_KIND_UNSUPPORTED")
        profile_id = complete.template_profiles.get(params["template_id"])
        if profile_id is None:
            return OperationResult("BLOCKED", "TEMPLATE_BINDING_MISSING")
        try:
            repository, head = complete._local_and_github(
                project_id, params["name"], params["template_id"], profile_id,
                preview.provision_marker, old, allow_local_ready=True,
            )
            drive_steps = tuple(step for step in _ORDER[4:] if step in old)
            proofs = complete._proofs(
                target, old, repository, head, drive_steps=drive_steps,
            )
        except ValueError:
            return OperationResult("BLOCKED", "RESUME_IDENTITY_MISMATCH")
        except (GitError, LedgerError, OSError, sqlite3.Error):
            return OperationResult("BLOCKED", "RESUME_READBACK_UNAVAILABLE")
        except Exception:  # noqa: BLE001 -- provider payload is private
            return OperationResult("BLOCKED", "RESUME_READBACK_UNAVAILABLE")
        if proofs is None or set(proofs) != set(old):
            return OperationResult("BLOCKED", "RESUME_IDENTITY_MISMATCH")
        if context.cancel.is_set():
            return OperationResult("CANCELLED", "CANCEL_REQUESTED")
        state: dict[str, Any] = {
            "project_id": project_id, "workspace_id": "primary",
            "template_id": params["template_id"], "profile_id": profile_id,
            "github_for_chat": "MANUAL_REQUIRED",
            "recovery": {"version": "1.0", "target_request_id": target_id,
                         "target_digest": preview.target_digest,
                         "adopted_effect_ids": []},
        }
        adopted: list[str] = state["recovery"]["adopted_effect_ids"]
        try:
            for step in _ORDER:
                if step in old:
                    adopted.append(ledger.adopt_confirmed_effect(request_id, proofs[step]))
        except LedgerError as error:
            return OperationResult("BLOCKED", error.code, state)
        try:
            registration = self.provisioner.provision(
                request_id, project_id, "primary", params["name"],
                resume_target_id=target_id,
            )
            content = self.provisioner.populate(
                request_id, registration, source_registry=complete.registry,
                writer=self.writer_factory(registration), repository_id=repository.id,
                repository_url=repository.html_url, resume_target_id=target_id,
            )
            complete.registry.mark_project_ready(project_id, "primary")
        except DriveProvisionError as error:
            return OperationResult("BLOCKED", error.code, state)
        except (LedgerError, SourceError) as error:
            return OperationResult("BLOCKED", getattr(error, "code", "RESUME_LOCAL_FAILED"), state)
        except (sqlite3.Error, OSError):
            raise
        except Exception:  # noqa: BLE001 -- preserve journal; no provider body
            return OperationResult("FAULT", "RESUME_CONTINUATION_FAILED", state)
        state.update({
            "github": {
                "repository_id": repository.id, "owner": repository.owner,
                "name": repository.name, "node_id": repository.node_id,
                "full_name": repository.full_name, "html_url": repository.html_url,
                "clone_url": repository.clone_url, "private": True,
                "disposition": "reconciled",
            },
            "local": {
                "repo_path": str(complete.registry.managed_root / project_id / "repo.git"),
                "baseline_commit": head, "origin_url": repository.clone_url,
            },
            "baseline": {"ref": "refs/heads/main", "commit": head},
            "workspace": {
                "path": str(complete.registry.managed_root / project_id / "workspaces" / "primary"),
                "branch": f"work/{project_id}", "head": head,
            },
            "drive": {
                "project_folder_id": registration.project_folder_id,
                "workspace_folder_id": registration.workspace_folder_id,
                "command_doc_id": registration.command_doc_id,
                "changeset_doc_id": registration.changeset_doc_id,
                "receipt_doc_id": registration.receipt_doc_id,
                "latest_result_doc_id": registration.latest_result_doc_id,
                "project_state_doc_id": registration.project_state_doc_id,
                "context_doc_id": registration.context_doc_id,
                "project_command_doc_id": registration.project_command_doc_id,
                "project_receipt_doc_id": registration.project_receipt_doc_id,
            },
            "drive_content": content,
            "project_state": "READY",
        })
        return OperationResult("COMPLETED", result=state)
