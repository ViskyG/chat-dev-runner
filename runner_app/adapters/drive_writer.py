"""Revision-guarded writes to locally registered, runner-owned output Docs.

This module cannot create a Doc, select one by title, write a command/change set,
or retry a potentially applied write. A caller must attach the existing installation
and supply an observed revision before replacing one output document.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from runner_app.adapters.drive_docs import (
    FILE_FIELDS,
    DocumentSnapshot,
    DriveError,
    ReadProvider,
    extract_document,
)
from runner_app.adapters.drive_index import AttachedInstallation, recheck_attached_index

OutputKind = Literal["RECEIPT", "STATE", "LATEST_RESULT"]
WriteState = Literal["CONFIRMED", "UNCHANGED", "UNCERTAIN"]
_ID = re.compile(r"[A-Za-z0-9_-]{8,256}\Z")
_ALLOWED_APP_KEYS: Mapping[str, OutputKind] = {
    "receipt": "RECEIPT",
    "state": "STATE",
    "project-receipt": "RECEIPT",
    "project-state": "STATE",
    "source-context": "STATE",
    "workspace-receipt": "RECEIPT",
    "latest-result": "LATEST_RESULT",
}
_MAX_BYTES: Mapping[OutputKind, int] = {
    "RECEIPT": 65_536,
    "STATE": 65_536,
    "LATEST_RESULT": 262_144,
}


class WriteProvider(ReadProvider, Protocol):
    """The single mutable provider method; implementation must not retry writes."""

    def batch_update_document(
        self, file_id: str, *, requests: Sequence[Mapping[str, Any]],
        required_revision_id: str,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class RegisteredOutputDoc:
    """A trusted local registration from installation or project provisioning."""

    key: str
    kind: OutputKind
    file_id: str
    parent_id: str
    app_key: str

    def __post_init__(self) -> None:
        if (not isinstance(self.key, str) or not self.key
                or not isinstance(self.file_id, str) or not _ID.fullmatch(self.file_id)
                or not isinstance(self.parent_id, str) or not _ID.fullmatch(self.parent_id)
                or not isinstance(self.app_key, str)
                or _ALLOWED_APP_KEYS.get(self.app_key) != self.kind):
            raise DriveError("OUTPUT_REGISTRATION_INVALID", "Output Doc registration is invalid")


@dataclass(frozen=True)
class ObservedOutputDoc:
    key: str
    snapshot: DocumentSnapshot
    body_start_index: int
    body_end_index: int


@dataclass(frozen=True)
class WriteResult:
    state: WriteState
    key: str
    file_id: str
    base_revision: str
    desired_sha256: str
    resulting_revision: str | None = None
    observed_revision: str | None = None
    observed_sha256: str | None = None
    reason: str | None = None


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le", errors="strict")) // 2


def _plain_body_span(document: Mapping[str, Any], snapshot: DocumentSnapshot) -> tuple[int, int]:
    """Validate a complete plain body and use provider structural UTF-16 indices."""
    tabs = document.get("tabs")
    if not isinstance(tabs, list) or len(tabs) != 1 or snapshot.tab_id is None:
        raise DriveError("UNSUPPORTED_TABS", "Output Doc needs exactly one complete tab")
    tab = tabs[0]
    if not isinstance(tab, dict) or tab.get("childTabs"):
        raise DriveError("UNSUPPORTED_TABS", "Nested output tabs are unsupported")
    document_tab = tab.get("documentTab")
    body = document_tab.get("body") if isinstance(document_tab, dict) else None
    content = body.get("content") if isinstance(body, dict) else None
    if not isinstance(content, list) or not content:
        raise DriveError("DOC_STRUCTURE_INVALID", "Output Doc has no indexed body")
    position = 1
    paragraphs = 0
    text_parts: list[str] = []
    for offset, element in enumerate(content):
        if not isinstance(element, dict):
            raise DriveError("DOC_STRUCTURE_INVALID", "Output Doc has invalid body element")
        if "sectionBreak" in element:
            # Native Docs may omit the initial section break's startIndex; its
            # implicit value is zero, while a present nonzero value is invalid.
            if (offset != 0 or element.get("startIndex", 0) != 0
                    or element.get("endIndex") != 1):
                raise DriveError("DOC_STRUCTURE_INVALID", "Output Doc section index is invalid")
            continue
        paragraph = element.get("paragraph")
        if (not isinstance(paragraph, dict) or paragraph.get("bullet") is not None
                or not isinstance(paragraph.get("elements"), list)
                or not paragraph["elements"]):
            raise DriveError("UNSUPPORTED_DOC_CONTENT", "Output Doc must contain plain paragraphs")
        start = element.get("startIndex")
        end = element.get("endIndex")
        if (type(start) is not int or type(end) is not int or start != position
                or end <= start):
            raise DriveError("DOC_STRUCTURE_INVALID", "Output Doc paragraph indices are invalid")
        paragraph_parts: list[str] = []
        for run in paragraph["elements"]:
            if not isinstance(run, dict) or not isinstance(run.get("textRun"), dict):
                raise DriveError("UNSUPPORTED_DOC_CONTENT", "Output Doc has a nontext run")
            value = run["textRun"].get("content")
            run_start = run.get("startIndex")
            run_end = run.get("endIndex")
            if (not isinstance(value, str) or type(run_start) is not int
                    or type(run_end) is not int or run_start != position):
                raise DriveError("DOC_STRUCTURE_INVALID", "Output Doc run indices are invalid")
            try:
                units = _utf16_units(value)
            except UnicodeEncodeError as error:
                raise DriveError("INVALID_DOC_UTF8", "Output Doc text is not valid Unicode") from error
            if run_end != run_start + units or run_end > end:
                raise DriveError("DOC_STRUCTURE_INVALID", "Output Doc UTF-16 span is invalid")
            position = run_end
            text_parts.append(value)
            paragraph_parts.append(value)
        paragraph_text = "".join(paragraph_parts)
        if (position != end or not paragraph_text.endswith("\n")
                or "\n" in paragraph_text[:-1]):
            raise DriveError("DOC_STRUCTURE_INVALID", "Output Doc paragraph terminator is invalid")
        paragraphs += 1
    if paragraphs == 0 or "".join(text_parts) != snapshot.text or not snapshot.text.endswith("\n"):
        raise DriveError("DOC_STRUCTURE_INVALID", "Output Doc text and indexed body differ")
    return 1, position


def _validate_new_text(text: str, *, max_bytes: int) -> str:
    if not isinstance(text, str) or not text.endswith("\n"):
        raise DriveError("OUTPUT_TEXT_INVALID", "Output Doc text must end with a newline")
    try:
        encoded = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise DriveError("OUTPUT_TEXT_INVALID", "Output Doc text is not valid Unicode") from error
    if len(encoded) > max_bytes:
        raise DriveError("OUTPUT_TEXT_TOO_LARGE", "Output Doc exceeds local byte limit")
    # The Docs API silently strips these characters from InsertText requests.
    if any((ord(char) < 32 and char not in "\n\t")
           or 0xE000 <= ord(char) <= 0xF8FF for char in text):
        raise DriveError("OUTPUT_TEXT_INVALID", "Output Doc contains unsupported controls")
    return hashlib.sha256(encoded).hexdigest()


class DocsCASWriter:
    """Exact-ID output writer anchored to the existing installation index."""

    def __init__(
        self, provider: WriteProvider, attached: AttachedInstallation,
        *, extra_docs: Iterable[RegisteredOutputDoc] = (),
    ) -> None:
        index = attached.index.files
        root_docs = (
            RegisteredOutputDoc("root-receipt", "RECEIPT", index["receipt"].file_id,
                                index["control"].file_id, "receipt"),
            RegisteredOutputDoc("system-state", "STATE", index["state"].file_id,
                                index["control"].file_id, "state"),
        )
        child_docs = tuple(extra_docs)
        if any(doc.app_key in {"receipt", "state"} for doc in child_docs):
            raise DriveError("OUTPUT_REGISTRATION_INVALID", "Root output roles cannot be added")
        registered: dict[str, RegisteredOutputDoc] = {}
        seen_ids: set[str] = set()
        forbidden_ids = {item.file_id for key, item in index.items() if key not in {"receipt", "state"}}
        for doc in (*root_docs, *child_docs):
            if doc.key in registered or doc.file_id in seen_ids or doc.file_id in forbidden_ids:
                raise DriveError("OUTPUT_REGISTRATION_INVALID", "Output Doc ID or key is duplicated")
            registered[doc.key] = doc
            seen_ids.add(doc.file_id)
        self._provider = provider
        self._attached = attached
        self._installation_id = attached.index.installation_id
        self._legacy_root_names = (
            {"root-receipt": index["receipt"].name, "system-state": index["state"].name}
            if attached.index.identity_mode == "owner_manifest" else {}
        )
        self._docs = registered

    def _registered(self, key: str) -> RegisteredOutputDoc:
        try:
            return self._docs[key]
        except KeyError as error:
            raise DriveError("OUTPUT_NOT_REGISTERED", "Output Doc is not locally registered") from error

    def registration(self, key: str) -> RegisteredOutputDoc:
        """Expose the trusted local output registration for caller scope checks."""
        return self._registered(key)

    def _read(self, registered: RegisteredOutputDoc) -> ObservedOutputDoc:
        if registered.key in self._legacy_root_names:
            recheck_attached_index(self._provider, self._attached)
        try:
            metadata = self._provider.get_file(registered.file_id, fields=FILE_FIELDS)
        except (OSError, PermissionError, FileNotFoundError) as error:
            raise DriveError("OUTPUT_ACCESS_MISSING", "Registered output file is unavailable") from error
        if not isinstance(metadata, Mapping):
            raise DriveError("OUTPUT_METADATA_INVALID", "Registered output metadata is invalid")
        if (metadata.get("id") != registered.file_id or metadata.get("trashed") is not False
                or metadata.get("mimeType") != "application/vnd.google-apps.document"
                or metadata.get("parents") != [registered.parent_id]):
            raise DriveError("OUTPUT_IDENTITY_CHANGED", "Registered output file identity changed")
        capabilities = metadata.get("capabilities")
        if not isinstance(capabilities, dict) or capabilities.get("canEdit") is not True:
            raise DriveError("OUTPUT_NOT_WRITABLE", "Registered output Doc is not writable")
        app_properties = metadata.get("appProperties")
        legacy_name = self._legacy_root_names.get(registered.key)
        if legacy_name is None:
            if metadata.get("isAppAuthorized") is not True:
                raise DriveError("OUTPUT_IDENTITY_CHANGED", "Registered output file identity changed")
            if (not isinstance(app_properties, dict)
                    or app_properties.get("cdr_installation") != self._installation_id
                    or app_properties.get("cdr_key") != registered.app_key):
                raise DriveError("OUTPUT_OWNERSHIP_CHANGED", "Registered output Doc ownership changed")
        else:
            if (metadata.get("name") != legacy_name
                    or not isinstance(metadata.get("isAppAuthorized"), bool)):
                raise DriveError("OUTPUT_IDENTITY_CHANGED", "Indexed root output identity changed")
            if app_properties is not None and not isinstance(app_properties, dict):
                raise DriveError("OUTPUT_OWNERSHIP_CHANGED", "Visible root output properties are invalid")
            if isinstance(app_properties, dict) and (
                "cdr_installation" in app_properties or "cdr_key" in app_properties
            ) and (app_properties.get("cdr_installation") != self._installation_id
                   or app_properties.get("cdr_key") != registered.app_key):
                raise DriveError("OUTPUT_OWNERSHIP_CHANGED", "Visible root output properties conflict")
        try:
            raw = self._provider.get_document(registered.file_id, include_tabs_content=True)
        except (OSError, PermissionError, FileNotFoundError) as error:
            raise DriveError("OUTPUT_ACCESS_MISSING", "Registered output Doc is unavailable") from error
        if not isinstance(raw, Mapping):
            raise DriveError("OUTPUT_DOCUMENT_INVALID", "Registered output Doc response is invalid")
        snapshot = extract_document(raw, registered.file_id, max_bytes=_MAX_BYTES[registered.kind])
        start, end = _plain_body_span(raw, snapshot)
        return ObservedOutputDoc(registered.key, snapshot, start, end)

    def read(self, key: str) -> ObservedOutputDoc:
        return self._read(self._registered(key))

    def _observe_after_attempt(self, registered: RegisteredOutputDoc) -> ObservedOutputDoc | None:
        try:
            return self._read(registered)
        except Exception:  # noqa: BLE001 - preserve uncertain write without exposing provider error
            return None

    @staticmethod
    def _uncertain(
        registered: RegisteredOutputDoc, base: str, desired: str, reason: str,
        observed: ObservedOutputDoc | None,
    ) -> WriteResult:
        return WriteResult(
            "UNCERTAIN", registered.key, registered.file_id, base, desired,
            observed_revision=observed.snapshot.revision_id if observed else None,
            observed_sha256=observed.snapshot.sha256 if observed else None,
            reason=reason,
        )

    def replace(self, key: str, text: str, *, expected: ObservedOutputDoc) -> WriteResult:
        """One CAS attempt. An ambiguous outcome is returned and never replayed here."""
        registered = self._registered(key)
        desired = _validate_new_text(text, max_bytes=_MAX_BYTES[registered.kind])
        if expected.key != key or expected.snapshot.file_id != registered.file_id:
            raise DriveError("OUTPUT_EXPECTATION_INVALID", "Expected observation belongs to another Doc")
        current = self._read(registered)
        if (current.snapshot.revision_id != expected.snapshot.revision_id
                or current.snapshot.sha256 != expected.snapshot.sha256
                or current.snapshot.tab_id != expected.snapshot.tab_id
                or current.body_end_index != expected.body_end_index):
            raise DriveError("OUTPUT_CHANGED", "Output Doc changed since caller observed it")
        base = current.snapshot.revision_id
        if current.snapshot.sha256 == desired:
            return WriteResult(
                "UNCHANGED", key, registered.file_id, base, desired,
                resulting_revision=base, observed_revision=base, observed_sha256=desired,
            )
        tab_id = current.snapshot.tab_id
        if tab_id is None:
            raise DriveError("UNSUPPORTED_TABS", "Output Doc tab ID is missing")
        requests: list[Mapping[str, Any]] = []
        if current.body_end_index - 1 > current.body_start_index:
            requests.append({"deleteContentRange": {"range": {
                "startIndex": current.body_start_index,
                "endIndex": current.body_end_index - 1,
                "tabId": tab_id,
            }}})
        if text[:-1]:
            requests.append({"insertText": {
                "location": {"index": current.body_start_index, "tabId": tab_id},
                "text": text[:-1],
            }})
        if not requests:
            raise AssertionError("Changed output text generated no Docs requests")
        if registered.key in self._legacy_root_names:
            recheck_attached_index(self._provider, self._attached)
        try:
            response = self._provider.batch_update_document(
                registered.file_id, requests=requests, required_revision_id=base,
            )
        except Exception:  # noqa: BLE001 - a lost acknowledgement may follow an applied write
            observed = self._observe_after_attempt(registered)
            return self._uncertain(registered, base, desired, "WRITE_ACK_UNCERTAIN", observed)
        observed = self._observe_after_attempt(registered)
        control = response.get("writeControl") if isinstance(response, Mapping) else None
        revision = control.get("requiredRevisionId") if isinstance(control, Mapping) else None
        if not isinstance(revision, str) or not revision:
            return self._uncertain(registered, base, desired, "WRITE_RESPONSE_INVALID", observed)
        if (observed is None or observed.snapshot.revision_id != revision
                or observed.snapshot.sha256 != desired or observed.snapshot.tab_id != tab_id):
            return self._uncertain(registered, base, desired, "READBACK_MISMATCH", observed)
        return WriteResult(
            "CONFIRMED", key, registered.file_id, base, desired,
            resulting_revision=revision, observed_revision=revision, observed_sha256=desired,
        )
