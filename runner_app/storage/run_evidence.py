"""Durable, immutable records for runs produced by the contained executor.

Only a trusted finite workflow calls this store. The transport cannot submit a run
manifest or a containment claim. Raw outputs stay under a private local evidence root;
registered IDs, not request paths, select them for later download.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

from bridge_reference.protocol import Rejected, safe_relative_path
from runner_app.git.changes import ChangeError, safe_source_path
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.resources import load_schema
from runner_app.storage.database import Database
from runner_app.storage.paths import (
    UnsafePathError,
    _absolute,
    _check_private,
    _open_directory,
    ensure_private_directory,
    open_private_file,
)

_RUN = Draft202012Validator(load_schema("run-manifest"), format_checker=FormatChecker())
_OUTPUT = Draft202012Validator(load_schema("output"), format_checker=FormatChecker())
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_IMAGE = re.compile(
    r"[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?/[a-z0-9][a-z0-9._/-]*"
    r"@sha256:[0-9a-f]{64}\Z"
)
_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
_MAX_INPUT_MANIFEST_BYTES = 1_048_576


class EvidenceError(ValueError):
    """Stable refusal without exposing source bytes or private paths."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _uuid(value: Any) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise EvidenceError("INVALID_RUN_ID", "A canonical run UUID is required") from None
    return value


