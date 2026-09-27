"""Offline, immutable Drive delivery bundle from registered local run evidence.

Only bundle items are eligible for a future provider write. Original output files
remain in the private RunEvidenceStore and never enter that upload list. This
module does not connect to Google, retry a command, or run project code.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

from runner_app.domain.commands import DomainError, parse_command
from runner_app.git.materialize import _rename_no_replace
from runner_app.outputs.collection import OutputError
from runner_app.outputs.redaction import redact_text_export
from runner_app.resources import load_schema
from runner_app.storage.paths import UnsafePathError, _absolute, ensure_private_directory
from runner_app.storage.run_evidence import OutputHandle

_RUN = Draft202012Validator(load_schema("run-manifest"), format_checker=FormatChecker())
_OPERATION = Draft202012Validator(load_schema("operation-manifest"), format_checker=FormatChecker())
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_MAX_EXPORTS = 32
_MAX_EXPORT_BYTES = 2 * 1024 * 1024
_MAX_BUNDLE_BYTES = 32 * 1024 * 1024
_MAX_INPUT_MANIFEST_BYTES = 1_048_576
_TEXT_MEDIA = frozenset({
    "text/plain", "text/markdown", "text/csv", "application/json",
    "application/x-ndjson", "application/xml", "text/xml",
})


class DeliveryError(ValueError):
    """Stable preparation refusal without output bytes or private paths."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class EvidenceReader(Protocol):
    def open_output(self, run_id: str, output_id: str) -> OutputHandle: ...

    def read_input_manifest(self, run_id: str) -> bytes: ...


class RunReader(Protocol):
    def get(self, request_id: str) -> dict[str, Any] | None: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class BundleItem:
    name: str
    media_type: str
    path: Path
    sha256: str
    size_bytes: int
    source_output_id: str | None = None


@dataclass(frozen=True)
class DeliveryBundle:
    source_request_id: str
    delivery_request_id: str
    run_id: str
    root: Path
    items: tuple[BundleItem, ...]
    quarantined_output_ids: tuple[str, ...]


def _uuid(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise DeliveryError("INVALID_DELIVERY_ID", "A canonical UUID is required") from None
    return value


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError):
        raise DeliveryError("INVALID_DELIVERY_INPUT", "Bundle input is not finite JSON") from None


def _write_file(path: Path, content: bytes) -> BundleItem:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise DeliveryError("BUNDLE_WRITE_FAILED", "Bundle storage made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return BundleItem(path.name, "application/json", path, hashlib.sha256(content).hexdigest(),
                      len(content))


