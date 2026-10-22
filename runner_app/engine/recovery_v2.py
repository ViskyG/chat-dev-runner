"""One startup reconciliation of retained Compose work, never execution replay."""
from __future__ import annotations

import hashlib
import json
import os
import platform
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from runner_app.domain.contracts_v2 import validate_schema
from runner_app.domain.project_v2 import parse_project
from runner_app.engine.intake_v2 import notify_terminal
from runner_app.engine.queue_v2 import TaskOutcome
from runner_app.executors.compose_v2 import ComposeTaskBackend
from runner_app.executors.rootless_v2 import RootlessError
from runner_app.git.results_v2 import ResultError, bundle_digest
from runner_app.storage.paths import ensure_private_directory, open_private_file

if TYPE_CHECKING:
    from runner_app.engine.task_v2 import NativeTaskRunner, TaskProject
    from runner_app.git.source_v2 import SourceSnapshot


def _read_json(path: Path) -> dict[str, Any]:
    descriptor = open_private_file(path)
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ResultError("RECOVERY_EVIDENCE_LIMIT")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ResultError("RECOVERY_EVIDENCE_INVALID")
    return value


def _sealed(runner: NativeTaskRunner, operation: dict[str, Any], project: TaskProject,
            snapshot: SourceSnapshot, events: Sequence[tuple[str, Mapping[str, Any]]]) -> TaskOutcome | None:
    seals = [detail for event, detail in events if event == "RESULT_SEALED"]
    if not seals:
        return None
    if len(seals) != 1:
        raise ResultError("RECOVERY_RESULT_IDENTITY_MISMATCH")
    bundle = project.results_root / operation["runner_id"] / operation["request_id"] / "bundle"
    seal = seals[0]
    if seal.get("bundle_path") != str(bundle):
        raise ResultError("RECOVERY_RESULT_IDENTITY_MISMATCH")
    digest = bundle_digest(bundle, runner_id=operation["runner_id"], request_id=operation["request_id"])
    if digest != seal.get("bundle_sha256"):
        raise ResultError("RESULT_BUNDLE_CHANGED")
    manifest = _read_json(bundle / "result.json")
    validate_schema(manifest, "v2-result")
    if (any(manifest[key] != operation[key] for key in ("runner_id", "project_id", "request_id"))
            or manifest["source"]["workload_sha256"] != snapshot.workload_sha256
            or manifest["source"]["commit"] != snapshot.source_commit
            or manifest["source"]["branch"] != snapshot.branch):
        raise ResultError("RECOVERY_RESULT_IDENTITY_MISMATCH")
    # A submitted ref write may have succeeded before its acknowledgement was lost.
    # Read it back, but do not invent another publication attempt after restart.
    prior_intent = any(event == "RESULT_PUBLISH_INTENT" for event, _ in events)
    publication = runner._publish(operation, project, snapshot.branch, bundle, digest,
                                  reconcile_only=prior_intent)
    status = ("INTERRUPTED" if manifest["execution"]["outcome"] == "UNKNOWN"
              else manifest["operation_status"])
    return TaskOutcome(status, result={
        "manifest": manifest, "bundle_path": str(bundle), "bundle_sha256": digest,
        "branch": snapshot.branch, "delivery": asdict(publication),
        "recovery": {"cleanup": "CONFIRMED"}}, cleanup_verified=True)


def _preserve_unsealed(runner: NativeTaskRunner, operation: dict[str, Any], project: TaskProject) -> None:
    root = project.results_root / operation["runner_id"] / operation["request_id"]
    if not root.exists() and not root.is_symlink():
        return
    ensure_private_directory(root)
    if len(list(root.parent.glob(root.name + ".unsealed-*"))) >= 8:
        raise ResultError("RECOVERY_PARTIAL_CAPTURE_LIMIT")
    retained = root.with_name(root.name + ".unsealed-" + uuid4().hex)
    runner.store.record_event(operation["request_id"], "RECOVERY_PARTIAL_CAPTURE_RETAINED",
                              {"path": str(retained)})
    # This directory has no durable RESULT_SEALED identity. Preserve it intact;
    # only the backend's independently sealed capture supplies the new bundle.
    os.rename(root, retained)


