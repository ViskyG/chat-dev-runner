"""Journaled root receipt for a Drive-originated CREATE_PROJECT command.

The existing installation supplies both document IDs. A local UI command is
inert here; a Drive command must still be READY with the exact admitted digest.
No command document is written, and an uncertain receipt write is never replayed.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from bridge_reference.protocol import Rejected, validate_branch
from runner_app.adapters.drive_docs import FILE_FIELDS, ReadProvider, read_ready_snapshot
from runner_app.adapters.drive_index import AttachedInstallation
from runner_app.adapters.drive_writer import DocsCASWriter, RegisteredOutputDoc
from runner_app.storage.ledger import Ledger

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
_MAX_MESSAGE_BYTES = 4096
_MAX_RECEIPT_BYTES = 8192


class RootReceiptError(ValueError):
    """Stable finding with no provider response or command intent."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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
    message = f"CREATE_PROJECT {status}; " + "; ".join(
        f"{key}={value}" for key, value in summary.items()
    )
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

    def _current(self, operation: Mapping[str, Any], request_id: str) -> None:
        try:
            metadata = self.reader.get_file(self.command_id, fields=FILE_FIELDS)
        except Exception:  # noqa: BLE001 - provider errors may contain credentials
            raise RootReceiptError("ROOT_COMMAND_READ_FAILED") from None
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
        try:
            frozen = read_ready_snapshot(self.reader, self.attached.root_mailbox())
        except Exception:  # noqa: BLE001 - provider errors may contain credentials
            raise RootReceiptError("ROOT_COMMAND_READ_FAILED") from None
        if (frozen is None or frozen.command.command["action"] != "CREATE_PROJECT"
                or frozen.command.command["request_id"] != request_id
                or frozen.command.digest != operation["digest"]
                or frozen.command.command != operation["command"]):
            raise RootReceiptError("ROOT_COMMAND_MISMATCH")

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
            self.writer.replace("root-receipt", desired, expected=observed)
            readback = self.writer.read("root-receipt")
        except Exception:  # noqa: BLE001 - leave the effect pending for exact reconciliation
            raise RootReceiptError("ROOT_RECEIPT_WRITE_UNCERTAIN") from None
        if (readback.snapshot.file_id != self.receipt_id
                or readback.snapshot.sha256 != desired_sha256
                or readback.snapshot.text != desired):
            raise RootReceiptError("ROOT_RECEIPT_WRITE_UNCERTAIN")
        self._current(operation, request_id)
        self.ledger.confirm_effect(effect_id, self.receipt_id)
        return self.receipt_id
