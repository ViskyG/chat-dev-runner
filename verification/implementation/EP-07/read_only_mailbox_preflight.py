"""Read-only exact-ID Drive mailbox preflight before EP-07 host maintenance.

Run with the installed wheel Python and selected XDG_DATA_HOME, from outside the
source checkout. The report contains command/receipt identity metadata only;
document bodies, intent, credentials, and provider exception text are omitted.
An expired OAuth token can be refreshed in memory with an explicit flag; the
private token file is never changed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from importlib import import_module
from typing import Any

from runner_app.adapters.drive_auth import (
    DRIVE_SCOPE,
    DriveAuthError,
    _GoogleBackend,
    _load_client,
    _load_token,
    _serialized_credentials,
    load_authorized_credentials,
)
from runner_app.adapters.drive_check import _selected_installation
from runner_app.adapters.drive_docs import (
    MAX_COMMAND_DOC_BYTES,
    DriveError,
    ReadProvider,
    RegisteredMailbox,
    read_document,
)
from runner_app.adapters.drive_google import GoogleReadProvider
from runner_app.adapters.drive_index import LegacyInstallationManifest, attach_existing
from runner_app.adapters.drive_registry import WorkspaceDriveRegistry
from runner_app.adapters.drive_transport import isolated_request_builder
from runner_app.domain.commands import DomainError, parse_command, parse_json
from runner_app.storage.paths import Paths

_WORKSPACE_RECORD = re.compile(r"[0-9a-f]{64}\.json\Z")
_PROJECT_ANCHOR = re.compile(r"project-[0-9a-f]{64}\.json\Z")
_TERMINAL = frozenset({"COMPLETED", "REJECTED", "BLOCKED", "FAULT", "INTERRUPTED", "CANCELLED"})
_WORKSPACE_RECEIPT_HEADER = "CHAT-DEV RUNNER / RECEIPT\n"
_DELIVERY_STATUSES = frozenset({"DELIVERED", "PARTIAL", "FAILED", "PENDING"})


class PreflightError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read_receipt(provider: ReadProvider, mailbox: RegisteredMailbox) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if mailbox.receipt_id is None:
        raise PreflightError("RECEIPT_ID_MISSING")
    snapshot = read_document(provider, mailbox.receipt_id, max_bytes=MAX_COMMAND_DOC_BYTES)
    metadata: dict[str, Any] = {"revision": snapshot.revision_id, "sha256": snapshot.sha256}
    if not snapshot.text.strip():
        return metadata, None
    body = (snapshot.text[len(_WORKSPACE_RECEIPT_HEADER):].encode("utf-8")
            if snapshot.text.startswith(_WORKSPACE_RECEIPT_HEADER)
            else snapshot.raw_bytes)
    try:
        parsed = parse_json(body)
    except DomainError as error:
        metadata["parse_code"] = error.code
        return metadata, None
    return metadata, parsed


def summarize_mailbox(provider: ReadProvider, mailbox: RegisteredMailbox, *,
                      scope: str, now: datetime) -> dict[str, Any]:
    """Read command/receipt/command and retain only bounded identity fields."""
    first = read_document(provider, mailbox.command_id, max_bytes=MAX_COMMAND_DOC_BYTES)
    receipt_metadata, receipt = _read_receipt(provider, mailbox)
    second = read_document(provider, mailbox.command_id, max_bytes=MAX_COMMAND_DOC_BYTES)
    if (first.revision_id, first.sha256) != (second.revision_id, second.sha256):
        raise PreflightError("COMMAND_CHANGED_DURING_PREFLIGHT")
    result: dict[str, Any] = {
        "scope": scope,
        "project_id": mailbox.project_id,
        "workspace_id": mailbox.workspace_id,
        "command_doc_id": mailbox.command_id,
        "command_revision": first.revision_id,
        "command_sha256": first.sha256,
        "receipt_doc_id": mailbox.receipt_id,
        "receipt_revision": receipt_metadata["revision"],
        "receipt_sha256": receipt_metadata["sha256"],
        "dispatch": None,
        "request_id": None,
        "expires_at": None,
        "matching_receipt_status": None,
        "maintenance_pending": False,
    }
    if "parse_code" in receipt_metadata:
        result["receipt_parse_code"] = receipt_metadata["parse_code"]
    if not first.text.strip():
        return result
    try:
        frozen = parse_command(first.raw_bytes)
    except DomainError as error:
        result["command_parse_code"] = error.code
        result["maintenance_pending"] = True
        return result
    command = frozen.command
    result.update({
        "dispatch": command["dispatch"],
        "request_id": command["request_id"],
        "expires_at": command["expires_at"],
    })
    terminal_proof = False
    if receipt is not None and receipt.get("request_id") == command["request_id"]:
        if (receipt.get("request_digest") == frozen.digest
                and receipt.get("status") in _TERMINAL):
            result["matching_receipt_status"] = receipt["status"]
            terminal_proof = True
        elif receipt.get("delivery_status") in _DELIVERY_STATUSES:
            # Workspace delivery receipts omit the command digest, so they
            # identify a prior delivery but cannot clear a fresh READY gate.
            result["matching_receipt_status"] = receipt["delivery_status"]
    expiry = datetime.fromisoformat(command["expires_at"].replace("Z", "+00:00"))
    result["maintenance_pending"] = (
        command["dispatch"] == "READY" and expiry.astimezone(timezone.utc) > now
        and not terminal_proof
    )
    return result


def registered_mailboxes(paths: Paths, provider: ReadProvider) -> list[tuple[str, RegisteredMailbox]]:
    """Use only locally registered IDs, verifying their current Drive identities."""
    config = _selected_installation(None)
    manifest = LegacyInstallationManifest.from_file(
        paths.config_dir / "installation-legacy.json", config,
    )
    attached = attach_existing(provider, config, legacy_manifest=manifest)
    result = [("root", attached.root_mailbox())]
    directory = paths.state_dir / "drive-registrations"
    if not directory.is_dir() or directory.is_symlink():
        raise PreflightError("REGISTRATION_DIRECTORY_MISSING")
    registry = WorkspaceDriveRegistry(directory, attached, provider)
    project_seen: set[str] = set()
    workspace_seen: set[tuple[str, str]] = set()
    for entry in sorted(directory.iterdir()):
        if _PROJECT_ANCHOR.fullmatch(entry.name) is not None:
            continue  # Project anchors are checked by WorkspaceDriveRegistry.get.
        if _WORKSPACE_RECORD.fullmatch(entry.name) is None:
            raise PreflightError("REGISTRATION_DIRECTORY_UNEXPECTED")
        candidate = registry._read(entry.name)  # Private no-follow, owner/mode, size checks.
        if (candidate.project_id, candidate.workspace_id) in workspace_seen:
            raise PreflightError("REGISTRATION_DUPLICATE")
        if registry._filename(candidate.project_id, candidate.workspace_id) != entry.name:
            raise PreflightError("REGISTRATION_FILENAME_MISMATCH")
        value = registry.get(candidate.project_id, candidate.workspace_id)
        workspace_seen.add((value.project_id, value.workspace_id))
        if value.project_id not in project_seen:
            result.append(("project", value.project_mailbox()))
            project_seen.add(value.project_id)
        result.append(("workspace", value.mailbox()))
    return result


def _credentials(paths: Paths, *, ephemeral_refresh: bool) -> Any:
    try:
        return load_authorized_credentials(paths, refresh=False)
    except DriveAuthError as error:
        if error.code != "TOKEN_REFRESH_REQUIRED" or not ephemeral_refresh:
            raise
    # Only an in-memory refresh. Never call the stored-token writer.
    client = _load_client(paths)
    info = _load_token(paths)
    backend = _GoogleBackend()
    try:
        credentials = backend.from_authorized_info(
            {**info, "client_id": client["client_id"], "client_secret": client["client_secret"]},
            (DRIVE_SCOPE,),
        )
        if not credentials.refresh_token:
            raise DriveAuthError("TOKEN_REFRESH_MISSING")
        backend.refresh(credentials)
        _serialized_credentials(credentials, client)  # Recheck granted scope without writing.
        return credentials
    except DriveAuthError:
        raise
    except Exception:  # noqa: BLE001 - provider errors may contain OAuth material
        raise DriveAuthError("OAUTH_REFRESH_FAILED") from None


def _provider(credentials: Any) -> GoogleReadProvider:
    """Use the same request-local HTTP transport as the installed multi-threaded runner."""
    try:
        build = import_module("googleapiclient.discovery").build
        request_builder = isolated_request_builder(credentials)
        drive = build("drive", "v3", credentials=credentials,
                      requestBuilder=request_builder, cache_discovery=False)
        docs = build("docs", "v1", credentials=credentials,
                     requestBuilder=request_builder, cache_discovery=False)
        return GoogleReadProvider(drive, docs)
    except Exception:  # noqa: BLE001 - Google exceptions may contain OAuth material
        raise DriveError("GOOGLE_SERVICE_UNAVAILABLE", "Google services unavailable") from None


def preflight(paths: Paths, *, ephemeral_refresh: bool = False) -> dict[str, Any]:
    credentials = _credentials(paths, ephemeral_refresh=ephemeral_refresh)
    provider = _provider(credentials)
    now = datetime.now(timezone.utc)
    mailboxes = [
        summarize_mailbox(provider, mailbox, scope=scope, now=now)
        for scope, mailbox in registered_mailboxes(paths, provider)
    ]
    return {
        "mode": "READ_ONLY_EXACT_ID_MAILBOX_PREFLIGHT",
        "checked_at": now.isoformat(),
        "remote_writes": False,
        "local_token_file_changed": False,
        "ephemeral_refresh_allowed": ephemeral_refresh,
        "maintenance_safe": all(not row["maintenance_pending"] for row in mailboxes),
        "mailboxes": mailboxes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-ephemeral-refresh", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = preflight(Paths.discover(), ephemeral_refresh=args.allow_ephemeral_refresh)
    except (DriveAuthError, DriveError, DomainError, PreflightError) as error:
        print(json.dumps({"mode": "READ_ONLY_EXACT_ID_MAILBOX_PREFLIGHT",
                          "error": error.code, "maintenance_safe": False}))
        return 2
    except Exception:  # noqa: BLE001 - provider exceptions can contain OAuth material
        print(json.dumps({"mode": "READ_ONLY_EXACT_ID_MAILBOX_PREFLIGHT",
                          "error": "PREFLIGHT_FAILED", "maintenance_safe": False}))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["maintenance_safe"] else 3


if __name__ == "__main__":
    sys.exit(main())
