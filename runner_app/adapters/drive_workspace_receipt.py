"""Exact-ID terminal receipts for workspace RETARGET_WORKSPACE commands.

A workspace retarget receipt mirrors the committed SQLite receipt, including
refusals. The command's frozen revision and registered workspace identity are
rechecked before any output write. One durable intent precedes one Docs CAS;
an uncertain CAS is proved by exact readback, never replayed.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, cast

from runner_app.adapters.drive_docs import FrozenDriveInput, ReadProvider, read_ready_snapshot
from runner_app.adapters.drive_index import AttachedInstallation
from runner_app.adapters.drive_poller import FrozenSlotStore
from runner_app.adapters.drive_registry import WorkspaceDriveRegistry
from runner_app.adapters.drive_writer import DocsCASWriter
from runner_app.domain.commands import Provenance
from runner_app.storage.ledger import Ledger

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
_STEP_ID = "drive-workspace-terminal-receipt"


class WorkspaceReceiptError(ValueError):
    """Stable refusal without command, provider, or credential contents."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _same_input(left: FrozenDriveInput, right: FrozenDriveInput) -> bool:
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


class WorkspaceTerminalReceiptPublisher:
    """Publish only the terminal receipt of a registered workspace COMMAND."""

    def __init__(self, ledger: Ledger, attached: AttachedInstallation,
                 reader: ReadProvider, registry: WorkspaceDriveRegistry) -> None:
        self.ledger = ledger
        self.attached = attached
        self.reader = reader
        self.registry = registry

    def publish_if_terminal(
        self, observed: FrozenDriveInput, provenance: Provenance,
        slots: FrozenSlotStore,
    ) -> str | None:
        command = observed.command.command
        if (provenance.kind != "drive" or provenance.project_id is None
                or provenance.workspace_id is None
                or observed.command_doc_id != provenance.source_id
                or command.get("action") != "RETARGET_WORKSPACE"
                or command.get("project_id") != provenance.project_id
                or command.get("workspace_id") != provenance.workspace_id):
            raise WorkspaceReceiptError("WORKSPACE_RECEIPT_SCOPE_INVALID")
        request_id = command["request_id"]
        operation = self.ledger.get(request_id)
        if operation is None:
            raise WorkspaceReceiptError("WORKSPACE_RECEIPT_OPERATION_MISSING")
        if operation["status"] == "RUNNING":
            return None
        receipt = operation["receipt"]
        if (operation["source_id"] != provenance.source_id
                or operation["project_id"] != provenance.project_id
                or operation["workspace_id"] != provenance.workspace_id
                or operation["action"] != command["action"]
                or operation["command"] != command
                or operation["digest"] != observed.command.digest
                or not isinstance(receipt, dict)
                or receipt.get("request_id") != request_id
                or receipt.get("request_digest") != observed.command.digest
                or receipt.get("status") != operation["status"]
                or receipt.get("code") != operation["code"]
                or operation["status"] not in {
                    "COMPLETED", "REJECTED", "BLOCKED", "FAULT", "INTERRUPTED", "CANCELLED",
                }
                or (operation["code"] is not None
                    and (not isinstance(operation["code"], str)
                         or _CODE.fullmatch(operation["code"]) is None))):
            raise WorkspaceReceiptError("WORKSPACE_RECEIPT_OPERATION_MISMATCH")
        try:
            registration = self.registry.get(provenance.project_id, provenance.workspace_id)
            mailbox = registration.mailbox()
        except Exception:  # noqa: BLE001 -- local/provider details stay behind stable code
            raise WorkspaceReceiptError("WORKSPACE_RECEIPT_REGISTRATION_INVALID") from None
        if (registration.installation_id != self.attached.index.installation_id
                or mailbox.command_id != provenance.source_id
                or mailbox.project_id != provenance.project_id
                or mailbox.workspace_id != provenance.workspace_id):
            raise WorkspaceReceiptError("WORKSPACE_RECEIPT_REGISTRATION_INVALID")
        try:
            frozen = slots.load_frozen(request_id, provenance)
            current = read_ready_snapshot(self.reader, mailbox)
        except Exception:  # noqa: BLE001 -- no unverified output after input drift
            raise WorkspaceReceiptError("WORKSPACE_RECEIPT_INPUT_UNVERIFIED") from None
        if (current is None or not _same_input(frozen, observed)
                or not _same_input(current, observed)):
            raise WorkspaceReceiptError("WORKSPACE_RECEIPT_INPUT_CHANGED")
        desired = json.dumps(receipt, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")) + "\n"
        desired_sha256 = hashlib.sha256(desired.encode("utf-8")).hexdigest()
        if _SHA256.fullmatch(desired_sha256) is None:
            raise WorkspaceReceiptError("WORKSPACE_RECEIPT_INVALID")
        key = f"workspace-receipt:{provenance.project_id}/{provenance.workspace_id}"
        try:
            writer = DocsCASWriter(
                cast(Any, self.reader), self.attached,
                extra_docs=(registration.output_docs(
                    receipt_key=key,
                    latest_result_key=f"latest-result:{provenance.project_id}/{provenance.workspace_id}",
                )[0],),
            )
            remote = writer.read(key)
        except Exception:  # noqa: BLE001
            raise WorkspaceReceiptError("WORKSPACE_RECEIPT_READ_FAILED") from None
        if remote.snapshot.file_id != mailbox.receipt_id:
            raise WorkspaceReceiptError("WORKSPACE_RECEIPT_WRITER_MISMATCH")
        previous = [
            item for item in operation["effects"]
            if item["step_id"] == _STEP_ID
        ]
        if len(previous) > 1 or (previous and previous[0]["kind"] != "drive_file"):
            raise WorkspaceReceiptError("WORKSPACE_RECEIPT_EFFECT_CONFLICT")
        core = {
            "command_doc_id": provenance.source_id,
            "receipt_doc_id": mailbox.receipt_id,
            "project_id": provenance.project_id,
            "workspace_id": provenance.workspace_id,
            "request_digest": observed.command.digest,
            "status": operation["status"],
            "code": operation["code"],
            "desired_sha256": desired_sha256,
            "frozen_command_revision": observed.command_revision,
            "frozen_command_sha256": observed.command_text_sha256,
        }
        if previous:
            effect = previous[0]
            prior_intent = effect["intent"]
            if any(prior_intent.get(name) != value for name, value in core.items()):
                raise WorkspaceReceiptError("WORKSPACE_RECEIPT_INTENT_CHANGED")
            if remote.snapshot.text != desired or remote.snapshot.sha256 != desired_sha256:
                raise WorkspaceReceiptError("WORKSPACE_RECEIPT_WRITE_UNCERTAIN")
            if effect["state"] not in {"INTENDED", "UNKNOWN", "CONFIRMED"}:
                raise WorkspaceReceiptError("WORKSPACE_RECEIPT_EFFECT_CONFLICT")
            try:
                if effect["state"] != "CONFIRMED":
                    self.ledger.confirm_effect(effect["effect_id"], mailbox.receipt_id)
                elif effect["provider_id"] != mailbox.receipt_id:
                    raise WorkspaceReceiptError("WORKSPACE_RECEIPT_EFFECT_CONFLICT")
                self.ledger.complete_terminal_workspace_receipt(
                    request_id, source_id=provenance.source_id,
                    digest=observed.command.digest,
                    receipt_doc_id=mailbox.receipt_id,
                    effect_id=effect["effect_id"],
                )
            except WorkspaceReceiptError:
                raise
            except Exception:  # noqa: BLE001
                raise WorkspaceReceiptError("WORKSPACE_RECEIPT_CONFIRM_FAILED") from None
            return mailbox.receipt_id
        if remote.snapshot.text == desired:
            raise WorkspaceReceiptError("WORKSPACE_RECEIPT_UNJOURNALED")
        try:
            old = json.loads(remote.snapshot.text)
        except (TypeError, ValueError):
            old = None
        if isinstance(old, dict) and old.get("request_id") == request_id:
            raise WorkspaceReceiptError("WORKSPACE_RECEIPT_UNJOURNALED")
        intent: Mapping[str, Any] = {
            **core, "base_revision": remote.snapshot.revision_id,
        }
        try:
            effect_id = self.ledger.begin_terminal_workspace_receipt(
                request_id, source_id=provenance.source_id,
                digest=observed.command.digest, intent=intent,
            )
        except Exception:  # noqa: BLE001
            raise WorkspaceReceiptError("WORKSPACE_RECEIPT_INTENT_FAILED") from None
        try:
            writer.replace(key, desired, expected=remote)
            readback = writer.read(key)
        except Exception:  # noqa: BLE001 -- intent is durable; no blind replay
            raise WorkspaceReceiptError("WORKSPACE_RECEIPT_WRITE_UNCERTAIN") from None
        if (readback.snapshot.file_id != mailbox.receipt_id
                or readback.snapshot.text != desired
                or readback.snapshot.sha256 != desired_sha256):
            raise WorkspaceReceiptError("WORKSPACE_RECEIPT_WRITE_UNCERTAIN")
        try:
            self.ledger.confirm_effect(effect_id, mailbox.receipt_id)
            self.ledger.complete_terminal_workspace_receipt(
                request_id, source_id=provenance.source_id,
                digest=observed.command.digest,
                receipt_doc_id=mailbox.receipt_id,
                effect_id=effect_id,
            )
        except Exception:  # noqa: BLE001
            raise WorkspaceReceiptError("WORKSPACE_RECEIPT_CONFIRM_FAILED") from None
        return mailbox.receipt_id
