"""Finite admitted-run orchestration from immutable Git source to local evidence.

This service is a trusted Engine-handler building block, never a remote endpoint.
It does not finalize the ledger operation: the Engine handler uses the returned
operation status to finish once. Project test failures are completed operations;
capture/collection faults remain separate from the execution outcome.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Protocol
from uuid import UUID, uuid4

from runner_app.domain.commands import DomainError, validate_shape
from runner_app.git.materialize import materialize_snapshot
from runner_app.git.registry import SourceRegistry
from runner_app.outputs.capture import CaptureResult
from runner_app.outputs.collection import (
    CollectionResult,
    OutputError,
    OutputSpec,
    collect_outputs,
)
from runner_app.storage.ledger import Ledger
from runner_app.storage.paths import (
    UnsafePathError,
    _absolute,
    ensure_private_directory,
    open_private_file,
)
from runner_app.storage.run_evidence import RunEvidenceStore

from .broker import BrokerResult, SnapshotInput, _sha256_argv
from .profiles import ApprovedProfile, ProfileRegistry

_CAPTURE_NAMES = {
    "stdout-raw": "stdout.bin",
    "stdout-view": "stdout.txt",
    "stderr-raw": "stderr.bin",
    "stderr-view": "stderr.txt",
    "capture-events": "events.jsonl",
    "capture-metadata": "capture.json",
}
_MAX_FROZEN_FILE_BYTES = 64 * 1024 * 1024
_MAX_COLLECTION_METADATA_BYTES = 1024 * 1024


class RunServiceError(ValueError):
    """Stable orchestration refusal without source bytes, private paths or credentials."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class RunCompletion:
    manifest: dict[str, Any]
    findings: tuple[str, ...]
    parse_summaries: dict[str, dict[str, Any]]
    operation_status: str
    operation_code: str | None

    @property
    def run_id(self) -> str:
        return str(self.manifest["run_id"])


def _uuid(value: str | None) -> str:
    if value is None:
        return str(uuid4())
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise RunServiceError("INVALID_RUN_ID", "A canonical run UUID is required") from None
    return value


def _private_root(value: Path) -> Path:
    try:
        root = _absolute(Path(value))
        ensure_private_directory(root)
    except (OSError, UnsafePathError):
        raise RunServiceError("UNSAFE_RUN_ROOT", "Run storage root is not private") from None
    return root


def _freeze_one(source: Path, destination: Path, *, expected_size: int,
                expected_sha256: str) -> None:
    try:
        source_fd = open_private_file(source)
    except (OSError, UnsafePathError):
        raise RunServiceError("UNSAFE_CAPTURE", "Broker capture file is not private") from None
    destination_fd = -1
    try:
        before = os.fstat(source_fd)
        if before.st_size > _MAX_FROZEN_FILE_BYTES:
            raise RunServiceError("CAPTURE_TOO_LARGE", "Capture file exceeds local evidence limit")
        destination_fd = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
        )
        os.fchmod(destination_fd, 0o600)
        digest = hashlib.sha256()
        count = 0
        while chunk := os.read(source_fd, 65_536):
            count += len(chunk)
            if count > _MAX_FROZEN_FILE_BYTES:
                raise RunServiceError("CAPTURE_TOO_LARGE", "Capture file exceeds local evidence limit")
            digest.update(chunk)
            offset = 0
            while offset < len(chunk):
                written = os.write(destination_fd, chunk[offset:])
                if written <= 0:
                    raise RunServiceError("CAPTURE_FREEZE_FAILED", "Capture freeze made no progress")
                offset += written
        after = os.fstat(source_fd)
        if (count != expected_size or digest.hexdigest() != expected_sha256
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                    before.st_ctime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                 after.st_ctime_ns)):
            raise RunServiceError("CAPTURE_CHANGED", "Broker capture differs from reported bytes")
        os.fsync(destination_fd)
    except BaseException:
        if destination_fd >= 0:
            os.close(destination_fd)
            destination_fd = -1
            destination.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)


