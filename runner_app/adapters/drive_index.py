"""Attach to the owner-supplied existing Drive installation by exact IDs only.

The seed is a reference layout. This module never creates files, searches by
title, imports OAuth credentials, or treats a folder grant as recursive access.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from runner_app.adapters.drive_docs import (
    FILE_FIELDS,
    MAX_INDEX_DOC_BYTES,
    DriveError,
    ReadProvider,
    RegisteredMailbox,
    read_document,
)
from runner_app.config import DRIVE_INDEX_ID, DRIVE_ROOT_ID
from runner_app.domain.commands import DomainError, parse_json
from runner_app.storage.paths import UnsafePathError, _open_directory

_DOC = "application/vnd.google-apps.document"
_FOLDER = "application/vnd.google-apps.folder"
_INDEX_HEADER = "CHAT-DEV RUNNER / INSTALLATION INDEX"
_ID = re.compile(r"[A-Za-z0-9_-]{8,256}\Z")

# Installation keys and topology are the stable bootstrap specification. Names
# aid drift diagnosis, but routing always uses the indexed ID.
_LAYOUT: dict[str, tuple[str, str, str | None]] = {
    "root": ("Chat-Dev Runner", _FOLDER, None),
    "control": ("00 Control", _FOLDER, "root"),
    "projects": ("01 Projects", _FOLDER, "root"),
    "results": ("02 Results", _FOLDER, "root"),
    "contracts": ("03 Contracts", _FOLDER, "root"),
    "templates": ("04 Templates", _FOLDER, "root"),
    "start-here": ("START HERE", _DOC, "root"),
    "chat-protocol": ("CHAT PROTOCOL", _DOC, "control"),
    "command": ("COMMAND", _DOC, "control"),
    "receipt": ("RECEIPT", _DOC, "control"),
    "state": ("SYSTEM STATE", _DOC, "control"),
    "recipes": ("COMMAND RECIPES", _DOC, "control"),
    "project-template": ("PROJECT README", _DOC, "templates"),
    "workspace-template": ("WORKSPACE STATE", _DOC, "templates"),
    "context-template": ("SOURCE CONTEXT", _DOC, "templates"),
    "changeset-template": ("CHANGESET", _DOC, "templates"),
    "result-template": ("LATEST RESULT", _DOC, "templates"),
    "output-formats": ("OUTPUT FORMATS", _DOC, "contracts"),
    "schema-application-log.schema": ("application-log.schema.json", "application/json", "contracts"),
    "schema-changeset.schema": ("changeset.schema.json", "application/json", "contracts"),
    "schema-command.schema": ("command.schema.json", "application/json", "contracts"),
    "schema-operation-manifest.schema": (
        "operation-manifest.schema.json", "application/json", "contracts"
    ),
    "schema-output.schema": ("output.schema.json", "application/json", "contracts"),
    "schema-receipt.schema": ("receipt.schema.json", "application/json", "contracts"),
    "schema-run-manifest.schema": ("run-manifest.schema.json", "application/json", "contracts"),
    "installation-index": ("INSTALLATION INDEX", _DOC, "root"),
}
_REQUIRED_WRITABLE_DOCS = frozenset({
    "installation-index", "receipt", "state", "command",
})
_REQUIRED_CHILD_FOLDERS = frozenset({"root", "projects", "results", "control"})
_LEGACY_REQUIRED = frozenset({
    "root", "control", "projects", "results", "contracts", "templates",
    "start-here", "chat-protocol", "command", "receipt", "state", "recipes",
    "installation-index",
})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_LEGACY_MANIFEST_BYTES = 65_536


@dataclass(frozen=True)
class ExistingInstallation:
    root_id: str
    index_id: str
    root_name: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ExistingInstallation:
        if (value.get("schema_version") != "1.0"
                or value.get("mode") != "attach_existing"
                or value.get("bootstrap_allowed") is not False):
            raise DriveError("INSTALLATION_CONFIG_INVALID", "Existing-installation mode is required")
        if value.get("root_folder_id") != DRIVE_ROOT_ID or value.get(
            "installation_index_document_id"
        ) != DRIVE_INDEX_ID or value.get("root_folder_name") != "Chat-Dev Runner":
            raise DriveError("INSTALLATION_ID_MISMATCH", "Owner-supplied installed IDs differ")
        return cls(DRIVE_ROOT_ID, DRIVE_INDEX_ID, "Chat-Dev Runner")

    @classmethod
    def from_file(cls, path: Path) -> ExistingInstallation:
        try:
            raw = path.read_bytes()
            value = parse_json(raw, max_bytes=16_384)
        except (OSError, DomainError) as error:
            raise DriveError("INSTALLATION_CONFIG_INVALID", "Local installation config is invalid") from error
        return cls.from_mapping(value)


@dataclass(frozen=True)
class IndexedFile:
    key: str
    file_id: str
    name: str
    mime_type: str
    parents: tuple[str, ...]
    app_properties: Mapping[str, str]


@dataclass(frozen=True)
class InstallationIndex:
    installation_id: str
    files: Mapping[str, IndexedFile]
    identity_mode: Literal["app_properties", "owner_manifest"] = "app_properties"


@dataclass(frozen=True)
class LegacyInstallationManifest:
    """Owner-private exact-ID map for a pre-existing, non-app-tagged installation.

    This file is local authority, not parsed from a Drive Doc or a title search.
    The Index text digest pins the reviewed human-readable document.
    """

    root_id: str
    index_id: str
    index_text_sha256: str
    installation_id: str
    files: Mapping[str, IndexedFile]

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any], config: ExistingInstallation,
    ) -> LegacyInstallationManifest:
        if (not isinstance(value, Mapping) or value.get("schema_version") != "1.0"
                or value.get("mode") != "owner_verified_legacy"):
            raise DriveError("LEGACY_MANIFEST_INVALID", "Owner manifest mode is invalid")
        root_id = value.get("root_folder_id")
        index_id = value.get("installation_index_document_id")
        digest = value.get("installation_index_text_sha256")
        installation_id = value.get("installation_id")
        if root_id != config.root_id or index_id != config.index_id:
            raise DriveError("INSTALLATION_ID_MISMATCH", "Owner manifest differs from selected installation")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise DriveError("LEGACY_MANIFEST_INVALID", "Index text digest is invalid")
        if not isinstance(installation_id, str) or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            installation_id,
        ):
            raise DriveError("LEGACY_MANIFEST_INVALID", "Installation identity is invalid")
        documents = value.get("documents")
        if not isinstance(documents, dict) or not _LEGACY_REQUIRED.issubset(documents):
            raise DriveError("LEGACY_MANIFEST_INCOMPLETE", "Required existing roles are missing")
        if set(documents) != _LEGACY_REQUIRED:
            raise DriveError("LEGACY_MANIFEST_INVALID", "Owner manifest contains an unreviewed role")
        indexed: dict[str, IndexedFile] = {}
        seen_ids: set[str] = set()
        for key, raw in documents.items():
            if not isinstance(raw, dict) or set(raw) != {"id", "name", "mimeType", "parents"}:
                raise DriveError("LEGACY_MANIFEST_INVALID", "Indexed record has invalid fields")
            file_id, name, mime, parents = (
                raw["id"], raw["name"], raw["mimeType"], raw["parents"]
            )
            if (not isinstance(file_id, str) or _ID.fullmatch(file_id) is None
                    or not isinstance(name, str) or not name or len(name) > 256
                    or not isinstance(mime, str) or mime != _LAYOUT[key][1]
                    or not isinstance(parents, list)
                    or not all(isinstance(parent, str) and _ID.fullmatch(parent)
                               for parent in parents)):
                raise DriveError("LEGACY_MANIFEST_INVALID", "Indexed record is invalid")
            if file_id in seen_ids:
                raise DriveError("DUPLICATE_INDEX_ID", "Two installation roles share one file ID")
            seen_ids.add(file_id)
            if key != "start-here" and name != _LAYOUT[key][0]:
                raise DriveError("LEGACY_MANIFEST_INVALID", "Active role name differs from contract")
            indexed[key] = IndexedFile(key, file_id, name, mime, tuple(parents), {})
        if (indexed["root"].file_id != config.root_id
                or indexed["installation-index"].file_id != config.index_id):
            raise DriveError("INSTALLATION_ID_MISMATCH", "Manifest does not point at selected IDs")
        for key, item in indexed.items():
            parent_key = _LAYOUT[key][2]
            if parent_key is None:
                continue
            parent = indexed.get(parent_key)
            if parent is None or item.parents != (parent.file_id,):
                raise DriveError("INDEX_PARENT_MISMATCH", "Manifest topology differs from installed layout")
        return cls(root_id, index_id, digest, installation_id, MappingProxyType(indexed))

    @classmethod
    def from_file(
        cls, path: Path, config: ExistingInstallation,
    ) -> LegacyInstallationManifest:
        """Read a 0600 regular manifest from a 0700 owner directory, no symlinks."""
        try:
            directory_fd = _open_directory(path.parent, create=False)
            try:
                fd = os.open(
                    path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
                try:
                    before = os.fstat(fd)
                    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                            or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                            or before.st_size < 1 or before.st_size > _MAX_LEGACY_MANIFEST_BYTES):
                        raise OSError("Unsafe manifest file")
                    raw = os.read(fd, before.st_size + 1)
                    after = os.fstat(fd)
                    current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
                    if (len(raw) != before.st_size or before.st_size != after.st_size
                            or before.st_mtime_ns != after.st_mtime_ns
                            or before.st_ctime_ns != after.st_ctime_ns
                            or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
                        raise OSError("Manifest changed during read")
                finally:
                    os.close(fd)
            finally:
                os.close(directory_fd)
            value = parse_json(raw, max_bytes=_MAX_LEGACY_MANIFEST_BYTES)
        except (OSError, UnsafePathError, DomainError) as error:
            raise DriveError("LEGACY_MANIFEST_INVALID", "Owner-private manifest is unavailable") from error
        return cls.from_mapping(value, config)


@dataclass(frozen=True)
class AttachedInstallation:
    config: ExistingInstallation
    index: InstallationIndex
    index_revision: str
    index_text_sha256: str
    metadata: Mapping[str, Mapping[str, Any]]

    def root_mailbox(self) -> RegisteredMailbox:
        return RegisteredMailbox(
            command_id=self.index.files["command"].file_id,
            parent_id=self.index.files["control"].file_id,
            receipt_id=self.index.files["receipt"].file_id,
            legacy_attachment=self if self.index.identity_mode == "owner_manifest" else None,
        )


def parse_installation_index(text: str, config: ExistingInstallation) -> InstallationIndex:
    if not isinstance(text, str) or not text.startswith(_INDEX_HEADER):
        raise DriveError("INDEX_HEADER_INVALID", "Installation Index header is missing")
    opening = text.find("{", len(_INDEX_HEADER))
    if opening < 0:
        raise DriveError("INDEX_JSON_MISSING", "Installation Index has no machine mapping")
    try:
        value = parse_json(text[opening:].encode("utf-8"), max_bytes=MAX_INDEX_DOC_BYTES)
    except (UnicodeEncodeError, DomainError) as error:
        raise DriveError("INDEX_JSON_INVALID", "Installation Index mapping is invalid") from error
    installation_id = value.get("installation_id")
    if not isinstance(installation_id, str) or not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        installation_id,
    ):
        raise DriveError("INDEX_ID_INVALID", "Installation identity must be a canonical UUID")
    documents = value.get("documents")
    if not isinstance(documents, dict):
        raise DriveError("INDEX_LAYOUT_INVALID", "Installation file mapping is missing")
    if set(_LAYOUT) - set(documents):
        raise DriveError("INDEX_LAYOUT_INCOMPLETE", "Required installed files are missing")
    indexed: dict[str, IndexedFile] = {}
    seen_ids: set[str] = set()
    for key, expected in _LAYOUT.items():
        raw = documents.get(key)
        if not isinstance(raw, dict):
            raise DriveError("INDEX_LAYOUT_INVALID", "Indexed file record is invalid")
        file_id = raw.get("id")
        name = raw.get("name")
        mime = raw.get("mimeType")
        parents = raw.get("parents")
        props = raw.get("appProperties", {})
        if (not isinstance(file_id, str) or not _ID.fullmatch(file_id)
                or not isinstance(name, str) or not isinstance(mime, str)
                or not isinstance(parents, list)
                or not all(isinstance(parent, str) for parent in parents)
                or not isinstance(props, dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in props.items())):
            raise DriveError("INDEX_LAYOUT_INVALID", "Indexed file record has invalid fields")
        if file_id in seen_ids:
            raise DriveError("DUPLICATE_INDEX_ID", "Two installation keys share one file ID")
        seen_ids.add(file_id)
        if name != expected[0] or mime != expected[1]:
            raise DriveError("INDEX_LAYOUT_MISMATCH", "Installed file type/name differs from layout")
        if (props.get("cdr_installation") != installation_id
                or props.get("cdr_key") != key):
            raise DriveError("INDEX_OWNERSHIP_MISMATCH", "Indexed app property disagrees with key")
        indexed[key] = IndexedFile(key, file_id, name, mime, tuple(parents), props)
    if indexed["root"].file_id != config.root_id or indexed["installation-index"].file_id != config.index_id:
        raise DriveError("INSTALLATION_ID_MISMATCH", "Index does not point at owner-supplied IDs")
    for key, (_, _, parent_key) in _LAYOUT.items():
        if parent_key is None:
            continue
        expected_parent = indexed[parent_key].file_id
        if indexed[key].parents != (expected_parent,):
            raise DriveError("INDEX_PARENT_MISMATCH", "Indexed resource moved from installed layout")
    return InstallationIndex(installation_id, indexed)


def _read_file(provider: ReadProvider, file_id: str, key: str) -> Mapping[str, Any]:
    try:
        return provider.get_file(file_id, fields=FILE_FIELDS)
    except (OSError, PermissionError, FileNotFoundError) as error:
        raise DriveError(
            "PER_FILE_ACCESS_MISSING", f"OAuth app cannot verify indexed file key {key}"
        ) from error


def _validate_metadata(
    key: str, indexed: IndexedFile, metadata: Mapping[str, Any], installation_id: str,
    *, identity_mode: str = "app_properties",
) -> None:
    if metadata.get("id") != indexed.file_id:
        raise DriveError("FILE_ID_MISMATCH", "Provider returned a different file")
    if metadata.get("trashed") is not False:
        raise DriveError("FILE_UNAVAILABLE", "Indexed file is trashed or status is unknown")
    if metadata.get("name") != indexed.name or metadata.get("mimeType") != indexed.mime_type:
        raise DriveError("FILE_METADATA_MISMATCH", "Indexed file metadata changed")
    if key != "root" and metadata.get("parents") != list(indexed.parents):
        raise DriveError("FILE_MOVED", "Indexed file parent changed")
    props = metadata.get("appProperties")
    if identity_mode == "app_properties":
        if metadata.get("isAppAuthorized") is not True:
            raise DriveError("PER_FILE_GRANT_MISSING", "OAuth app grant for indexed file is absent")
        if not isinstance(props, dict):
            raise DriveError(
                "APP_IDENTITY_UNVERIFIED",
                "OAuth app cannot see the installer-owned file properties; per-file app grant is unresolved",
            )
        if props.get("cdr_installation") != installation_id or props.get("cdr_key") != key:
            raise DriveError("APP_IDENTITY_MISMATCH", "Visible app properties conflict with index")
    elif identity_mode == "owner_manifest":
        if not isinstance(metadata.get("isAppAuthorized"), bool):
            raise DriveError("PER_FILE_GRANT_UNKNOWN", "App authorization status is unknown")
        if props is not None and not isinstance(props, dict):
            raise DriveError("APP_IDENTITY_UNVERIFIED", "Visible app properties are malformed")
        if isinstance(props, dict) and (
            "cdr_installation" in props or "cdr_key" in props
        ) and (props.get("cdr_installation") != installation_id or props.get("cdr_key") != key):
            raise DriveError("APP_IDENTITY_MISMATCH", "Visible app properties conflict with owner manifest")
    else:
        raise DriveError("INDEX_IDENTITY_MODE_INVALID", "Installation identity mode is invalid")
    capabilities = metadata.get("capabilities")
    if not isinstance(capabilities, dict):
        raise DriveError("CAPABILITY_UNKNOWN", "File capabilities were not returned")
    if key in _REQUIRED_WRITABLE_DOCS and capabilities.get("canEdit") is not True:
        raise DriveError("WRITE_CAPABILITY_MISSING", "Required control Doc is not writable")
    if key in _REQUIRED_CHILD_FOLDERS and capabilities.get("canAddChildren") is not True:
        raise DriveError("CHILD_CREATE_CAPABILITY_MISSING", "Required folder cannot receive children")


def _revalidate_legacy_manifest(
    manifest: LegacyInstallationManifest, config: ExistingInstallation,
) -> LegacyInstallationManifest:
    """Do not let direct construction or mutation bypass the local map parser."""
    if not isinstance(manifest, LegacyInstallationManifest) or not isinstance(manifest.files, Mapping):
        raise DriveError("LEGACY_MANIFEST_INVALID", "Owner manifest is invalid")
    documents: dict[str, dict[str, Any]] = {}
    for key, item in manifest.files.items():
        if not isinstance(key, str) or not isinstance(item, IndexedFile) or item.key != key:
            raise DriveError("LEGACY_MANIFEST_INVALID", "Owner manifest record is invalid")
        documents[key] = {
            "id": item.file_id, "name": item.name, "mimeType": item.mime_type,
            "parents": list(item.parents),
        }
    return LegacyInstallationManifest.from_mapping({
        "schema_version": "1.0", "mode": "owner_verified_legacy",
        "root_folder_id": manifest.root_id,
        "installation_index_document_id": manifest.index_id,
        "installation_index_text_sha256": manifest.index_text_sha256,
        "installation_id": manifest.installation_id,
        "documents": documents,
    }, config)


def attach_existing(
    provider: ReadProvider, config: ExistingInstallation, *,
    legacy_manifest: LegacyInstallationManifest | None = None,
) -> AttachedInstallation:
    """Validate exact owner-supplied IDs in strict or pinned legacy mode."""
    root = _read_file(provider, config.root_id, "root")
    if (root.get("id") != config.root_id or root.get("name") != config.root_name
            or root.get("mimeType") != _FOLDER or root.get("trashed") is not False):
        raise DriveError("ROOT_METADATA_MISMATCH", "Owner-supplied root is incompatible")
    index_meta = _read_file(provider, config.index_id, "installation-index")
    if (index_meta.get("id") != config.index_id or index_meta.get("mimeType") != _DOC
            or index_meta.get("parents") != [config.root_id]
            or index_meta.get("trashed") is not False):
        raise DriveError("INDEX_METADATA_MISMATCH", "Owner-supplied index is incompatible")
    document = read_document(provider, config.index_id, max_bytes=MAX_INDEX_DOC_BYTES)
    if legacy_manifest is None:
        index = parse_installation_index(document.text, config)
    else:
        reviewed = _revalidate_legacy_manifest(legacy_manifest, config)
        if document.sha256 != reviewed.index_text_sha256:
            raise DriveError("INDEX_TEXT_MISMATCH", "Reviewed Installation Index text changed")
        for item in reviewed.files.values():
            if re.search(r"(?<![A-Za-z0-9_-])" + re.escape(item.file_id)
                         + r"(?![A-Za-z0-9_-])", document.text) is None:
                raise DriveError(
                    "INDEX_ID_NOT_REFERENCED", "Owner manifest ID is absent from Installation Index",
                )
        index = InstallationIndex(reviewed.installation_id, reviewed.files, "owner_manifest")
    checked: dict[str, Mapping[str, Any]] = {}
    for key, item in index.files.items():
        metadata = root if key == "root" else (
            index_meta if key == "installation-index" else _read_file(provider, item.file_id, key)
        )
        _validate_metadata(
            key, item, metadata, index.installation_id, identity_mode=index.identity_mode,
        )
        checked[key] = metadata
    final_document = read_document(provider, config.index_id, max_bytes=MAX_INDEX_DOC_BYTES)
    if (final_document.revision_id != document.revision_id
            or final_document.sha256 != document.sha256):
        raise DriveError("INPUT_CHANGED", "Installation Index changed during attachment")
    final_index_meta = _read_file(provider, config.index_id, "installation-index")
    _validate_metadata(
        "installation-index", index.files["installation-index"],
        final_index_meta, index.installation_id, identity_mode=index.identity_mode,
    )
    return AttachedInstallation(
        config=config, index=index, index_revision=document.revision_id,
        index_text_sha256=document.sha256, metadata=checked,
    )


def recheck_attached_index(provider: ReadProvider, attached: AttachedInstallation) -> None:
    """Fence use of a long-lived attachment against Index edits or relocation."""
    indexed = attached.index.files.get("installation-index")
    if indexed is None:
        raise DriveError("INSTALLATION_NOT_ATTACHED", "Installation Index is not registered")
    metadata = _read_file(provider, attached.config.index_id, "installation-index")
    _validate_metadata(
        "installation-index", indexed, metadata, attached.index.installation_id,
        identity_mode=attached.index.identity_mode,
    )
    document = read_document(provider, attached.config.index_id, max_bytes=MAX_INDEX_DOC_BYTES)
    if (document.revision_id != attached.index_revision
            or document.sha256 != attached.index_text_sha256):
        raise DriveError("INDEX_CHANGED", "Installation Index changed after attachment")


def list_children_bounded(
    provider: Any, parent_id: str, *, page_size: int = 100,
    max_pages: int = 20, max_files: int = 1000,
) -> tuple[Mapping[str, Any], ...]:
    """Bounded diagnostic enumeration; never used to choose a watched mailbox."""
    if not _ID.fullmatch(parent_id) or min(page_size, max_pages, max_files) < 1 or page_size > 1000:
        raise ValueError("Invalid listing limits or parent ID")
    entries: list[Mapping[str, Any]] = []
    seen_ids: set[str] = set()
    seen_tokens: set[str] = set()
    token: str | None = None
    for _ in range(max_pages):
        page = provider.list_children(parent_id, page_token=token, page_size=page_size)
        files = page.get("files")
        if not isinstance(files, list):
            raise DriveError("PAGINATION_INVALID", "Provider returned an invalid file page")
        for file in files:
            if not isinstance(file, dict) or not isinstance(file.get("id"), str):
                raise DriveError("PAGINATION_INVALID", "Provider returned an invalid file record")
            file_id = file["id"]
            if file_id in seen_ids:
                raise DriveError("PAGINATION_DUPLICATE_ID", "One file appeared on multiple pages")
            seen_ids.add(file_id)
            entries.append(file)
            if len(entries) > max_files:
                raise DriveError("PAGINATION_FILE_LIMIT", "Drive listing exceeded local file cap")
        next_token = page.get("nextPageToken")
        if next_token is None:
            return tuple(entries)
        if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
            raise DriveError("PAGINATION_INVALID", "Provider cursor is invalid or repeated")
        seen_tokens.add(next_token)
        token = next_token
    raise DriveError("PAGINATION_PAGE_LIMIT", "Drive listing exceeded local page cap")
