"""Journaled root receipt for a Drive-originated CREATE_PROJECT command.

The existing installation supplies both document IDs. A local UI command is
inert here; a Drive command must still be READY with the exact admitted digest.
No command document is written, and an uncertain receipt write is never replayed.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from bridge_reference.protocol import Rejected, validate_branch
from runner_app.adapters.drive_docs import (
    FILE_FIELDS,
    FrozenDriveInput,
    ReadProvider,
    read_ready_snapshot,
)
from runner_app.adapters.drive_index import AttachedInstallation
from runner_app.adapters.drive_poller import FrozenSlotStore
from runner_app.adapters.drive_writer import DocsCASWriter, RegisteredOutputDoc, WriteResult
from runner_app.domain.commands import Provenance
from runner_app.storage.ledger import (
    Ledger,
    LedgerError,
    confirmed_create_effects_proof,
)

_ID = re.compile(r"[A-Za-z0-9_-]{8,256}\Z")
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
_REPOSITORY_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})\Z"
)
_STATUSES = frozenset({"COMPLETED", "BLOCKED", "FAULT", "INTERRUPTED", "CANCELLED"})
_DRIVE_FIELDS = (
    "project_folder_id", "workspace_folder_id", "project_state_doc_id",
    "project_command_doc_id", "project_receipt_doc_id", "context_doc_id",
    "command_doc_id", "changeset_doc_id", "receipt_doc_id",
    "latest_result_doc_id",
)
_STEP_ID = "drive-root-create-receipt"
_PRE_DISPATCH_CODE = "INTERRUPTED_BEFORE_EXECUTION"
_PARTIAL_CODE = "PROVISIONING_PARTIAL"
_MAX_MESSAGE_BYTES = 4096
_MAX_RECEIPT_BYTES = 8192


class RootReceiptError(ValueError):
    """Stable finding with no provider response or command intent."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _CommandIdentityReader:
    """Validate the owner-manifest COMMAND on snapshot's existing metadata reads."""

    def __init__(self, delegate: ReadProvider, command_id: str,
                 validate: Callable[[Mapping[str, Any]], None]) -> None:
        self._delegate = delegate
        self._command_id = command_id
        self._validate = validate

    def get_file(self, file_id: str, *, fields: str = FILE_FIELDS) -> Mapping[str, Any]:
        metadata = self._delegate.get_file(file_id, fields=fields)
        if file_id == self._command_id:
            self._validate(metadata)
        return metadata

    def get_document(self, file_id: str, *, include_tabs_content: bool = True
                     ) -> Mapping[str, Any]:
        return self._delegate.get_document(
            file_id, include_tabs_content=include_tabs_content,
        )


@dataclass(frozen=True)
class RootPartialReceiptProof:
    request_id: str
    request_digest: str
    receipt_doc_id: str
    receipt_sha256: str
    confirmed_effect_count: int
    confirmed_effects_sha256: str


def _mapping(value: object) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise RootReceiptError("ROOT_RECEIPT_RESOURCES_INVALID")
    return value


