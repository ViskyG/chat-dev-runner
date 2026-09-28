"""Stopped-runner, owner-local snapshot and same-path restore of durable state.

The archive never contains configuration, tokens, runtime sockets or cache. Managed
Git source is part of durable state; safe relative symlinks are recorded as link
metadata, never dereferenced. An archive is private and is promoted only after
all bytes, the SQLite snapshot and manifest have been durably written.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import posixpath
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from runner_app.engine.singleton import AlreadyRunning, Singleton

from .database import SCHEMA_VERSION, Database
from .paths import (
    Paths,
    UnsafePathError,
    _absolute,
    _check_private,
    _open_directory,
    ensure_private_directory,
    open_private_file,
)

_FORMAT = "chat-dev-runner-state-backup"
_VERSION = 1
_MAX_MANIFEST = 64 * 1024 * 1024
_CHUNK = 1024 * 1024
_STATE_DIRS = frozenset({
    "managed", "outputs", "preparation-evidence", "drive-registrations",
    "drive-frozen-root", "drive-frozen-workspaces", "delivery-bundles",
    "import-handles", "execution-runs", "execution-sources",
})
_IGNORED = frozenset({"runner.lock", "state.sqlite-wal", "state.sqlite-shm", "state.sqlite-journal"})


class BackupError(ValueError):
    """A stable local backup/restore refusal without source content or secrets."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _safe_relative(value: Any) -> str:
    if (not isinstance(value, str) or not value or value.startswith("/")
            or "\x00" in value or "//" in value
            or str(PurePosixPath(value)) != value
            or any(part in {"", ".", ".."} for part in PurePosixPath(value).parts)):
        raise BackupError("BACKUP_MANIFEST_INVALID")
    return value


def _safe_link(path: str, target: Any) -> str:
    if (not isinstance(target, str) or not target or target.startswith("/")
            or "\x00" in target):
        raise BackupError("UNSAFE_STATE_LINK")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    if resolved in {"", ".", ".."} or resolved.startswith(("../", "/")):
        raise BackupError("UNSAFE_STATE_LINK")
    return target


def _owned(info: os.stat_result, *, root_dev: int, directory: bool = False, file: bool = False) -> None:
    if info.st_dev != root_dev or info.st_uid != os.geteuid() or info.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise BackupError("UNSAFE_STATE_ENTRY")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise BackupError("UNSAFE_STATE_ENTRY")
    if file and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise BackupError("UNSAFE_STATE_ENTRY")


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise BackupError("BACKUP_IO_FAILED")
        view = view[written:]


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns,
    )


def _hash_file(path: Path) -> tuple[str, int]:
    descriptor = open_private_file(path)
    try:
        digest = hashlib.sha256()
        size = 0
        while data := os.read(descriptor, _CHUNK):
            digest.update(data)
            size += len(data)
        return digest.hexdigest(), size
    finally:
        os.close(descriptor)


def _copy_state_tree(source_fd: int, archive_root: Path, prefix: str,
                     root_dev: int, entries: list[dict[str, Any]]) -> None:
    for item in sorted(os.scandir(source_fd), key=lambda entry: entry.name):
        name = item.name
        relative = f"{prefix}/{name}" if prefix else name
        _safe_relative(relative)
        before = item.stat(follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            if before.st_uid != os.geteuid() or before.st_dev != root_dev:
                raise BackupError("UNSAFE_STATE_LINK")
            target = _safe_link(relative, os.readlink(name, dir_fd=source_fd))
            after = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            if not _same_file(before, after):
                raise BackupError("STATE_CHANGED_DURING_BACKUP")
            entries.append({"path": relative, "type": "symlink", "target": target})
            continue
        if stat.S_ISDIR(before.st_mode):
            _owned(before, root_dev=root_dev, directory=True)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=source_fd)
            try:
                if not _same_file(before, os.fstat(child)):
                    raise BackupError("STATE_CHANGED_DURING_BACKUP")
                ensure_private_directory(archive_root / relative)
                entries.append({"path": relative, "type": "directory"})
                _copy_state_tree(child, archive_root, relative, root_dev, entries)
                if not _same_file(before, os.fstat(child)):
                    raise BackupError("STATE_CHANGED_DURING_BACKUP")
            finally:
                os.close(child)
            continue
        _owned(before, root_dev=root_dev, file=True)
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=source_fd)
        try:
            if not _same_file(before, os.fstat(source)):
                raise BackupError("STATE_CHANGED_DURING_BACKUP")
            destination = open_private_file(archive_root / relative, create=True)
            try:
                digest = hashlib.sha256()
                size = 0
                while data := os.read(source, _CHUNK):
                    _write_all(destination, data)
                    digest.update(data)
                    size += len(data)
                os.fsync(destination)
            finally:
                os.close(destination)
            if size != before.st_size or not _same_file(before, os.fstat(source)):
                raise BackupError("STATE_CHANGED_DURING_BACKUP")
            entries.append({"path": relative, "type": "file", "sha256": digest.hexdigest(),
                            "size": size, "executable": bool(before.st_mode & 0o111)})
        finally:
            os.close(source)


