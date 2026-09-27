"""Owner-issued, durable handles for local Git checkout imports.

The remote command transports only an opaque handle. Its pathname and inode
binding stay in runner-owned private state. A later Git import should use
``open_source`` and keep its descriptor open while copying the source.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runner_app.domain.commands import DomainError
from runner_app.storage.paths import (
    UnsafePathError,
    _open_directory,
    ensure_private_directory,
    open_private_file,
)

_DIRECTORY_FLAGS = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_HANDLE_LENGTH = 48  # 192 random bits, within command.schema.json's 48-character limit.
_MAX_RECORD_BYTES = 8192


def _path(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value) or len(str(value)) > 4096:
        raise DomainError("INVALID_IMPORT_PATH", "Требуется абсолютный локальный каталог.")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise DomainError("INVALID_IMPORT_PATH", "Требуется абсолютный локальный каталог.")
    return path


def _open_chain(path: Path, *, code: str) -> tuple[int, tuple[tuple[int, int], ...]]:
    """Pin every path component without following a symlink or crossing `..`."""
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    identities = []
    try:
        root = os.fstat(descriptor)
        identities.append((root.st_dev, root.st_ino))
        for part in path.parts[1:]:
            next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            info = os.fstat(descriptor)
            identities.append((info.st_dev, info.st_ino))
        return descriptor, tuple(identities)
    except OSError as error:
        os.close(descriptor)
        raise DomainError(code, "Локальный каталог недоступен или изменился.") from error


def _git_directory_identity(source_fd: int, *, code: str) -> tuple[int, int]:
    try:
        source = os.fstat(source_fd)
        if source.st_uid != os.geteuid():
            raise ValueError("Source has a different owner")
        git_fd = os.open(".git", _DIRECTORY_FLAGS, dir_fd=source_fd)
        try:
            marker = os.fstat(git_fd)
            if marker.st_uid != os.geteuid():
                raise ValueError("Git metadata has a different owner")
            head_fd = os.open("HEAD", os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=git_fd)
            try:
                if not stat.S_ISREG(os.fstat(head_fd).st_mode):
                    raise ValueError("Git HEAD is not a regular file")
            finally:
                os.close(head_fd)
            return marker.st_dev, marker.st_ino
        finally:
            os.close(git_fd)
    except (OSError, ValueError) as error:
        raise DomainError(code, "Локальный источник не является безопасным Git-каталогом.") from error


@dataclass(frozen=True)
class _Root:
    path: Path
    identities: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class PinnedImport:
    """Source identity pinned by an open directory descriptor.

    Callers must keep the surrounding ``open_source`` context alive until the
    managed copy has completed. The fd is deliberately not serialized into a
    command, result, or response.
    """

    path: Path
    directory_fd: int

    @property
    def child_path(self) -> str:
        """Path pinned for a child process given ``pass_fds=self.pass_fds``."""
        return f"/proc/self/fd/{self.directory_fd}"

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return (self.directory_fd,)


class ImportGrants:
    def __init__(self, store_dir: Path, *, roots: Sequence[Path] = ()) -> None:
        configured: list[_Root] = []
        for raw in roots:
            root = _path(raw)
            descriptor, identities = _open_chain(root, code="INVALID_IMPORT_ROOT")
            os.close(descriptor)
            configured.append(_Root(root, identities))
        self._roots = tuple(configured)
        self._store_dir = store_dir
        ensure_private_directory(store_dir)

    @property
    def configured(self) -> bool:
        return bool(self._roots)

    def _root_for(self, path: Path, identities: tuple[tuple[int, int], ...]) -> _Root | None:
        candidates = [
            root for root in self._roots
            if path.parts[:len(root.path.parts)] == root.path.parts
            and identities[:len(root.identities)] == root.identities
        ]
        return max(candidates, key=lambda root: len(root.path.parts), default=None)

    def grant(self, path: str) -> str:
        if not self._roots:
            raise DomainError("IMPORT_NOT_CONFIGURED", "Локальный импорт не настроен владельцем.")
        source_path = _path(path)
        source_fd, identities = _open_chain(source_path, code="INVALID_IMPORT_PATH")
        try:
            root = self._root_for(source_path, identities)
            if root is None:
                raise DomainError("IMPORT_NOT_APPROVED", "Каталог не входит в разрешённые корни.")
            git_identity = _git_directory_identity(source_fd, code="IMPORT_SOURCE_UNSAFE")
            record = {
                "version": 1,
                "path": str(source_path),
                "root": str(root.path),
                "identities": identities,
                "git_identity": git_identity,
            }
            content = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if len(content) > _MAX_RECORD_BYTES:
                raise DomainError("INVALID_IMPORT_PATH", "Путь к каталогу слишком длинный.")
            directory_fd = _open_directory(self._store_dir)
            try:
                for _ in range(3):
                    handle = secrets.token_hex(24)
                    try:
                        record_fd = os.open(handle, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                            os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                                            dir_fd=directory_fd)
                    except FileExistsError:
                        continue
                    try:
                        os.fchmod(record_fd, 0o600)
                        while content:
                            content = content[os.write(record_fd, content):]
                        os.fsync(record_fd)
                    except BaseException:
                        os.unlink(handle, dir_fd=directory_fd)
                        raise
                    finally:
                        os.close(record_fd)
                    sync_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                                      dir_fd=directory_fd)
                    try:
                        os.fsync(sync_fd)
                    finally:
                        os.close(sync_fd)
                    return handle
                raise RuntimeError("Could not allocate a unique import handle")
            finally:
                os.close(directory_fd)
        finally:
            os.close(source_fd)

    def _record(self, handle: str) -> dict[str, Any]:
        if len(handle) != _HANDLE_LENGTH or any(ch not in "0123456789abcdef" for ch in handle):
            raise DomainError("IMPORT_NOT_APPROVED", "Дескриптор импорта не разрешён.")
        try:
            descriptor = open_private_file(self._store_dir / handle)
        except (OSError, UnsafePathError) as error:
            raise DomainError("IMPORT_NOT_APPROVED", "Дескриптор импорта не разрешён.") from error
        try:
            if os.fstat(descriptor).st_size > _MAX_RECORD_BYTES:
                raise ValueError("Oversized import record")
            chunks = bytearray()
            while True:
                chunk = os.read(descriptor, _MAX_RECORD_BYTES + 1 - len(chunks))
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > _MAX_RECORD_BYTES:
                    raise ValueError("Oversized import record")
            value = json.loads(chunks)
            if not isinstance(value, dict) or set(value) != {
                "version", "path", "root", "identities", "git_identity"
            } or type(value["version"]) is not int or value["version"] != 1:
                raise ValueError("Invalid import record")
            return value
        except (ValueError, UnicodeDecodeError, TypeError) as error:
            raise DomainError("IMPORT_NOT_APPROVED", "Дескриптор импорта не разрешён.") from error
        finally:
            os.close(descriptor)

    @contextmanager
    def open_source(self, handle: str) -> Iterator[PinnedImport]:
        if not self._roots:
            raise DomainError("IMPORT_NOT_CONFIGURED", "Локальный импорт не настроен владельцем.")
        record = self._record(handle)
        try:
            path = _path(record["path"])
            saved = tuple(tuple(pair) for pair in record["identities"])
            marker = tuple(record["git_identity"])
            if (len(saved) != len(path.parts) or len(marker) != 2
                    or any(len(pair) != 2 or any(type(item) is not int for item in pair)
                           for pair in saved)
                    or any(type(item) is not int for item in marker)):
                raise ValueError("Invalid identity binding")
            root = next((item for item in self._roots if str(item.path) == record["root"]), None)
            if root is None:
                raise DomainError("IMPORT_NOT_APPROVED", "Корень импорта больше не разрешён.")
        except (KeyError, TypeError, ValueError) as error:
            raise DomainError("IMPORT_NOT_APPROVED", "Дескриптор импорта не разрешён.") from error
        descriptor, current = _open_chain(path, code="IMPORT_SOURCE_CHANGED")
        try:
            if (current != saved or self._root_for(path, current) != root
                    or _git_directory_identity(descriptor, code="IMPORT_SOURCE_CHANGED") != marker):
                raise DomainError("IMPORT_SOURCE_CHANGED", "Локальный источник изменился.")
            yield PinnedImport(path, descriptor)
        finally:
            os.close(descriptor)

    def resolve(self, handle: str) -> Path:
        """Validate a handle for authorization; use open_source for copying."""
        with self.open_source(handle) as source:
            return source.path
