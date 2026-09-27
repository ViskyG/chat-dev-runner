"""Finite, journaled publication of a prepared run bundle to an attached Drive.

A trusted handler supplies an immutable local bundle and registered workspace output
Docs. This adapter neither runs code nor accepts Drive text as authority. Every remote
write has a durable effect intent; uncertain writes stop the plan for reconciliation.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from jsonschema import Draft202012Validator, FormatChecker

from runner_app.adapters.drive_delivery import BundleItem, DeliveryBundle, DeliveryError
from runner_app.adapters.drive_docs import DriveError
from runner_app.adapters.drive_index import AttachedInstallation
from runner_app.adapters.drive_writer import (
    DocsCASWriter,
    ObservedOutputDoc,
    RegisteredOutputDoc,
)
from runner_app.resources import load_schema
from runner_app.storage.ledger import DeliveryPreflight
from runner_app.storage.paths import (
    UnsafePathError,
    ensure_private_directory,
    open_private_file,
)

_OPERATION = Draft202012Validator(load_schema("operation-manifest"), format_checker=FormatChecker())
_RUN = Draft202012Validator(load_schema("run-manifest"), format_checker=FormatChecker())
_LATEST_HEADER = "CHAT-DEV RUNNER / LATEST RESULT\n"
_RECEIPT_HEADER = "CHAT-DEV RUNNER / RECEIPT\n"
_MAX_ITEM_BYTES = 5 * 1024 * 1024
_MAX_ITEMS = 40

DeliveryStatus = Literal["DELIVERED", "PARTIAL", "PENDING"]


class DeliveryLedger(Protocol):
    def require_delivery_preflight(self, delivery_request_id: str,
                                   run_id: str) -> DeliveryPreflight: ...

    def get(self, request_id: str) -> dict[str, Any] | None: ...

    def begin_effect(self, request_id: str, step_id: str, kind: str,
                     intent: dict[str, Any]) -> str: ...

    def confirm_effect(self, effect_id: str, provider_id: str) -> None: ...

    def record_delivery(self, request_id: str, status: str,
                        code: str | None = None) -> None: ...


class WorkspaceRegistry(Protocol):
    def require_output_pair(
        self, project_id: str, workspace_id: str,
        receipt: RegisteredOutputDoc, latest: RegisteredOutputDoc,
    ) -> Any: ...


class UploadProvider(Protocol):
    def get_file(self, file_id: str, **expected: Any) -> Mapping[str, Any]: ...

    def find_by_key(self, parent_id: str, installation_id: str, key: str,
                    **expected: Any) -> Mapping[str, Any] | None: ...

    def create_folder(self, parent_id: str, installation_id: str, key: str,
                      name: str) -> Mapping[str, Any]: ...

    def upload_file(self, parent_id: str, installation_id: str, key: str,
                    name: str, path: Path, media_type: str, size_bytes: int,
                    sha256: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class DeliveryResult:
    status: DeliveryStatus
    code: str | None
    request_folder_id: str | None
    run_folder_id: str | None
    confirmed_assets: tuple[str, ...]
    latest_skipped_newer: bool = False
    index_file_id: str | None = None


@dataclass(frozen=True)
class ReconcileResult:
    state: Literal["CONFIRMED", "UNRESOLVED"]
    effect_id: str
    provider_id: str | None = None
    code: str | None = None


@dataclass(frozen=True)
class _ItemCheck:
    item: BundleItem
    md5: str
    display_name: str


def _read_item(bundle: DeliveryBundle, item: BundleItem) -> bytes:
    if (not isinstance(item.name, str) or not item.name
            or Path(item.name).is_absolute() or ".." in Path(item.name).parts
            or item.path != bundle.root / item.name
            or isinstance(item.size_bytes, bool) or not isinstance(item.size_bytes, int)
            or item.size_bytes < 0 or item.size_bytes > _MAX_ITEM_BYTES):
        raise DeliveryError("BUNDLE_ITEM_INVALID", "Bundle item path or size is invalid")
    try:
        descriptor = open_private_file(item.path)
    except (OSError, UnsafePathError):
        raise DeliveryError("BUNDLE_ITEM_UNSAFE", "Bundle item is not a private regular file") from None
    try:
        content = bytearray()
        while len(content) <= item.size_bytes:
            chunk = os.read(
                descriptor, min(65_536, item.size_bytes + 1 - len(content))
            )
            if not chunk:
                break
            content.extend(chunk)
        if (len(content) != item.size_bytes
                or hashlib.sha256(content).hexdigest() != item.sha256):
            raise DeliveryError("BUNDLE_ITEM_CHANGED", "Bundle item differs from frozen hash")
        return bytes(content)
    finally:
        os.close(descriptor)


def _load_json(raw: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError):
        raise DeliveryError("BUNDLE_MANIFEST_INVALID", f"{name} is not JSON") from None
    if not isinstance(value, dict):
        raise DeliveryError("BUNDLE_MANIFEST_INVALID", f"{name} is not an object")
    return value


def _bundle_preflight(bundle: DeliveryBundle) -> tuple[dict[str, Any], dict[str, Any],
                                                       tuple[_ItemCheck, ...]]:
    if not 6 <= len(bundle.items) <= _MAX_ITEMS:
        raise DeliveryError("BUNDLE_ITEM_SET_INVALID", "Bundle item count is invalid")
    names = [item.name for item in bundle.items]
    required = {"operation-manifest.json", "run-manifest.json", "input-manifest.json",
                "frozen-command.json", "report.md", "bundle-index.json"}
    if len(names) != len(set(names)) or not required <= set(names):
        raise DeliveryError("BUNDLE_ITEM_SET_INVALID", "Bundle is missing required items")
    checked: list[_ItemCheck] = []
    content_by_name: dict[str, bytes] = {}
    for item in bundle.items:
        raw = _read_item(bundle, item)
        content_by_name[item.name] = raw
        display_name = (f"bundle-index-{bundle.delivery_request_id}.json"
                        if item.name == "bundle-index.json" else Path(item.name).name)
        checked.append(_ItemCheck(
            item, hashlib.md5(raw, usedforsecurity=False).hexdigest(), display_name,
        ))
    operation = _load_json(content_by_name["operation-manifest.json"], "Operation manifest")
    run = _load_json(content_by_name["run-manifest.json"], "Run manifest")
    input_manifest = _load_json(content_by_name["input-manifest.json"], "Input manifest")
    command_view = _load_json(content_by_name["frozen-command.json"], "Command delivery view")
    index = _load_json(content_by_name["bundle-index.json"], "Bundle index")
    if (not _OPERATION.is_valid(operation) or not _RUN.is_valid(run)
            or operation["request_id"] != bundle.source_request_id
            or run["request_id"] != bundle.source_request_id
            or run["run_id"] != bundle.run_id
            or operation["project_id"] != run["project_id"]
            or operation["workspace_id"] != run["workspace_id"]
            or input_manifest.get("project_id") != run["project_id"]
            or input_manifest.get("workspace_id") != run["workspace_id"]
            or input_manifest.get("commit") != run["snapshot"]["commit"]
            or input_manifest.get("tree") != run["snapshot"]["tree"]
            or command_view.get("kind") != "redacted-command/v1"
            or command_view.get("redacted_fields") != ["intent"]
            or not isinstance(command_view.get("command"), dict)
            or command_view["command"].get("intent") != "[REDACTED FOR DELIVERY]"
            or command_view["command"].get("request_id") != bundle.source_request_id
            or command_view["command"].get("project_id") != run["project_id"]
            or command_view["command"].get("workspace_id") != run["workspace_id"]
            or bundle.run_id not in operation["run_ids"]
            or index.get("source_request_id") != bundle.source_request_id
            or index.get("delivery_request_id") != bundle.delivery_request_id
            or index.get("run_id") != bundle.run_id
            or index.get("tested_commit") != run["snapshot"]["commit"]
            or index.get("items") != [
                {"name": item.name, "media_type": item.media_type,
                 "size_bytes": item.size_bytes, "sha256": item.sha256,
                 "source_output_id": item.source_output_id}
                for item in bundle.items if item.name != "bundle-index.json"
            ]):
        raise DeliveryError("BUNDLE_MANIFEST_MISMATCH", "Bundle identities or index differ")
    return operation, run, tuple(checked)


def _json_doc(header: str, payload: Mapping[str, Any]) -> str:
    return header + json.dumps(payload, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False) + "\n"


def _is_newer_latest(observed: ObservedOutputDoc, run: Mapping[str, Any],
                     delivery_request_id: str, delivery_sequence: int) -> bool:
    text = observed.snapshot.text
    if text in {"\n", "DRAFT\n"}:
        return False
    if not text.startswith(_LATEST_HEADER):
        raise DeliveryError("LATEST_RESULT_UNRECOGNIZED",
                            "Registered latest-result pointer is not a known draft or result")
    try:
        prior = json.loads(text[len(_LATEST_HEADER):])
        if not isinstance(prior, dict):
            raise TypeError
        previous_time = datetime.fromisoformat(
            prior["run_created_at"].replace("Z", "+00:00")
        )
        new_time = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if previous_time.tzinfo is None or new_time.tzinfo is None:
            raise ValueError
        if prior.get("run_id") == run["run_id"]:
            previous_sequence = prior.get("delivery_sequence")
            if type(previous_sequence) is not int or previous_sequence < 1:
                raise ValueError
            if previous_sequence == delivery_sequence:
                if prior.get("delivery_request_id") != delivery_request_id:
                    raise DeliveryError("LATEST_ORDER_AMBIGUOUS",
                                        "Distinct deliveries have equal sequence")
                return False
            return previous_sequence > delivery_sequence
        previous_utc = previous_time.astimezone(timezone.utc)
        new_utc = new_time.astimezone(timezone.utc)
        if previous_utc == new_utc:
            raise DeliveryError("LATEST_ORDER_AMBIGUOUS", "Distinct runs have equal timestamps")
        return previous_utc > new_utc
    except DeliveryError:
        raise
    except (TypeError, KeyError, ValueError, AttributeError):
        raise DeliveryError("LATEST_RESULT_UNPARSEABLE", "Registered latest-result pointer is malformed") from None


def _asset_key(name: str) -> str:
    return "asset:" + hashlib.sha256(name.encode("utf-8")).hexdigest()


def _publication_index(bundle: DeliveryBundle, run: Mapping[str, Any],
                       request_folder_id: str, run_folder_id: str,
                       assets: list[dict[str, Any]]) -> _ItemCheck:
    """Keep the remote-ID commit marker locally for reconciliation and audit."""
    payload = {
        "schema_version": "1.0", "source_request_id": bundle.source_request_id,
        "delivery_request_id": bundle.delivery_request_id, "run_id": bundle.run_id,
        "tested_commit": run["snapshot"]["commit"],
        "request_folder_id": request_folder_id, "run_folder_id": run_folder_id,
        "assets": assets, "quarantined_output_ids": list(bundle.quarantined_output_ids),
    }
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > _MAX_ITEM_BYTES:
        raise DeliveryError("DELIVERY_INDEX_TOO_LARGE", "Remote index exceeds upload limit")
    directory = bundle.root.parent / "publication-indices"
    try:
        ensure_private_directory(directory)
        name = f"delivery-index-{bundle.delivery_request_id}.json"
        path = directory / name
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                                 | os.O_CLOEXEC, 0o600)
        except FileExistsError:
            # A repeated same-operation call can only reuse the exact local index.
            descriptor = open_private_file(path)
            try:
                existing = os.read(descriptor, len(raw) + 1)
                if existing != raw or os.read(descriptor, 1):
                    raise DeliveryError("DELIVERY_INDEX_CHANGED", "Retained remote index differs")
            finally:
                os.close(descriptor)
        else:
            try:
                os.fchmod(descriptor, 0o600)
                offset = 0
                while offset < len(raw):
                    count = os.write(descriptor, raw[offset:])
                    if count <= 0:
                        raise OSError("Index write made no progress")
                    offset += count
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except (OSError, UnsafePathError):
        raise DeliveryError("DELIVERY_INDEX_LOCAL_FAILED", "Cannot retain remote index") from None
    item = BundleItem(name, "application/json", path, hashlib.sha256(raw).hexdigest(), len(raw))
    return _ItemCheck(item, hashlib.md5(raw, usedforsecurity=False).hexdigest(), name)


def _saved_partial_report_status(
    preflight: DeliveryPreflight, operation: Mapping[str, Any],
    source: Mapping[str, Any],
) -> bool:
    """Permit only the pre-delivery manifest frozen in a failed full-route result.

    The terminal source status may become BLOCKED or FAULT after Drive delivery
    failed, while its already frozen operation manifest still says COMPLETED.
    The ledger preflight pins that saved manifest's canonical hash. A report
    retry must upload those exact bytes rather than rewrite history.
    """
    result = source.get("result")
    command = source.get("command")
    if not isinstance(result, dict) or not isinstance(command, dict):
        return False
    delivery = result.get("delivery")
    manifest = result.get("operation_manifest")
    return (
        preflight.action == "PUBLISH_REPORT"
        and preflight.source_action == "DELIVER_VERIFY_PUBLISH"
        and preflight.source_operation_manifest_sha256 is not None
        and source.get("action") == "DELIVER_VERIFY_PUBLISH"
        and command.get("action") == source.get("action")
        and source.get("request_id") == preflight.source_request_id
        and preflight.source_status == source.get("status")
        and source.get("status") in {"BLOCKED", "FAULT", "CANCELLED"}
        and source.get("delivery_status") in {"PENDING", "PARTIAL", "FAILED"}
        and result.get("run_id") == preflight.run_id
        and isinstance(delivery, dict)
        and delivery.get("status") == source.get("delivery_status")
        and isinstance(manifest, dict)
        and manifest == operation
        and operation.get("status") in {"COMPLETED", "BLOCKED"}
        and operation.get("request_id") == preflight.source_request_id
        and operation.get("project_id") == preflight.project_id
        and operation.get("workspace_id") == preflight.workspace_id
        and preflight.run_id in operation.get("run_ids", [])
    )


class DriveDeliveryPublisher:
    """Publish once under a live admitted operation; no automatic write retries."""

    def __init__(self, ledger: DeliveryLedger, upload: UploadProvider,
                 attached: AttachedInstallation, writer: DocsCASWriter,
                 workspace_registry: WorkspaceRegistry) -> None:
        self.ledger = ledger
        self.upload = upload
        self.attached = attached
        self.writer = writer
        self.workspace_registry = workspace_registry
        self._lock = threading.RLock()

    def _existing_effect(self, request_id: str, step_id: str, intent: dict[str, Any]
                         ) -> Mapping[str, Any] | None:
        operation = self.ledger.get(request_id)
        if operation is None or operation["status"] != "RUNNING":
            raise DeliveryError("DELIVERY_ACTOR_STOPPED", "Delivery operation is no longer running")
        effects = [
            item for item in operation["effects"]
            if item["step_id"] == step_id and item["kind"] == "drive_file"
        ]
        if len(effects) > 1:
            raise DeliveryError("DELIVERY_EFFECT_DUPLICATED", "Delivery effect is duplicated")
        if not effects:
            return None
        effect = effects[0]
        if effect["intent"] != intent:
            raise DeliveryError("DELIVERY_INTENT_CHANGED", "Delivery effect intent differs")
        if effect["state"] != "CONFIRMED":
            raise DeliveryError("DELIVERY_NEEDS_RECONCILIATION", "Previous write outcome is uncertain")
        return dict(effect)

    def _verify_chain(self, source_request_id: str, run_id: str,
                      request_folder_id: str | None = None,
                      run_folder_id: str | None = None) -> None:
        """Recheck the indexed Results topology around non-transactional writes."""
        installation = self.attached.index.installation_id
        results_id = self.attached.index.files["results"].file_id
        self.upload.get_file(
            results_id,
            expected_parent_id=self.attached.index.files["root"].file_id,
            expected_installation_id=installation, expected_key="results",
            expected_mime_type="application/vnd.google-apps.folder",
            expected_name="02 Results",
        )
        if request_folder_id is not None:
            self.upload.get_file(
                request_folder_id, expected_parent_id=results_id,
                expected_installation_id=installation,
                expected_key=f"result-request:{source_request_id}",
                expected_mime_type="application/vnd.google-apps.folder",
                expected_name=source_request_id,
            )
        if run_folder_id is not None:
            if request_folder_id is None:
                raise DeliveryError("DELIVERY_CHAIN_INVALID", "Run folder has no request parent")
            self.upload.get_file(
                run_folder_id, expected_parent_id=request_folder_id,
                expected_installation_id=installation,
                expected_key=f"result-run:{run_id}",
                expected_mime_type="application/vnd.google-apps.folder",
                expected_name=run_id,
            )

    def _folder(self, request_id: str, parent_id: str, key: str, name: str,
                step_id: str) -> str:
        installation = self.attached.index.installation_id
        mime = "application/vnd.google-apps.folder"
        intent = {"parent_id": parent_id, "installation_id": installation,
                  "key": key, "name": name, "mime_type": mime}
        previous = self._existing_effect(request_id, step_id, intent)
        expected = {
            "expected_parent_id": parent_id, "expected_installation_id": installation,
            "expected_key": key, "expected_mime_type": mime, "expected_name": name,
        }
        if previous is not None:
            remote = self.upload.get_file(previous["provider_id"], **expected)
            return str(remote["id"])
        existing = self.upload.find_by_key(
            parent_id, installation, key, expected_mime_type=mime, expected_name=name,
        )
        effect_id = self.ledger.begin_effect(request_id, step_id, "drive_file", intent)
        remote = existing or self.upload.create_folder(parent_id, installation, key, name)
        verified = self.upload.get_file(str(remote["id"]), **expected)
        self.ledger.confirm_effect(effect_id, str(verified["id"]))
        return str(verified["id"])

    def _asset(self, request_id: str, parent_id: str, check: _ItemCheck,
               logical_name: str) -> str:
        item = check.item
        installation = self.attached.index.installation_id
        key = _asset_key(logical_name)
        step_id = "drive-asset:" + hashlib.sha256(logical_name.encode("utf-8")).hexdigest()
        mime = item.media_type.split(";", 1)[0]
        intent = {
            "parent_id": parent_id, "installation_id": installation, "key": key,
            "name": check.display_name, "mime_type": mime,
            "sha256": item.sha256, "size_bytes": item.size_bytes, "md5": check.md5,
        }
        previous = self._existing_effect(request_id, step_id, intent)
        expected = {
            "expected_parent_id": parent_id, "expected_installation_id": installation,
            "expected_key": key, "expected_mime_type": mime, "expected_name": check.display_name,
            "expected_sha256": item.sha256, "expected_size_bytes": item.size_bytes,
            "expected_md5": check.md5,
        }
        if previous is not None:
            remote = self.upload.get_file(previous["provider_id"], **expected)
            return str(remote["id"])
        existing = self.upload.find_by_key(
            parent_id, installation, key, expected_mime_type=mime,
            expected_name=check.display_name, expected_sha256=item.sha256,
            expected_size_bytes=item.size_bytes, expected_md5=check.md5,
        )
        effect_id = self.ledger.begin_effect(request_id, step_id, "drive_file", intent)
        remote = existing or self.upload.upload_file(
            parent_id, installation, key, check.display_name, item.path,
            item.media_type, item.size_bytes, item.sha256,
        )
        verified = self.upload.get_file(str(remote["id"]), **expected)
        self.ledger.confirm_effect(effect_id, str(verified["id"]))
        return str(verified["id"])

    def _doc(self, request_id: str, key: str, text: str,
             expected: ObservedOutputDoc) -> None:
        desired_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        step = "drive-doc:" + hashlib.sha256(key.encode("utf-8")).hexdigest()
        intent = {
            "file_id": expected.snapshot.file_id, "key": key,
            "base_revision": expected.snapshot.revision_id, "desired_sha256": desired_hash,
        }
        previous = self._existing_effect(request_id, step, intent)
        if previous is not None:
            current = self.writer.read(key)
            if (current.snapshot.file_id != previous["provider_id"]
                    or current.snapshot.sha256 != desired_hash):
                raise DeliveryError("DELIVERY_DOC_CHANGED", "Confirmed output Doc changed")
            return
        effect_id = self.ledger.begin_effect(request_id, step, "drive_file", intent)
        result = self.writer.replace(key, text, expected=expected)
        if result.state == "UNCERTAIN":
            raise DeliveryError("DELIVERY_DOC_UNCERTAIN", "Output Doc write needs reconciliation")
        if result.state not in {"CONFIRMED", "UNCHANGED"}:
            raise DeliveryError("DELIVERY_DOC_UNCERTAIN", "Output Doc write needs reconciliation")
        self.ledger.confirm_effect(effect_id, result.file_id)

    def reconcile_effect(
        self, request_id: str, effect_id: str, *,
        receipt_key: str, latest_result_key: str,
    ) -> ReconcileResult:
        """Confirm a lost-ack Drive effect only after positive exact-ID readback.

        A negative lookup never proves that a create did not happen; it leaves the
        effect unresolved and never makes another write. The caller separately
        proves any interrupted worker is gone before releasing its ledger lock.
        """
        with self._lock:
            operation = self.ledger.get(request_id)
            if operation is None or operation["status"] == "RUNNING":
                raise DeliveryError("RECONCILIATION_NOT_READY",
                                    "Stopped delivery operation is required")
            effects = [item for item in operation["effects"] if item["effect_id"] == effect_id]
            if len(effects) != 1 or effects[0]["kind"] != "drive_file":
                raise DeliveryError("RECONCILIATION_EFFECT_INVALID", "Drive effect is unavailable")
            effect = effects[0]
            intent = effect["intent"]
            if not isinstance(intent, dict):
                raise DeliveryError("RECONCILIATION_EFFECT_INVALID", "Drive intent is malformed")
            if effect["state"] == "CONFIRMED":
                return ReconcileResult("CONFIRMED", effect_id, effect["provider_id"])
            if effect["state"] not in {"INTENDED", "UNKNOWN"}:
                raise DeliveryError("RECONCILIATION_EFFECT_INVALID", "Drive effect state is invalid")
            try:
                if "parent_id" in intent:
                    if intent.get("installation_id") != self.attached.index.installation_id:
                        raise DeliveryError("RECONCILIATION_INSTALLATION_MISMATCH",
                                            "Drive effect belongs to another installation")
                    expected: dict[str, Any] = {
                        "expected_mime_type": intent["mime_type"],
                        "expected_name": intent["name"],
                    }
                    for source, destination in (
                        ("sha256", "expected_sha256"),
                        ("size_bytes", "expected_size_bytes"),
                        ("md5", "expected_md5"),
                    ):
                        if source in intent:
                            expected[destination] = intent[source]
                    remote = self.upload.find_by_key(
                        intent["parent_id"], intent["installation_id"],
                        intent["key"], **expected,
                    )
                    if remote is None:
                        return ReconcileResult("UNRESOLVED", effect_id,
                                               code="REMOTE_EFFECT_NOT_OBSERVED")
                    provider_id = str(remote["id"])
                elif "file_id" in intent:
                    project_id = operation["project_id"]
                    workspace_id = operation["workspace_id"]
                    receipt = self.writer.registration(receipt_key)
                    latest = self.writer.registration(latest_result_key)
                    self.workspace_registry.require_output_pair(
                        project_id, workspace_id, receipt, latest,
                    )
                    if intent.get("key") not in {receipt_key, latest_result_key}:
                        raise DeliveryError("RECONCILIATION_DOC_SCOPE_INVALID",
                                            "Output Doc effect is outside this workspace")
                    observed = self.writer.read(intent["key"])
                    if (observed.snapshot.file_id != intent["file_id"]
                            or observed.snapshot.sha256 != intent.get("desired_sha256")):
                        return ReconcileResult("UNRESOLVED", effect_id,
                                               code="REMOTE_EFFECT_NOT_OBSERVED")
                    provider_id = observed.snapshot.file_id
                else:
                    raise DeliveryError("RECONCILIATION_EFFECT_INVALID", "Drive intent is malformed")
            except DriveError as error:
                return ReconcileResult("UNRESOLVED", effect_id, code=error.code)
            self.ledger.confirm_effect(effect_id, provider_id)
            return ReconcileResult("CONFIRMED", effect_id, provider_id)

    def publish(self, bundle: DeliveryBundle, *, receipt_key: str,
                latest_result_key: str) -> DeliveryResult:
        """Attempt a finite plan; return pending/partial on remote uncertainty.

        Calling this method again with the same request after an uncertain effect
        cannot replay that write. A new PUBLISH_REPORT operation may reuse confirmed
        files from the same stored run after any old uncertainty is reconciled.
        """
        with self._lock:
            preflight = self.ledger.require_delivery_preflight(
                bundle.delivery_request_id, bundle.run_id,
            )
            operation, run, checked = _bundle_preflight(bundle)
            item_by_name = {item.item.name: item.item for item in checked}
            if (item_by_name["run-manifest.json"].sha256 != preflight.run_manifest_sha256
                    or item_by_name["input-manifest.json"].sha256
                    != preflight.input_manifest_sha256
                    or (preflight.source_operation_manifest_sha256 is not None
                        and item_by_name["operation-manifest.json"].sha256
                        != preflight.source_operation_manifest_sha256)):
                raise DeliveryError("DELIVERY_DURABLE_MANIFEST_MISMATCH",
                                    "Bundle differs from durable local manifests")
            source = self.ledger.get(bundle.source_request_id)
            command_view = _load_json(
                _read_item(bundle, item_by_name["frozen-command.json"]), "Command delivery view",
            )
            if (source is None or source["project_id"] != run["project_id"]
                    or source["workspace_id"] != run["workspace_id"]
                    or command_view.get("request_digest") != source["digest"]
                    or command_view.get("command") != {
                        **source["command"], "intent": "[REDACTED FOR DELIVERY]",
                    }
                    or (source["status"] != "RUNNING"
                        and operation["status"] != source["status"]
                        and not _saved_partial_report_status(preflight, operation, source))):
                raise DeliveryError("DELIVERY_SOURCE_CHANGED", "Source operation differs from bundle")
            receipt = self.writer.registration(receipt_key)
            latest = self.writer.registration(latest_result_key)
            self.workspace_registry.require_output_pair(
                run["project_id"], run["workspace_id"], receipt, latest,
            )
            actor = self.ledger.get(bundle.delivery_request_id)
            delivery_sequence = actor.get("sequence") if actor is not None else None
            if type(delivery_sequence) is not int or delivery_sequence < 1:
                raise DeliveryError("DELIVERY_SEQUENCE_MISSING",
                                    "Admitted delivery sequence is unavailable")
            # Read both Docs before the first external write. CAS still rechecks on write.
            latest_observed = self.writer.read(latest_result_key)
            receipt_observed = self.writer.read(receipt_key)
            newer = _is_newer_latest(
                latest_observed, run, bundle.delivery_request_id, delivery_sequence,
            )
            results_id = self.attached.index.files["results"].file_id
            self._verify_chain(bundle.source_request_id, bundle.run_id)
            request_folder: str | None = None
            run_folder: str | None = None
            confirmed: list[str] = []
            index_id: str | None = None
            self.ledger.record_delivery(bundle.delivery_request_id, "PENDING")
            try:
                request_folder = self._folder(
                    bundle.delivery_request_id, results_id,
                    f"result-request:{bundle.source_request_id}",
                    bundle.source_request_id, "drive-request-folder",
                )
                self._verify_chain(bundle.source_request_id, bundle.run_id, request_folder)
                run_folder = self._folder(
                    bundle.delivery_request_id, request_folder,
                    f"result-run:{bundle.run_id}", bundle.run_id, "drive-run-folder",
                )
                self._verify_chain(bundle.source_request_id, bundle.run_id,
                                   request_folder, run_folder)
                # Bundle index records local bytes. The final publication index
                # adds exact verified Drive IDs and commits the remote bundle.
                ordered = [item for item in checked if item.item.name != "bundle-index.json"]
                ordered.extend(item for item in checked if item.item.name == "bundle-index.json")
                assets: list[dict[str, Any]] = []
                for check in ordered:
                    target = (request_folder if check.item.name in {
                        "operation-manifest.json", "frozen-command.json",
                    } else run_folder)
                    logical = (
                        f"{bundle.source_request_id}/{bundle.run_id}/{check.item.name}"
                        if check.item.name != "bundle-index.json" else
                        f"{bundle.source_request_id}/{bundle.run_id}/index/{bundle.delivery_request_id}"
                    )
                    remote_id = self._asset(bundle.delivery_request_id, target, check, logical)
                    self._verify_chain(bundle.source_request_id, bundle.run_id,
                                       request_folder, run_folder)
                    assets.append({
                        "bundle_name": check.item.name, "drive_file_id": remote_id,
                        "display_name": check.display_name, "sha256": check.item.sha256,
                        "size_bytes": check.item.size_bytes,
                        "media_type": check.item.media_type.split(";", 1)[0],
                    })
                    confirmed.append(check.item.name)
                publication_index = _publication_index(
                    bundle, run, request_folder, run_folder, assets,
                )
                index_id = self._asset(
                    bundle.delivery_request_id, run_folder, publication_index,
                    f"{bundle.source_request_id}/{bundle.run_id}/publication-index/"
                    f"{bundle.delivery_request_id}",
                )
                confirmed.append(publication_index.item.name)
                self._verify_chain(bundle.source_request_id, bundle.run_id,
                                   request_folder, run_folder)
                folder_url = f"https://drive.google.com/drive/folders/{run_folder}"
                report_payload = {
                    "schema_version": "1.0", "source_request_id": bundle.source_request_id,
                    "delivery_request_id": bundle.delivery_request_id,
                    "delivery_sequence": delivery_sequence,
                    "run_id": bundle.run_id, "run_created_at": run["created_at"],
                    "tested_commit": run["snapshot"]["commit"],
                    "execution_outcome": run["execution"]["outcome"],
                    "run_folder_id": run_folder, "run_folder_url": folder_url,
                    "index_file_id": index_id,
                    "quarantined_output_ids": list(bundle.quarantined_output_ids),
                }
                if not newer:
                    self._doc(
                        bundle.delivery_request_id, latest_result_key,
                        _json_doc(_LATEST_HEADER, report_payload), latest_observed,
                    )
                receipt_payload = {
                    "schema_version": "1.0", "request_id": bundle.delivery_request_id,
                    "delivery_sequence": delivery_sequence,
                    "source_request_id": bundle.source_request_id, "run_id": bundle.run_id,
                    "delivery_status": "DELIVERED", "run_folder_id": run_folder,
                    "index_file_id": index_id,
                    "latest_skipped_newer": newer,
                }
                self._doc(
                    bundle.delivery_request_id, receipt_key,
                    _json_doc(_RECEIPT_HEADER, receipt_payload), receipt_observed,
                )
                self._verify_chain(bundle.source_request_id, bundle.run_id,
                                   request_folder, run_folder)
            except (DriveError, DeliveryError, OSError) as error:
                code = getattr(error, "code", "DELIVERY_IO_FAILED")
                status: DeliveryStatus = (
                    "PARTIAL" if confirmed or request_folder else "PENDING"
                )
                self.ledger.record_delivery(bundle.delivery_request_id, status, code)
                return DeliveryResult(
                    status, code, request_folder, run_folder, tuple(confirmed), newer, index_id,
                )
            self.ledger.record_delivery(bundle.delivery_request_id, "DELIVERED")
            return DeliveryResult(
                "DELIVERED", None, request_folder, run_folder, tuple(confirmed), newer, index_id,
            )