def _sqlite_snapshot(source: Path, destination: Path) -> int:
    if not os.path.lexists(source):
        raise BackupError("STATE_DATABASE_MISSING")
    _check_private(source.lstat(), directory=False)
    os.close(open_private_file(destination, create=True))
    with closing(Database(source).connect()) as opened, closing(sqlite3.connect(destination)) as target:
        version = int(opened.execute("PRAGMA user_version").fetchone()[0])
        if version < 1 or version > SCHEMA_VERSION:
            raise BackupError("STATE_SCHEMA_UNSUPPORTED")
        opened.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise BackupError("STATE_DATABASE_INVALID")
        target.commit()
    os.chmod(destination, 0o600)
    descriptor = open_private_file(destination)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return version


def _sync_directories(root: Path) -> None:
    for directory, _, _ in os.walk(root, topdown=False, followlinks=False):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _rename_new(source: Path, target: Path) -> None:
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
        failure = ctypes.get_errno()
        if failure == errno.EEXIST:
            raise BackupError("BACKUP_TARGET_EXISTS")
        raise BackupError("BACKUP_PROMOTION_FAILED")
    try:
        parent = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except OSError:
        # The fully written target now exists, but power-loss durability is uncertain.
        raise BackupError("PROMOTION_DURABILITY_UNCERTAIN") from None


def _private_parent(path: Path) -> None:
    try:
        descriptor = _open_directory(path, create=False)
    except (UnsafePathError, OSError):
        raise BackupError("UNSAFE_BACKUP_PATH") from None
    os.close(descriptor)


