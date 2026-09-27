"""Private, exact-ID workspace Drive registrations for the existing installation.

Provisioning supplies provider-returned IDs. Registration verifies every ID and
its scoped appProperties before an atomic local write. Loading rechecks the same
remote identities. No title search, Drive creation, or command admission occurs.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from runner_app.adapters.drive_docs import FILE_FIELDS, DriveError, RegisteredMailbox
from runner_app.adapters.drive_index import AttachedInstallation
from runner_app.adapters.drive_writer import RegisteredOutputDoc
from runner_app.storage.paths import UnsafePathError, _absolute, ensure_private_directory

_ID = re.compile(r"[A-Za-z0-9_-]{8,256}\Z")
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_FOLDER = "application/vnd.google-apps.folder"
_DOC = "application/vnd.google-apps.document"
_MAX_RECORD_BYTES = 16_384
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_RENAME_NOREPLACE = 1


class DriveRegistryError(DriveError):
    """Sanitized local or remote registration refusal."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


class RegistrationReadProvider(Protocol):
    def get_file(self, file_id: str, *, fields: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class WorkspaceDriveRegistration:
    """Provider-returned resource IDs, scoped to one project/workspace pair."""

    installation_id: str
    project_id: str
    workspace_id: str
    project_folder_id: str
    workspace_folder_id: str
    command_doc_id: str
    changeset_doc_id: str
    receipt_doc_id: str
    latest_result_doc_id: str

    def __post_init__(self) -> None:
        try:
            valid_installation = str(UUID(self.installation_id)) == self.installation_id
        except (ValueError, TypeError, AttributeError):
            valid_installation = False
        ids = (self.project_folder_id, self.workspace_folder_id, self.command_doc_id,
               self.changeset_doc_id, self.receipt_doc_id, self.latest_result_doc_id)
        if (not valid_installation or not isinstance(self.project_id, str)
                or _SLUG.fullmatch(self.project_id) is None
                or not isinstance(self.workspace_id, str)
                or _SLUG.fullmatch(self.workspace_id) is None
                or any(not isinstance(value, str) or _ID.fullmatch(value) is None for value in ids)
                or len(set(ids)) != len(ids)):
            raise DriveRegistryError("REGISTRATION_INVALID", "Workspace registration identity is invalid")

    def mailbox(self) -> RegisteredMailbox:
        return RegisteredMailbox(
            command_id=self.command_doc_id, parent_id=self.workspace_folder_id,
            receipt_id=self.receipt_doc_id, changeset_id=self.changeset_doc_id,
            project_id=self.project_id, workspace_id=self.workspace_id,
        )

    def output_docs(self, *, receipt_key: str, latest_result_key: str
                    ) -> tuple[RegisteredOutputDoc, RegisteredOutputDoc]:
        return (
            RegisteredOutputDoc(receipt_key, "RECEIPT", self.receipt_doc_id,
                                self.workspace_folder_id, "workspace-receipt"),
            RegisteredOutputDoc(latest_result_key, "LATEST_RESULT", self.latest_result_doc_id,
                                self.workspace_folder_id, "latest-result"),
        )


@dataclass(frozen=True)
class _RemoteExpectation:
    file_id: str
    parent_id: str
    name: str
    mime_type: str
    app_key: str
    workspace_scoped: bool


class WorkspaceDriveRegistry:
    """No-replace private records plus current exact-ID Drive read validation."""

    def __init__(self, directory: Path, attached: AttachedInstallation,
                 provider: RegistrationReadProvider) -> None:
        try:
            self.directory = _absolute(Path(directory))
            ensure_private_directory(self.directory)
        except (OSError, UnsafePathError):
            raise DriveRegistryError("REGISTRY_NOT_PRIVATE", "Registration store is unavailable") from None
        projects = attached.index.files.get("projects")
        if projects is None or _ID.fullmatch(projects.file_id) is None:
            raise DriveRegistryError("INSTALLATION_NOT_ATTACHED", "Project root is not indexed")
        self._directory_identity = self._dir_identity()
        self._attached = attached
        self._provider = provider
        self._projects_folder_id = projects.file_id
        self._projects_folder_name = projects.name
        self._root_id = attached.config.root_id

    def _dir_identity(self) -> tuple[int, int]:
        try:
            descriptor = os.open(self.directory, _DIR_FLAGS)
            try:
                info = os.fstat(descriptor)
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o700):
                    raise DriveRegistryError("REGISTRY_NOT_PRIVATE", "Registration store is unsafe")
                return info.st_dev, info.st_ino
            finally:
                os.close(descriptor)
        except OSError:
            raise DriveRegistryError("REGISTRY_NOT_PRIVATE", "Registration store is unavailable") from None

    def _open_dir(self) -> int:
        try:
            descriptor = os.open(self.directory, _DIR_FLAGS)
        except OSError:
            raise DriveRegistryError("REGISTRY_NOT_PRIVATE", "Registration store is unavailable") from None
        info = os.fstat(descriptor)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or (info.st_dev, info.st_ino) != self._directory_identity):
            os.close(descriptor)
            raise DriveRegistryError("REGISTRY_NOT_PRIVATE", "Registration store changed")
        return descriptor

    @staticmethod
    def _filename(project_id: str, workspace_id: str) -> str:
        if (not isinstance(project_id, str) or _SLUG.fullmatch(project_id) is None
                or not isinstance(workspace_id, str) or _SLUG.fullmatch(workspace_id) is None):
            raise DriveRegistryError("REGISTRATION_INVALID", "Project/workspace identity is invalid")
        digest = hashlib.sha256((project_id + "\0" + workspace_id).encode("ascii")).hexdigest()
        return digest + ".json"

    @staticmethod
    def _project_filename(project_id: str) -> str:
        if not isinstance(project_id, str) or _SLUG.fullmatch(project_id) is None:
            raise DriveRegistryError("REGISTRATION_INVALID", "Project identity is invalid")
        return "project-" + hashlib.sha256(project_id.encode("ascii")).hexdigest() + ".json"

    def _expectations(self, value: WorkspaceDriveRegistration) -> tuple[_RemoteExpectation, ...]:
        project = value.project_id
        workspace = value.workspace_id
        return (
            _RemoteExpectation(value.project_folder_id, self._projects_folder_id, project,
                               _FOLDER, f"project:{project}", False),
            _RemoteExpectation(value.workspace_folder_id, value.project_folder_id, workspace,
                               _FOLDER, f"workspace:{project}/{workspace}", True),
            _RemoteExpectation(value.command_doc_id, value.workspace_folder_id, "COMMAND",
                               _DOC, "workspace-command", True),
            _RemoteExpectation(value.changeset_doc_id, value.workspace_folder_id, "CHANGESET",
                               _DOC, "workspace-changeset", True),
            _RemoteExpectation(value.receipt_doc_id, value.workspace_folder_id, "RECEIPT",
                               _DOC, "workspace-receipt", True),
            _RemoteExpectation(value.latest_result_doc_id, value.workspace_folder_id,
                               "LATEST RESULT", _DOC, "latest-result", True),
        )

    def _validate_remote(self, value: WorkspaceDriveRegistration) -> None:
        if value.installation_id != self._attached.index.installation_id:
            raise DriveRegistryError("INSTALLATION_ID_MISMATCH", "Registration belongs to another installation")
        # Attachment can become stale: pin the installed Projects root again
        # before accepting any child registration or output pointer.
        try:
            projects = self._provider.get_file(self._projects_folder_id, fields=FILE_FIELDS)
        except Exception:  # noqa: BLE001 - provider errors may contain credentials
            raise DriveRegistryError("REGISTRATION_ACCESS_MISSING", "Project root is unavailable") from None
        if (not isinstance(projects, Mapping)
                or projects.get("id") != self._projects_folder_id
                or projects.get("name") != self._projects_folder_name
                or projects.get("mimeType") != _FOLDER
                or projects.get("parents") != [self._root_id]
                or projects.get("trashed") is not False
                or projects.get("isAppAuthorized") is not True):
            raise DriveRegistryError("PROJECT_ROOT_CHANGED", "Indexed project root identity changed")
        projects_props = projects.get("appProperties")
        projects_capabilities = projects.get("capabilities")
        if (not isinstance(projects_props, Mapping)
                or projects_props.get("cdr_installation") != value.installation_id
                or projects_props.get("cdr_key") != "projects"
                or not isinstance(projects_capabilities, Mapping)
                or projects_capabilities.get("canAddChildren") is not True):
            raise DriveRegistryError("PROJECT_ROOT_CHANGED", "Indexed project root grant changed")
        indexed_ids = {item.file_id for item in self._attached.index.files.values()}
        if any(expectation.file_id in indexed_ids for expectation in self._expectations(value)):
            raise DriveRegistryError("REGISTRATION_ID_CONFLICT", "Workspace reuses an installation ID")
        for expected in self._expectations(value):
            try:
                metadata = self._provider.get_file(expected.file_id, fields=FILE_FIELDS)
            except Exception:  # noqa: BLE001 - provider exceptions may contain credentials
                raise DriveRegistryError("REGISTRATION_ACCESS_MISSING", "Registered file is unavailable") from None
            if not isinstance(metadata, Mapping):
                raise DriveRegistryError("REGISTRATION_METADATA_INVALID", "Registered metadata is invalid")
            if (metadata.get("id") != expected.file_id or metadata.get("trashed") is not False
                    or metadata.get("parents") != [expected.parent_id]
                    or metadata.get("mimeType") != expected.mime_type
                    or metadata.get("name") != expected.name
                    or metadata.get("isAppAuthorized") is not True):
                raise DriveRegistryError("REGISTRATION_IDENTITY_CHANGED", "Registered file identity changed")
            props = metadata.get("appProperties")
            if (not isinstance(props, Mapping)
                    or props.get("cdr_installation") != value.installation_id
                    or props.get("cdr_key") != expected.app_key
                    or props.get("cdr_project") != value.project_id
                    or (expected.workspace_scoped
                        and props.get("cdr_workspace") != value.workspace_id)):
                raise DriveRegistryError("REGISTRATION_SCOPE_CHANGED", "Registered file scope changed")
            capabilities = metadata.get("capabilities")
            required = "canAddChildren" if expected.mime_type == _FOLDER else "canEdit"
            if not isinstance(capabilities, Mapping) or capabilities.get(required) is not True:
                raise DriveRegistryError("REGISTRATION_CAPABILITY_MISSING", "Registered file grant is insufficient")

    def _read_raw(self, filename: str) -> bytes:
        descriptor = self._open_dir()
        try:
            try:
                file_descriptor = os.open(
                    filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
            except OSError:
                raise DriveRegistryError("REGISTRATION_UNAVAILABLE", "Private registration is unavailable") from None
            try:
                before = os.fstat(file_descriptor)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                        or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                        or before.st_size < 1 or before.st_size > _MAX_RECORD_BYTES):
                    raise DriveRegistryError("REGISTRATION_INVALID", "Private registration is malformed")
                raw = os.read(file_descriptor, before.st_size + 1)
                after = os.fstat(file_descriptor)
                current = os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
                if (len(raw) != before.st_size or os.read(file_descriptor, 1)
                        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                            before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size,
                                                   after.st_mtime_ns, after.st_ctime_ns)
                        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
                    raise DriveRegistryError("REGISTRATION_INVALID", "Private registration changed")
            except OSError:
                raise DriveRegistryError("REGISTRATION_INVALID", "Private registration changed") from None
            finally:
                os.close(file_descriptor)
        finally:
            os.close(descriptor)
        return raw

    def _read(self, filename: str) -> WorkspaceDriveRegistration:
        try:
            parsed = json.loads(self._read_raw(filename).decode("utf-8"))
            expected_keys = {"schema_version", *WorkspaceDriveRegistration.__dataclass_fields__}
            if not isinstance(parsed, dict) or set(parsed) != expected_keys or parsed["schema_version"] != "1.0":
                raise ValueError
            return WorkspaceDriveRegistration(**{
                key: parsed[key] for key in WorkspaceDriveRegistration.__dataclass_fields__
            })
        except DriveRegistryError:
            raise
        except (UnicodeError, TypeError, ValueError, KeyError):
            raise DriveRegistryError("REGISTRATION_INVALID", "Private registration is malformed") from None

    def _anchor(self, value: WorkspaceDriveRegistration) -> dict[str, str]:
        return {
            "schema_version": "1.0", "installation_id": value.installation_id,
            "project_id": value.project_id, "project_folder_id": value.project_folder_id,
        }

    def _require_anchor(self, value: WorkspaceDriveRegistration) -> None:
        try:
            saved = json.loads(self._read_raw(self._project_filename(value.project_id)).decode("utf-8"))
        except DriveRegistryError:
            raise
        except (UnicodeError, ValueError):
            raise DriveRegistryError("REGISTRATION_INVALID", "Project anchor is malformed") from None
        if saved != self._anchor(value):
            raise DriveRegistryError("PROJECT_REGISTRATION_CONFLICT", "Project folder identity differs")

    def _ensure_anchor(self, value: WorkspaceDriveRegistration) -> None:
        raw = (json.dumps(self._anchor(value), sort_keys=True, separators=(",", ":")) + "\n").encode()
        if not self._store_raw(self._project_filename(value.project_id), raw):
            self._require_anchor(value)

    @staticmethod
    def _rename_no_replace(directory_fd: int, temporary: str, target: str) -> bool:
        library = ctypes.CDLL(None, use_errno=True)
        function = getattr(library, "renameat2", None)
        if function is None:
            raise DriveRegistryError("ATOMIC_REGISTRATION_UNAVAILABLE", "Atomic registration is unavailable")
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                             ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        if function(directory_fd, os.fsencode(temporary), directory_fd,
                    os.fsencode(target), _RENAME_NOREPLACE) == 0:
            return True
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            return False
        if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
            raise DriveRegistryError("ATOMIC_REGISTRATION_UNAVAILABLE", "Atomic registration is unavailable")
        raise DriveRegistryError("REGISTRATION_STORE_IO", "Registration could not be stored")

    def _store_raw(self, filename: str, raw: bytes) -> bool:
        if not raw or len(raw) > _MAX_RECORD_BYTES:
            raise DriveRegistryError("REGISTRATION_INVALID", "Registration record is too large")
        directory_fd = self._open_dir()
        temporary = "." + uuid4().hex + ".tmp"
        try:
            file_fd = os.open(temporary, _FILE_FLAGS, 0o600, dir_fd=directory_fd)
            try:
                os.fchmod(file_fd, 0o600)
                view = memoryview(raw)
                while view:
                    written = os.write(file_fd, view)
                    if written <= 0:
                        raise DriveRegistryError("REGISTRATION_STORE_IO", "Registration write made no progress")
                    view = view[written:]
                os.fsync(file_fd)
            finally:
                os.close(file_fd)
            created = self._rename_no_replace(directory_fd, temporary, filename)
            if created:
                os.fsync(directory_fd)
            return created
        except OSError:
            raise DriveRegistryError("REGISTRATION_STORE_IO", "Registration could not be stored") from None
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            finally:
                os.close(directory_fd)

    def register(self, value: WorkspaceDriveRegistration) -> WorkspaceDriveRegistration:
        """Store one verified identity, refusing to replace a different existing record."""
        if not isinstance(value, WorkspaceDriveRegistration):
            raise DriveRegistryError("REGISTRATION_INVALID", "Workspace registration is invalid")
        self._validate_remote(value)
        self._ensure_anchor(value)
        filename = self._filename(value.project_id, value.workspace_id)
        raw = (json.dumps({"schema_version": "1.0", **asdict(value)}, sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        if self._store_raw(filename, raw):
            return value
        existing = self.get(value.project_id, value.workspace_id)
        if existing != value:
            raise DriveRegistryError("REGISTRATION_CONFLICT", "Existing workspace identity differs")
        return existing

    def get(self, project_id: str, workspace_id: str) -> WorkspaceDriveRegistration:
        """Load one private registration and verify current remote identity/grants."""
        value = self._read(self._filename(project_id, workspace_id))
        if value.project_id != project_id or value.workspace_id != workspace_id:
            raise DriveRegistryError("REGISTRATION_SCOPE_CHANGED", "Private registration scope differs")
        self._require_anchor(value)
        self._validate_remote(value)
        return value

    def require_output_pair(
        self, project_id: str, workspace_id: str,
        receipt: RegisteredOutputDoc, latest: RegisteredOutputDoc,
    ) -> WorkspaceDriveRegistration:
        """Bind caller-selected writer Docs to the exact local workspace record."""
        value = self.get(project_id, workspace_id)
        if (receipt.kind != "RECEIPT" or receipt.app_key != "workspace-receipt"
                or receipt.file_id != value.receipt_doc_id
                or receipt.parent_id != value.workspace_folder_id
                or latest.kind != "LATEST_RESULT" or latest.app_key != "latest-result"
                or latest.file_id != value.latest_result_doc_id
                or latest.parent_id != value.workspace_folder_id):
            raise DriveRegistryError("OUTPUT_SCOPE_MISMATCH", "Output Docs differ from workspace registration")
        return value
