"""Known-ID native Docs reads and coherent offline mailbox freezing.

Provider calls are a narrow interface. This module does not implement OAuth,
perform network I/O, or issue writes. Only trusted local registration supplies
mailbox IDs and scope; document text can never grant a new watched mailbox.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from runner_app.domain.commands import (
    PROJECT_ACTIONS,
    ROOT_ACTIONS,
    WORKSPACE_ACTIONS,
    DomainError,
    FrozenCommand,
    parse_command,
    parse_json,
)
from runner_app.git.changes import MAX_CHANGESET_BYTES, ChangeError, ChangePlan, validate_changeset

MAX_COMMAND_DOC_BYTES = 262_144
MAX_INDEX_DOC_BYTES = 1_048_576
FILE_FIELDS = (
    "id,name,mimeType,parents,trashed,isAppAuthorized,appProperties,"
    "capabilities(canEdit,canAddChildren),version"
)


class DriveError(ValueError):
    """Stable, sanitized Drive transport/configuration finding."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ReadProvider(Protocol):
    def get_file(self, file_id: str, *, fields: str = FILE_FIELDS) -> Mapping[str, Any]: ...

    def get_document(self, file_id: str, *,
                     include_tabs_content: bool = True) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class DocumentSnapshot:
    file_id: str
    revision_id: str
    text: str
    tab_id: str | None
    sha256: str

    @property
    def raw_bytes(self) -> bytes:
        return self.text.encode("utf-8", errors="strict")


@dataclass(frozen=True)
class RegisteredMailbox:
    """Trusted local registration, never constructed from remote COMMAND fields."""

    command_id: str
    parent_id: str
    receipt_id: str | None = None
    changeset_id: str | None = None
    project_id: str | None = None
    workspace_id: str | None = None

    def __post_init__(self) -> None:
        if not self.command_id or not self.parent_id:
            raise DriveError("INVALID_MAILBOX", "Known command and parent IDs are required")
        if self.workspace_id is not None and (
            self.project_id is None or self.changeset_id is None
        ):
            raise DriveError("INVALID_MAILBOX", "Workspace mailbox needs project and changeset IDs")


@dataclass(frozen=True)
class FrozenDriveInput:
    command: FrozenCommand
    raw_command: bytes
    command_doc_id: str
    command_revision: str
    command_text_sha256: str
    raw_changeset: bytes | None
    changeset_doc_id: str | None
    changeset_revision: str | None
    changeset_text_sha256: str | None
    change_plan: ChangePlan | None