def _hex(value: Any) -> str:
    if not isinstance(value, str) or _HEX.fullmatch(value) is None:
        raise EvidenceError("INVALID_RUN_EVIDENCE", "A SHA-256 digest is required")
    return value


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise EvidenceError("INVALID_RUN_EVIDENCE", "Run time must include a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except ValueError:
        raise EvidenceError("INVALID_RUN_EVIDENCE", "Run time must include a timezone") from None


def _hash_opened(path: Path) -> tuple[str, int, int]:
    try:
        descriptor = open_private_file(path)
    except (OSError, UnsafePathError):
        raise EvidenceError("UNSAFE_OUTPUT_FILE", "Output is not a private regular file") from None
    try:
        before = os.fstat(descriptor)
        if before.st_size > _MAX_OUTPUT_BYTES:
            raise EvidenceError("OUTPUT_TOO_LARGE", "Output exceeds the local evidence limit")
        digest = hashlib.sha256()
        count = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            count += len(chunk)
            if count > _MAX_OUTPUT_BYTES:
                raise EvidenceError("OUTPUT_TOO_LARGE", "Output exceeds the local evidence limit")
        after = os.fstat(descriptor)
        if count != before.st_size or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise EvidenceError("OUTPUT_CHANGED", "Output changed during evidence registration")
        return digest.hexdigest(), count, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _sealed_output_copy(path: Path, expected_digest: str, expected_size: int) -> int:
    """Hash bytes into a sealed Linux memfd, so streaming cannot race a later file edit."""
    try:
        source = open_private_file(path)
    except (OSError, UnsafePathError):
        raise EvidenceError("UNSAFE_OUTPUT_FILE", "Output is not a private regular file") from None
    copy = -1
    try:
        before = os.fstat(source)
        if before.st_size > _MAX_OUTPUT_BYTES:
            raise EvidenceError("OUTPUT_TOO_LARGE", "Output exceeds the local evidence limit")
        try:
            copy = os.memfd_create("cdr-output", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        except (AttributeError, OSError):
            raise EvidenceError("EVIDENCE_SEAL_UNAVAILABLE", "Sealed output download is unavailable") from None
        digest = hashlib.sha256()
        count = 0
        while chunk := os.read(source, 1024 * 1024):
            count += len(chunk)
            if count > _MAX_OUTPUT_BYTES:
                raise EvidenceError("OUTPUT_TOO_LARGE", "Output exceeds the local evidence limit")
            digest.update(chunk)
            offset = 0
            while offset < len(chunk):
                written = os.write(copy, chunk[offset:])
                if written <= 0:
                    raise EvidenceError("OUTPUT_COPY_FAILED", "Sealed output copy made no progress")
                offset += written
        after = os.fstat(source)
        if (count != expected_size or digest.hexdigest() != expected_digest
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
                )):
            raise EvidenceError("OUTPUT_CHANGED", "Registered output bytes have changed")
        seals = (fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK
                 | fcntl.F_SEAL_SEAL)
        try:
            fcntl.fcntl(copy, fcntl.F_ADD_SEALS, seals)
        except OSError:
            raise EvidenceError("EVIDENCE_SEAL_UNAVAILABLE", "Sealed output download is unavailable") from None
        os.lseek(copy, 0, os.SEEK_SET)
        result = copy
        copy = -1
        return result
    finally:
        os.close(source)
        if copy >= 0:
            os.close(copy)


@dataclass(frozen=True)
class OutputHandle:
    descriptor: int
    size_bytes: int
    sha256: str
    output_id: str

    def close(self) -> None:
        os.close(self.descriptor)


class RunEvidenceStore:
    def __init__(self, database: Database, registry: SourceRegistry, evidence_root: Path) -> None:
        if database.path != registry.database.path:
            raise ValueError("Run evidence and source registry must share one database")
        self.database = database
        self.registry = registry
        self.evidence_root = _absolute(evidence_root)
        ensure_private_directory(self.evidence_root)
        ensure_private_directory(self.evidence_root / ".locks")
        self.database.migrate()

    def record_contained_run(self, manifest: Mapping[str, Any], *, snapshot_id: str,
                             provenance: Mapping[str, Any],
                             files: Mapping[str, Path]) -> dict[str, Any]:
        """Persist an internally produced completed run and its frozen local output files.

        `provenance` comes from a verified rootless broker result, never from a command.
        The caller freezes outputs outside the container before this transaction.
        """
        value = dict(manifest)
        if not _RUN.is_valid(value):
            raise EvidenceError("INVALID_RUN_MANIFEST", "Run manifest does not match v1 schema")
        run_id, request_id = _uuid(value["run_id"]), _uuid(value["request_id"])
        snapshot_id = _uuid(snapshot_id)
        output_ids = [item["output_id"] for item in value["outputs"]]
        if len(set(output_ids)) != len(output_ids) or set(output_ids) != set(files):
            raise EvidenceError("OUTPUT_SET_MISMATCH", "Frozen files and output IDs differ")
        output_by_id = {item["output_id"]: item for item in value["outputs"]}
        for stream in ("stdout", "stderr"):
            raw = output_by_id.get(f"{stream}-raw")
            if (raw is None or raw["kind"] != "console" or raw["format"] != "raw-bytes-v1"
                    or raw["relative_path"] != f"capture/{stream}.bin"
                    or raw["publication"] != "local_only"):
                raise EvidenceError("CAPTURE_INCOMPLETE", "Both raw process streams are required")
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            raise EvidenceError("INVALID_RUN_MANIFEST", "Run manifest has non-finite or invalid JSON") from None
        snapshot = self.registry.get_snapshot(snapshot_id)
        if (snapshot is None or value["project_id"] != snapshot["project_id"]
                or value["workspace_id"] != snapshot["workspace_id"]
                or value["snapshot"] != {
                    "commit": snapshot["snapshot_commit"], "tree": snapshot["tree_digest"],
                    "source_dirty_at_capture": snapshot["dirty_at_capture"],
                }):
            raise EvidenceError("SNAPSHOT_MISMATCH", "Run does not name its exact source snapshot")
        if provenance.get("backend") != "rootless_docker" or not isinstance(
            provenance.get("containment"), dict
        ) or provenance["containment"].get("verified") is not True:
            raise EvidenceError("CONTAINMENT_UNVERIFIED", "A verified rootless broker result is required")
        if (provenance.get("snapshot_id") != snapshot_id
                or provenance.get("run_id") != run_id
                or provenance.get("snapshot_commit") != snapshot["snapshot_commit"]
                or provenance.get("snapshot_tree") != snapshot["tree_digest"]):
            raise EvidenceError("SNAPSHOT_MISMATCH", "Broker run identity differs from source snapshot")
        profile_id = provenance.get("profile_id")
        image = provenance.get("image_digest")
        if (not isinstance(profile_id, str) or _SLUG.fullmatch(profile_id) is None
                or not isinstance(image, str) or _IMAGE.fullmatch(image) is None):
            raise EvidenceError("INVALID_RUN_EVIDENCE", "Approved profile identity is invalid")
        revision = _hex(provenance.get("profile_revision"))
        argv_sha256 = _hex(provenance.get("argv_sha256"))
        started_at = provenance.get("started_at")
        ended_at = provenance.get("ended_at")
        if _time(started_at) > _time(ended_at):
            raise EvidenceError("INVALID_RUN_EVIDENCE", "Run ended before it started")
        actual = value["execution"]
        duration = actual["duration_seconds"]
        if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or duration < 0):
            raise EvidenceError("INVALID_RUN_MANIFEST", "Run duration must be finite")
        try:
            json.dumps(provenance["containment"], allow_nan=False)
        except (TypeError, ValueError):
            raise EvidenceError("INVALID_RUN_EVIDENCE", "Containment facts must be finite JSON") from None
        if (actual["outcome"] not in {"PASSED", "FAILED", "TIMED_OUT", "CANCELLED", "UNKNOWN"}
                or actual["outcome"] != provenance.get("outcome")
                or actual["exit_code"] != provenance.get("exit_code")
                or actual["duration_seconds"] != provenance.get("duration_seconds")):
            raise EvidenceError("EXECUTION_MISMATCH", "Run outcome differs from contained execution")
        capture_fault = provenance.get("capture_fault")
        if (capture_fault is not None and
                (not isinstance(capture_fault, str) or not 1 <= len(capture_fault) <= 128)):
            raise EvidenceError("INVALID_RUN_EVIDENCE", "Capture fault code is invalid")
        if (actual["outcome"] == "UNKNOWN" and
                (value["operation_status"] != "FAULT" or capture_fault is None)):
            raise EvidenceError("EXECUTION_MISMATCH", "Unknown execution requires a faulted run")
        if (value["operation_status"] == "COMPLETED" and
                (capture_fault is not None or any(not item["complete"] for item in value["outputs"]))):
            raise EvidenceError("CAPTURE_INCOMPLETE", "Incomplete capture cannot complete a run")
        run_dir = self.evidence_root / run_id
        ensure_private_directory(run_dir)
        opened: list[int] = []
        paths: list[tuple[str, str, str, int]] = []
        try:
            for output in value["outputs"]:
                output_id = output["output_id"]
                if not _OUTPUT.is_valid(output):
                    raise EvidenceError("INVALID_OUTPUT", "Run contains an invalid output")
                relative = output["relative_path"]
                try:
                    safe_relative_path(relative)
                except Rejected:
                    raise EvidenceError("INVALID_OUTPUT_PATH", "Output path is not canonical") from None
                path = _absolute(Path(files[output_id]))
                if path != run_dir / relative:
                    raise EvidenceError("INVALID_OUTPUT_PATH", "Output is outside its private run slot")
                digest, size, descriptor = _hash_opened(path)
                opened.append(descriptor)
                if digest != output["sha256"] or size != output["size_bytes"]:
                    raise EvidenceError("OUTPUT_CHANGED", "Output bytes differ from their manifest")
                paths.append((output_id, str(path), digest, size))
            timestamp = datetime.now(timezone.utc).isoformat()
            connection = self.database.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                operation = connection.execute(
                    "SELECT status,project_id,workspace_id,command_json FROM operations "
                    "WHERE request_id=?", (request_id,),
                ).fetchone()
                if (operation is None or operation[0] != "RUNNING"
                        or operation[1] != value["project_id"]
                        or operation[2] != value["workspace_id"]):
                    raise EvidenceError("OPERATION_NOT_RUNNING", "Run requires its admitted operation")
                try:
                    command = json.loads(operation[3])
                    action = command["action"]
                    expected_profile = command["params"]["profile_id"]
                    expected_head = command["expected_head"]
                except (TypeError, ValueError, KeyError):
                    raise EvidenceError("COMMAND_MISMATCH", "Admitted run command is invalid") from None
                if (action not in {"VERIFY", "DELIVER_VERIFY_PUBLISH"}
                        or expected_profile != profile_id
                        or expected_head != snapshot["base_head"]):
                    raise EvidenceError("COMMAND_MISMATCH", "Run differs from admitted source or profile")
                scope = (f"workspace:{value['project_id']}/{value['workspace_id']}"
                         if action == "VERIFY" else f"repo:{value['project_id']}")
                if connection.execute(
                    "SELECT 1 FROM locks WHERE request_id=? AND scope=?", (request_id, scope),
                ).fetchone() is None:
                    raise EvidenceError("SOURCE_LOCK_MISSING", "Run source lock is not held")
                effect = connection.execute(
                    "SELECT intent_json,state,provider_id FROM effects WHERE request_id=? "
                    "AND step_id='source-snapshot' AND kind='git_ref'", (request_id,),
                ).fetchone()
                try:
                    intent = json.loads(effect[0]) if effect else None
                except (TypeError, ValueError):
                    intent = None
                if (effect is None or effect[1] != "CONFIRMED"
                        or effect[2] != snapshot["snapshot_commit"]
                        or not isinstance(intent, dict)
                        or intent.get("snapshot_id") != snapshot_id
                        or intent.get("expected_head") != snapshot["base_head"]):
                    raise EvidenceError("SNAPSHOT_NOT_ADMITTED", "Run snapshot was not captured for this operation")
                connection.execute(
                    "INSERT INTO runs(run_id,request_id,project_id,workspace_id,manifest_json,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (run_id, request_id, value["project_id"], value["workspace_id"],
                     json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False), timestamp),
                )
                connection.execute(
                    "INSERT INTO run_executions(run_id,snapshot_id,profile_id,profile_revision,"
                    "image_digest,backend,argv_sha256,started_at,ended_at,containment_json,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, snapshot_id, profile_id, revision, image, "rootless_docker", argv_sha256,
                     started_at, ended_at,
                     json.dumps(provenance["containment"], sort_keys=True, separators=(",", ":"),
                                allow_nan=False),
                     timestamp),
                )
                for output, (output_id, private_path, digest, size) in zip(value["outputs"], paths):
                    connection.execute(
                        "INSERT INTO outputs(output_id,request_id,run_id,manifest_json,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (output_id, request_id, run_id,
                         json.dumps(output, sort_keys=True, separators=(",", ":"), allow_nan=False),
                         timestamp),
                    )
                    connection.execute(
                        "INSERT INTO output_files(request_id,run_id,output_id,private_path,sha256,"
                        "size_bytes,created_at) VALUES(?,?,?,?,?,?,?)",
                        (request_id, run_id, output_id, private_path, digest, size, timestamp),
                    )
                if (value["operation_status"] == "COMPLETED"
                        and actual["outcome"] in {"PASSED", "FAILED", "TIMED_OUT"}
                        and provenance["containment"].get("output_collection_complete") is True):
                    self.registry._bind_tested_run_record(connection, run_id, snapshot_id, snapshot)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        except sqlite3.IntegrityError:
            raise EvidenceError("RUN_ALREADY_RECORDED", "Immutable run or output identity is reused") from None
        finally:
            for descriptor in opened:
                os.close(descriptor)
        return value

    def read_input_manifest(self, run_id: str) -> bytes:
        """Return exact registered snapshot metadata for this contained run.

        The run and execution point to one immutable source snapshot. The input
        manifest is read from its owner-private Git slot, bounded and rehashed
        through a nofollow descriptor immediately before use by delivery.
        """
        run_id = _uuid(run_id)
        connection = self.database.connect()
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT r.request_id,r.project_id,r.workspace_id,r.manifest_json,"
                "e.snapshot_id,e.backend,e.containment_json,"
                "s.project_id AS source_project_id,s.workspace_id AS source_workspace_id,"
                "s.base_head,s.snapshot_commit,s.tree_digest,s.source_generation,"
                "s.capture_fingerprint,s.provenance,s.dirty_at_capture,s.included_paths_json,"
                "s.manifest_path,s.manifest_sha256,"
                "o.command_json "
                "FROM runs r JOIN run_executions e ON e.run_id=r.run_id "
                "JOIN source_snapshots s ON s.snapshot_id=e.snapshot_id "
                "JOIN operations o ON o.request_id=r.request_id WHERE r.run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise EvidenceError("INPUT_MANIFEST_NOT_FOUND", "Contained run input is unavailable")
            effect = connection.execute(
                "SELECT intent_json,state,provider_id FROM effects WHERE request_id=? "
                "AND step_id='source-snapshot' AND kind='git_ref'", (row["request_id"],),
            ).fetchone()
            binding = connection.execute(
                "SELECT snapshot_id FROM source_run_snapshots WHERE run_id=?", (run_id,),
            ).fetchone()
        finally:
            connection.close()
        try:
            run = json.loads(row["manifest_json"])
            command = json.loads(row["command_json"])
            containment = json.loads(row["containment_json"])
            intent = json.loads(effect[0]) if effect is not None else None
            included = json.loads(row["included_paths_json"])
        except (TypeError, ValueError):
            raise EvidenceError("INPUT_MANIFEST_SCOPE_MISMATCH", "Run input identity is invalid") from None
        if (not isinstance(run, dict) or not _RUN.is_valid(run)
                or not isinstance(command, dict) or not isinstance(containment, dict)
                or not isinstance(intent, dict) or not isinstance(included, list)
                or row["backend"] != "rootless_docker" or containment.get("verified") is not True
                or run["run_id"] != run_id or run["request_id"] != row["request_id"]
                or run["project_id"] != row["project_id"]
                or run["workspace_id"] != row["workspace_id"]
                or row["project_id"] != row["source_project_id"]
                or row["workspace_id"] != row["source_workspace_id"]
                or run["snapshot"] != {
                    "commit": row["snapshot_commit"], "tree": row["tree_digest"],
                    "source_dirty_at_capture": bool(row["dirty_at_capture"]),
                }
                or command.get("request_id") != row["request_id"]
                or command.get("project_id") != row["project_id"]
                or command.get("workspace_id") != row["workspace_id"]
                or command.get("expected_head") != row["base_head"]
                or command.get("action") not in {"VERIFY", "DELIVER_VERIFY_PUBLISH"}
                or effect is None or effect[1] != "CONFIRMED"
                or effect[2] != row["snapshot_commit"]
                or intent.get("snapshot_id") != row["snapshot_id"]
                or intent.get("expected_head") != row["base_head"]
                or (binding is not None and binding[0] != row["snapshot_id"])):
            raise EvidenceError("INPUT_MANIFEST_SCOPE_MISMATCH", "Run input belongs to another source")
        expected_path = (self.registry.managed_root / row["project_id"] / "snapshots"
                         / row["snapshot_id"] / "manifest.json")
        try:
            path = self.registry._managed_path(
                Path(row["manifest_path"]), directory=False, expected=expected_path,
            )
            descriptor = open_private_file(path)
        except (OSError, UnsafePathError, SourceError):
            raise EvidenceError("INPUT_MANIFEST_UNSAFE", "Private input manifest is unavailable") from None
        try:
            before = os.fstat(descriptor)
            if before.st_size > _MAX_INPUT_MANIFEST_BYTES:
                raise EvidenceError("INPUT_MANIFEST_TOO_LARGE", "Input manifest exceeds local limit")
            raw = bytearray()
            while chunk := os.read(descriptor, min(65_536, _MAX_INPUT_MANIFEST_BYTES + 1 - len(raw))):
                raw.extend(chunk)
                if len(raw) > _MAX_INPUT_MANIFEST_BYTES:
                    raise EvidenceError("INPUT_MANIFEST_TOO_LARGE", "Input manifest exceeds local limit")
            after = os.fstat(descriptor)
            named = os.stat(path, follow_symlinks=False)
            if (len(raw) != before.st_size or
                    (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                     before.st_ctime_ns) !=
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                     after.st_ctime_ns) or
                    (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)):
                raise EvidenceError("INPUT_MANIFEST_CHANGED", "Input manifest changed during read")
        except OSError:
            raise EvidenceError("INPUT_MANIFEST_CHANGED", "Input manifest changed during read") from None
        finally:
            os.close(descriptor)
        content = bytes(raw)
        if hashlib.sha256(content).hexdigest() != row["manifest_sha256"]:
            raise EvidenceError("INPUT_MANIFEST_CHANGED", "Input manifest differs from registered hash")
        try:
            value = json.loads(content.decode("utf-8", "strict"))
        except (TypeError, ValueError, UnicodeDecodeError):
            raise EvidenceError("INPUT_MANIFEST_INVALID", "Input manifest is not valid JSON") from None
        if (not isinstance(value, dict) or set(value) != {
                "schema_version", "snapshot_id", "project_id", "workspace_id", "base_head",
                "commit", "tree", "source_generation", "capture_fingerprint", "provenance",
                "dirty_at_capture", "included_paths", "approved_untracked_paths", "files",
                "retained_ref",
            } or value["schema_version"] != "1.0"
                or value["snapshot_id"] != row["snapshot_id"]
                or value["project_id"] != row["project_id"]
                or value["workspace_id"] != row["workspace_id"]
                or value["base_head"] != row["base_head"]
                or value["commit"] != row["snapshot_commit"]
                or value["tree"] != row["tree_digest"]
                or value["source_generation"] != row["source_generation"]
                or value["capture_fingerprint"] != row["capture_fingerprint"]
                or value["provenance"] != row["provenance"]
                or value["dirty_at_capture"] is not bool(row["dirty_at_capture"])
                or value["included_paths"] != included
                or value["retained_ref"] != f"refs/cdr/snapshots/{row['snapshot_id']}"
                or not isinstance(value["approved_untracked_paths"], list)
                or not isinstance(value["files"], list)
                or len(value["approved_untracked_paths"]) > 1000
                or len(value["files"]) > 1000
                or any(not isinstance(item, dict) or set(item) != {
                    "path", "op", "base_sha256", "sha256", "mode",
                } for item in value["files"])):
            raise EvidenceError("INPUT_MANIFEST_INVALID", "Input manifest differs from source record")
        try:
            paths = value["included_paths"] + value["approved_untracked_paths"]
            if any(not isinstance(path, str) for path in paths) or len(set(paths)) != len(paths):
                raise ValueError
            for path in paths:
                safe_source_path(path)
            file_paths: set[str] = set()
            for item in value["files"]:
                path = item["path"]
                if not isinstance(path, str) or path in file_paths:
                    raise ValueError
                safe_source_path(path)
                file_paths.add(path)
                if (item["op"] not in {"upsert", "delete"}
                        or type(item["mode"]) is not int
                        or item["mode"] not in {0o100644, 0o100755}
                        or (item["base_sha256"] is not None and
                            (not isinstance(item["base_sha256"], str)
                             or _HEX.fullmatch(item["base_sha256"]) is None))
                        or (item["op"] == "delete" and item["sha256"] is not None)
                        or (item["op"] == "upsert" and
                            (not isinstance(item["sha256"], str)
                             or _HEX.fullmatch(item["sha256"]) is None))):
                    raise ValueError
        except (ChangeError, TypeError, ValueError):
            raise EvidenceError("INPUT_MANIFEST_INVALID", "Input manifest contains unsafe metadata") from None
        return content

    @contextmanager
    def _run_lock(self, run_id: str, *, exclusive: bool = False) -> Iterator[None]:
        """A stable owner-private flock coordinates sealed readers and physical deletion."""
        try:
            descriptor = open_private_file(
                self.evidence_root / ".locks" / f"{run_id}.lock", create=True,
            )
        except (OSError, UnsafePathError):
            raise EvidenceError("EVIDENCE_LOCK_UNAVAILABLE", "Run evidence lock is unsafe") from None
        try:
            mode = fcntl.LOCK_EX | fcntl.LOCK_NB if exclusive else fcntl.LOCK_SH
            try:
                fcntl.flock(descriptor, mode)
            except BlockingIOError:
                raise EvidenceError("EVIDENCE_BUSY", "Run evidence is being read") from None
            except OSError:
                raise EvidenceError("EVIDENCE_LOCK_UNAVAILABLE", "Run evidence lock failed") from None
            yield
        finally:
            os.close(descriptor)

    @staticmethod
    def _retention_time(now: datetime | None, retention_days: int) -> tuple[datetime, str]:
        if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 1:
            raise EvidenceError("RETENTION_POLICY_INVALID", "Retention days must be positive")
        observed = datetime.now(timezone.utc) if now is None else now
        if not isinstance(observed, datetime) or observed.tzinfo is None or observed.utcoffset() is None:
            raise EvidenceError("RETENTION_POLICY_INVALID", "Retention time must include a timezone")
        observed = observed.astimezone(timezone.utc)
        try:
            cutoff = observed - timedelta(days=retention_days)
        except OverflowError:
            raise EvidenceError("RETENTION_POLICY_INVALID", "Retention age is out of range") from None
        return cutoff, observed.isoformat()

    def set_run_pin(self, run_id: str, *, pinned: bool) -> bool:
        """Trusted local owner policy records PIN/UNPIN as append-only decisions."""
        run_id = _uuid(run_id)
        if type(pinned) is not bool:
            raise EvidenceError("RETENTION_POLICY_INVALID", "Pin state must be explicit")
        connection = self.database.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
                raise EvidenceError("RUN_NOT_FOUND", "Contained run is unavailable")
            if pinned and connection.execute(
                "SELECT 1 FROM output_tombstones WHERE run_id=? LIMIT 1", (run_id,),
            ).fetchone() is not None:
                raise EvidenceError("EVIDENCE_EXPIRED", "Expired evidence cannot be pinned")
            previous = connection.execute(
                "SELECT action FROM run_retention_pin_events WHERE run_id=? "
                "ORDER BY event_id DESC LIMIT 1", (run_id,),
            ).fetchone()
            was_pinned = previous is not None and previous[0] == "PIN"
            if was_pinned != pinned:
                connection.execute(
                    "INSERT INTO run_retention_pin_events(run_id,action,created_at) VALUES(?,?,?)",
                    (run_id, "PIN" if pinned else "UNPIN", datetime.now(timezone.utc).isoformat()),
                )
            connection.execute("COMMIT")
            return was_pinned != pinned
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def run_retention_status(self, run_id: str) -> dict[str, Any]:
        """Return current owner pin plus immutable output/tombstone identities."""
        run_id = _uuid(run_id)
        connection = self.database.connect()
        try:
            if connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
                raise EvidenceError("RUN_NOT_FOUND", "Contained run is unavailable")
            latest = connection.execute(
                "SELECT action FROM run_retention_pin_events WHERE run_id=? "
                "ORDER BY event_id DESC LIMIT 1", (run_id,),
            ).fetchone()
            pinned = latest is not None and latest[0] == "PIN"
        finally:
            connection.close()
        return {"run_id": run_id, "pinned": pinned,
                "outputs": self.output_retention_index(run_id)}

    def output_retention_index(self, run_id: str) -> list[dict[str, Any]]:
        """Local index overlays immutable manifests with explicit expiry tombstones."""
        run_id = _uuid(run_id)
        connection = self.database.connect()
        try:
            if connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
                raise EvidenceError("RUN_NOT_FOUND", "Contained run is unavailable")
            rows = connection.execute(
                "SELECT f.output_id,f.sha256,f.size_bytes,t.reason,t.created_at "
                "FROM output_files f LEFT JOIN output_tombstones t "
                "ON t.run_id=f.run_id AND t.output_id=f.output_id "
                "WHERE f.run_id=? ORDER BY f.output_id", (run_id,),
            ).fetchall()
            return [{
                "output_id": row[0], "sha256": row[1], "size_bytes": row[2],
                "state": "EXPIRED" if row[3] is not None else "AVAILABLE",
                "tombstone_reason": row[3], "tombstone_at": row[4],
            } for row in rows]
        finally:
            connection.close()

    def _purge_tombstoned_file(self, run_id: str, private_path: str,
                              expected_digest: str, expected_size: int) -> None:
        path = _absolute(Path(private_path))
        try:
            relative = path.relative_to(self.evidence_root / run_id)
        except ValueError:
            raise EvidenceError("INVALID_OUTPUT_PATH", "Registered output left its private run slot") from None
        if not relative.parts or ".." in relative.parts:
            raise EvidenceError("INVALID_OUTPUT_PATH", "Registered output left its private run slot")
        try:
            parent_fd = _open_directory(path.parent, create=False)
        except (OSError, UnsafePathError):
            raise EvidenceError("EVIDENCE_PURGE_FAILED", "Output parent is unavailable") from None
        descriptor = -1
        sync_fd = -1
        try:
            try:
                descriptor = os.open(
                    path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                # Persist an earlier unlink even if this retry found no entry.
                sync_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                                  dir_fd=parent_fd)
                os.fsync(sync_fd)
                return
            _check_private(os.fstat(descriptor), directory=False)
            before = os.fstat(descriptor)
            if before.st_size != expected_size or before.st_size > _MAX_OUTPUT_BYTES:
                raise EvidenceError("OUTPUT_CHANGED", "Tombstoned output differs from registration")
            digest = hashlib.sha256()
            count = 0
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
                count += len(chunk)
                if count > _MAX_OUTPUT_BYTES:
                    raise EvidenceError("OUTPUT_CHANGED", "Tombstoned output exceeds local limit")
            after = os.fstat(descriptor)
            named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if (count != expected_size or digest.hexdigest() != expected_digest
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)):
                raise EvidenceError("OUTPUT_CHANGED", "Tombstoned output changed before purge")
            os.unlink(path.name, dir_fd=parent_fd)
            sync_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                              dir_fd=parent_fd)
            os.fsync(sync_fd)
        except EvidenceError:
            raise
        except (OSError, UnsafePathError):
            raise EvidenceError("EVIDENCE_PURGE_FAILED", "Tombstoned output could not be purged") from None
        finally:
            if sync_fd >= 0:
                os.close(sync_fd)
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent_fd)

    def expire_output(self, run_id: str, output_id: str, *, retention_days: int = 14,
                      now: datetime | None = None) -> dict[str, str]:
        """Retire one old output after checking references under SQLite and a run flock.

        The append-only tombstone commits before unlink. A crash after commit is
        recoverable by calling this method again; the public reader refuses the
        tombstoned ID even if physical removal failed.
        """
        run_id = _uuid(run_id)
        if not isinstance(output_id, str) or _SLUG.fullmatch(output_id) is None:
            raise EvidenceError("INVALID_OUTPUT_ID", "Output ID is invalid")
        cutoff, timestamp = self._retention_time(now, retention_days)
        with self._run_lock(run_id, exclusive=True):
            connection = self.database.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT f.private_path,f.sha256,f.size_bytes,r.created_at,"
                    "o.status,o.action,o.delivery_status,o.reconcile_required,o.capacity_held,"
                    "t.created_at AS tombstone_at "
                    "FROM output_files f JOIN runs r ON r.run_id=f.run_id "
                    "JOIN operations o ON o.request_id=r.request_id "
                    "LEFT JOIN output_tombstones t ON t.run_id=f.run_id AND t.output_id=f.output_id "
                    "WHERE f.run_id=? AND f.output_id=?", (run_id, output_id),
                ).fetchone()
                if row is None:
                    raise EvidenceError("OUTPUT_NOT_FOUND", "Output is not registered")
                tombstone_at = row[9]
                if tombstone_at is None:
                    if _time(row[3]) > cutoff:
                        raise EvidenceError("RETENTION_NOT_DUE", "Output has not reached retention age")
                    if (row[4] == "RUNNING" or row[7] or row[8]
                            or row[6] in {"PENDING", "PARTIAL", "FAILED"}
                            or (row[5] == "DELIVER_VERIFY_PUBLISH" and row[6] != "DELIVERED")):
                        raise EvidenceError("EVIDENCE_REFERENCED", "Run has pending execution or delivery")
                    if connection.execute(
                        "SELECT 1 FROM effects e JOIN runs r ON r.request_id=e.request_id "
                        "WHERE r.run_id=? AND e.state IN ('INTENDED','UNKNOWN') LIMIT 1", (run_id,),
                    ).fetchone() is not None:
                        raise EvidenceError("EVIDENCE_REFERENCED", "Run has an uncertain external effect")
                    if connection.execute(
                        "SELECT 1 FROM run_delivery_claims c LEFT JOIN run_delivery_claim_releases x "
                        "ON x.delivery_request_id=c.delivery_request_id "
                        "WHERE c.run_id=? AND x.delivery_request_id IS NULL LIMIT 1", (run_id,),
                    ).fetchone() is not None:
                        raise EvidenceError("EVIDENCE_REFERENCED", "Run has an active delivery claim")
                    pin = connection.execute(
                        "SELECT action FROM run_retention_pin_events WHERE run_id=? "
                        "ORDER BY event_id DESC LIMIT 1", (run_id,),
                    ).fetchone()
                    if pin is not None and pin[0] == "PIN":
                        raise EvidenceError("EVIDENCE_PINNED", "Run is pinned by local owner policy")
                    connection.execute(
                        "INSERT INTO output_tombstones(run_id,output_id,reason,created_at) "
                        "VALUES(?,?,'RETENTION_EXPIRED',?)", (run_id, output_id, timestamp),
                    )
                    tombstone_at = timestamp
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
            self._purge_tombstoned_file(run_id, str(row[0]), str(row[1]), int(row[2]))
            return {"run_id": run_id, "output_id": output_id,
                    "reason": "RETENTION_EXPIRED", "created_at": str(tombstone_at)}

    def open_output(self, run_id: str, output_id: str) -> OutputHandle:
        run_id = _uuid(run_id)
        if not isinstance(output_id, str) or _SLUG.fullmatch(output_id) is None:
            raise EvidenceError("INVALID_OUTPUT_ID", "Output ID is invalid")
        # Unknown authenticated IDs must not create unbounded lock files.
        connection = self.database.connect()
        try:
            known = connection.execute(
                "SELECT 1 FROM runs WHERE run_id=?", (run_id,),
            ).fetchone() is not None
        finally:
            connection.close()
        if not known:
            raise EvidenceError("OUTPUT_NOT_FOUND", "Output is not registered")
        with self._run_lock(run_id):
            connection = self.database.connect()
            try:
                row = connection.execute(
                    "SELECT f.private_path,f.sha256,f.size_bytes,t.created_at "
                    "FROM output_files f LEFT JOIN output_tombstones t "
                    "ON t.run_id=f.run_id AND t.output_id=f.output_id "
                    "WHERE f.run_id=? AND f.output_id=?", (run_id, output_id),
                ).fetchone()
            finally:
                connection.close()
            if row is None:
                raise EvidenceError("OUTPUT_NOT_FOUND", "Output is not registered")
            if row[3] is not None:
                raise EvidenceError("EVIDENCE_EXPIRED", "Output retention has expired")
            path = _absolute(Path(row[0]))
            try:
                relative = path.relative_to(self.evidence_root / run_id)
            except ValueError:
                raise EvidenceError("INVALID_OUTPUT_PATH", "Registered output left its private run slot") from None
            if not relative.parts or ".." in relative.parts:
                raise EvidenceError("INVALID_OUTPUT_PATH", "Registered output left its private run slot")
            descriptor = _sealed_output_copy(path, str(row[1]), int(row[2]))
            return OutputHandle(descriptor, int(row[2]), str(row[1]), output_id)