def _summary(resources: Mapping[str, Any] | None, command: Mapping[str, Any],
             status: str, *, trusted_github_owner: str) -> dict[str, str]:
    """Select only fixed, bounded identifiers; ignore all free-form state."""
    source = resources or {}
    project = command["project_id"]
    template = command["params"]["template_id"]
    if (source.get("project_id", project) != project
            or source.get("workspace_id", "primary") != "primary"
            or source.get("template_id", template) != template):
        raise RootReceiptError("ROOT_RECEIPT_RESOURCES_MISMATCH")
    result = {
        "project_id": project, "workspace_id": "primary", "template_id": template,
    }
    profile = source.get("profile_id")
    if profile is not None:
        if not isinstance(profile, str) or _SLUG.fullmatch(profile) is None:
            raise RootReceiptError("ROOT_RECEIPT_RESOURCES_INVALID")
        result["profile_id"] = profile

    github = _mapping(source.get("github"))
    command_source = command["params"]["source"]
    source_kind = command_source["kind"]
    expected_name = project
    expected_owner = trusted_github_owner
    if source_kind == "github":
        expected_owner, expected_name = command_source["repository"].split("/", 1)
    if github is not None:
        repository_id, url = github.get("repository_id"), github.get("html_url")
        owner, name = github.get("owner"), github.get("name")
        match = _REPOSITORY_URL.fullmatch(url) if isinstance(url, str) else None
        if (type(repository_id) is not int or repository_id < 1 or match is None
                or not isinstance(owner, str) or not isinstance(name, str)
                or github.get("full_name") != f"{owner}/{name}"
                or match.group(1) != owner or match.group(2) != name
                or name.casefold() != expected_name.casefold()
                or owner.casefold() != expected_owner.casefold()
                or (source_kind == "github"
                    and f"{owner}/{name}" != command_source["repository"])
                or github.get("private") is not True):
            raise RootReceiptError("ROOT_RECEIPT_RESOURCES_INVALID")
        result["repository_id"] = str(repository_id)
        result["repository_url"] = url
        if source_kind == "local":
            source_branch = github.get("source_branch")
            if not isinstance(source_branch, str):
                raise RootReceiptError("ROOT_RECEIPT_RESOURCES_INVALID")
            try:
                validate_branch(source_branch)
            except Rejected:
                raise RootReceiptError("ROOT_RECEIPT_RESOURCES_INVALID") from None
            result["source_ref"] = f"refs/heads/{source_branch}"

    baseline = _mapping(source.get("baseline"))
    if baseline is not None:
        commit, ref = baseline.get("commit"), baseline.get("ref")
        if source_kind in {"new", "local"}:
            expected_ref = "refs/heads/main"
        elif source_kind == "github" and github is not None:
            branch = github.get("default_branch")
            if not isinstance(branch, str):
                raise RootReceiptError("ROOT_RECEIPT_RESOURCES_INVALID")
            try:
                validate_branch(branch)
            except Rejected:
                raise RootReceiptError("ROOT_RECEIPT_RESOURCES_INVALID") from None
            expected_ref = f"refs/heads/{branch}"
        else:
            raise RootReceiptError("ROOT_RECEIPT_RESOURCES_INVALID")
        if (ref != expected_ref or not isinstance(commit, str)
                or _COMMIT.fullmatch(commit) is None
                or (source_kind == "local"
                    and baseline.get("source_ref") != result.get("source_ref"))):
            raise RootReceiptError("ROOT_RECEIPT_RESOURCES_INVALID")
        result["baseline_ref"] = ref
        result["baseline_commit"] = commit

    workspace = _mapping(source.get("workspace"))
    if workspace is not None:
        branch, head = workspace.get("branch"), workspace.get("head")
        if (branch != f"work/{project}" or not isinstance(head, str)
                or _COMMIT.fullmatch(head) is None):
            raise RootReceiptError("ROOT_RECEIPT_RESOURCES_INVALID")
        result["workspace_branch"] = branch
        result["workspace_head"] = head

    drive = _mapping(source.get("drive"))
    if drive is not None:
        seen: set[str] = set()
        for key in _DRIVE_FIELDS:
            value = drive.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or _ID.fullmatch(value) is None or value in seen:
                raise RootReceiptError("ROOT_RECEIPT_RESOURCES_INVALID")
            seen.add(value)
            result[key] = value

    if status == "COMPLETED":
        required = {
            "profile_id", "repository_id", "repository_url",
            "baseline_ref", "baseline_commit",
            "workspace_branch", "workspace_head", *_DRIVE_FIELDS,
        }
        if (not required <= result.keys()
                or (source_kind == "local" and "source_ref" not in result)
                or result["baseline_commit"] != result["workspace_head"]
                or source.get("project_state") != "READY"):
            raise RootReceiptError("ROOT_RECEIPT_RESOURCES_INCOMPLETE")
    return result


def _text(request_id: str, digest: str, status: str, code: str | None,
          observed_at: str, summary: Mapping[str, str]) -> str:
    heading = ("CREATE_PROJECT INTERRUPTED/PARTIAL"
               if status == "INTERRUPTED" and code == _PARTIAL_CODE
               else f"CREATE_PROJECT {status}")
    message = heading + "; " + "; ".join(
        f"{key}={value}" for key, value in summary.items()
    )
    if status == "INTERRUPTED" and code == _PRE_DISPATCH_CODE:
        message += "; execution never started; no project effects; submit a fresh request_id"
    if status == "INTERRUPTED" and code == _PARTIAL_CODE:
        message += "; completion unverified; inspect original request before resume"
    if len(message.encode("utf-8")) > _MAX_MESSAGE_BYTES:
        raise RootReceiptError("ROOT_RECEIPT_TOO_LARGE")
    receipt = {
        "schema_version": "1.0", "request_id": request_id,
        "request_digest": digest, "status": status, "code": code,
        "message": message, "observed_at": observed_at, "run_id": None,
    }
    value = json.dumps(receipt, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":")) + "\n"
    if len(value.encode("utf-8")) > _MAX_RECEIPT_BYTES:
        raise RootReceiptError("ROOT_RECEIPT_TOO_LARGE")
    return value