def extract_document(document: Mapping[str, Any], expected_id: str, *,
                     max_bytes: int) -> DocumentSnapshot:
    """Read every text run of one tab; reject unsupported/missing tab content."""
    if max_bytes < 1:
        raise ValueError("Document byte limit must be positive")
    if document.get("documentId") != expected_id:
        raise DriveError("DOCUMENT_ID_MISMATCH", "Provider returned a different document")
    revision = document.get("revisionId")
    if not isinstance(revision, str) or not revision:
        raise DriveError("REVISION_MISSING", "Native Docs revision is required")
    tabs = document.get("tabs")
    tab_id: str | None = None
    if tabs is not None:
        if not isinstance(tabs, list) or len(tabs) != 1:
            raise DriveError("UNSUPPORTED_TABS", "Exactly one complete document tab is required")
        tab = tabs[0]
        if not isinstance(tab, dict) or tab.get("childTabs"):
            raise DriveError("UNSUPPORTED_TABS", "Nested or multiple tabs are not supported")
        tab_properties = tab.get("tabProperties")
        if not isinstance(tab_properties, dict) or not isinstance(
            tab_properties.get("tabId"), str
        ):
            raise DriveError("UNSUPPORTED_TABS", "Native tab ID is missing")
        tab_id = tab_properties["tabId"]
        document_tab = tab.get("documentTab")
        if not isinstance(document_tab, dict):
            raise DriveError("UNSUPPORTED_DOC_CONTENT", "Complete tab body is required")
        body = document_tab.get("body")
    else:
        body = document.get("body")
    if not isinstance(body, dict) or not isinstance(body.get("content"), list):
        raise DriveError("UNSUPPORTED_DOC_CONTENT", "Complete plain body content is required")
    parts: list[str] = []
    size = 0
    for element in body["content"]:
        if not isinstance(element, dict):
            raise DriveError("UNSUPPORTED_DOC_CONTENT", "Unknown structural element")
        if "sectionBreak" in element:
            if len(element.keys() - {"sectionBreak", "startIndex", "endIndex"}) != 0:
                raise DriveError("UNSUPPORTED_DOC_CONTENT", "Mixed section content is unsupported")
            continue
        paragraph = element.get("paragraph")
        if not isinstance(paragraph, dict) or not isinstance(paragraph.get("elements"), list):
            raise DriveError("UNSUPPORTED_DOC_CONTENT", "Tables or nonparagraph content are unsupported")
        for run in paragraph["elements"]:
            if not isinstance(run, dict) or not isinstance(run.get("textRun"), dict):
                raise DriveError("UNSUPPORTED_DOC_CONTENT", "Inline objects are unsupported")
            content = run["textRun"].get("content")
            if not isinstance(content, str):
                raise DriveError("UNSUPPORTED_DOC_CONTENT", "Text run has no plain content")
            try:
                encoded = content.encode("utf-8", errors="strict")
            except UnicodeEncodeError as error:
                raise DriveError("INVALID_DOC_UTF8", "Document text is not valid Unicode") from error
            size += len(encoded)
            if size > max_bytes:
                raise DriveError("DOCUMENT_TOO_LARGE", "Document exceeds local byte limit")
            parts.append(content)
    text = "".join(parts)
    return DocumentSnapshot(
        file_id=expected_id, revision_id=revision, text=text, tab_id=tab_id,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def read_document(provider: ReadProvider, file_id: str, *,
                  max_bytes: int) -> DocumentSnapshot:
    try:
        document = provider.get_document(file_id, include_tabs_content=True)
    except (OSError, PermissionError, FileNotFoundError) as error:
        raise DriveError("DOCUMENT_ACCESS_MISSING", "Known document is unavailable") from error
    return extract_document(document, file_id, max_bytes=max_bytes)


def require_registered_doc(provider: ReadProvider, file_id: str, parent_id: str) -> None:
    """Check exact native Doc metadata; never search by title or parent contents."""
    try:
        metadata = provider.get_file(file_id, fields=FILE_FIELDS)
    except (OSError, PermissionError, FileNotFoundError) as error:
        raise DriveError("PER_FILE_ACCESS_MISSING", "Known document has no verified file grant") from error
    if metadata.get("id") != file_id:
        raise DriveError("FILE_ID_MISMATCH", "Provider returned another file")
    if metadata.get("trashed") is not False:
        raise DriveError("FILE_UNAVAILABLE", "Known document is trashed or status is unknown")
    if metadata.get("mimeType") != "application/vnd.google-apps.document":
        raise DriveError("FILE_TYPE_MISMATCH", "Known mailbox must remain a native Google Doc")
    if metadata.get("parents") != [parent_id]:
        raise DriveError("FILE_MOVED", "Known document parent no longer matches local registration")
    if metadata.get("isAppAuthorized") is not True:
        raise DriveError("PER_FILE_GRANT_MISSING", "OAuth app has no verified per-file grant")


def _scope(command: Mapping[str, Any], mailbox: RegisteredMailbox) -> None:
    action = command["action"]
    project_id = command["project_id"]
    workspace_id = command["workspace_id"]
    if mailbox.project_id is None:
        allowed = action in ROOT_ACTIONS and workspace_id is None
    elif mailbox.workspace_id is None:
        allowed = (action in PROJECT_ACTIONS and project_id == mailbox.project_id
                   and workspace_id is None)
    else:
        allowed = (action in WORKSPACE_ACTIONS and project_id == mailbox.project_id
                   and workspace_id == mailbox.workspace_id)
    if not allowed:
        raise DriveError("SOURCE_SCOPE", "Command does not match registered mailbox scope")


def read_ready_snapshot(provider: ReadProvider, mailbox: RegisteredMailbox) -> FrozenDriveInput | None:
    """Freeze one READY observation before passing raw bytes to the EP-01 engine.

    The engine still performs epoch/time/policy/admission checks atomically.
    Repeated observations are passed to that same ledger, not queued here.
    """
    require_registered_doc(provider, mailbox.command_id, mailbox.parent_id)
    first = read_document(provider, mailbox.command_id, max_bytes=MAX_COMMAND_DOC_BYTES)
    try:
        frozen = parse_command(first.raw_bytes)
    except DomainError as error:
        raise DriveError(error.code, "Known command body is invalid") from error
    command = frozen.command
    if command["dispatch"] != "READY":
        return None
    _scope(command, mailbox)
    change_first: DocumentSnapshot | None = None
    raw_changeset: bytes | None = None
    plan: ChangePlan | None = None
    if command["action"] == "DELIVER_VERIFY_PUBLISH":
        expected_id = command["params"]["changeset_doc_id"]
        if mailbox.changeset_id is None or expected_id != mailbox.changeset_id:
            raise DriveError("UNREGISTERED_CHANGESET", "Changeset ID is not locally registered")
        require_registered_doc(provider, mailbox.changeset_id, mailbox.parent_id)
        change_first = read_document(
            provider, mailbox.changeset_id, max_bytes=MAX_CHANGESET_BYTES,
        )
        if change_first.revision_id != command["params"]["changeset_revision"]:
            raise DriveError("INPUT_CHANGED", "Changeset revision differs from submitted command")
        raw_changeset = change_first.raw_bytes
        try:
            parsed = parse_json(raw_changeset, max_bytes=MAX_CHANGESET_BYTES)
            plan = validate_changeset(
                parsed, project_id=command["project_id"],
                workspace_id=command["workspace_id"],
                expected_head=command["expected_head"],
            )
        except (DomainError, ChangeError) as error:
            raise DriveError(error.code, "Changeset is invalid") from error
    second = read_document(provider, mailbox.command_id, max_bytes=MAX_COMMAND_DOC_BYTES)
    if second.revision_id != first.revision_id or second.sha256 != first.sha256:
        raise DriveError("INPUT_CHANGED", "Command changed while being frozen")
    require_registered_doc(provider, mailbox.command_id, mailbox.parent_id)
    if change_first is not None and mailbox.changeset_id is not None:
        change_second = read_document(
            provider, mailbox.changeset_id, max_bytes=MAX_CHANGESET_BYTES,
        )
        if (change_second.revision_id != change_first.revision_id
                or change_second.sha256 != change_first.sha256):
            raise DriveError("INPUT_CHANGED", "Changeset changed while being frozen")
        require_registered_doc(provider, mailbox.changeset_id, mailbox.parent_id)
        final_command = read_document(
            provider, mailbox.command_id, max_bytes=MAX_COMMAND_DOC_BYTES,
        )
        if (final_command.revision_id != first.revision_id
                or final_command.sha256 != first.sha256):
            raise DriveError("INPUT_CHANGED", "Command changed during final changeset recheck")
    return FrozenDriveInput(
        command=frozen, raw_command=first.raw_bytes, command_doc_id=mailbox.command_id,
        command_revision=first.revision_id, command_text_sha256=first.sha256,
        raw_changeset=raw_changeset,
        changeset_doc_id=mailbox.changeset_id if change_first is not None else None,
        changeset_revision=change_first.revision_id if change_first is not None else None,
        changeset_text_sha256=change_first.sha256 if change_first is not None else None,
        change_plan=plan,
    )
