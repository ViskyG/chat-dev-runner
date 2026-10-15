"""Exact-tab v2 Docs mailbox; one revision-guarded write and readback only.

The caller supplies a trusted local document/tab registration and an already
configured provider. No installation-root discovery, legacy receipt writer,
credential access, document creation or automatic write retry occurs here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from runner_app.adapters.drive_auth import CLIENT_FILENAME, DRIVE_SCOPE, TOKEN_FILENAME
from runner_app.adapters.drive_docs import FILE_FIELDS, DriveError, extract_document
from runner_app.adapters.drive_index import (
    AttachedInstallation,
    ExistingInstallation,
    LegacyInstallationManifest,
    attach_existing,
)
from runner_app.adapters.drive_writer import (
    WriteProvider,
    _plain_body_span,
    _validate_new_text,
)
from runner_app.config import DRIVE_INDEX_ID, DRIVE_ROOT_ID
from runner_app.domain.commands import DomainError
from runner_app.domain.contracts_v2 import parse_contract_json
from runner_app.domain.mailbox_v2 import validate_mailbox_shape
from runner_app.engine.intake_v2 import AckResult, Observation
from runner_app.storage.paths import Paths, open_private_file

_DOCUMENT_ID = re.compile(r"[A-Za-z0-9_-]{8,256}\Z", re.ASCII)
_TAB_ID = re.compile(r"[A-Za-z0-9._-]{1,256}\Z", re.ASCII)
_DEFINITE_WRITE_REJECTIONS = frozenset({
    "STALE_REVISION", "STALE_REVISION_OR_INVALID_BATCH", "PER_FILE_GRANT_MISSING",
    "OAUTH_SCOPE_INSUFFICIENT", "FILE_UNAVAILABLE", "PER_FILE_ACCESS_MISSING",
})


class DocsMailboxError(OSError):
    """A stable observation failure without provider or credential details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DriveDocsAPI(WriteProvider, Protocol):
    """Existing GoogleReadProvider-compatible API; writes must not be retried."""


@dataclass(frozen=True)
class DocsProviderConfig:
    """Owner-only references; loading never reads OAuth/client/token contents.

    Runtime order is from_file, load_authorized_credentials(credential_paths(...)),
    provider creation, attach(provider), then mailbox construction. Google SDKs
    and network access remain behind the existing explicit factories.
    """

    oauth_client_file: Path
    token_file: Path
    legacy_manifest_file: Path | None = None

    @classmethod
    def from_file(cls, config_path: Path) -> DocsProviderConfig:
        try:
            descriptor = open_private_file(Path(config_path))
            with os.fdopen(descriptor, "rb") as stream:
                if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) != 0o600:
                    raise DocsMailboxError("DOCS_CONFIG_PERMISSIONS")
                raw = stream.read(65_537)
            value = parse_contract_json(raw, max_bytes=65_536)
            required = {"oauth_client_file", "token_file", "scope", "root_folder_id",
                        "installation_index_document_id"}
            if (not required <= value.keys() or value.keys() - required - {"legacy_manifest_file"}
                    or value["scope"] != DRIVE_SCOPE or value["root_folder_id"] != DRIVE_ROOT_ID
                    or value["installation_index_document_id"] != DRIVE_INDEX_ID):
                raise DocsMailboxError("DOCS_CONFIG_IDENTITY_OR_SCOPE_INVALID")

            def reference(name: str) -> Path:
                raw_path = value[name]
                if not isinstance(raw_path, str):
                    raise DocsMailboxError("DOCS_CONFIG_REFERENCE_INVALID")
                path = Path(raw_path)

                def repository_parent(parent: Path) -> bool:
                    git = parent / ".git"
                    return (git.is_file() or (git.is_dir() and (git / "HEAD").is_file())
                            or parent.name.endswith(".git") and (parent / "HEAD").is_file())

                if (not path.is_absolute() or ".." in path.parts or path.resolve() != path
                        or Path(__file__).resolve().parents[2] in path.parents
                        or any(repository_parent(parent) for parent in path.parents)):
                    raise DocsMailboxError("DOCS_CONFIG_REFERENCE_INVALID")
                info = path.lstat()  # Metadata only; never open/read credential contents here.
                if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_nlink != 1 or info.st_uid != os.geteuid()):
                    raise DocsMailboxError("DOCS_CONFIG_REFERENCE_INVALID")
                return path

            return cls(reference("oauth_client_file"), reference("token_file"),
                       reference("legacy_manifest_file") if "legacy_manifest_file" in value else None)
        except DocsMailboxError:
            raise
        except Exception:  # noqa: BLE001 - owner config errors must not expose credential references
            raise DocsMailboxError("DOCS_CONFIG_INVALID") from None

    def credential_paths(self, paths: Paths) -> Paths:
        """Adapt existing credential layout without copying, renaming or reading files."""
        directory = self.oauth_client_file.parent
        if (self.oauth_client_file.name != CLIENT_FILENAME
                or self.token_file != directory / "tokens" / TOKEN_FILENAME):
            raise DocsMailboxError("DOCS_AUTH_LAYOUT_UNSUPPORTED")
        return replace(paths, config_dir=directory)

    def attach(self, provider: DriveDocsAPI) -> AttachedInstallation:
        """Mandatory runtime read-only attachment before enabling Docs mailbox writes."""
        installation = ExistingInstallation.from_mapping({
            "schema_version": "1.0", "mode": "attach_existing", "bootstrap_allowed": False,
            "root_folder_name": "Chat-Dev Runner", "root_folder_id": DRIVE_ROOT_ID,
            "installation_index_document_id": DRIVE_INDEX_ID,
        })
        manifest = (LegacyInstallationManifest.from_file(self.legacy_manifest_file, installation)
                    if self.legacy_manifest_file is not None else None)
        return attach_existing(provider, installation, legacy_manifest=manifest)


