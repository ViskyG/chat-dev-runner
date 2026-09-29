"""Exact registered Drive association update after a clean workspace retarget.

The old identity is read before local Git mutation. Two durable document intents
precede either CAS write, so a partial cloud update retains the repository lock
for read-only reconciliation. Unrelated PROJECT STATE entries retain their
values, and later SOURCE CONTEXT content retains its text.
"""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, cast

from runner_app.adapters.drive_docs import DriveError, ReadProvider
from runner_app.adapters.drive_index import AttachedInstallation
from runner_app.adapters.drive_registry import WorkspaceDriveRegistry
from runner_app.adapters.drive_writer import DocsCASWriter, ObservedOutputDoc
from runner_app.storage.ledger import Ledger


@dataclass(frozen=True)
class _DocumentPlan:
    step: str
    observed: ObservedOutputDoc
    desired: str


@dataclass(frozen=True)
class RetargetStatePlan:
    writer: DocsCASWriter
    project_id: str
    workspace_id: str
    old_generation: int
    request_digest: str
    documents: tuple[_DocumentPlan, _DocumentPlan]


class RetargetDriveState:
    def __init__(self, ledger: Ledger, attached: AttachedInstallation,
                 provider: ReadProvider, registry: WorkspaceDriveRegistry) -> None:
        self.ledger = ledger
        self.attached = attached
        self.provider = provider
        self.registry = registry

    def preflight(self, command: dict[str, Any], *, generation: int,
                  digest: str) -> RetargetStatePlan:
        project_id, workspace_id = command["project_id"], command["workspace_id"]
        registration = self.registry.get(project_id, workspace_id)
        if (registration.installation_id != self.attached.index.installation_id
                or registration.project_id != project_id
                or registration.workspace_id != workspace_id):
            raise DriveError("RETARGET_DRIVE_REGISTRATION_CHANGED", "Workspace registration changed")
        project_doc, context_doc = registration.state_docs()
        writer = DocsCASWriter(cast(Any, self.provider), self.attached,
                               extra_docs=(project_doc, context_doc))
        with ThreadPoolExecutor(max_workers=2) as pool:
            project_future = pool.submit(writer.read, project_doc.key)
            context_future = pool.submit(writer.read, context_doc.key)
            project_observed = project_future.result()
            context_observed = context_future.result()
        prefix = "PROJECT STATE\n"
        if not project_observed.snapshot.text.startswith(prefix):
            raise DriveError("RETARGET_PROJECT_STATE_INVALID", "Project state is invalid")
        try:
            state = json.loads(project_observed.snapshot.text[len(prefix):].strip())
        except (TypeError, ValueError):
            raise DriveError("RETARGET_PROJECT_STATE_INVALID", "Project state is invalid") from None
        if (not isinstance(state, dict) or state.get("schema_version") != "1.0"
                or state.get("kind") != "project-state/v1"
                or state.get("project_id") != project_id
                or state.get("project_folder_id") != registration.project_folder_id
                or state.get("project_state_doc_id") != project_doc.file_id
                or not isinstance(state.get("workspaces"), list)):
            raise DriveError("RETARGET_PROJECT_STATE_INVALID", "Project state identity changed")
        entries = [entry for entry in state["workspaces"]
                   if isinstance(entry, dict) and entry.get("workspace_id") == workspace_id]
        if len(entries) != 1:
            raise DriveError("RETARGET_PROJECT_STATE_INVALID", "Workspace association is missing")
        entry = entries[0]
        params = command["params"]
        if (entry.get("branch") != params["expected_branch"]
                or entry.get("head") != command["expected_head"]
                or entry.get("source_generation") != generation
                or entry.get("command_doc_id") != registration.command_doc_id
                or entry.get("changeset_doc_id") != registration.changeset_doc_id
                or entry.get("receipt_doc_id") != registration.receipt_doc_id
                or entry.get("latest_result_doc_id") != registration.latest_result_doc_id
                or entry.get("context_doc_id") != context_doc.file_id):
            raise DriveError("RETARGET_PROJECT_STATE_STALE", "Workspace association is stale")
        entry.update(branch=params["target_branch"], head=params["target_commit"],
                     source_generation=generation + 1)
        desired_project = prefix + json.dumps(
            state, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ) + "\n"
        old_context = context_observed.snapshot.text
        head_line = f"Snapshot: `{command['expected_head']}`\n"
        branch_line = f"Branch: `{params['expected_branch']}`\n"
        if (old_context.count(head_line) != 1 or old_context.count(branch_line) != 1
                or f"Project/workspace: `{project_id}` / `{workspace_id}`\n" not in old_context):
            raise DriveError("RETARGET_SOURCE_CONTEXT_STALE", "Source context identity is stale")
        desired_context = old_context.replace(
            head_line, f"Snapshot: `{params['target_commit']}`\n", 1,
        ).replace(branch_line, f"Branch: `{params['target_branch']}`\n", 1)
        return RetargetStatePlan(
            writer, project_id, workspace_id, generation, digest,
            (_DocumentPlan("drive-retarget-project-state", project_observed, desired_project),
             _DocumentPlan("drive-retarget-source-context", context_observed, desired_context)),
        )

    def publish(self, request_id: str, plan: RetargetStatePlan) -> dict[str, str]:
        operation = self.ledger.get(request_id)
        if (operation is None or operation["status"] != "RUNNING"
                or operation["project_id"] != plan.project_id
                or operation["workspace_id"] != plan.workspace_id
                or operation["digest"] != plan.request_digest
                or operation["action"] != "RETARGET_WORKSPACE"):
            raise DriveError("RETARGET_STATE_ADMISSION_MISMATCH", "Retarget state command differs")
        intents: list[tuple[_DocumentPlan, str, str]] = []
        for document in plan.documents:
            desired_sha = hashlib.sha256(document.desired.encode("utf-8")).hexdigest()
            observed = document.observed
            intent = {
                "file_id": observed.snapshot.file_id, "key": observed.key,
                "project_id": plan.project_id, "workspace_id": plan.workspace_id,
                "request_digest": plan.request_digest,
                "base_revision": observed.snapshot.revision_id,
                "base_sha256": observed.snapshot.sha256,
                "desired_sha256": desired_sha,
            }
            effect_id = self.ledger.begin_effect(request_id, document.step, "drive_file", intent)
            intents.append((document, desired_sha, effect_id))
        result: dict[str, str] = {}
        for document, desired_sha, effect_id in intents:
            try:
                plan.writer.replace(document.observed.key, document.desired,
                                    expected=document.observed)
                readback = plan.writer.read(document.observed.key)
            except Exception:  # noqa: BLE001 -- retain both intents for exact reconciliation
                raise DriveError("RETARGET_STATE_WRITE_UNCERTAIN", "State write outcome is uncertain") from None
            if (readback.snapshot.file_id != document.observed.snapshot.file_id
                    or readback.snapshot.sha256 != desired_sha
                    or readback.snapshot.text != document.desired):
                raise DriveError("RETARGET_STATE_WRITE_UNCERTAIN", "State write outcome is uncertain")
            self.ledger.confirm_effect(effect_id, readback.snapshot.file_id)
            result["project_state_sha256" if document.step.endswith("project-state")
                   else "context_sha256"] = desired_sha
        return result
