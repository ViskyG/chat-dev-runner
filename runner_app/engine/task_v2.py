"""Finite v2 task assembly: current source, native phases, evidence and delivery.

The worker owns admission and the single lane. This handler never polls a model,
creates a repository, changes a user's checkout or invents a missing toolchain.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from runner_app import __version__
from runner_app.config_v2 import NativeProfile
from runner_app.domain.commands import DomainError
from runner_app.domain.contracts_v2 import validate_schema
from runner_app.domain.project_v2 import parse_project
from runner_app.engine.queue_v2 import TaskOutcome
from runner_app.executors.native_v2 import execute_native, shell_argv
from runner_app.executors.rootless_v2 import RootlessError, RootlessTaskBackend
from runner_app.executors.task_plan_v2 import ResolvedStep, TaskPlan, plan_task
from runner_app.executors.tool_versions_v2 import observe_tool
from runner_app.git.argv import GitError
from runner_app.git.results_v2 import (
    ResultError,
    ResultPublication,
    ResultTransport,
    bundle_digest,
    publish_results,
)
from runner_app.git.source_v2 import SourceError, SourceResolver, SourceSnapshot
from runner_app.git.state_v2 import publish_state
from runner_app.outputs.bundle_v2 import FrozenOutputs, collect_bundle_outputs
from runner_app.outputs.parsers import MAX_PARSE_BYTES, ParseResult
from runner_app.outputs.quality_v2 import (
    aggregate_maven_reports,
    evaluate_quality,
    parse_test_report,
)
from runner_app.storage.intake_v2 import IntakeStore
from runner_app.storage.paths import ensure_private_directory


@dataclass(frozen=True)
class TaskProject:
    repository: Path
    results_root: Path
    native_profiles: tuple[str, ...]
    transport: ResultTransport | None = None
    container_profiles: tuple[str, ...] = ()


def _log_id(base: str, declarations: list[dict[str, Any]]) -> str:
    occupied = {item["id"] for item in declarations}
    candidate, suffix = base, 0
    while candidate in occupied:
        suffix += 1
        candidate = f"{base}-cdr-{suffix}"
    return candidate


def _write_json(path: Path, value: Any, *, replace: bool = False) -> None:
    def path_value(item: Any) -> str:
        if isinstance(item, Path):
            return str(item)
        raise TypeError("Unsupported evidence value")

    raw = (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2,
                      default=path_value) + "\n").encode()
    target = path.with_name(f".{path.name}.{uuid4().hex}.tmp") if replace else path
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if replace:
        os.replace(target, path)


def _working_copy(snapshot: SourceSnapshot) -> Path:
    workspace = snapshot.path.parent / "work"
    workspace.mkdir(mode=0o700)
    for item in snapshot.files:
        target = workspace / item.path
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(snapshot.path / item.path, target, follow_symlinks=False)
        # Read-only source remains unchanged; build tools receive a private copy.
        os.chmod(target, 0o700 if item.mode & 0o111 else 0o600)
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        if digest.hexdigest() != item.sha256:
            raise DomainError("INPUT_CHANGED", "Frozen source changed before preparation")
    return workspace


def _reports(outputs: FrozenOutputs, declarations: list[dict[str, Any]]) -> dict[str, ParseResult]:
    reports: dict[str, ParseResult] = {}
    for declaration in declarations:
        output_id = declaration["id"]
        if declaration["kind"] != "test_result" or output_id not in outputs.available_outputs:
            continue
        files = outputs.originals[output_id]
        total = sum(path.stat().st_size for path in files.values())
        if total > 8 * MAX_PARSE_BYTES or any(path.stat().st_size > MAX_PARSE_BYTES for path in files.values()):
            reports[output_id] = ParseResult("test_result", declaration["format"], "UNPARSED", {},
                                            ("PARSER_BYTE_LIMIT",))
        elif declaration["format"] in {"surefire-xml", "failsafe-xml"}:
            reports[output_id] = aggregate_maven_reports(
                {name: path.read_bytes() for name, path in files.items()},
                phase="surefire" if declaration["format"] == "surefire-xml" else "failsafe",
            )
        elif len(files) == 1:
            reports[output_id] = parse_test_report(next(iter(files.values())).read_bytes(),
                                                   declaration["format"])
        else:
            reports[output_id] = ParseResult("test_result", declaration["format"], "UNPARSED", {},
                                            ("AMBIGUOUS_REPORT_SET",))
    return reports


class NativeTaskRunner:
    def __init__(self, store: IntakeStore, resolver: SourceResolver,
                 projects: Mapping[str, TaskProject], profiles: Mapping[str, NativeProfile], *,
                 container_backends: Mapping[str, RootlessTaskBackend] | None = None) -> None:
        self.store, self.resolver = store, resolver
        self.projects, self.profiles = dict(projects), dict(profiles)
        self.container_backends = dict(container_backends or {})

    def _state_view(self, operation: dict[str, Any]) -> dict[str, Any]:
        result = operation["result"] or {}
        started = self.store.execution_started(operation["request_id"])
        uncertain = operation["status"] in {"STARTING", "RUNNING", "NEEDS_RECONCILIATION", "INTERRUPTED"}
        execution = result.get("manifest", {}).get("execution") or {
            "outcome": "UNKNOWN" if started or uncertain else "NOT_RUN",
            "quality_gate": "INSUFFICIENT" if started else "NOT_APPLICABLE",
            "cleanup": "UNCERTAIN" if uncertain else "CONFIRMED",
        }
        return {"schema_version": 1, "runner_id": operation["runner_id"],
                "project_id": operation["project_id"], "request_id": operation["request_id"],
                "request_digest": operation["digest"], "action": operation["action"],
                "status": operation["status"], "code": operation["code"],
                "updated_at": operation["updated_at"], "execution": execution,
                "source": result.get("manifest", {}).get("source")
                or operation["payload"]["request"].get("source"),
                "delivery": result.get("delivery"),
                "result": {key: result[key] for key in (
                    "bundle_path", "bundle_sha256", "branch", "phase_evidence_path",
                    "target_request_id", "status", "sequence") if key in result}}

    def publish_state(self, request_id: str) -> None:
        """Export terminal/control state outside immutable bundles and mailbox intent."""
        actor = self.store.get(request_id)
        if actor is None:
            return
        operations = [actor]
        target_id = actor["payload"]["request"].get("target_request_id")
        target = self.store.get(target_id) if target_id else None
        if (target is not None and target["runner_id"] == actor["runner_id"]
                and target["project_id"] == actor["project_id"]):
            if target["request_id"] != request_id:
                operations.append(target)
        else:
            target = None
        for operation in operations:
            project = self.projects[operation["project_id"]]
            state = self._state_view(operation)
            if operation is actor and target is not None:
                state["target"] = self._state_view(target)
                state["updated_at"] = max(state["updated_at"], target["updated_at"])
            # STATUS during a run must not pre-create its future sealed result directory.
            state_dir = project.results_root / "state" / operation["runner_id"]
            ensure_private_directory(state_dir)
            _write_json(state_dir / f"{operation['request_id']}.json", state, replace=True)
            branches = {json.loads(origin["coordinates_json"])["branch"]
                        for origin in self.store.request_origins(operation["request_id"])
                        if origin["provider"] == "github"}
            if not branches:
                branch = operation["payload"]["request"].get("source", {}).get("branch")
                if branch:
                    branches.add(branch)
            if project.transport is None:
                self.store.record_event(operation["request_id"], "REMOTE_STATE_LOCAL_ONLY",
                                        {"code": "GITHUB_DELIVERY_NOT_CONFIGURED"})
                continue
            for branch in sorted(branches):
                def record_intent(detail: Mapping[str, Any],
                                  request_id: str = operation["request_id"]) -> None:
                    self.store.record_event(request_id, "STATE_PUBLISH_INTENT", detail)

                receipt = publish_state(
                    project.repository, project.transport, branch=branch, state=state,
                    record_intent=record_intent,
                )
                self.store.record_event(operation["request_id"], "STATE_DELIVERY", asdict(receipt))

    def __call__(self, request: dict[str, Any], cancel: threading.Event) -> TaskOutcome:
        operation = request
        request = operation["payload"]["request"]
        if request["action"] == "REDELIVER":
            try:
                return self._redeliver(operation)
            except (OSError, ValueError, ResultError) as error:
                return TaskOutcome("REJECTED", getattr(error, "code", "RESULT_NOT_AVAILABLE"))
        if request["action"] not in {"RUN_TASK", "SHELL"}:
            return TaskOutcome("REJECTED", "ACTION_NOT_SUPPORTED")
        host_os = {"Darwin": "macos", "Windows": "windows"}.get(platform.system(), "linux")
        if request["execution"]["mode"] == "native" and host_os != request["execution"]["expected_os"]:
            return TaskOutcome("REJECTED", "EXECUTION_OS_MISMATCH")
        try:
            if request["execution"]["mode"] == "container":
                return self._execute_container(operation, cancel, host_os)
            return self._execute(operation, cancel, host_os)
        except RootlessError as error:
            return TaskOutcome("FAILED", error.code, cleanup_verified=error.cleanup_verified)
        except (OSError, ValueError, GitError, SourceError) as error:
            # These failures happen during planning, or after every spawned
            # phase has returned cleanup proof. Unexpected exceptions propagate
            # to QueueWorker, which conservatively retains a RUNNING lane.
            current = self.store.get(operation["request_id"])
            if current and current["status"] == "RUNNING":
                raise
            return TaskOutcome("FAILED", getattr(error, "code", "TASK_INPUT_UNAVAILABLE"))

    def _execute_container(self, operation: dict[str, Any], cancel: threading.Event,
                           host_os: str) -> TaskOutcome:
        request_id, project_id = operation["request_id"], operation["project_id"]
        request = operation["payload"]["request"]
        project = self.projects[project_id]
        backend = self.container_backends.get(request["execution"]["backend_id"])
        if backend is None or request["action"] != "RUN_TASK":
            return TaskOutcome("REJECTED", "CONTAINER_TASK_ROUTE_NOT_CONFIGURED")
        snapshot = self.resolver.resolve_current(project_id, request_id, request["source"])
        with (snapshot.path / ".runner/project.json").open("rb") as manifest_stream:
            frozen = parse_project(manifest_stream.read(262145))
        if frozen.project["project_id"] != project_id:
            raise DomainError("PROJECT_SCOPE_MISMATCH", "Project manifest identity differs")
        task = frozen.project["tasks"].get(request["task"])
        if task is None:
            raise DomainError("TASK_NOT_FOUND", "Task is absent from current source")
        profile_id = request["execution"].get("profile_id", task["profile_id"])
        if profile_id != task["profile_id"] or profile_id not in project.container_profiles:
            return TaskOutcome("REJECTED", "PROFILE_NOT_ENROLLED")
        profile = backend.profile(profile_id)
        if request["execution"]["expected_os"] != profile.platform.split("/")[0]:
            return TaskOutcome("REJECTED", "EXECUTION_OS_MISMATCH")
        if (len(task["steps"]) != 1 or task["steps"][0]["phase"] != "test"
                or task["steps"][0]["cwd"] != "."
                or tuple(task["steps"][0]["argv"]) != profile.argv
                or task["steps"][0]["timeout_seconds"] != profile.timeout_seconds
                or request["execution"]["timeout_seconds"] < backend.timeout_seconds(profile_id)):
            return TaskOutcome("REJECTED", "FIXED_CONTAINER_TASK_REQUIRED")
        specifications = {item.id: asdict(item) for item in profile.collect}
        declarations: list[dict[str, Any]] = []
        for item in task["outputs"]:
            approved = specifications.get(item["id"])
            if (approved is None or item["source_root"] != "out"
                    or any(item[key] != approved[key] for key in ("path", "kind", "format", "required"))):
                return TaskOutcome("REJECTED", "FIXED_CONTAINER_OUTPUTS_REQUIRED")
            declarations.append(dict(item))
        if any(spec.get("required") and name not in {item["id"] for item in declarations}
               for name, spec in specifications.items()):
            return TaskOutcome("REJECTED", "FIXED_CONTAINER_OUTPUTS_REQUIRED")
        if cancel.is_set():
            return TaskOutcome("CANCELLED", "CANCELLED_BEFORE_SPAWN")
        task_digest = hashlib.sha256(json.dumps(task, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.store.mark_running(request_id, {"source_commit": snapshot.source_commit,
                                            "workload_sha256": snapshot.workload_sha256,
                                            "task_digest": task_digest, "mode": "container",
                                            "profile_revision": profile.revision_sha256})
        started = datetime.now(timezone.utc).isoformat()
        result = backend.run(snapshot, request_id=request_id, profile_id=profile_id, cancel=cancel)
        output_descriptors = result.provenance.get("outputs", [])
        if not isinstance(output_descriptors, list):
            output_descriptors = []
        collected = {str(item["output_id"]): item for item in output_descriptors
                     if isinstance(item, dict)}
        for item in declarations:
            if item["id"] in collected:
                item["path"] = Path(collected[item["id"]]["relative_path"]).name
        roots = {"out": result.output_root, "workspace": snapshot.path}
        for name, path in (("stdout", result.stdout_path), ("stderr", result.stderr_path)):
            if path is not None and path.is_file():
                roots[name] = path.parent
                declarations.append({"id": _log_id(f"container-{name}", declarations),
                                     "kind": "console", "format": "raw-bytes",
                                     "source_root": name, "path": path.name, "required": False,
                                     "publish": "git_if_fits"})
        phase = {"step": task["steps"][0]["id"], "phase": "test", "argv": list(result.argv),
                 "outcome": result.outcome, "exit_code": result.exit_code,
                 "cleanup_verified": result.cleanup_verified, "duration_seconds": result.duration_seconds}
        try:
            return self._seal(operation, project, snapshot, snapshot.path, result.output_root, None,
                              profile_id, None, task_digest, backend.timeout_seconds(profile_id), host_os,
                              started, [phase], declarations, roots, task["quality_gate"],
                              result.cleanup_verified, result.outcome, result.exit_code,
                              backend_details={"mode": "container", "execution_os": result.execution_os,
                                               "execution_arch": result.execution_arch,
                                               "image": result.image, "rootless": dict(result.provenance)})
        except (OSError, ValueError, ResultError) as error:
            return TaskOutcome("FAILED", getattr(error, "code", "LOCAL_PERSISTENCE_FAILED"),
                               {"phase_evidence_path": str(result.output_root.parent)}, result.cleanup_verified)

    def _execute(self, operation: dict[str, Any], cancel: threading.Event, host_os: str) -> TaskOutcome:
        request_id, project_id = operation["request_id"], operation["project_id"]
        request = operation["payload"]["request"]
        project = self.projects[project_id]
        snapshot = self.resolver.resolve_current(project_id, request_id, request["source"])
        workspace = _working_copy(snapshot)
        out = snapshot.path.parent / "out"
        out.mkdir(mode=0o700)
        plan: TaskPlan | None = None
        if request["action"] == "RUN_TASK":
            manifest = workspace / ".runner/project.json"
            with manifest.open("rb") as manifest_stream:
                frozen = parse_project(manifest_stream.read(262145))
            if frozen.project["project_id"] != project_id:
                raise DomainError("PROJECT_SCOPE_MISMATCH", "Project manifest identity differs")
            task = frozen.project["tasks"].get(request["task"])
            if task is None:
                raise DomainError("TASK_NOT_FOUND", "Task is absent from current source")
            profile_id = request["execution"].get("profile_id", task["profile_id"])
            if profile_id != task["profile_id"]:
                raise DomainError("PROFILE_MISMATCH", "Task and requested profile differ")
        else:
            selected = request["execution"].get("profile_id")
            profile_id = selected or (project.native_profiles[0] if len(project.native_profiles) == 1 else "")
        if profile_id not in project.native_profiles or profile_id not in self.profiles:
            raise DomainError("PROFILE_NOT_ENROLLED", "Native profile requires owner enrollment")
        profile = self.profiles[profile_id]
        budget = min(profile.timeout_seconds, request["execution"]["timeout_seconds"])
        if request["action"] == "RUN_TASK":
            plan = plan_task(frozen, request["task"], workspace=workspace, out=out,
                             tool_paths=profile.tools, timeout_budget_seconds=budget,
                             run_id=request_id, source_sha=snapshot.source_commit)
            steps = plan.steps
            declarations = [{"id": item.id, "path": str(item.path.relative_to(
                out if item.source_root == "out" else workspace)), "source_root": item.source_root,
                "kind": item.kind, "format": item.format, "required": item.required,
                "publish": item.publish} for item in plan.outputs]
            gate: Mapping[str, Any] = plan.quality_gate
            task_digest = plan.task_digest
            env = {**profile.environment, **plan.environment}
            if "PATH" not in profile.environment:
                directories = dict.fromkeys(str(Path(tool.argv_prefix[0]).parent) for tool in plan.tools)
                env["PATH"] = os.pathsep.join([*directories, os.defpath])
        else:
            shell = request["shell"]
            if shell["dialect"] not in profile.shell_dialects:
                raise DomainError("SHELL_NOT_ENROLLED", "Shell dialect requires owner enrollment")
            argv = tuple(shell_argv(shell["dialect"], shell["script"]))
            steps = (ResolvedStep("shell", "run", argv, workspace, budget),)
            declarations = []
            gate = {"test_required": False, "minimum_executed": 0, "required_outputs": []}
            task_digest = hashlib.sha256(json.dumps(shell, sort_keys=True).encode()).hexdigest()
            env = dict(profile.environment)
        env.update({"CDR_RUN_ID": request_id, "CDR_SOURCE_SHA": snapshot.source_commit})
        if cancel.is_set():
            return TaskOutcome("CANCELLED", "CANCELLED_BEFORE_SPAWN")
        self.store.mark_running(request_id, {"source_commit": snapshot.source_commit,
                                            "workload_sha256": snapshot.workload_sha256,
                                            "task_digest": task_digest, "mode": "native"})
        started = datetime.now(timezone.utc).isoformat()
        deadline = time.monotonic() + budget
        phase_results: list[dict[str, Any]] = []
        roots = {"workspace": workspace, "out": out}
        cleanup = True
        outcome, exit_code = "NOT_RUN", None
        observed_tools: set[str] = set()
        for index, step in enumerate(steps):
            capture = snapshot.path.parent / f"capture-{index}"
            for tool in plan.tools if plan else ():
                if tool.id in observed_tools:
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                observation = observe_tool(
                    tool, cwd=step.cwd, env=env, capture_dir=snapshot.path.parent / f"version-{tool.id}",
                    artifact_dir=out, timeout_seconds=min(2.0, remaining), cancel=cancel,
                    record_intent=lambda detail: self.store.record_event(request_id, "TOOL_PROBE_INTENT", detail),
                )
                if observation.state == "NOT_READY":
                    continue
                observed_tools.add(tool.id)
                observed = asdict(observation)
                process = observation.execution
                if process is not None:
                    observed["execution"]["stdout_path"] = str(process.stdout_path)
                    observed["execution"]["stderr_path"] = str(process.stderr_path)
                    cleanup = cleanup and process.cleanup_verified
                phase_results.append({"step": f"version-{tool.id}", "phase": "probe",
                                      "outcome": process.outcome if process else "NOT_RUN",
                                      "tool_observation": observed})
                self.store.record_event(request_id, "TOOL_OBSERVED", observed)
                if not cleanup:
                    break
            if not cleanup:
                outcome = "UNKNOWN"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = "TIMED_OUT"
                break
            self.store.record_event(request_id, "SPAWN_INTENT", {
                "step": step.id, "phase": step.phase, "argv": list(step.argv),
                "task_digest": task_digest,
            })
            result = execute_native(list(step.argv), cwd=step.cwd, env=env, output_dir=capture,
                                    artifact_dir=out, timeout_seconds=min(step.timeout_seconds, remaining),
                                    cancel=cancel, max_log_bytes=profile.max_log_bytes)
            cleanup = cleanup and result.cleanup_verified
            outcome, exit_code = result.outcome, result.exit_code
            detail = asdict(result)
            detail.update({"stdout_path": str(result.stdout_path), "stderr_path": str(result.stderr_path),
                           "step": step.id, "phase": step.phase, "argv": list(step.argv),
                           "cwd_relative": str(step.cwd.relative_to(workspace))})
            phase_results.append(detail)
            self.store.record_event(request_id, "PHASE_FINISHED", detail)
            if result.logs_persisted:
                roots[f"capture-{index}"] = capture
                for stream in ("stdout", "stderr"):
                    declarations.append({"id": _log_id(f"phase-{index}-{stream}", declarations),
                                         "kind": "console",
                                         "format": "raw-bytes", "source_root": f"capture-{index}",
                                         "path": f"{stream}.bin", "required": False, "publish": "git_if_fits"})
            if not cleanup or outcome != "PASSED":
                break
        try:
            return self._seal(operation, project, snapshot, workspace, out, plan, profile_id,
                              profile, task_digest, budget, host_os, started, phase_results,
                              declarations, roots, gate, cleanup, outcome, exit_code)
        except (OSError, ValueError, ResultError) as error:
            self.store.record_event(request_id, "RESULT_PERSISTENCE_FAILED", {
                "code": getattr(error, "code", "LOCAL_PERSISTENCE_FAILED"),
                "cleanup_verified": cleanup, "phase_evidence_path": str(snapshot.path.parent),
            })
            return TaskOutcome("FAILED", "LOCAL_PERSISTENCE_FAILED",
                               {"phase_evidence_path": str(snapshot.path.parent)}, cleanup)

    def _seal(self, operation: dict[str, Any], project: TaskProject, snapshot: SourceSnapshot,
              workspace: Path, out: Path, plan: TaskPlan | None, profile_id: str,
              profile: NativeProfile | None, task_digest: str, budget: int, host_os: str, started: str,
              phase_results: list[dict[str, Any]], declarations: list[dict[str, Any]],
              roots: Mapping[str, Path], gate: Mapping[str, Any], cleanup: bool,
              outcome: str, exit_code: int | None, *,
              backend_details: Mapping[str, Any] | None = None) -> TaskOutcome:
        request_id, project_id = operation["request_id"], operation["project_id"]
        runner_id, request = operation["runner_id"], operation["payload"]["request"]
        # Collection remains useful after a project failure or timeout. If a
        # process is unaccounted for, snapshots are diagnostic, never complete.
        result_dir = project.results_root / runner_id / request_id
        frozen_outputs = collect_bundle_outputs(roots, declarations, result_dir)
        reports = _reports(frozen_outputs, declarations)
        quality = evaluate_quality(gate, reports, available_outputs=frozen_outputs.available_outputs,
                                   invalid_outputs=frozen_outputs.invalid_outputs,
                                   execution_outcomes=[phase["outcome"] for phase in phase_results
                                                       if phase["phase"] == "test"])
        bundle = result_dir / "bundle"
        _write_json(bundle / "phases.json", phase_results)
        _write_json(bundle / "tests.json", {key: asdict(value) for key, value in reports.items()})
        reproducibility = "unknown"
        observations = [phase["tool_observation"] for phase in phase_results if phase["phase"] == "probe"]
        versions = {tool.id: next((item["version"] for item in observations if item["id"] == tool.id), None)
                    for tool in plan.tools} if plan else {}
        backend_details = dict(backend_details or {})
        mode = backend_details.get("mode", "native")
        execution_os = backend_details.get("execution_os", host_os)
        execution_arch = backend_details.get("execution_arch", platform.machine() or "unknown")
        environment_details = {
            "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
            "host_os": host_os, "host_release": platform.release(), "host_arch": platform.machine(),
            "execution_os": execution_os, "execution_arch": execution_arch, "mode": mode,
            "runner_version": __version__, "profile_id": profile_id, "task_digest": task_digest,
            "tools": [asdict(tool) for tool in plan.tools] if plan else [],
            "tool_observations": observations,
            "locks": dict(plan.lock_hashes) if plan else {}, "reproducibility": reproducibility,
            "budgets": {"timeout_seconds": budget,
                        "saved_log_bytes_per_phase": profile.max_log_bytes if profile else None,
                        "memory_enforced": False if mode == "native" else None,
                        "disk_enforced": False if mode == "native" else None},
            "backend": backend_details,
        }
        # Tool paths are diagnostic explicit configuration, never the ambient environment.
        _write_json(bundle / "environment.json", environment_details)
        shutil.copyfile(snapshot.manifest_path, bundle / "source.json")
        os.chmod(bundle / "source.json", 0o600)
        manifest_value = {
            "protocol": "cdr-result/2", "evidence_level": "OBSERVED_RUN", "request_id": request_id,
            "runner_id": runner_id, "project_id": project_id,
            "source": {"kind": snapshot.kind, "branch": snapshot.branch, "commit": snapshot.source_commit,
                       "workload_sha256": snapshot.workload_sha256, "dirty": snapshot.dirty,
                       "overlay_sha256": snapshot.overlay_sha256},
            "operation_status": "CANCELLED" if outcome == "CANCELLED" else "COMPLETED",
            "execution": {"outcome": outcome if cleanup else "UNKNOWN", "exit_code": exit_code,
                          "quality_gate": quality.quality_gate,
                          "cleanup": "CONFIRMED" if cleanup else "UNCERTAIN"},
            "environment": {"host_os": host_os, "host_arch": platform.machine() or "unknown",
                            "execution_os": execution_os, "execution_arch": execution_arch,
                            "mode": mode, "runner_version": __version__, "profile_id": profile_id,
                            "tool_versions": versions, "reproducibility": reproducibility,
                            "details_path": "environment.json"},
            "delivery": {"status": "LOCAL_ONLY", "branch": snapshot.branch,
                         "local_bundle_path": str(bundle), "reason": "PUBLICATION_PENDING"},
            "artifacts": list(frozen_outputs.artifacts),
            "warnings": list(frozen_outputs.findings) + list(quality.findings)
                        + ([] if cleanup else ["CLEANUP_UNCERTAIN: artifacts may still change at origin"]),
        }
        validate_schema(manifest_value, "v2-result")
        _write_json(bundle / "result.json", manifest_value)
        _write_json(bundle / "artifacts.json", manifest_value["artifacts"])
        summary = (f"# {project_id}: {request.get('task', 'SHELL')}\n\n"
                   f"Branch: `{snapshot.branch}`\nSource: `{snapshot.source_commit}`\n\n"
                   f"Execution: **{outcome}**; quality: **{quality.quality_gate}**; "
                   f"cleanup: **{'CONFIRMED' if cleanup else 'UNCERTAIN'}**.\n\n"
                   "Original reports and raw logs are indexed in artifacts.json. "
                   "result.json and artifacts.json describe sealing-time availability. "
                   f"Read `.runner/state/{runner_id}/{request_id}.json` on the command branch "
                   "for terminal status, errors and final delivery proof.\n")
        (bundle / "summary.md").write_text(summary, encoding="utf-8")
        os.chmod(bundle / "summary.md", 0o600)
        digest = bundle_digest(bundle, runner_id=runner_id, request_id=request_id)
        self.store.record_event(request_id, "RESULT_SEALED", {"bundle_path": str(bundle),
                                                            "bundle_sha256": digest})
        publication = self._publish(operation, project, snapshot.branch, bundle, digest)
        state = {"manifest": manifest_value, "bundle_path": str(bundle), "bundle_sha256": digest,
                 "branch": snapshot.branch, "delivery": asdict(publication)}
        status = "CANCELLED" if outcome == "CANCELLED" else "COMPLETED"
        return TaskOutcome(status, "CLEANUP_UNCERTAIN" if not cleanup else None, state, cleanup)

    def _publish(self, operation: dict[str, Any], project: TaskProject, branch: str,
                 bundle: Path, digest: str, *, original_request_id: str | None = None) -> ResultPublication:
        runner_id = operation["runner_id"]
        request_id = original_request_id or operation["request_id"]
        if project.transport is None:
            publication = ResultPublication("LOCAL_ONLY", "GITHUB_DELIVERY_NOT_CONFIGURED", None,
                                            str(bundle), True, digest)
        else:
            publication = publish_results(
                project.repository, project.transport, tested_branch=branch, runner_id=runner_id,
                request_id=request_id, result_dir=bundle, expected_bundle_sha256=digest,
                record_intent=lambda intent: self.store.record_event(
                    operation["request_id"], "RESULT_PUBLISH_INTENT", asdict(intent)),
            )
        _write_json(bundle.parent / "delivery.json", asdict(publication), replace=True)
        self.store.record_event(operation["request_id"], "RESULT_DELIVERY", asdict(publication))
        return publication

    def _redeliver(self, operation: dict[str, Any]) -> TaskOutcome:
        target = self.store.get(operation["payload"]["request"]["target_request_id"])
        if (target is None or target["runner_id"] != operation["runner_id"]
                or target["project_id"] != operation["project_id"]
                or not target["result"] or not target["result"].get("bundle_sha256")):
            return TaskOutcome("REJECTED", "RESULT_NOT_AVAILABLE")
        result = target["result"]
        project = self.projects[operation["project_id"]]
        expected = project.results_root / operation["runner_id"] / target["request_id"] / "bundle"
        if result["bundle_path"] != str(expected):
            return TaskOutcome("REJECTED", "RESULT_IDENTITY_MISMATCH")
        digest = bundle_digest(expected, runner_id=operation["runner_id"], request_id=target["request_id"])
        if digest != result["bundle_sha256"]:
            return TaskOutcome("REJECTED", "RESULT_BUNDLE_CHANGED")
        publication = self._publish(operation, project, result["branch"], expected, digest,
                                    original_request_id=target["request_id"])
        self.store.record_redelivery(target["request_id"], actor_id=operation["request_id"],
                                     delivery=asdict(publication))
        return TaskOutcome("COMPLETED", result={"target_request_id": target["request_id"],
                                               "delivery": asdict(publication)})