def _freeze_capture(capture: CaptureResult, run_root: Path,
                    evidence_dir: Path) -> tuple[tuple[dict[str, Any], ...], dict[str, Path]]:
    if {stream.name for stream in capture.streams} != {"stdout", "stderr"}:
        raise RunServiceError("CAPTURE_INCOMPLETE", "Both process streams are required")
    original_files = capture.files()
    if not {"stdout-raw", "stderr-raw"} <= set(original_files):
        raise RunServiceError("CAPTURE_INCOMPLETE", "Both raw process streams are required")
    expected_dir = run_root / "capture"
    for output_id, source in original_files.items():
        if (output_id not in _CAPTURE_NAMES
                or Path(source) != expected_dir / _CAPTURE_NAMES[output_id]):
            raise RunServiceError("UNSAFE_CAPTURE", "Capture file is outside its run slot")
    descriptors = capture.outputs(relative_prefix="capture")
    if {item["output_id"] for item in descriptors} != set(original_files):
        raise RunServiceError("CAPTURE_INCOMPLETE", "Capture descriptors and files differ")
    frozen_dir = evidence_dir / "capture"
    ensure_private_directory(frozen_dir)
    frozen: dict[str, Path] = {}
    for item in descriptors:
        output_id = item["output_id"]
        destination = frozen_dir / _CAPTURE_NAMES[output_id]
        _freeze_one(
            Path(original_files[output_id]), destination,
            expected_size=item["size_bytes"], expected_sha256=item["sha256"],
        )
        frozen[output_id] = destination
    return descriptors, frozen


def _specs(profile: ApprovedProfile) -> tuple[OutputSpec, ...]:
    specs: list[OutputSpec] = []
    for item in profile.collect:
        if item.id == "runner-collection" or item.id in _CAPTURE_NAMES:
            raise RunServiceError("PROFILE_OUTPUT_ID_RESERVED", "Profile output ID is reserved")
        specs.append(OutputSpec(
            output_id=item.id, kind=item.kind, path=item.path, format=item.format,
            required=item.required,
            publication="redacted_upload" if item.upload_policy == "sanitized_only"
                        else "local_only",
        ))
    return tuple(specs)


def _collection_metadata(result: CollectionResult, capture: CaptureResult,
                         path: Path, *, complete: bool, findings: tuple[str, ...]) -> dict[str, Any]:
    parsed = {
        output_id: {
            "status": value.status, "kind": value.kind, "format": value.format,
            "summary": value.summary, "findings": list(value.findings),
        }
        for output_id, value in result.parses.items()
    }
    value = {
        "schema_version": "1.0",
        "collection_complete": complete,
        "capture_fault": capture.capture_fault,
        "capture_events": {
            "observed": capture.observed_events, "retained": capture.retained_events,
        },
        "findings": list(findings),
        "parses": parsed,
        "provenance": result.provenance,
        "capture": {
            stream.name: {
                "observed_bytes": stream.observed_bytes,
                "retained_bytes": stream.retained_bytes,
                "complete": stream.complete,
                "truncated_reason": stream.truncated_reason,
            }
            for stream in capture.streams
        },
    }
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                     allow_nan=False).encode("utf-8")
    if len(raw) > _MAX_COLLECTION_METADATA_BYTES:
        raise RunServiceError("COLLECTION_METADATA_TOO_LARGE", "Collection metadata exceeds local limit")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise RunServiceError("COLLECTION_METADATA_FAILED", "Metadata write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return {
        "output_id": "runner-collection", "kind": "runner_log",
        "format": "collection-metadata/v1", "media_type": "application/json",
        "relative_path": path.name, "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(), "complete": True,
        "truncated_reason": None, "publication": "local_only",
    }


class ContainedRunBroker(Protocol):
    """Trusted contained-run boundary; a fake is allowed only in offline tests."""

    def run(self, profile: ApprovedProfile, snapshot: SnapshotInput, run_root: Path,
            *, run_id: str, writable_volume: Path | None = None,
            cancel: Event | None = None) -> BrokerResult: ...