def create_backup(paths: Paths, archive_path: Path) -> dict[str, Any]:
    """Snapshot all known durable state while the runner singleton is stopped.

    ``archive_path`` must be absent beneath an existing 0700 owner directory.
    Configuration and credentials are intentionally outside the archive.
    """
    try:
        source_root, archive = _absolute(paths.state_dir), _absolute(archive_path)
    except UnsafePathError:
        raise BackupError("UNSAFE_BACKUP_PATH") from None
    if archive == source_root or archive.is_relative_to(source_root) or source_root.is_relative_to(archive):
        raise BackupError("BACKUP_PATH_OVERLAP")
    for excluded in (paths.config_dir, paths.token_dir, paths.cache_dir, paths.runtime_dir):
        if excluded is None:
            continue
        excluded = _absolute(excluded)
        if (excluded == source_root or excluded.is_relative_to(source_root)
                or source_root.is_relative_to(excluded)):
            raise BackupError("BACKUP_PATH_OVERLAP")
    _private_parent(archive.parent)
    if os.path.lexists(archive):
        raise BackupError("BACKUP_TARGET_EXISTS")
    _private_parent(source_root)
    if not os.path.lexists(paths.database_path):
        raise BackupError("STATE_DATABASE_MISSING")
    try:
        singleton = Singleton(source_root / "runner.lock")
    except AlreadyRunning:
        raise BackupError("RUNNER_RUNNING") from None
    except UnsafePathError:
        raise BackupError("UNSAFE_STATE_ENTRY") from None
    stage: Path | None = None
    try:
        stage = Path(tempfile.mkdtemp(prefix=f".{archive.name}.partial-", dir=archive.parent))
        ensure_private_directory(stage)
        state_copy = stage / "state"
        ensure_private_directory(state_copy)
        schema = _sqlite_snapshot(paths.database_path, state_copy / "state.sqlite")
        database_hash, database_size = _hash_file(state_copy / "state.sqlite")
        entries: list[dict[str, Any]] = [{
            "path": "state.sqlite", "type": "file", "sha256": database_hash,
            "size": database_size, "executable": False,
        }]
        root_fd = os.open(source_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            root_before = os.fstat(root_fd)
            root_dev = root_before.st_dev
            for item in sorted(os.scandir(root_fd), key=lambda entry: entry.name):
                if item.name in _IGNORED or item.name == "state.sqlite":
                    continue
                if item.name not in _STATE_DIRS or not item.is_dir(follow_symlinks=False):
                    raise BackupError("UNKNOWN_STATE_ENTRY")
                child = os.open(item.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=root_fd)
                try:
                    before = item.stat(follow_symlinks=False)
                    _owned(before, root_dev=root_dev, directory=True)
                    if not _same_file(before, os.fstat(child)):
                        raise BackupError("STATE_CHANGED_DURING_BACKUP")
                    ensure_private_directory(state_copy / item.name)
                    entries.append({"path": item.name, "type": "directory"})
                    _copy_state_tree(child, state_copy, item.name, root_dev, entries)
                    if not _same_file(before, os.fstat(child)):
                        raise BackupError("STATE_CHANGED_DURING_BACKUP")
                finally:
                    os.close(child)
            if not _same_file(root_before, os.fstat(root_fd)):
                raise BackupError("STATE_CHANGED_DURING_BACKUP")
        finally:
            os.close(root_fd)
        manifest = {
            "format": _FORMAT, "version": _VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_state_dir": str(source_root), "database_schema_version": schema,
            "entries": sorted(entries, key=lambda entry: entry["path"]),
        }
        serialized = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(serialized) > _MAX_MANIFEST:
            raise BackupError("BACKUP_MANIFEST_TOO_LARGE")
        descriptor = open_private_file(stage / "manifest.json", create=True)
        try:
            _write_all(descriptor, serialized)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _sync_directories(stage)
        current_fd = os.open(source_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if not _same_file(root_before, os.fstat(current_fd)):
                raise BackupError("STATE_CHANGED_DURING_BACKUP")
        finally:
            os.close(current_fd)
        _rename_new(stage, archive)
        stage = None
        return {"archive": str(archive), "manifest_sha256": hashlib.sha256(serialized).hexdigest(),
                "files": sum(entry["type"] == "file" for entry in entries),
                "symlinks": sum(entry["type"] == "symlink" for entry in entries),
                "database_schema_version": schema}
    except BackupError:
        raise
    except UnsafePathError:
        raise BackupError("UNSAFE_STATE_ENTRY") from None
    except (OSError, sqlite3.Error, ValueError, AttributeError):
        raise BackupError("BACKUP_IO_FAILED") from None
    finally:
        singleton.close()
        if stage is not None and stage.exists():
            shutil.rmtree(stage)


def _manifest(archive: Path) -> tuple[dict[str, Any], bytes]:
    descriptor = _open_directory(archive, create=False)
    os.close(descriptor)
    if set(os.listdir(archive)) != {"manifest.json", "state"}:
        raise BackupError("BACKUP_MANIFEST_INVALID")
    state = archive / "state"
    descriptor = _open_directory(state, create=False)
    os.close(descriptor)
    descriptor = open_private_file(archive / "manifest.json")
    try:
        raw = os.read(descriptor, _MAX_MANIFEST + 1)
        if len(raw) > _MAX_MANIFEST or os.read(descriptor, 1):
            raise BackupError("BACKUP_MANIFEST_INVALID")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise BackupError("BACKUP_MANIFEST_INVALID") from None
    if (not isinstance(value, dict) or set(value) != {
        "format", "version", "created_at", "source_state_dir",
        "database_schema_version", "entries",
    } or value["format"] != _FORMAT or value["version"] != _VERSION
            or not isinstance(value["created_at"], str)
            or type(value["database_schema_version"]) is not int
            or not 1 <= value["database_schema_version"] <= SCHEMA_VERSION
            or not isinstance(value["entries"], list)):
        raise BackupError("BACKUP_MANIFEST_INVALID")
    try:
        if str(_absolute(Path(value["source_state_dir"]))) != value["source_state_dir"]:
            raise BackupError("BACKUP_MANIFEST_INVALID")
    except (TypeError, UnsafePathError):
        raise BackupError("BACKUP_MANIFEST_INVALID") from None
    entries = value["entries"]
    if len(entries) > 1_000_000:
        raise BackupError("BACKUP_MANIFEST_INVALID")
    previous = ""
    directories: set[str] = set()
    expected_files: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or "path" not in entry or "type" not in entry:
            raise BackupError("BACKUP_MANIFEST_INVALID")
        path = _safe_relative(entry["path"])
        kind = entry["type"]
        if path <= previous or (path != "state.sqlite" and path.split("/", 1)[0] not in _STATE_DIRS):
            raise BackupError("BACKUP_MANIFEST_INVALID")
        previous = path
        parent = posixpath.dirname(path)
        if parent and parent not in directories:
            raise BackupError("BACKUP_MANIFEST_INVALID")
        if kind == "directory":
            if set(entry) != {"path", "type"} or path == "state.sqlite":
                raise BackupError("BACKUP_MANIFEST_INVALID")
            directories.add(path)
        elif kind == "file":
            if (set(entry) != {"path", "type", "sha256", "size", "executable"}
                    or not isinstance(entry["sha256"], str)
                    or len(entry["sha256"]) != 64
                    or any(letter not in "0123456789abcdef" for letter in entry["sha256"])
                    or type(entry["size"]) is not int or entry["size"] < 0
                    or type(entry["executable"]) is not bool
                    or (path == "state.sqlite" and entry["executable"])):
                raise BackupError("BACKUP_MANIFEST_INVALID")
            expected_files.add(path)
        elif kind == "symlink":
            if set(entry) != {"path", "type", "target"} or path == "state.sqlite":
                raise BackupError("BACKUP_MANIFEST_INVALID")
            try:
                _safe_link(path, entry["target"])
            except BackupError:
                raise BackupError("BACKUP_MANIFEST_INVALID") from None
        else:
            raise BackupError("BACKUP_MANIFEST_INVALID")
    if "state.sqlite" not in expected_files:
        raise BackupError("BACKUP_MANIFEST_INVALID")
    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    for current, dirnames, filenames in os.walk(state, followlinks=False):
        relative_parent = Path(current).relative_to(state)
        for name in dirnames:
            entry_path = Path(current) / name
            _check_private(entry_path.lstat(), directory=True)
            actual_dirs.add((relative_parent / name).as_posix())
        for name in filenames:
            entry_path = Path(current) / name
            _check_private(entry_path.lstat(), directory=False)
            actual_files.add((relative_parent / name).as_posix())
    if actual_dirs != directories or actual_files != expected_files:
        raise BackupError("BACKUP_MANIFEST_INVALID")
    for entry in entries:
        if entry["type"] == "file":
            digest, size = _hash_file(state / entry["path"])
            if digest != entry["sha256"] or size != entry["size"]:
                raise BackupError("BACKUP_HASH_MISMATCH")
    database = state / "state.sqlite"
    uri = f"file:{quote(str(database))}?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            if (connection.execute("PRAGMA integrity_check").fetchone() != ("ok",)
                    or int(connection.execute("PRAGMA user_version").fetchone()[0])
                    != value["database_schema_version"]):
                raise BackupError("BACKUP_DATABASE_INVALID")
    except sqlite3.Error:
        raise BackupError("BACKUP_DATABASE_INVALID") from None
    return value, raw


def _restore_file(source: Path, target: Path, entry: dict[str, Any]) -> None:
    reader = open_private_file(source)
    writer = open_private_file(target, create=True)
    try:
        before = os.fstat(reader)
        digest = hashlib.sha256()
        size = 0
        while data := os.read(reader, _CHUNK):
            _write_all(writer, data)
            digest.update(data)
            size += len(data)
        os.fsync(writer)
        if (not _same_file(before, os.fstat(reader)) or digest.hexdigest() != entry["sha256"]
                or size != entry["size"]):
            raise BackupError("BACKUP_HASH_MISMATCH")
    finally:
        os.close(reader)
        os.close(writer)
    if entry["executable"]:
        os.chmod(target, 0o700)


def restore_backup(archive_path: Path, target_state_dir: Path) -> dict[str, Any]:
    """Verify a private archive and restore only to its original, absent state path.

    Existing state is never replaced. The caller must keep the service stopped and
    move a damaged state directory aside before requesting recovery.
    """
    try:
        archive, target = _absolute(archive_path), _absolute(target_state_dir)
    except UnsafePathError:
        raise BackupError("UNSAFE_BACKUP_PATH") from None
    if archive == target or archive.is_relative_to(target) or target.is_relative_to(archive):
        raise BackupError("BACKUP_PATH_OVERLAP")
    _private_parent(archive.parent)
    _private_parent(target.parent)
    if os.path.lexists(target):
        raise BackupError("RESTORE_TARGET_EXISTS")
    stage: Path | None = None
    try:
        manifest, raw = _manifest(archive)
        if manifest["source_state_dir"] != str(target):
            raise BackupError("TARGET_PATH_MISMATCH")
        stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent))
        ensure_private_directory(stage)
        archive_state = archive / "state"
        for entry in manifest["entries"]:
            path = stage / entry["path"]
            if entry["type"] == "directory":
                ensure_private_directory(path)
            elif entry["type"] == "file":
                _restore_file(archive_state / entry["path"], path, entry)
        for entry in manifest["entries"]:
            if entry["type"] == "symlink":
                path = stage / entry["path"]
                os.symlink(entry["target"], path)
        _sync_directories(stage)
        try:
            _rename_new(stage, target)
        except BackupError as error:
            if error.code == "BACKUP_TARGET_EXISTS":
                raise BackupError("RESTORE_TARGET_EXISTS") from None
            raise
        stage = None
        return {"state_dir": str(target), "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                "files": sum(entry["type"] == "file" for entry in manifest["entries"]),
                "symlinks": sum(entry["type"] == "symlink" for entry in manifest["entries"]),
                "database_schema_version": manifest["database_schema_version"]}
    except BackupError:
        raise
    except UnsafePathError:
        raise BackupError("BACKUP_MANIFEST_INVALID") from None
    except (OSError, sqlite3.Error, ValueError, AttributeError):
        raise BackupError("RESTORE_IO_FAILED") from None
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)