def _recover_one(runner: NativeTaskRunner, operation: dict[str, Any],
                 events: Sequence[tuple[str, Mapping[str, Any]]]) -> TaskOutcome:
    request_id, request = operation["request_id"], operation["payload"]["request"]
    started = [detail for event, detail in events if event == "RUNNING"]
    if not started:
        return TaskOutcome("INTERRUPTED", "RESTART_BEFORE_EXECUTION", {
            "manifest": {"execution": {"outcome": "NOT_RUN", "quality_gate": "NOT_APPLICABLE",
                                         "cleanup": "NOT_NEEDED"}}})
    if len(started) != 1:
        raise ResultError("RECOVERY_EXECUTION_IDENTITY_MISMATCH")
    provenance = started[0]
    project = runner.projects[operation["project_id"]]
    backend = runner.container_backends.get("rootless-compose")
    if not isinstance(backend, ComposeTaskBackend):
        raise ResultError("COMPOSE_RECOVERY_BACKEND_UNAVAILABLE")
    snapshot = runner.resolver.load_existing(operation["project_id"], request_id)
    if (snapshot.source_commit != provenance.get("source_commit")
            or snapshot.workload_sha256 != provenance.get("workload_sha256")
            or (provenance.get("source_manifest_sha256") is not None
                and snapshot.manifest_sha256 != provenance["source_manifest_sha256"])):
        raise ResultError("RECOVERY_EXECUTION_IDENTITY_MISMATCH")
    with (snapshot.path / ".runner/project.json").open("rb") as stream:
        frozen = parse_project(stream.read(262145))
    task = frozen.project["tasks"][request["task"]]
    profile_id = request["execution"].get("profile_id", task["profile_id"])
    task_digest = hashlib.sha256(json.dumps(task, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if (frozen.project["project_id"] != operation["project_id"]
            or task_digest != provenance.get("task_digest")
            or profile_id != task["profile_id"] or profile_id not in project.container_profiles
            or backend.profile(profile_id).revision_sha256 != provenance.get("profile_revision")):
        raise ResultError("RECOVERY_PROFILE_OR_TASK_CHANGED")
    result = backend.recover(snapshot, request_id=request_id, profile_id=profile_id, journal=events)
    if not result.cleanup_verified:
        raise RootlessError(result.error_code or "COMPOSE_RECOVERY_CLEANUP_UNCERTAIN")
    sealed = _sealed(runner, operation, project, snapshot, events)
    if sealed is not None:
        return sealed
    _preserve_unsealed(runner, operation, project)
    host_os = {"Darwin": "macos", "Windows": "windows"}.get(platform.system(), "linux")
    return runner._seal_container(operation, snapshot, backend, profile_id, task, task_digest,
                                   operation["created_at"], host_os, result, recovering=True)


def recover_interrupted(runner: NativeTaskRunner, runner_id: str, session_id: str) -> None:
    """Reconcile only the prior-session request still owning the durable lane."""
    for operation in runner.store.recovery_candidates(runner_id, session_id):
        request = operation["payload"]["request"]
        if (request["action"] != "RUN_TASK"
                or request.get("execution", {}).get("backend_id") != "rootless-compose"):
            continue
        request_id = operation["request_id"]
        try:
            outcome = _recover_one(runner, operation, runner.store.journal(request_id))
        except Exception as error:  # noqa: BLE001 -- preserve uncertain resources; no provider details
            outcome = TaskOutcome("NEEDS_RECONCILIATION",
                                  getattr(error, "code", "COMPOSE_RECOVERY_FAILED"),
                                  operation["result"] or {}, cleanup_verified=False)
        runner.store.finish_recovery(request_id, session_id=session_id, status=outcome.status,
                                     code=outcome.code, result=outcome.result,
                                     cleanup_verified=outcome.cleanup_verified)
        notify_terminal(runner.store, runner.publish_state, request_id)