class RunService:
    """Run exactly one already-admitted VERIFY/DELIVER_VERIFY_PUBLISH operation."""

    def __init__(self, ledger: Ledger, registry: SourceRegistry, profiles: ProfileRegistry,
                 broker: ContainedRunBroker, evidence: RunEvidenceStore, *,
                 sources_root: Path, runs_root: Path) -> None:
        if not (ledger.database.path == registry.database.path == evidence.database.path):
            raise ValueError("Run components must share one database")
        self.ledger = ledger
        self.registry = registry
        self.profiles = profiles
        self.broker = broker
        self.evidence = evidence
        self.sources_root = _private_root(sources_root)
        self.runs_root = _private_root(runs_root)
        roots = (self.sources_root, self.runs_root, self.evidence.evidence_root)
        if any(left == right or left.is_relative_to(right) or right.is_relative_to(left)
               for position, left in enumerate(roots) for right in roots[position + 1:]):
            raise RunServiceError("UNSAFE_RUN_ROOT", "Run storage roots must be separate")

    def _preflight(self, request_id: str, snapshot_id: str
                   ) -> tuple[dict[str, Any], dict[str, Any], Path]:
        operation = self.ledger.get(request_id)
        if operation is None or operation["status"] != "RUNNING":
            raise RunServiceError("OPERATION_NOT_RUNNING", "Run requires a live admitted operation")
        command = operation["command"]
        try:
            validate_shape(command)
        except DomainError:
            raise RunServiceError("COMMAND_MISMATCH", "Admitted run command is invalid") from None
        action = command["action"]
        if action not in {"VERIFY", "DELIVER_VERIFY_PUBLISH"}:
            raise RunServiceError("WRONG_RUN_ACTION", "Operation does not request a finite run")
        snapshot = self.registry.get_snapshot(snapshot_id)
        if (snapshot is None or snapshot["project_id"] != command["project_id"]
                or snapshot["workspace_id"] != command["workspace_id"]
                or snapshot["base_head"] != command["expected_head"]):
            raise RunServiceError("SNAPSHOT_MISMATCH", "Run snapshot differs from admitted source")
        scope = (f"workspace:{command['project_id']}/{command['workspace_id']}"
                 if action == "VERIFY" else f"repo:{command['project_id']}")
        connection = self.registry.database.connect()
        try:
            held = connection.execute(
                "SELECT 1 FROM locks WHERE request_id=? AND scope=?", (request_id, scope),
            ).fetchone()
            effect = connection.execute(
                "SELECT intent_json,state,provider_id FROM effects WHERE request_id=? "
                "AND step_id='source-snapshot' AND kind='git_ref'", (request_id,),
            ).fetchone()
        finally:
            connection.close()
        if held is None:
            raise RunServiceError("SOURCE_LOCK_MISSING", "Admitted source lock is not held")
        try:
            intent = json.loads(effect[0]) if effect else None
        except (TypeError, ValueError):
            intent = None
        if (effect is None or effect[1] != "CONFIRMED"
                or effect[2] != snapshot["snapshot_commit"] or not isinstance(intent, dict)
                or intent.get("snapshot_id") != snapshot_id
                or intent.get("project_id") != command["project_id"]
                or intent.get("workspace_id") != command["workspace_id"]
                or intent.get("kind") != snapshot["provenance"]
                or intent.get("expected_head") != snapshot["base_head"]):
            raise RunServiceError("SNAPSHOT_NOT_ADMITTED", "Snapshot was not captured for this operation")
        project = self.registry.get_project(command["project_id"])
        if project is None:
            raise RunServiceError("PROJECT_NOT_REGISTERED", "Managed project source is unavailable")
        return command, snapshot, Path(project["repo_path"])

    def _reserve_attempt(self, request_id: str, snapshot_id: str, run_id: str) -> None:
        """Persist one execution attempt before touching source or starting a container.

        A crashed or failed attempt is never retried under the same command ID. The
        owner must submit a new command after reconciling any uncertain effects.
        """
        connection = self.registry.database.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            operation = connection.execute(
                "SELECT status FROM operations WHERE request_id=?", (request_id,),
            ).fetchone()
            if operation is None or operation[0] != "RUNNING":
                raise RunServiceError("OPERATION_NOT_RUNNING", "Run operation is no longer active")
            if connection.execute(
                "SELECT 1 FROM runs WHERE request_id=?", (request_id,),
            ).fetchone() is not None:
                raise RunServiceError("RUN_ALREADY_ATTEMPTED", "Run command already has evidence")
            connection.execute(
                "INSERT INTO run_attempts(request_id,run_id,snapshot_id,created_at) "
                "VALUES(?,?,?,?)",
                (request_id, run_id, snapshot_id, datetime.now(timezone.utc).isoformat()),
            )
            connection.execute("COMMIT")
        except sqlite3.IntegrityError:
            connection.execute("ROLLBACK")
            raise RunServiceError("RUN_ALREADY_ATTEMPTED", "Run command already has an attempt") from None
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def execute(self, request_id: str, snapshot_id: str, *,
                run_id: str | None = None, writable_volume: Path | None = None,
                cancel: Event | None = None) -> RunCompletion:
        run_id = _uuid(run_id)
        command, snapshot, repo = self._preflight(request_id, snapshot_id)
        profile = self.profiles.require(command["params"]["profile_id"])
        specs = _specs(profile)
        source_path = self.sources_root / run_id
        run_root = self.runs_root / run_id
        evidence_dir = self.evidence.evidence_root / run_id
        if any(os.path.lexists(path) for path in (source_path, run_root, evidence_dir)):
            raise RunServiceError("RUN_ID_REUSED", "Run ID already has local files")
        self._reserve_attempt(request_id, snapshot_id, run_id)
        materialized = materialize_snapshot(
            repo, snapshot_id, snapshot["snapshot_commit"], snapshot["tree_digest"], source_path,
        )
        # A stopped or reconciled operation cannot start a new container.
        self._preflight(request_id, snapshot_id)
        result = self.broker.run(
            profile, materialized, run_root, run_id=run_id,
            writable_volume=writable_volume, cancel=cancel,
        )
        self._verify_result(result, profile, materialized, run_root, writable_volume)
        ensure_private_directory(evidence_dir)
        capture_outputs, frozen_capture = _freeze_capture(result.capture, run_root, evidence_dir)
        expected_output_root = (writable_volume / "out" if writable_volume is not None
                                else run_root / "out")
        if Path(result.output_root) != expected_output_root:
            raise RunServiceError("OUTPUT_ROOT_MISMATCH", "Broker output root differs from its run slot")
        try:
            collection = collect_outputs(
                result.output_root, specs, evidence_dir / "collected",
                max_total_bytes=profile.output_budget_mb * 1024 * 1024,
            )
        except (OutputError, OSError) as error:
            code = error.code if isinstance(error, OutputError) else "OUTPUT_COLLECTION_FAILED"
            collection = CollectionResult((), {}, {}, (code,), {}, False)
        capture_incomplete = tuple(
            f"{stream.name}:CAPTURE_TRUNCATED" for stream in result.capture.streams
            if not stream.complete
        )
        event_incomplete = (() if result.capture.observed_events == result.capture.retained_events
                            else ("capture-events:CAPTURE_TRUNCATED",))
        capture_fault = (() if result.capture.capture_fault is None
                         else (f"CAPTURE_FAULT:{result.capture.capture_fault}",))
        findings = (*collection.findings, *capture_incomplete, *event_incomplete, *capture_fault)
        collection_complete = collection.complete and not (
            capture_incomplete or event_incomplete or capture_fault
        )
        metadata_path = evidence_dir / "collection.json"
        metadata_output = _collection_metadata(
            collection, result.capture, metadata_path,
            complete=collection_complete, findings=findings,
        )
        outputs = [*capture_outputs, *collection.outputs, metadata_output]
        files = {**frozen_capture, **collection.files, "runner-collection": metadata_path}
        if len({output["output_id"] for output in outputs}) != len(outputs):
            raise RunServiceError("OUTPUT_ID_COLLISION", "Collected output IDs collide")
        operation_status = (
            "FAULT" if not collection_complete or result.outcome == "UNKNOWN"
            else "CANCELLED" if result.outcome == "CANCELLED"
            else "COMPLETED"
        )
        operation_code = (
            "CAPTURE_FAULT" if capture_fault
            else "CAPTURE_INCOMPLETE" if capture_incomplete or event_incomplete
            else "OUTPUT_COLLECTION_INCOMPLETE" if operation_status == "FAULT"
            else None
        )
        manifest = {
            "schema_version": "1.0", "run_id": run_id, "request_id": request_id,
            "project_id": command["project_id"], "workspace_id": command["workspace_id"],
            "snapshot": {
                "commit": snapshot["snapshot_commit"], "tree": snapshot["tree_digest"],
                "source_dirty_at_capture": snapshot["dirty_at_capture"],
            },
            "operation_status": operation_status,
            "execution": {
                "outcome": result.outcome, "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
            },
            "delivery": {"status": "NOT_ATTEMPTED", "reason": None},
            "outputs": outputs, "created_at": datetime.now(timezone.utc).isoformat(),
        }
        containment = dict(result.containment)
        containment["output_collection_complete"] = collection_complete
        provenance = {
            "backend": "rootless_docker", "run_id": result.run_id,
            "snapshot_id": result.snapshot_id,
            "snapshot_commit": result.snapshot_commit, "snapshot_tree": result.snapshot_tree,
            "profile_id": result.profile_id, "profile_revision": result.profile_revision_sha256,
            "image_digest": result.image, "argv_sha256": result.argv_sha256,
            "started_at": result.started_at, "ended_at": result.ended_at,
            "containment": containment, "outcome": result.outcome,
            "exit_code": result.exit_code, "duration_seconds": result.duration_seconds,
            "capture_fault": result.capture.capture_fault,
        }
        recorded = self.evidence.record_contained_run(
            manifest, snapshot_id=snapshot_id, provenance=provenance, files=files,
        )
        return RunCompletion(
            manifest=recorded,
            findings=findings,
            parse_summaries={output_id: value.summary for output_id, value in collection.parses.items()
                             if value.status == "PARSED"},
            operation_status=operation_status, operation_code=operation_code,
        )

    @staticmethod
    def _verify_result(result: BrokerResult, profile: ApprovedProfile, materialized: SnapshotInput,
                       run_root: Path, writable_volume: Path | None) -> None:
        if (result.run_id != run_root.name or result.snapshot_id != materialized.snapshot_id
                or result.snapshot_commit != materialized.commit
                or result.snapshot_tree != materialized.tree
                or Path(result.snapshot_path) != materialized.path
                or result.profile_id != profile.profile_id
                or result.profile_revision_sha256 != profile.revision_sha256
                or result.image != profile.image
                or result.argv_sha256 != _sha256_argv(profile.argv)):
            raise RunServiceError("BROKER_IDENTITY_MISMATCH", "Broker returned a different run identity")
        capture = result.capture
        if (result.outcome not in {"PASSED", "FAILED", "TIMED_OUT", "CANCELLED", "UNKNOWN"}
                or isinstance(result.duration_seconds, bool)
                or not isinstance(result.duration_seconds, (int, float))
                or not math.isfinite(result.duration_seconds)
                or result.duration_seconds < 0
                or (result.outcome == "UNKNOWN" and capture.capture_fault is None)
                or (result.outcome != "UNKNOWN" and capture.outcome_hint != result.outcome)
                or (result.outcome in {"PASSED", "FAILED"}
                    and capture.exit_code != result.exit_code)):
            raise RunServiceError("BROKER_RESULT_INVALID", "Broker result is not a finite captured run")
        containment = result.containment
        if (not isinstance(containment, Mapping) or containment.get("verified") is not True
                or containment.get("backend") != "rootless_docker"
                or containment.get("daemon_rootless") is not True
                or containment.get("cleanup_verified") is not True
                or containment.get("image_repo_digest") != profile.image
                or containment.get("network") != "none"
                or containment.get("read_only_source") is not True):
            raise RunServiceError("CONTAINMENT_UNVERIFIED", "Broker containment is not verified")
        expected_output = writable_volume / "out" if writable_volume is not None else run_root / "out"
        if Path(result.output_root) != expected_output:
            raise RunServiceError("OUTPUT_ROOT_MISMATCH", "Broker output root differs from its run slot")