def _read_registered(evidence: EvidenceReader, run_id: str, output: Mapping[str, Any]) -> bytes:
    size = output["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or size > _MAX_EXPORT_BYTES:
        raise DeliveryError("EXPORT_BYTE_LIMIT", "Approved output exceeds export limit")
    handle = evidence.open_output(run_id, output["output_id"])
    try:
        content = bytearray()
        while chunk := os.read(handle.descriptor, min(65_536, _MAX_EXPORT_BYTES + 1 - len(content))):
            content.extend(chunk)
            if len(content) > _MAX_EXPORT_BYTES:
                raise DeliveryError("EXPORT_BYTE_LIMIT", "Approved output exceeds export limit")
        if len(content) != size or hashlib.sha256(content).hexdigest() != output["sha256"]:
            raise DeliveryError("EXPORT_SOURCE_CHANGED", "Registered output changed during export")
        return bytes(content)
    finally:
        handle.close()


def _report(run: Mapping[str, Any], operation: Mapping[str, Any],
            exported: Sequence[BundleItem], quarantined: Sequence[str]) -> bytes:
    # Every interpolated value is a schema-checked ID, digest, enum, or count.
    lines = [
        "# Chat-Dev Runner result", "",
        f"Request: `{run['request_id']}`",
        f"Run: `{run['run_id']}`",
        f"Project/workspace: `{run['project_id']}` / `{run['workspace_id']}`",
        f"Tested commit: `{run['snapshot']['commit']}`",
        f"Execution: **{run['execution']['outcome']}** (exit `{run['execution']['exit_code']}`)",
        f"Operation: **{operation['status']}**",
        f"Publication: **{operation['publication']['status']}**",
        f"Delivery at bundle preparation: **{operation['delivery']['status']}**", "",
        f"Local original outputs retained: {len(run['outputs'])}",
        f"Approved sanitized exports: {len(exported)}",
        f"Quarantined exports requiring review: {len(quarantined)}", "",
        "Raw stdout/stderr and other local-only evidence are available through the authenticated local runner.",
        "Sanitization is best effort; exported files have separate hashes from their originals.",
    ]
    result = ("\n".join(lines) + "\n").encode("utf-8")
    if len(result) > 6144:
        raise DeliveryError("REPORT_TOO_LARGE", "Compact result report exceeds its bound")
    return result


def prepare_run_bundle(
    ledger: RunReader,
    evidence: EvidenceReader,
    run_id: str,
    delivery_request_id: str,
    operation_manifest: Mapping[str, Any],
    bundle_parent: Path,
    *,
    approved_output_ids: Sequence[str] = (),
    reviewed_output_ids: Sequence[str] = (),
) -> DeliveryBundle:
    """Freeze schema-valid manifests and selected sanitized variants for one run.

    `approved_output_ids` and `reviewed_output_ids` are trusted local policy,
    never parsed from a Drive command or project file. A retry uses a new
    delivery_request_id and only stored run evidence; it cannot rerun tests.
    """
    run_id, delivery_request_id = _uuid(run_id), _uuid(delivery_request_id)
    run = ledger.get_run(run_id)
    if run is None or not _RUN.is_valid(run) or run.get("run_id") != run_id:
        raise DeliveryError("RUN_NOT_REGISTERED", "Immutable source run is unavailable")
    source_request_id = _uuid(run["request_id"])
    operation = ledger.get(source_request_id)
    if operation is None or operation.get("request_id") != source_request_id:
        raise DeliveryError("OPERATION_NOT_REGISTERED", "Source operation is unavailable")
    if (not _OPERATION.is_valid(dict(operation_manifest))
            or operation_manifest["request_id"] != source_request_id
            or operation_manifest["project_id"] != run["project_id"]
            or operation_manifest["workspace_id"] != run["workspace_id"]
            or run_id not in operation_manifest["run_ids"]
            or operation_manifest["status"] == "RUNNING"):
        raise DeliveryError("OPERATION_MANIFEST_MISMATCH", "Operation result does not name this run")
    command_bytes = _json_bytes(operation["command"])
    try:
        command = parse_command(command_bytes)
    except DomainError:
        raise DeliveryError("FROZEN_COMMAND_INVALID", "Source command is not schema-valid") from None
    if command.digest != operation["digest"] or command.command["request_id"] != source_request_id:
        raise DeliveryError("FROZEN_COMMAND_CHANGED", "Source command differs from durable digest")
    # The local canonical digest remains authoritative. The upload view cannot
    # re-enter the command parser and never contains the free-form intent text.
    redacted_command = {**command.command, "intent": "[REDACTED FOR DELIVERY]"}
    command_delivery = _json_bytes({
        "schema_version": "1.0", "kind": "redacted-command/v1",
        "request_digest": command.digest, "redacted_fields": ["intent"],
        "command": redacted_command,
    })
    input_manifest = evidence.read_input_manifest(run_id)
    if (not isinstance(input_manifest, bytes)
            or len(input_manifest) > _MAX_INPUT_MANIFEST_BYTES):
        raise DeliveryError("INPUT_MANIFEST_INVALID", "Bounded registered input manifest is required")
    try:
        input_value = json.loads(input_manifest.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError):
        raise DeliveryError("INPUT_MANIFEST_INVALID", "Registered input manifest is invalid") from None
    if (not isinstance(input_value, dict)
            or input_value.get("project_id") != run["project_id"]
            or input_value.get("workspace_id") != run["workspace_id"]
            or input_value.get("base_head") != command.command["expected_head"]
            or input_value.get("commit") != run["snapshot"]["commit"]
            or input_value.get("tree") != run["snapshot"]["tree"]):
        raise DeliveryError("INPUT_MANIFEST_MISMATCH", "Input manifest differs from source run")
    approved = tuple(approved_output_ids)
    reviewed = frozenset(reviewed_output_ids)
    if (len(approved) > _MAX_EXPORTS or len(set(approved)) != len(approved)
            or any(not isinstance(item, str) or _ID.fullmatch(item) is None for item in approved)
            or not reviewed <= set(approved)):
        raise DeliveryError("EXPORT_POLICY_INVALID", "Local export allowlist is invalid")
    outputs = {item["output_id"]: item for item in run["outputs"]}
    if len(outputs) != len(run["outputs"]) or any(item not in outputs for item in approved):
        raise DeliveryError("EXPORT_OUTPUT_UNKNOWN", "Approved output is not in the run")
    try:
        parent = _absolute(Path(bundle_parent))
        ensure_private_directory(parent)
    except (OSError, UnsafePathError):
        raise DeliveryError("BUNDLE_ROOT_UNSAFE", "Bundle parent must be owner-private") from None
    target = parent / delivery_request_id
    if target.exists() or target.is_symlink():
        raise DeliveryError("BUNDLE_EXISTS", "Delivery attempt already has a local bundle")
    stage = Path(tempfile.mkdtemp(prefix=f".{delivery_request_id}.stage-", dir=parent))
    try:
        os.chmod(stage, 0o700)
        items: list[BundleItem] = []
        for name, payload in (
            ("operation-manifest.json", _json_bytes(dict(operation_manifest))),
            ("run-manifest.json", _json_bytes(run)),
            ("input-manifest.json", input_manifest),
            ("frozen-command.json", command_delivery),
        ):
            items.append(_write_file(stage / name, payload))
        exports_dir = stage / "exports"
        ensure_private_directory(exports_dir)
        quarantined: list[str] = []
        for output_id in approved:
            output = outputs[output_id]
            if output["media_type"].split(";", 1)[0].lower() not in _TEXT_MEDIA:
                raise DeliveryError("EXPORT_FORMAT_UNSUPPORTED", "Only reviewed text can be exported")
            if not output["complete"]:
                raise DeliveryError("EXPORT_INCOMPLETE", "Incomplete output cannot be exported")
            content = _read_registered(evidence, run_id, output)
            raw_copy = stage / f".{output_id}.raw"
            _write_file(raw_copy, content)
            try:
                variant = redact_text_export(output, raw_copy, exports_dir, allowlisted=True,
                                             reviewed_suspicious=output_id in reviewed,
                                             max_bytes=_MAX_EXPORT_BYTES)
            except OutputError as error:
                raise DeliveryError(error.code, "Approved output cannot be exported") from None
            finally:
                raw_copy.unlink(missing_ok=True)
            if variant.quarantined:
                quarantined.append(output_id)
                continue
            items.append(BundleItem(
                f"exports/{variant.file.name}", variant.output["media_type"], variant.file,
                variant.export_sha256, variant.output["size_bytes"], output_id,
            ))
        report = _report(run, operation_manifest, [item for item in items if item.source_output_id],
                         quarantined)
        report_item = _write_file(stage / "report.md", report)
        items.append(BundleItem(report_item.name, "text/markdown; charset=utf-8", report_item.path,
                                report_item.sha256, report_item.size_bytes))
        index = {
            "schema_version": "1.0", "source_request_id": source_request_id,
            "delivery_request_id": delivery_request_id, "run_id": run_id,
            "tested_commit": run["snapshot"]["commit"],
            "items": [
                {"name": item.name, "media_type": item.media_type,
                 "size_bytes": item.size_bytes, "sha256": item.sha256,
                 "source_output_id": item.source_output_id}
                for item in items
            ],
            "quarantined_output_ids": quarantined,
            "raw_outputs_local_only": [item["output_id"] for item in run["outputs"]],
        }
        index_item = _write_file(stage / "bundle-index.json", _json_bytes(index))
        items.append(index_item)
        if sum(item.size_bytes for item in items) > _MAX_BUNDLE_BYTES:
            raise DeliveryError("BUNDLE_TOO_LARGE", "Prepared upload exceeds local bundle budget")
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            _rename_no_replace(parent_fd, stage.name, target.name)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return DeliveryBundle(source_request_id, delivery_request_id, run_id, target,
                              tuple(BundleItem(item.name, item.media_type, target / item.name,
                                               item.sha256, item.size_bytes, item.source_output_id)
                                    for item in items), tuple(quarantined))
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