@dataclass(frozen=True)
class _TabObservation:
    observation: Observation
    document_revision: str
    start: int
    end: int


class DocsMailboxPort:
    """A locally enrolled native Google Doc tab carrying strict plain JSON.

    Observation bytes exclude exactly the compulsory final Docs newline; every
    other byte of user formatting is retained. Inserted ACK bytes are therefore
    identical to the coordinator's durable intent and exact readback hash.
    """

    def __init__(self, provider: DriveDocsAPI, *, document_id: str, tab_id: str,
                 max_bytes: int = 262_144) -> None:
        if (not isinstance(document_id, str) or _DOCUMENT_ID.fullmatch(document_id) is None
                or not isinstance(tab_id, str) or _TAB_ID.fullmatch(tab_id) is None
                or type(max_bytes) is not int or not 1 <= max_bytes <= 262_144):
            raise DocsMailboxError("INVALID_DOCS_MAILBOX_BINDING")
        self.provider, self.document_id, self.tab_id = provider, document_id, tab_id
        self.max_bytes = max_bytes

    def _tab(self, document: Mapping[str, Any]) -> Mapping[str, Any]:
        tabs = document.get("tabs")
        if not isinstance(tabs, list) or not tabs:
            raise DocsMailboxError("DOCS_TAB_MISSING")
        pending = list(tabs)
        seen: set[str] = set()
        selected: Mapping[str, Any] | None = None
        inspected = 0
        while pending:
            tab = pending.pop()
            inspected += 1
            if inspected > 256 or not isinstance(tab, Mapping):
                raise DocsMailboxError("DOCS_TABS_INVALID")
            properties = tab.get("tabProperties")
            actual = properties.get("tabId") if isinstance(properties, Mapping) else None
            if (not isinstance(actual, str) or _TAB_ID.fullmatch(actual) is None
                    or actual in seen):
                raise DocsMailboxError("DOCS_TABS_INVALID")
            seen.add(actual)
            if actual == self.tab_id:
                selected = tab
            children = tab.get("childTabs", [])
            if not isinstance(children, list) or len(pending) + len(children) > 256:
                raise DocsMailboxError("DOCS_TABS_INVALID")
            pending.extend(children)
        if selected is None:
            raise DocsMailboxError("DOCS_TAB_MISSING")
        return selected

    def _revision(self, revision: str, payload: bytes) -> str:
        bound = {"document_id": self.document_id, "tab_id": self.tab_id,
                 "revision_id": revision, "sha256": hashlib.sha256(payload).hexdigest()}
        encoded = json.dumps(bound, sort_keys=True, separators=(",", ":")).encode()
        return "docs-v2:" + base64.urlsafe_b64encode(encoded).decode("ascii")

    def _read(self) -> _TabObservation:
        try:
            metadata = self.provider.get_file(self.document_id, fields=FILE_FIELDS)
            if (not isinstance(metadata, Mapping) or metadata.get("id") != self.document_id
                    or metadata.get("trashed") is not False
                    or metadata.get("mimeType") != "application/vnd.google-apps.document"
                    or metadata.get("isAppAuthorized") is not True):
                raise DocsMailboxError("DOCS_FILE_IDENTITY_CHANGED")
            capabilities = metadata.get("capabilities")
            if not isinstance(capabilities, Mapping) or capabilities.get("canEdit") is not True:
                raise DocsMailboxError("DOCS_MAILBOX_NOT_WRITABLE")
            document = self.provider.get_document(self.document_id, include_tabs_content=True)
            if not isinstance(document, Mapping) or document.get("documentId") != self.document_id:
                raise DocsMailboxError("DOCS_DOCUMENT_IDENTITY_CHANGED")
            revision = document.get("revisionId")
            if not isinstance(revision, str) or not 1 <= len(revision) <= 1024:
                raise DocsMailboxError("DOCS_REVISION_MISSING")
            selected = self._tab(document)
            # Existing body validation handles this tab's UTF-16 indices. Other
            # tabs and child tabs are neither interpreted nor included in writes.
            projected = {"documentId": self.document_id, "revisionId": revision,
                         "tabs": [{**selected, "childTabs": []}]}
            snapshot = extract_document(projected, self.document_id, max_bytes=self.max_bytes + 1)
            start, end = _plain_body_span(projected, snapshot)
            payload = snapshot.raw_bytes[:-1]
            if len(payload) > self.max_bytes:
                raise DocsMailboxError("DOCS_MAILBOX_TOO_LARGE")
            parse_contract_json(payload)  # No Markdown, duplicate keys or non-object JSON.
            observation = Observation(payload, self._revision(revision, payload))
            return _TabObservation(observation, revision, start, end)
        except DocsMailboxError:
            raise
        except (DriveError, DomainError) as error:
            raise DocsMailboxError(error.code) from None
        except Exception:  # noqa: BLE001 - provider exceptions may expose private account details
            raise DocsMailboxError("DOCS_OBSERVATION_FAILED") from None

    def read_current(self) -> Observation:
        return self._read().observation

    def _validate_ack(self, observed: Observation, desired: bytes) -> str:
        if (not isinstance(observed.payload, bytes) or not isinstance(desired, bytes)
                or len(observed.payload) > self.max_bytes or len(desired) > self.max_bytes
                or not isinstance(observed.revision, str) or not observed.revision.startswith("docs-v2:")
                or len(observed.revision) > 8192):
            raise DocsMailboxError("INVALID_DOCS_ACK")
        bound = json.loads(base64.b64decode(observed.revision[8:], altchars=b"-_", validate=True))
        if (not isinstance(bound, dict) or bound.get("document_id") != self.document_id
                or bound.get("tab_id") != self.tab_id
                or not isinstance(bound.get("revision_id"), str)
                or not 1 <= len(bound["revision_id"]) <= 1024
                or bound.get("sha256") != hashlib.sha256(observed.payload).hexdigest()):
            raise DocsMailboxError("INVALID_DOCS_ACK_OBSERVATION")
        before, after = parse_contract_json(observed.payload), parse_contract_json(desired)
        validate_mailbox_shape(before)
        validate_mailbox_shape(after)
        mutable = {"dispatch", "receipt", "slot_token"}
        if ({key: value for key, value in before.items() if key not in mutable}
                != {key: value for key, value in after.items() if key not in mutable}
                or UUID(before["slot_token"]) == UUID(after["slot_token"])
                or after["dispatch"] == "READY"
                or after["dispatch"] == "DRAFT" and before["dispatch"] != "DRAFT"):
            raise DocsMailboxError("DOCS_ACK_MUTATES_USER_PAYLOAD")
        text = desired.decode("utf-8", errors="strict")
        _validate_new_text(text + "\n", max_bytes=self.max_bytes + 1)
        return text

    def ack_if_current(self, observed: Observation, desired: bytes) -> AckResult:
        try:
            text = self._validate_ack(observed, desired)
        except (DocsMailboxError, DriveError, DomainError, ValueError, TypeError, KeyError, AttributeError):
            return AckResult("FAILED")
        digest = hashlib.sha256(desired).hexdigest()

        def proof(current: Observation) -> AckResult:
            return AckResult("APPLIED", current.revision, digest)

        try:
            current = self._read()
        except OSError:
            return AckResult("FAILED")  # No write has been attempted.
        if current.observation.payload == desired:
            return proof(current.observation)
        if (current.observation.revision != observed.revision
                or current.observation.payload != observed.payload):
            return AckResult("CONFLICT")
        requests: list[Mapping[str, Any]] = []
        if current.end - 1 > current.start:
            requests.append({"deleteContentRange": {"range": {
                "startIndex": current.start, "endIndex": current.end - 1, "tabId": self.tab_id,
            }}})
        requests.append({"insertText": {
            "location": {"index": current.start, "tabId": self.tab_id}, "text": text,
        }})
        rejected = False
        try:
            self.provider.batch_update_document(
                self.document_id, requests=requests, required_revision_id=current.document_revision,
            )
        except DriveError as error:
            rejected = error.code in _DEFINITE_WRITE_REJECTIONS
        except Exception:  # noqa: BLE001 - a sent batch may have lost its response
            rejected = False
        try:
            after = self.read_current()
        except OSError:
            return AckResult("FAILED" if rejected else "UNKNOWN")
        if after.payload == desired:
            return proof(after)
        if rejected:
            return AckResult("CONFLICT" if after.revision != observed.revision else "FAILED")
        return AckResult("UNKNOWN")
