"""Exact-ID Drive folder/file writes with bounded app-property reconciliation.

A create/upload is attempted once. A lost acknowledgement is never followed by
an automatic second create: callers must reconcile the stable appProperties key
and persist the returned provider ID before deciding a later operation step.
Services are injected; constructing this adapter imports no Google package and
makes no request. The existing installation's root/index are never created here.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from runner_app.adapters.drive_docs import DriveError
from runner_app.adapters.drive_index import AttachedInstallation
from runner_app.storage.paths import UnsafePathError, open_private_file

FOLDER_MIME = "application/vnd.google-apps.folder"
METADATA_FIELDS = (
    "id,name,mimeType,parents,trashed,isAppAuthorized,appProperties,webViewLink,"
    "capabilities(canEdit,canAddChildren),size,md5Checksum,sha256Checksum"
)
LIST_FIELDS = f"nextPageToken,files({METADATA_FIELDS})"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # Multipart upload bound from Drive guidance.
_ID = re.compile(r"[A-Za-z0-9_-]{8,256}\Z")
_INSTALLATION_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
_KEY = re.compile(r"[A-Za-z0-9_./:-]{1,117}\Z")  # cdr_key + value <= 124 bytes.
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MD5 = re.compile(r"[0-9a-f]{32}\Z")
_MEDIA_TYPE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:; charset=utf-8)?\Z")


class DriveUploadError(DriveError):
    """Sanitized provider failure with explicit uncertain-write semantics."""

    def __init__(self, code: str, *, effect_uncertain: bool = False) -> None:
        self.effect_uncertain = effect_uncertain
        super().__init__(code, code)


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _identity(parent_id: str, installation_id: str, key: str) -> None:
    if (not _valid_id(parent_id) or not isinstance(installation_id, str)
            or _INSTALLATION_ID.fullmatch(installation_id) is None
            or not isinstance(key, str) or _KEY.fullmatch(key) is None):
        raise DriveUploadError("UPLOAD_IDENTITY_INVALID")


def _name(value: str) -> None:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise DriveUploadError("UPLOAD_NAME_INVALID")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        raise DriveUploadError("UPLOAD_NAME_INVALID") from None
    if size > 200 or any(ord(char) < 32 for char in value):
        raise DriveUploadError("UPLOAD_NAME_INVALID")


def _scope_properties(installation_id: str, key: str) -> dict[str, str]:
    return {"cdr_installation": installation_id, "cdr_key": key}


def _validate_metadata(
    metadata: Mapping[str, Any], *, file_id: str, parent_id: str | None = None,
    installation_id: str | None = None, key: str | None = None,
    mime_type: str | None = None, name: str | None = None,
    sha256: str | None = None, size_bytes: int | None = None,
    md5: str | None = None, legacy_results_parent: bool = False,
) -> None:
    if metadata.get("id") != file_id or metadata.get("trashed") is not False:
        raise DriveUploadError("UPLOAD_FILE_IDENTITY_MISMATCH")
    actual_mime = metadata.get("mimeType")
    if not isinstance(actual_mime, str) or (mime_type is not None and actual_mime != mime_type):
        raise DriveUploadError("UPLOAD_FILE_TYPE_MISMATCH")
    if parent_id is not None and metadata.get("parents") != [parent_id]:
        raise DriveUploadError("UPLOAD_FILE_MOVED")
    if name is not None and metadata.get("name") != name:
        raise DriveUploadError("UPLOAD_FILE_NAME_CHANGED")
    props = metadata.get("appProperties")
    if legacy_results_parent:
        # The indexed folder may belong to the installing OAuth app. Its
        # current-app private properties can be absent, but visible identity
        # must either be wholly absent or match the verified attachment.
        if props is not None and not isinstance(props, dict):
            raise DriveUploadError("UPLOAD_APP_IDENTITY_MISSING")
        if isinstance(props, dict):
            if not all(isinstance(prop, str) and isinstance(value, str)
                       for prop, value in props.items()):
                raise DriveUploadError("UPLOAD_APP_IDENTITY_MISSING")
            visible = {prop for prop in props if prop.startswith("cdr_")}
            if (visible and (visible != {"cdr_installation", "cdr_key"}
                             or props["cdr_installation"] != installation_id
                             or props["cdr_key"] != key)):
                raise DriveUploadError("UPLOAD_APP_IDENTITY_MISMATCH")
        if props is None:
            props = {}
        capabilities = metadata.get("capabilities")
        if not isinstance(capabilities, dict) or capabilities.get("canAddChildren") is not True:
            raise DriveUploadError("UPLOAD_PARENT_NOT_WRITABLE")
    else:
        if metadata.get("isAppAuthorized") is not True:
            raise DriveUploadError("UPLOAD_PER_FILE_GRANT_MISSING")
        if not isinstance(props, dict):
            raise DriveUploadError("UPLOAD_APP_IDENTITY_MISSING")
        if installation_id is not None and props.get("cdr_installation") != installation_id:
            raise DriveUploadError("UPLOAD_APP_IDENTITY_MISMATCH")
        if key is not None and props.get("cdr_key") != key:
            raise DriveUploadError("UPLOAD_APP_IDENTITY_MISMATCH")
    if sha256 is not None:
        if props.get("cdr_sha256") != sha256:
            raise DriveUploadError("UPLOAD_CONTENT_DIGEST_MISMATCH")
        remote_sha = metadata.get("sha256Checksum")
        if remote_sha is not None and remote_sha != sha256:
            raise DriveUploadError("UPLOAD_CONTENT_DIGEST_MISMATCH")
        # cdr_sha256 is supplied by our own request; it is not a substitute for
        # a provider-calculated checksum when reconciling an uncertain upload.
        if remote_sha is None and md5 is None:
            raise DriveUploadError("UPLOAD_CHECKSUM_UNVERIFIED")
    if (size_bytes is not None
            and (props.get("cdr_size") != str(size_bytes)
                 or metadata.get("size") != str(size_bytes))):
        raise DriveUploadError("UPLOAD_CONTENT_SIZE_MISMATCH")
    if md5 is not None and metadata.get("md5Checksum") != md5:
        raise DriveUploadError("UPLOAD_CONTENT_DIGEST_MISMATCH")


def _read_source(path: Path, *, size_bytes: int, sha256: str) -> tuple[bytes, str]:
    if (isinstance(size_bytes, bool) or not isinstance(size_bytes, int)
            or size_bytes < 0 or size_bytes > MAX_UPLOAD_BYTES
            or not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None):
        raise DriveUploadError("UPLOAD_SOURCE_INVALID")
    try:
        descriptor = open_private_file(Path(path))
    except (UnsafePathError, OSError):
        raise DriveUploadError("UPLOAD_SOURCE_UNSAFE") from None
    try:
        if os.fstat(descriptor).st_size != size_bytes:
            raise DriveUploadError("UPLOAD_SOURCE_CHANGED")
        content = bytearray()
        while len(content) <= size_bytes:
            chunk = os.read(descriptor, min(65_536, size_bytes + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if (len(content) != size_bytes or os.fstat(descriptor).st_size != size_bytes
                or hashlib.sha256(content).hexdigest() != sha256):
            raise DriveUploadError("UPLOAD_SOURCE_CHANGED")
        return bytes(content), hashlib.md5(content, usedforsecurity=False).hexdigest()
    finally:
        os.close(descriptor)


def _default_media(content: bytes, media_type: str) -> Any:
    from googleapiclient.http import MediaIoBaseUpload  # type: ignore

    return MediaIoBaseUpload(io.BytesIO(content), mimetype=media_type, resumable=False)


class GoogleUploadProvider:
    """Single-attempt writes; caller owns durable effect journal and reconciliation."""

    def __init__(
        self, drive_service: Any, *, media_factory: Callable[[bytes, str], Any] | None = None,
        page_size: int = 100, max_pages: int = 10, max_files: int = 1000,
        attached: AttachedInstallation | None = None,
    ) -> None:
        if (page_size < 1 or page_size > 1000 or max_pages < 1 or max_pages > 100
                or max_files < 1 or max_files > 10_000):
            raise ValueError("Invalid bounded Drive listing limits")
        self._drive = drive_service
        self._media_factory = media_factory or _default_media
        self._page_size = page_size
        self._max_pages = max_pages
        self._max_files = max_files
        if attached is not None and not isinstance(attached, AttachedInstallation):
            raise TypeError("A verified Drive installation attachment is required")
        self._attached = attached

    def _legacy_results_parent(self, file_id: str, installation_id: str | None
                               ) -> tuple[str, str] | None:
        """Return the one indexed legacy Results contract, never a general grant."""
        attached = self._attached
        if (attached is None or attached.index.identity_mode != "owner_manifest"
                or installation_id != attached.index.installation_id):
            return None
        root = attached.index.files.get("root")
        results = attached.index.files.get("results")
        index = attached.index.files.get("installation-index")
        checked = attached.metadata.get("results")
        if (root is None or results is None or index is None
                or not isinstance(checked, Mapping)
                or root.file_id != attached.config.root_id
                or index.file_id != attached.config.index_id
                or results.file_id != file_id or results.key != "results"
                or results.name != "02 Results" or results.mime_type != FOLDER_MIME
                or results.parents != (root.file_id,)
                or checked.get("id") != file_id or checked.get("name") != results.name
                or checked.get("mimeType") != FOLDER_MIME
                or checked.get("parents") != [root.file_id]
                or checked.get("trashed") is not False):
            return None
        capabilities = checked.get("capabilities")
        if not isinstance(capabilities, Mapping) or capabilities.get("canAddChildren") is not True:
            return None
        return root.file_id, results.name

    def _read_request(self, request: Any) -> Mapping[str, Any]:
        try:
            value = request.execute(num_retries=0)
        except Exception:  # noqa: BLE001 - provider errors may contain credentials
            raise DriveUploadError("UPLOAD_PROVIDER_READ_FAILED") from None
        if not isinstance(value, dict):
            raise DriveUploadError("UPLOAD_PROVIDER_RESPONSE_INVALID")
        return value

    def get_file(
        self, file_id: str, *, expected_parent_id: str | None = None,
        expected_installation_id: str | None = None, expected_key: str | None = None,
        expected_mime_type: str | None = None, expected_name: str | None = None,
        expected_sha256: str | None = None, expected_size_bytes: int | None = None,
        expected_md5: str | None = None,
    ) -> Mapping[str, Any]:
        """Read one registered ID and optionally validate full remote identity/content."""
        if not _valid_id(file_id):
            raise DriveUploadError("UPLOAD_IDENTITY_INVALID")
        if expected_parent_id is not None and not _valid_id(expected_parent_id):
            raise DriveUploadError("UPLOAD_IDENTITY_INVALID")
        if expected_installation_id is not None and (
            not isinstance(expected_installation_id, str)
            or _INSTALLATION_ID.fullmatch(expected_installation_id) is None
        ):
            raise DriveUploadError("UPLOAD_IDENTITY_INVALID")
        if expected_key is not None and (
            not isinstance(expected_key, str) or _KEY.fullmatch(expected_key) is None
        ):
            raise DriveUploadError("UPLOAD_IDENTITY_INVALID")
        if expected_sha256 is not None and (
            not isinstance(expected_sha256, str) or _SHA256.fullmatch(expected_sha256) is None
        ):
            raise DriveUploadError("UPLOAD_SOURCE_INVALID")
        if expected_md5 is not None and (
            not isinstance(expected_md5, str) or _MD5.fullmatch(expected_md5) is None
        ):
            raise DriveUploadError("UPLOAD_SOURCE_INVALID")
        legacy = self._legacy_results_parent(file_id, expected_installation_id)
        legacy_results_parent = (
            legacy is not None and expected_parent_id == legacy[0]
            and expected_key == "results" and expected_mime_type == FOLDER_MIME
            and expected_name == legacy[1] and expected_sha256 is None
            and expected_size_bytes is None and expected_md5 is None
        )
        value = self._read_request(self._drive.files().get(fileId=file_id, fields=METADATA_FIELDS))
        _validate_metadata(
            value, file_id=file_id, parent_id=expected_parent_id,
            installation_id=expected_installation_id, key=expected_key,
            mime_type=expected_mime_type, name=expected_name,
            sha256=expected_sha256, size_bytes=expected_size_bytes, md5=expected_md5,
            legacy_results_parent=legacy_results_parent,
        )
        return value

    def _parent(self, parent_id: str, installation_id: str) -> None:
        legacy = self._legacy_results_parent(parent_id, installation_id)
        if legacy is None:
            parent = self.get_file(parent_id, expected_installation_id=installation_id,
                                   expected_mime_type=FOLDER_MIME)
        else:
            parent = self.get_file(
                parent_id, expected_parent_id=legacy[0],
                expected_installation_id=installation_id, expected_key="results",
                expected_mime_type=FOLDER_MIME, expected_name=legacy[1],
            )
        capabilities = parent.get("capabilities")
        if not isinstance(capabilities, dict) or capabilities.get("canAddChildren") is not True:
            raise DriveUploadError("UPLOAD_PARENT_NOT_WRITABLE")

    def find_by_key(
        self, parent_id: str, installation_id: str, key: str, *,
        expected_mime_type: str | None = None, expected_name: str | None = None,
        expected_sha256: str | None = None, expected_size_bytes: int | None = None,
        expected_md5: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Bounded exact app-property lookup only for uncertain-write reconciliation."""
        _identity(parent_id, installation_id, key)
        self._parent(parent_id, installation_id)
        # Search app identity globally, then verify the exact parent by ID. A
        # moved app-owned file must be detected, never mistaken for absence.
        query = (
            f"appProperties has {{ key='cdr_installation' and value='{installation_id}' }} "
            f"and appProperties has {{ key='cdr_key' and value='{key}' }}"
        )
        matches: list[str] = []
        seen_ids: set[str] = set()
        seen_tokens: set[str] = set()
        page_token: str | None = None
        for _ in range(self._max_pages):
            page = self._read_request(self._drive.files().list(
                q=query, fields=LIST_FIELDS, pageSize=self._page_size,
                pageToken=page_token,
            ))
            files = page.get("files")
            if not isinstance(files, list):
                raise DriveUploadError("UPLOAD_LIST_INVALID")
            for value in files:
                if not isinstance(value, dict) or not _valid_id(value.get("id")):
                    raise DriveUploadError("UPLOAD_LIST_INVALID")
                file_id = value["id"]
                if file_id in seen_ids:
                    raise DriveUploadError("UPLOAD_LIST_DUPLICATE_ID")
                seen_ids.add(file_id)
                matches.append(file_id)
                if len(seen_ids) > self._max_files:
                    raise DriveUploadError("UPLOAD_LIST_LIMIT")
            next_token = page.get("nextPageToken")
            if next_token is None:
                break
            if (not isinstance(next_token, str) or not next_token
                    or next_token in seen_tokens):
                raise DriveUploadError("UPLOAD_LIST_INVALID")
            seen_tokens.add(next_token)
            page_token = next_token
        else:
            raise DriveUploadError("UPLOAD_LIST_LIMIT")
        if len(matches) > 1:
            raise DriveUploadError("UPLOAD_DUPLICATE_KEY")
        if not matches:
            return None
        return self.get_file(
            matches[0], expected_parent_id=parent_id,
            expected_installation_id=installation_id, expected_key=key,
            expected_mime_type=expected_mime_type, expected_name=expected_name,
            expected_sha256=expected_sha256, expected_size_bytes=expected_size_bytes,
            expected_md5=expected_md5,
        )

    def _create(
        self, parent_id: str, installation_id: str, key: str, name: str,
        mime_type: str, *, media_body: Any = None,
        sha256: str | None = None, size_bytes: int | None = None,
        md5: str | None = None,
    ) -> Mapping[str, Any]:
        _identity(parent_id, installation_id, key)
        _name(name)
        self._parent(parent_id, installation_id)
        properties = _scope_properties(installation_id, key)
        if sha256 is not None and size_bytes is not None:
            properties.update(cdr_sha256=sha256, cdr_size=str(size_bytes))
        body = {"name": name, "mimeType": mime_type, "parents": [parent_id],
                "appProperties": properties}
        try:
            request = self._drive.files().create(
                body=body, media_body=media_body, fields=METADATA_FIELDS,
            )
            created = request.execute(num_retries=0)
        except Exception as error:  # noqa: BLE001 - no retry, sanitize provider error
            status = getattr(getattr(error, "resp", None), "status", None)
            if status in (400, 401, 403, 404):
                raise DriveUploadError("UPLOAD_PROVIDER_WRITE_REJECTED") from None
            raise DriveUploadError("UPLOAD_WRITE_UNCERTAIN", effect_uncertain=True) from None
        if not isinstance(created, dict) or not _valid_id(created.get("id")):
            raise DriveUploadError("UPLOAD_WRITE_UNCERTAIN", effect_uncertain=True)
        try:
            return self.get_file(
                created["id"], expected_parent_id=parent_id,
                expected_installation_id=installation_id, expected_key=key,
                expected_mime_type=mime_type, expected_name=name,
                expected_sha256=sha256, expected_size_bytes=size_bytes,
                expected_md5=md5,
            )
        except DriveUploadError:
            raise DriveUploadError("UPLOAD_VERIFY_UNCERTAIN", effect_uncertain=True) from None

    def create_folder(
        self, parent_id: str, installation_id: str, key: str, name: str,
    ) -> Mapping[str, Any]:
        """Create one folder under a verified parent; no retry."""
        return self._create(parent_id, installation_id, key, name, FOLDER_MIME)

    def upload_file(
        self, parent_id: str, installation_id: str, key: str, name: str,
        path: Path, media_type: str, size_bytes: int, sha256: str,
    ) -> Mapping[str, Any]:
        """Verify one private bundle file, then make one immutable-byte upload."""
        _identity(parent_id, installation_id, key)
        _name(name)
        if not isinstance(media_type, str) or _MEDIA_TYPE.fullmatch(media_type) is None:
            raise DriveUploadError("UPLOAD_MEDIA_TYPE_INVALID")
        content, md5 = _read_source(Path(path), size_bytes=size_bytes, sha256=sha256)
        try:
            media_body = self._media_factory(content, media_type)
        except Exception:  # noqa: BLE001 - optional provider import/errors are sanitized
            raise DriveUploadError("UPLOAD_MEDIA_PREPARATION_FAILED") from None
        return self._create(
            parent_id, installation_id, key, name, media_type.split(";", 1)[0],
            media_body=media_body,
            sha256=sha256, size_bytes=size_bytes, md5=md5,
        )
