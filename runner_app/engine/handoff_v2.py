"""Finite delivery dispatch inside the existing v2 lane; never executes input."""
from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from threading import Event
from typing import Any, TypedDict

from runner_app.config_v2 import HandoffTargetConfig, V2RuntimeConfig
from runner_app.engine.queue_v2 import TaskOutcome
from runner_app.engine.task_v2 import NativeTaskRunner
from runner_app.git.argv import run_git, run_git_bytes
from runner_app.git.state_v2 import publish_state
from runner_app.handoff.contracts import HandoffError, Limits, digest, validate
from runner_app.handoff.delivery import TargetBinding, deliver, reconcile
from runner_app.storage.intake_v2 import IntakeStore
from runner_app.storage.paths import Paths, ensure_private_directory, open_private_file


class HandoffResult(TypedDict):
    handoff_id: str
    project_id: str
    target_id: str
    manifest_sha256: str
    local_delivery_status: str
    files_count: int
    safe_relative_handoff_path: str
    codex_entrypoint: str
    delivery_receipt: str
    inbox_visible_in_git: bool


class HandoffRunner:
    def __init__(self, store: IntakeStore, config: V2RuntimeConfig, paths: Paths, *,
                 provider_factory: Any = None, protected_document_ids: frozenset[str] = frozenset(),
                 native: NativeTaskRunner | None = None) -> None:
        self.store, self.config, self.paths = store, config, paths
        self.provider_factory, self.native = provider_factory, native
        self.protected_document_ids = protected_document_ids
        self.identities: dict[tuple[str, str], tuple[int, int] | None] = {}
        for project in config.projects.values():
            for key, target in project.handoff_targets.items():
                workspace = project.workspaces[target.workspace_id].path
                try:
                    info = workspace.stat(follow_symlinks=False)
                    self.identities[project.project_id, key] = info.st_dev, info.st_ino
                except OSError:
                    self.identities[project.project_id, key] = None

    def handles(self, operation: Mapping[str, Any]) -> bool:
        if operation["action"] == "DELIVER_HANDOFF":
            return True
        target_id = operation["payload"]["request"].get("target_request_id")
        target = self.store.get(target_id) if target_id else None
        return bool(target and target["action"] == "DELIVER_HANDOFF"
                    and target["project_id"] == operation["project_id"]
                    and target["runner_id"] == operation["runner_id"])

    def _binding(self, operation: Mapping[str, Any], *, create: bool = False
                 ) -> tuple[TargetBinding, HandoffTargetConfig, Path, dict[str, Any]]:
        value = validate(operation["payload"]["request"]["handoff"])
        project_id, target_id = value["project_id"], value["target_id"]
        project = self.config.projects.get(project_id)
        target = project.handoff_targets.get(target_id) if project else None
        if project is None or target is None or project_id != operation["project_id"]:
            raise HandoffError("TARGET_NOT_ENROLLED")
        value = validate(value, Limits(**target.limits))
        workspace = project.workspaces[target.workspace_id].path
        if workspace.resolve() != workspace:
            raise HandoffError("TARGET_UNSAFE")
        info = workspace.stat(follow_symlinks=False)
        if self.identities[project_id, target_id] != (info.st_dev, info.st_ino):
            raise HandoffError("TARGET_CHANGED")
        if Path(run_git(workspace, ["rev-parse", "--show-toplevel"]).strip()) != workspace:
            raise HandoffError("TARGET_NOT_WORKSPACE")
        if run_git_bytes(workspace, ["ls-files", "-z", "--", ".runner/handoffs"]):
            raise HandoffError("HANDOFF_INBOX_TRACKED")
        inbox = workspace / ".runner" / "handoffs"
        if create:
            ensure_private_directory(inbox)
        binding = TargetBinding(project_id, target_id, inbox, target.allowed_drive_parents)
        return binding, target, workspace, value

    def _result(self, receipt: Mapping[str, Any], value: Mapping[str, Any], workspace: Path
                ) -> HandoffResult:
        relative = receipt["relative_path"]
        return {"handoff_id": value["handoff_id"], "project_id": value["project_id"],
                "target_id": value["target_id"], "manifest_sha256": receipt["manifest_sha256"],
                "local_delivery_status": "DELIVERED", "files_count": len(value["artifacts"]),
                "safe_relative_handoff_path": relative,
                "codex_entrypoint": relative + "/START_HERE.md",
                "delivery_receipt": relative + "/delivery.json",
                "inbox_visible_in_git": bool(run_git_bytes(workspace, [
                    "--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=normal",
                    "--", ".runner/handoffs"]))}

    def _local_result(self, receipt: Mapping[str, Any], value: Mapping[str, Any],
                      workspace: Path) -> dict[str, Any]:
        descriptor = open_private_file(workspace / receipt["relative_path"] / "CODEX_PROMPT.txt")
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise HandoffError("PROMPT_SIZE_LIMIT")
        return {"handoff": self._result(receipt, value, workspace),
                "codex_prompt": raw.decode("utf-8")}

    def __call__(self, operation: dict[str, Any], cancel: Event) -> TaskOutcome:
        if operation["action"] == "REDELIVER":
            return self._redeliver(operation)
        started = time.monotonic()
        try:
            binding, target, workspace, value = self._binding(operation)
            def remaining() -> float:
                seconds = target.timeout_seconds - (time.monotonic() - started)
                if seconds <= 0:
                    raise HandoffError("TIMED_OUT")
                return seconds

            self.store.record_event(operation["request_id"], "HANDOFF_INTENT", {
                "handoff_id": value["handoff_id"], "target_id": value["target_id"],
                "manifest_sha256": digest(value)})
            receipt = reconcile(value, binding, limits=Limits(**target.limits)) if binding.inbox.exists() else None
            if receipt is None:
                if self.provider_factory is None or self.config.google_config_file is None:
                    return TaskOutcome("FAILED", "HANDOFF_PROVIDER_NOT_CONFIGURED")
                provider = self.provider_factory(
                    self.config.google_config_file, limits=Limits(**target.limits),
                    timeout_seconds=remaining(),
                    request_timeout_seconds=target.request_timeout_seconds, cancelled=cancel.is_set)
                binding, target, workspace, value = self._binding(operation, create=True)
                receipt = deliver(value, provider, binding, request_id=operation["request_id"],
                                  limits=Limits(**target.limits), cancelled=cancel.is_set,
                                  timeout_seconds=remaining())
            self.store.record_event(operation["request_id"], "HANDOFF_LOCAL_COMMITTED", {
                "handoff_id": value["handoff_id"], "manifest_sha256": receipt["manifest_sha256"],
                "safe_relative_handoff_path": receipt["relative_path"]})
            return TaskOutcome("COMPLETED", result=self._local_result(receipt, value, workspace))
        except HandoffError as error:
            return TaskOutcome("CANCELLED" if error.code == "CANCELLED" else "FAILED", error.code)
        except (OSError, ValueError):
            return TaskOutcome("FAILED", "HANDOFF_TARGET_UNAVAILABLE")

    def _recover(self, operation: dict[str, Any]) -> dict[str, Any]:
        if operation["action"] != "DELIVER_HANDOFF" or (operation["result"] or {}).get("handoff"):
            return operation
        if operation["status"] not in {"INTERRUPTED", "SUSPENDED_RESTART", "FAILED"}:
            return operation
        try:
            binding, target, workspace, value = self._binding(operation)
            if not binding.inbox.exists():
                return operation
            receipt = reconcile(value, binding, limits=Limits(**target.limits))
            if receipt is not None:
                self.store.confirm_handoff_recovery(operation["request_id"],
                                                    self._local_result(receipt, value, workspace))
                return self.store.get(operation["request_id"]) or operation
        except (HandoffError, OSError, ValueError):
            return operation
        return operation

    def _redeliver(self, actor: dict[str, Any]) -> TaskOutcome:
        target = self.store.get(actor["payload"]["request"]["target_request_id"])
        if not target or not self.handles(actor):
            return TaskOutcome("FAILED", "TARGET_SCOPE_MISMATCH")
        target = self._recover(target)
        if not (target["result"] or {}).get("handoff"):
            return TaskOutcome("FAILED", "HANDOFF_NOT_DELIVERED")
        return TaskOutcome("COMPLETED", result={"target_request_id": target["request_id"]})

    def _view(self, operation: dict[str, Any]) -> dict[str, Any]:
        result = operation["result"] or {}
        handoff = result.get("handoff")
        status = "DELIVERED" if handoff else operation["status"]
        state: dict[str, Any] = {"protocol": "cdr-handoff-state/1", "schema_version": 1,
                 "runner_id": operation["runner_id"], "project_id": operation["project_id"],
                 "request_id": operation["request_id"], "request_digest": operation["digest"],
                 "action": operation["action"], "status": status, "operation_status": operation["status"],
                 "code": operation["code"], "updated_at": operation["updated_at"], "handoff": handoff}
        if not handoff and operation["action"] == "DELIVER_HANDOFF":
            value = operation["payload"]["request"]["handoff"]
            state["handoff"] = {"handoff_id": value["handoff_id"], "project_id": value["project_id"],
                                "target_id": value["target_id"], "manifest_sha256": digest(value),
                                "local_delivery_status": "NOT_DELIVERED"}
        if handoff:
            state["codex_prompt"] = result.get("codex_prompt")
        return state

    def publish_state(self, request_id: str) -> None:
        actor = self.store.get(request_id)
        if actor is None:
            return
        target_id = actor["payload"]["request"].get("target_request_id")
        target = self.store.get(target_id) if target_id else None
        if target and not self.handles(actor):
            target = None
        target = self._recover(target) if target else None
        operations = [actor] + ([target] if target and target["request_id"] != request_id
                                and target["status"] not in {
                                    "ACK_PENDING", "QUEUED", "STARTING", "RUNNING", "CONTROL",
                                    "CONTROL_RUNNING"} else [])
        for operation in operations:
            state = self._view(operation)
            if operation is actor and target:
                state["target"] = self._view(target)
            source = operation if operation["action"] == "DELIVER_HANDOFF" else target
            if source is None:
                continue
            manifest = source["payload"]["request"]["handoff"]
            project = self.config.projects[operation["project_id"]]
            enrolled = project.handoff_targets.get(manifest["target_id"])
            publication: Mapping[str, Any] = {"status": "UNCONFIRMED", "code": "HANDOFF_RESULT_NOT_CONFIGURED"}
            if (enrolled and enrolled.result_document_id and self.provider_factory is not None
                    and self.config.google_config_file is not None):
                try:
                    if enrolled.result_document_id in self.protected_document_ids:
                        raise HandoffError("HANDOFF_RESULT_OVERLAPS_COMMAND")
                    self.store.record_event(operation["request_id"], "HANDOFF_RECEIPT_INTENT", {
                        "request_id": operation["request_id"], "payload_sha256": digest(state),
                        "document_id": enrolled.result_document_id, "tab_id": enrolled.result_tab_id})
                    publisher = self.provider_factory.publisher(
                        self.config.google_config_file, document_id=enrolled.result_document_id,
                        tab_id=enrolled.result_tab_id, timeout_seconds=enrolled.timeout_seconds,
                        request_timeout_seconds=enrolled.request_timeout_seconds,
                        protected_document_ids=self.protected_document_ids)
                    publication = publisher.publish(state)
                except (HandoffError, OSError, ValueError) as error:
                    publication = {"status": "UNCONFIRMED", "code": "DELIVERY_RESULT_UNCONFIRMED",
                                   "reason": getattr(error, "code", "HANDOFF_RECEIPT_SETUP_FAILED")}
            self.store.record_handoff_publication(operation["request_id"], publication)
            if self.native is not None:
                task_project = self.native.projects[operation["project_id"]]
                branches = {json.loads(origin["coordinates_json"])["branch"]
                            for origin in self.store.request_origins(operation["request_id"])
                            if origin["provider"] == "github"}
                if task_project.transport is not None:
                    for branch in branches:
                        def record(detail: Mapping[str, Any],
                                   request: str = operation["request_id"]) -> None:
                            self.store.record_event(request, "STATE_PUBLISH_INTENT", detail)
                        result = publish_state(task_project.repository, task_project.transport,
                                               branch=branch, state=state, record_intent=record)
                        self.store.record_event(operation["request_id"], "STATE_DELIVERY", result.__dict__)


class V2TaskDispatcher:
    def __init__(self, native: NativeTaskRunner, handoff: HandoffRunner) -> None:
        self.native, self.handoff = native, handoff

    def __call__(self, request: dict[str, Any], cancel: Event) -> TaskOutcome:
        return self.handoff(request, cancel) if self.handoff.handles(request) else self.native(request, cancel)

    def publish_state(self, request_id: str) -> None:
        operation = self.handoff.store.get(request_id)
        if operation and self.handoff.handles(operation):
            self.handoff.publish_state(request_id)
        else:
            self.native.publish_state(request_id)