class RootCreateProjectReceiptPublisher:
    """Publish one root RECEIPT only for the current admitted Drive request."""

    def __init__(self, ledger: Ledger, attached: AttachedInstallation,
                 reader: ReadProvider, writer: DocsCASWriter, *,
                 trusted_github_owner: str) -> None:
        if (not isinstance(trusted_github_owner, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}",
                                 trusted_github_owner) is None):
            raise RootReceiptError("ROOT_RECEIPT_OWNER_INVALID")
        self.trusted_github_owner = trusted_github_owner
        self.ledger = ledger
        self.attached = attached
        self.reader = reader
        self.writer = writer
        files = attached.index.files
        command = files.get("command")
        control = files.get("control")
        receipt = files.get("receipt")
        if (command is None or control is None or receipt is None
                or command.parents != (control.file_id,)
                or receipt.parents != (control.file_id,)
                or command.name != "COMMAND" or receipt.name != "RECEIPT"
                or command.file_id == receipt.file_id):
            raise RootReceiptError("ROOT_RECEIPT_INSTALLATION_INVALID")
        self.command_id = command.file_id
        self.receipt_id = receipt.file_id
        self.control_id = control.file_id
        expected_output = RegisteredOutputDoc(
            "root-receipt", "RECEIPT", self.receipt_id, self.control_id, "receipt",
        )
        if writer.registration("root-receipt") != expected_output:
            raise RootReceiptError("ROOT_RECEIPT_WRITER_MISMATCH")

    def _validate_command_metadata(self, metadata: Mapping[str, Any]) -> None:
        if (not isinstance(metadata, Mapping)
                or metadata.get("id") != self.command_id
                or metadata.get("name") != "COMMAND"
                or metadata.get("mimeType") != "application/vnd.google-apps.document"
                or metadata.get("parents") != [self.control_id]
                or metadata.get("trashed") is not False
                or not isinstance(metadata.get("capabilities"), Mapping)
                or metadata["capabilities"].get("canEdit") is not True):
            raise RootReceiptError("ROOT_COMMAND_IDENTITY_CHANGED")
        props = metadata.get("appProperties")
        if self.attached.index.identity_mode == "app_properties":
            if (metadata.get("isAppAuthorized") is not True
                    or not isinstance(props, Mapping)
                    or props.get("cdr_installation") != self.attached.index.installation_id
                    or props.get("cdr_key") != "command"
                    or "cdr_project" in props or "cdr_workspace" in props):
                raise RootReceiptError("ROOT_COMMAND_IDENTITY_CHANGED")
        elif self.attached.index.identity_mode == "owner_manifest":
            if (not isinstance(metadata.get("isAppAuthorized"), bool)
                    or (props is not None and not isinstance(props, Mapping))
                    or (isinstance(props, Mapping)
                        and ("cdr_installation" in props or "cdr_key" in props)
                        and (props.get("cdr_installation") != self.attached.index.installation_id
                             or props.get("cdr_key") != "command"))):
                raise RootReceiptError("ROOT_COMMAND_IDENTITY_CHANGED")
        else:
            raise RootReceiptError("ROOT_RECEIPT_INSTALLATION_INVALID")

    def _current(self, operation: Mapping[str, Any], request_id: str) -> FrozenDriveInput:
        if self.attached.index.identity_mode == "owner_manifest":
            # The legacy snapshot already reads and validates COMMAND metadata
            # twice. Apply our exact receipt predicate to those same reads.
            reader: ReadProvider = _CommandIdentityReader(
                self.reader, self.command_id, self._validate_command_metadata,
            )
        else:
            try:
                metadata = self.reader.get_file(self.command_id, fields=FILE_FIELDS)
            except Exception:  # noqa: BLE001 - provider errors may contain credentials
                raise RootReceiptError("ROOT_COMMAND_READ_FAILED") from None
            self._validate_command_metadata(metadata)
            reader = self.reader
        try:
            frozen = read_ready_snapshot(reader, self.attached.root_mailbox())
        except RootReceiptError:
            raise
        except Exception:  # noqa: BLE001 - provider errors may contain credentials
            raise RootReceiptError("ROOT_COMMAND_READ_FAILED") from None
        if (frozen is None or frozen.command.command["action"] != "CREATE_PROJECT"
                or frozen.command.command["request_id"] != request_id
                or frozen.command.digest != operation["digest"]
                or frozen.command.command != operation["command"]):
            raise RootReceiptError("ROOT_COMMAND_MISMATCH")
        return frozen

    def _exact_receipt_readback(
        self, result: WriteResult | None, desired: str, desired_sha256: str,
    ) -> bool:
        # DocsCASWriter already read the exact ID (after CAS for CONFIRMED).
        # Reuse that observation; ambiguous outcomes get a fresh read.
        if (result is not None and result.state in {"CONFIRMED", "UNCHANGED"}
                and result.file_id == self.receipt_id
                and result.desired_sha256 == desired_sha256
                and result.observed_sha256 == desired_sha256
                and result.observed_text == desired):
            return True
        readback = self.writer.read("root-receipt")
        return (readback.snapshot.file_id == self.receipt_id
                and readback.snapshot.sha256 == desired_sha256
                and readback.snapshot.text == desired)

    def publish(self, request_id: str, *, status: str = "COMPLETED",
                code: str | None = None,
                resources: Mapping[str, Any] | None = None) -> str | None:
        """Return confirmed root RECEIPT ID, or None for a local UI operation.

        A lost write acknowledgement is reconciled only by exact-ID/hash readback.
        A subsequent call with an unresolved effect never attempts another write.
        """
        operation = self.ledger.get(request_id)
        if operation is None:
            raise RootReceiptError("ROOT_RECEIPT_OPERATION_MISSING")
        if operation["source_id"] != self.command_id:
            return None
        command = operation["command"]
        if (operation["status"] != "RUNNING" or command["action"] != "CREATE_PROJECT"
                or command["request_id"] != request_id
                or not self.ledger.holds_lock(request_id, f"repo:{command['project_id']}")
                or not self.ledger.holds_lock(request_id, "global:drive-installation")):
            raise RootReceiptError("ROOT_RECEIPT_NOT_RUNNING")
        if status not in _STATUSES or (code is not None and
                                       (not isinstance(code, str)
                                        or _CODE.fullmatch(code) is None)):
            raise RootReceiptError("ROOT_RECEIPT_STATUS_INVALID")
        if status == "COMPLETED" and code is not None:
            raise RootReceiptError("ROOT_RECEIPT_STATUS_INVALID")
        summary = _summary(
            resources, command, status,
            trusted_github_owner=self.trusted_github_owner,
        )
        if (status == "COMPLETED"
                and any(effect["state"] != "CONFIRMED"
                        for effect in operation["effects"] if effect["step_id"] != _STEP_ID)):
            raise RootReceiptError("ROOT_RECEIPT_EFFECTS_UNRESOLVED")
        self._current(operation, request_id)
        previous = [
            effect for effect in operation["effects"]
            if effect["step_id"] == _STEP_ID and effect["kind"] == "drive_file"
        ]
        if len(previous) > 1:
            raise RootReceiptError("ROOT_RECEIPT_EFFECT_DUPLICATED")
        core = {
            "command_doc_id": self.command_id, "receipt_doc_id": self.receipt_id,
            "request_digest": operation["digest"], "status": status,
            "code": code, "summary": summary,
        }
        if previous:
            effect = previous[0]
            intent = effect["intent"]
            if any(intent.get(key) != value for key, value in core.items()):
                raise RootReceiptError("ROOT_RECEIPT_INTENT_CHANGED")
            observed_at = intent.get("observed_at")
            if not isinstance(observed_at, str):
                raise RootReceiptError("ROOT_RECEIPT_EFFECT_INVALID")
            try:
                parsed_time = datetime.fromisoformat(observed_at)
            except ValueError:
                raise RootReceiptError("ROOT_RECEIPT_EFFECT_INVALID") from None
            if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
                raise RootReceiptError("ROOT_RECEIPT_EFFECT_INVALID")
        else:
            observed_at = datetime.now(timezone.utc).isoformat()
        desired = _text(request_id, operation["digest"], status, code,
                        observed_at, summary)
        desired_sha256 = hashlib.sha256(desired.encode("utf-8")).hexdigest()
        if previous and intent.get("desired_sha256") != desired_sha256:
            raise RootReceiptError("ROOT_RECEIPT_INTENT_CHANGED")
        try:
            observed = self.writer.read("root-receipt")
        except Exception:  # noqa: BLE001 - provider errors may contain credentials
            raise RootReceiptError("ROOT_RECEIPT_READ_FAILED") from None
        if observed.snapshot.file_id != self.receipt_id:
            raise RootReceiptError("ROOT_RECEIPT_WRITER_MISMATCH")
        if previous:
            if (observed.snapshot.sha256 != desired_sha256
                    or observed.snapshot.text != desired):
                raise RootReceiptError("ROOT_RECEIPT_WRITE_UNCERTAIN")
            if effect["state"] == "CONFIRMED":
                if effect["provider_id"] != self.receipt_id:
                    raise RootReceiptError("ROOT_RECEIPT_EFFECT_INVALID")
            elif effect["state"] in {"INTENDED", "UNKNOWN"}:
                self._current(operation, request_id)
                self.ledger.confirm_effect(effect["effect_id"], self.receipt_id)
            else:
                raise RootReceiptError("ROOT_RECEIPT_EFFECT_INVALID")
            return self.receipt_id
        if observed.snapshot.sha256 == desired_sha256:
            raise RootReceiptError("ROOT_RECEIPT_UNJOURNALED")
        try:
            existing = json.loads(observed.snapshot.text)
        except (ValueError, TypeError):
            existing = None
        if isinstance(existing, dict) and existing.get("request_id") == request_id:
            raise RootReceiptError("ROOT_RECEIPT_UNJOURNALED")
        intent = {
            **core, "observed_at": observed_at,
            "base_revision": observed.snapshot.revision_id,
            "desired_sha256": desired_sha256,
        }
        effect_id = self.ledger.begin_effect(request_id, _STEP_ID, "drive_file", intent)
        try:
            result = self.writer.replace("root-receipt", desired, expected=observed)
            exact_readback = self._exact_receipt_readback(result, desired, desired_sha256)
        except Exception:  # noqa: BLE001 - leave the effect pending for exact reconciliation
            raise RootReceiptError("ROOT_RECEIPT_WRITE_UNCERTAIN") from None
        if not exact_readback:
            raise RootReceiptError("ROOT_RECEIPT_WRITE_UNCERTAIN")
        self._current(operation, request_id)
        self.ledger.confirm_effect(effect_id, self.receipt_id)
        return self.receipt_id

    @staticmethod
    def _same_frozen_input(left: FrozenDriveInput, right: FrozenDriveInput) -> bool:
        return (
            left.command_doc_id == right.command_doc_id
            and left.command_revision == right.command_revision
            and left.command_text_sha256 == right.command_text_sha256
            and left.raw_command == right.raw_command
            and left.command.digest == right.command.digest
            and left.command.command == right.command.command
            and left.raw_changeset is None
            and right.raw_changeset is None
        )

    def recover_pre_dispatch(self, request_id: str, slots: FrozenSlotStore) -> str:
        """Close only a Drive-root CREATE interrupted before dispatch STARTED.

        The immutable local freeze and the current READY Doc must still be
        byte/revision-identical. A terminal receipt gets its own durable intent
        before one CAS; a prior uncertain intent is read back, never replayed.
        """
        operation = self.ledger.get(request_id)
        if operation is None or operation["source_id"] != self.command_id:
            raise RootReceiptError("ROOT_PRE_DISPATCH_TARGET_INVALID")
        command = operation["command"]
        if (operation["status"] != "INTERRUPTED"
                or command.get("action") != "CREATE_PROJECT"
                or command.get("request_id") != request_id
                or operation["digest"] != hashlib.sha256(
                    json.dumps(command, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8")
                ).hexdigest()):
            raise RootReceiptError("ROOT_PRE_DISPATCH_TARGET_INVALID")
        provenance = Provenance("drive", self.command_id)
        try:
            frozen = slots.load_frozen(request_id, provenance)
        except Exception:  # noqa: BLE001 - local slot errors stay behind a stable code
            raise RootReceiptError("ROOT_FROZEN_INPUT_INVALID") from None
        if (frozen.command.digest != operation["digest"]
                or frozen.command.command != command
                or frozen.command_doc_id != self.command_id):
            raise RootReceiptError("ROOT_FROZEN_INPUT_MISMATCH")
        current = self._current(operation, request_id)
        if not self._same_frozen_input(frozen, current):
            raise RootReceiptError("ROOT_FROZEN_INPUT_MISMATCH")

        summary = _summary(None, command, "INTERRUPTED",
                           trusted_github_owner=self.trusted_github_owner)
        previous = operation["effects"]
        if previous:
            if (len(previous) != 1 or previous[0]["step_id"] != _STEP_ID
                    or previous[0]["kind"] != "drive_file"):
                raise RootReceiptError("ROOT_PRE_DISPATCH_EFFECTS_PRESENT")
            effect = previous[0]
            intent = effect["intent"]
            core = {
                "command_doc_id": self.command_id,
                "receipt_doc_id": self.receipt_id,
                "request_digest": operation["digest"],
                "status": "INTERRUPTED",
                "code": _PRE_DISPATCH_CODE,
                "summary": summary,
                "frozen_command_revision": frozen.command_revision,
                "frozen_command_sha256": frozen.command_text_sha256,
            }
            if any(intent.get(key) != value for key, value in core.items()):
                raise RootReceiptError("ROOT_PRE_DISPATCH_INTENT_CHANGED")
            observed_at = intent.get("observed_at")
            if not isinstance(observed_at, str):
                raise RootReceiptError("ROOT_PRE_DISPATCH_INTENT_CHANGED")
            try:
                observed_time = datetime.fromisoformat(observed_at)
            except ValueError:
                raise RootReceiptError("ROOT_PRE_DISPATCH_INTENT_CHANGED") from None
            if observed_time.tzinfo is None or observed_time.utcoffset() is None:
                raise RootReceiptError("ROOT_PRE_DISPATCH_INTENT_CHANGED")
        else:
            observed_at = datetime.now(timezone.utc).isoformat()
            core = {
                "command_doc_id": self.command_id,
                "receipt_doc_id": self.receipt_id,
                "request_digest": operation["digest"],
                "status": "INTERRUPTED",
                "code": _PRE_DISPATCH_CODE,
                "summary": summary,
                "frozen_command_revision": frozen.command_revision,
                "frozen_command_sha256": frozen.command_text_sha256,
            }
        desired = _text(request_id, operation["digest"], "INTERRUPTED",
                        _PRE_DISPATCH_CODE, observed_at, summary)
        desired_sha256 = hashlib.sha256(desired.encode("utf-8")).hexdigest()
        if previous and intent.get("desired_sha256") != desired_sha256:
            raise RootReceiptError("ROOT_PRE_DISPATCH_INTENT_CHANGED")
        try:
            observed = self.writer.read("root-receipt")
        except Exception:  # noqa: BLE001 - provider details may contain credentials
            raise RootReceiptError("ROOT_RECEIPT_READ_FAILED") from None
        if observed.snapshot.file_id != self.receipt_id:
            raise RootReceiptError("ROOT_RECEIPT_WRITER_MISMATCH")

        if previous:
            if (observed.snapshot.sha256 != desired_sha256
                    or observed.snapshot.text != desired):
                raise RootReceiptError("ROOT_RECEIPT_WRITE_UNCERTAIN")
            effect_id = effect["effect_id"]
            if effect["state"] == "CONFIRMED":
                if effect["provider_id"] != self.receipt_id:
                    raise RootReceiptError("ROOT_RECEIPT_EFFECT_INVALID")
            elif effect["state"] in {"INTENDED", "UNKNOWN"}:
                self.ledger.confirm_effect(effect_id, self.receipt_id)
            else:
                raise RootReceiptError("ROOT_RECEIPT_EFFECT_INVALID")
        else:
            if observed.snapshot.sha256 == desired_sha256:
                raise RootReceiptError("ROOT_RECEIPT_UNJOURNALED")
            try:
                existing = json.loads(observed.snapshot.text)
            except (TypeError, ValueError):
                existing = None
            if isinstance(existing, dict) and existing.get("request_id") == request_id:
                raise RootReceiptError("ROOT_RECEIPT_UNJOURNALED")
            intent = {
                **core, "observed_at": observed_at,
                "base_revision": observed.snapshot.revision_id,
                "desired_sha256": desired_sha256,
            }
            try:
                effect_id = self.ledger.begin_pre_dispatch_root_receipt(
                    request_id, source_id=self.command_id,
                    digest=operation["digest"], intent=intent,
                )
            except LedgerError as error:
                raise RootReceiptError(error.code) from None
            # A lost acknowledgement can follow an applied CAS. Only an exact
            # subsequent read proves the output; this call never retries the CAS.
            try:
                result = self.writer.replace("root-receipt", desired, expected=observed)
            except Exception:  # noqa: BLE001 - readback decides the ambiguous outcome
                result = None
            try:
                exact_readback = self._exact_receipt_readback(result, desired, desired_sha256)
            except Exception:  # noqa: BLE001 - leave the intended effect unresolved
                raise RootReceiptError("ROOT_RECEIPT_WRITE_UNCERTAIN") from None
            if not exact_readback:
                raise RootReceiptError("ROOT_RECEIPT_WRITE_UNCERTAIN")
            self.ledger.confirm_effect(effect_id, self.receipt_id)

        # A changed READY Doc cannot be acknowledged as the frozen request.
        latest = self._current(operation, request_id)
        if not self._same_frozen_input(frozen, latest):
            raise RootReceiptError("ROOT_FROZEN_INPUT_MISMATCH")
        try:
            self.ledger.complete_pre_dispatch_root_receipt(
                request_id, source_id=self.command_id, digest=operation["digest"],
                receipt_doc_id=self.receipt_id, effect_id=effect_id,
            )
        except LedgerError as error:
            raise RootReceiptError(error.code) from None
        return self.receipt_id


    def recover_terminal_partial(self, request_id: str, slots: FrozenSlotStore) -> str:
        """Publish the original root receipt after a stopped partial CREATE.

        This route never resumes provisioning. All prior non-receipt effects
        must already be confirmed, with no held lock/reconciliation requirement.
        A root receipt CAS is journaled once; retries only use exact readback.
        """
        operation = self.ledger.get(request_id)
        if operation is None or operation["source_id"] != self.command_id:
            raise RootReceiptError("ROOT_TERMINAL_TARGET_INVALID")
        command = operation["command"]
        if (operation["status"] != "INTERRUPTED"
                or operation["code"] != "NEEDS_RECONCILIATION"
                or operation["reconcile_required"]
                or operation["capacity_held"]
                or command.get("action") != "CREATE_PROJECT"
                or command.get("request_id") != request_id
                or operation["digest"] != hashlib.sha256(
                    json.dumps(command, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8")
                ).hexdigest()):
            raise RootReceiptError("ROOT_TERMINAL_TARGET_INVALID")
        try:
            count, effects_sha256 = confirmed_create_effects_proof(operation["effects"])
            settled = self.ledger.terminal_root_create_effects(
                request_id, source_id=self.command_id, digest=operation["digest"],
            )
        except LedgerError as error:
            raise RootReceiptError(error.code) from None
        if settled != (count, effects_sha256):
            raise RootReceiptError("TERMINAL_ROOT_EFFECTS_CHANGED")
        try:
            frozen = slots.load_frozen(request_id, Provenance("drive", self.command_id))
        except Exception:  # noqa: BLE001 - local slot details stay private
            raise RootReceiptError("ROOT_FROZEN_INPUT_INVALID") from None
        if (frozen.command.digest != operation["digest"]
                or frozen.command.command != command
                or frozen.command_doc_id != self.command_id):
            raise RootReceiptError("ROOT_FROZEN_INPUT_MISMATCH")
        current = self._current(operation, request_id)
        if not self._same_frozen_input(frozen, current):
            raise RootReceiptError("ROOT_FROZEN_INPUT_MISMATCH")

        summary = _summary(None, command, "INTERRUPTED",
                           trusted_github_owner=self.trusted_github_owner)
        summary["confirmed_effect_count"] = str(count)
        core = {
            "command_doc_id": self.command_id,
            "receipt_doc_id": self.receipt_id,
            "request_digest": operation["digest"],
            "status": "INTERRUPTED", "code": _PARTIAL_CODE,
            "summary": summary,
            "frozen_command_revision": frozen.command_revision,
            "frozen_command_sha256": frozen.command_text_sha256,
            "confirmed_effect_count": count,
            "confirmed_effects_sha256": effects_sha256,
        }
        previous = [
            effect for effect in operation["effects"]
            if effect["step_id"] == _STEP_ID
        ]
        if len(previous) > 1:
            raise RootReceiptError("ROOT_RECEIPT_EFFECT_DUPLICATED")
        if previous:
            effect = previous[0]
            intent = effect["intent"]
            if (effect["kind"] != "drive_file"
                    or any(intent.get(key) != value for key, value in core.items())):
                raise RootReceiptError("ROOT_TERMINAL_INTENT_CHANGED")
            observed_at = intent.get("observed_at")
            if not isinstance(observed_at, str):
                raise RootReceiptError("ROOT_TERMINAL_INTENT_CHANGED")
            try:
                observed_time = datetime.fromisoformat(observed_at)
            except ValueError:
                raise RootReceiptError("ROOT_TERMINAL_INTENT_CHANGED") from None
            if observed_time.tzinfo is None or observed_time.utcoffset() is None:
                raise RootReceiptError("ROOT_TERMINAL_INTENT_CHANGED")
        else:
            observed_at = datetime.now(timezone.utc).isoformat()
        desired = _text(request_id, operation["digest"], "INTERRUPTED",
                        _PARTIAL_CODE, observed_at, summary)
        desired_sha256 = hashlib.sha256(desired.encode("utf-8")).hexdigest()
        if previous and intent.get("desired_sha256") != desired_sha256:
            raise RootReceiptError("ROOT_TERMINAL_INTENT_CHANGED")
        try:
            observed = self.writer.read("root-receipt")
        except Exception:  # noqa: BLE001 - provider details may contain credentials
            raise RootReceiptError("ROOT_RECEIPT_READ_FAILED") from None
        if observed.snapshot.file_id != self.receipt_id:
            raise RootReceiptError("ROOT_RECEIPT_WRITER_MISMATCH")
        if previous:
            if (observed.snapshot.sha256 != desired_sha256
                    or observed.snapshot.text != desired):
                raise RootReceiptError("ROOT_RECEIPT_WRITE_UNCERTAIN")
            effect_id = effect["effect_id"]
            if effect["state"] == "CONFIRMED":
                if effect["provider_id"] != self.receipt_id:
                    raise RootReceiptError("ROOT_RECEIPT_EFFECT_INVALID")
            elif effect["state"] in {"INTENDED", "UNKNOWN"}:
                self.ledger.confirm_effect(effect_id, self.receipt_id)
            else:
                raise RootReceiptError("ROOT_RECEIPT_EFFECT_INVALID")
        else:
            if observed.snapshot.sha256 == desired_sha256:
                raise RootReceiptError("ROOT_RECEIPT_UNJOURNALED")
            try:
                existing = json.loads(observed.snapshot.text)
            except (TypeError, ValueError):
                existing = None
            if isinstance(existing, dict) and existing.get("request_id") == request_id:
                raise RootReceiptError("ROOT_RECEIPT_UNJOURNALED")
            intent = {
                **core, "observed_at": observed_at,
                "base_revision": observed.snapshot.revision_id,
                "desired_sha256": desired_sha256,
            }
            try:
                effect_id = self.ledger.begin_terminal_root_receipt(
                    request_id, source_id=self.command_id,
                    digest=operation["digest"], intent=intent,
                )
            except LedgerError as error:
                raise RootReceiptError(error.code) from None
            # A failed acknowledgement may still follow an applied CAS.
            try:
                result = self.writer.replace("root-receipt", desired, expected=observed)
            except Exception:  # noqa: BLE001 - exact readback decides the outcome
                result = None
            try:
                exact_readback = self._exact_receipt_readback(result, desired, desired_sha256)
            except Exception:  # noqa: BLE001 - leave the intended effect unresolved
                raise RootReceiptError("ROOT_RECEIPT_WRITE_UNCERTAIN") from None
            if not exact_readback:
                raise RootReceiptError("ROOT_RECEIPT_WRITE_UNCERTAIN")
            self.ledger.confirm_effect(effect_id, self.receipt_id)
        latest = self._current(operation, request_id)
        if not self._same_frozen_input(frozen, latest):
            raise RootReceiptError("ROOT_FROZEN_INPUT_MISMATCH")
        try:
            self.ledger.complete_terminal_root_receipt(
                request_id, source_id=self.command_id, digest=operation["digest"],
                receipt_doc_id=self.receipt_id, effect_id=effect_id,
            )
        except LedgerError as error:
            raise RootReceiptError(error.code) from None
        return self.receipt_id


    def confirmed_partial(self, request_id: str,
                          slots: FrozenSlotStore) -> RootPartialReceiptProof:
        """Read-only proof that the original partial root receipt is durable.

        A RESUME handler may use this before adoption. It cannot create or
        confirm an effect, change the old operation, or write either Drive Doc.
        """
        operation = self.ledger.get(request_id)
        if operation is None or operation["source_id"] != self.command_id:
            raise RootReceiptError("ROOT_TERMINAL_TARGET_INVALID")
        command = operation["command"]
        if (operation["status"] != "INTERRUPTED"
                or operation["code"] != "NEEDS_RECONCILIATION"
                or operation["reconcile_required"]
                or operation["capacity_held"]
                or command.get("action") != "CREATE_PROJECT"
                or command.get("request_id") != request_id):
            raise RootReceiptError("ROOT_TERMINAL_TARGET_INVALID")
        try:
            intent = self.ledger.confirmed_terminal_root_receipt(
                request_id, source_id=self.command_id,
                digest=operation["digest"], receipt_doc_id=self.receipt_id,
            )
        except LedgerError as error:
            raise RootReceiptError(error.code) from None
        try:
            frozen = slots.load_frozen(request_id, Provenance("drive", self.command_id))
        except Exception:  # noqa: BLE001 - local slot details stay private
            raise RootReceiptError("ROOT_FROZEN_INPUT_INVALID") from None
        if (frozen.command.digest != operation["digest"]
                or frozen.command.command != command
                or frozen.command_doc_id != self.command_id
                or intent.get("frozen_command_revision") != frozen.command_revision
                or intent.get("frozen_command_sha256") != frozen.command_text_sha256):
            raise RootReceiptError("ROOT_FROZEN_INPUT_MISMATCH")
        current = self._current(operation, request_id)
        if not self._same_frozen_input(frozen, current):
            raise RootReceiptError("ROOT_FROZEN_INPUT_MISMATCH")
        count = intent["confirmed_effect_count"]
        summary = _summary(None, command, "INTERRUPTED",
                           trusted_github_owner=self.trusted_github_owner)
        summary["confirmed_effect_count"] = str(count)
        if intent.get("summary") != summary:
            raise RootReceiptError("ROOT_TERMINAL_INTENT_CHANGED")
        observed_at = intent.get("observed_at")
        if not isinstance(observed_at, str):
            raise RootReceiptError("ROOT_TERMINAL_INTENT_CHANGED")
        try:
            observed_time = datetime.fromisoformat(observed_at)
        except ValueError:
            raise RootReceiptError("ROOT_TERMINAL_INTENT_CHANGED") from None
        if observed_time.tzinfo is None or observed_time.utcoffset() is None:
            raise RootReceiptError("ROOT_TERMINAL_INTENT_CHANGED")
        desired = _text(request_id, operation["digest"], "INTERRUPTED",
                        _PARTIAL_CODE, observed_at, summary)
        desired_sha256 = hashlib.sha256(desired.encode("utf-8")).hexdigest()
        if intent.get("desired_sha256") != desired_sha256:
            raise RootReceiptError("ROOT_TERMINAL_INTENT_CHANGED")
        try:
            observed = self.writer.read("root-receipt")
        except Exception:  # noqa: BLE001 - provider details may contain credentials
            raise RootReceiptError("ROOT_RECEIPT_READ_FAILED") from None
        if (observed.snapshot.file_id != self.receipt_id
                or observed.snapshot.sha256 != desired_sha256
                or observed.snapshot.text != desired):
            raise RootReceiptError("ROOT_RECEIPT_PROOF_CHANGED")
        latest = self._current(operation, request_id)
        if not self._same_frozen_input(frozen, latest):
            raise RootReceiptError("ROOT_FROZEN_INPUT_MISMATCH")
        return RootPartialReceiptProof(
            request_id, operation["digest"], self.receipt_id, desired_sha256,
            count, intent["confirmed_effects_sha256"],
        )
