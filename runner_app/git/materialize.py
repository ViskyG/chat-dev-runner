"""Filter-free, bounded materialization of an exact retained Git snapshot.

This builds a new source tree from Git object bytes without checking out a
worktree. Git hooks, attributes, smudge filters, LFS and local edits cannot
change the bytes. The sealed tree is a broker input, not an execution sandbox;
the broker rehashes it immediately before starting a rootless container.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from runner_app.storage.paths import (
    UnsafePathError,
    _absolute,
    _open_directory,
    ensure_private_directory,
)

from .argv import GitError, run_git, run_git_bytes
from .changes import ChangeError, safe_source_path
from .worktrees import _inspect_config, _reject_object_alternates

if TYPE_CHECKING:
    from runner_app.executors.broker import SnapshotInput

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_MAX_FILES = 20_000
_MAX_BLOB_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_RENAME_NOREPLACE = 1
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class MaterializeError(ValueError):
    """Stable local refusal; Git stderr, source bytes and private paths are omitted."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _uuid(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise MaterializeError("INVALID_SNAPSHOT_ID", "A canonical snapshot UUID is required") from None
    return value


def _oid(value: str) -> str:
    if not isinstance(value, str) or _OID.fullmatch(value) is None:
        raise MaterializeError("INVALID_GIT_OBJECT", "A full lowercase Git object ID is required")
    return value


@contextmanager
def _pinned_repo(repo: Path) -> Iterator[tuple[Path, tuple[int, ...]]]:
    try:
        repo = _absolute(Path(repo))
    except UnsafePathError:
        raise MaterializeError("INVALID_REPOSITORY", "Managed repository path is unsafe") from None
    if repo.name != "repo.git" or _SLUG.fullmatch(repo.parent.name) is None:
        raise MaterializeError("INVALID_REPOSITORY", "Expected an owner-managed bare repository")
    try:
        descriptor = _open_directory(repo, create=False)
    except (OSError, UnsafePathError):
        raise MaterializeError("INVALID_REPOSITORY", "Managed repository is unavailable") from None
    try:
        yield Path(f"/proc/self/fd/{descriptor}"), (descriptor,)
    finally:
        os.close(descriptor)


def _verify_identity(repo: Path, inherited: tuple[int, ...], snapshot_id: str,
                     commit: str, tree: str) -> None:
    ref = f"refs/cdr/snapshots/{snapshot_id}"
    try:
        direct = run_git(repo, ["rev-parse", "--verify", ref], pass_fds=inherited).strip()
        kind = run_git(repo, ["cat-file", "-t", commit], pass_fds=inherited).strip()
        actual_tree = run_git(repo, ["rev-parse", "--verify", f"{commit}^{{tree}}"],
                              pass_fds=inherited).strip()
    except GitError:
        raise MaterializeError("SNAPSHOT_REF_MISSING", "Retained Git snapshot is unavailable") from None
    if direct != commit or kind != "commit" or actual_tree != tree:
        raise MaterializeError("SNAPSHOT_REF_CHANGED", "Retained Git snapshot differs from its identity")


def _listing(repo: Path, inherited: tuple[int, ...], commit: str
             ) -> list[tuple[str, str, int, bool]]:
    try:
        raw = run_git_bytes(repo, ["ls-tree", "-r", "-z", "-l", "--full-tree", commit],
                            pass_fds=inherited)
    except GitError:
        raise MaterializeError("SNAPSHOT_TREE_UNAVAILABLE", "Git snapshot tree is unavailable") from None
    if raw and not raw.endswith(b"\0"):
        raise MaterializeError("INVALID_GIT_TREE", "Git returned an incomplete tree listing")
    entries: list[tuple[str, str, int, bool]] = []
    seen_files: set[str] = set()
    seen_dirs: set[str] = set()
    total = 0
    for record in raw[:-1].split(b"\0") if raw else ():
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if separator != b"\t" or len(fields) != 4:
            raise MaterializeError("INVALID_GIT_TREE", "Git tree entry is malformed")
        mode, kind, raw_oid, raw_size = fields
        if mode == b"120000":
            raise MaterializeError("UNSUPPORTED_SYMLINK", "Source snapshots with links need separate policy")
        if mode != b"100644" and mode != b"100755":
            raise MaterializeError("UNSUPPORTED_TREE_ENTRY", "Source snapshot has an unsupported entry")
        if kind != b"blob":
            raise MaterializeError("INVALID_GIT_TREE", "Regular source entry is not a Git blob")
        try:
            path = raw_path.decode("utf-8", "strict")
            safe_source_path(path)
            object_id = raw_oid.decode("ascii", "strict")
            size = int(raw_size)
        except (UnicodeDecodeError, ValueError, ChangeError):
            raise MaterializeError("UNSAFE_SOURCE_PATH", "Source tree has an unsafe name or size") from None
        if _OID.fullmatch(object_id) is None or len(object_id) != len(commit):
            raise MaterializeError("INVALID_GIT_TREE", "Git blob identity is malformed")
        if size < 0 or size > _MAX_BLOB_BYTES:
            raise MaterializeError("SNAPSHOT_TOO_LARGE", "Source blob exceeds the materialization limit")
        if path in seen_files:
            raise MaterializeError("INVALID_GIT_TREE", "Git tree has a duplicate source path")
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in seen_files:
                raise MaterializeError("INVALID_GIT_TREE", "Git tree has a file/directory collision")
            seen_dirs.add(parent)
        if path in seen_dirs:
            raise MaterializeError("INVALID_GIT_TREE", "Git tree has a file/directory collision")
        seen_files.add(path)
        entries.append((path, object_id, size, mode == b"100755"))
        total += size
        if len(entries) > _MAX_FILES or total > _MAX_TOTAL_BYTES:
            raise MaterializeError("SNAPSHOT_TOO_LARGE", "Source tree exceeds local resource limits")
    return entries


def _blob(repo: Path, inherited: tuple[int, ...], object_id: str, size: int) -> bytes:
    try:
        payload = run_git_bytes(repo, ["cat-file", "blob", object_id],
                                pass_fds=inherited)
    except GitError:
        raise MaterializeError("SNAPSHOT_BLOB_UNAVAILABLE", "Git source blob is unavailable") from None
    if len(payload) != size:
        raise MaterializeError("SNAPSHOT_BLOB_CHANGED", "Git source blob changed size")
    algorithm = hashlib.sha1 if len(object_id) == 40 else hashlib.sha256
    actual = algorithm(f"blob {size}\0".encode("ascii") + payload).hexdigest()
    if actual != object_id:
        raise MaterializeError("SNAPSHOT_BLOB_CHANGED", "Git source blob changed content")
    return payload


def _parent_fd(root_fd: int, path: str) -> tuple[int, str]:
    parts = path.split("/")
    descriptor = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            info = os.fstat(descriptor)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise MaterializeError("UNSAFE_STAGE", "Source staging directory is not private")
        return descriptor, parts[-1]
    except BaseException:
        os.close(descriptor)
        raise


def _write_blob(root_fd: int, path: str, payload: bytes, *, executable: bool) -> None:
    parent_fd, name = _parent_fd(root_fd, path)
    try:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                             | os.O_CLOEXEC, 0o600, dir_fd=parent_fd)
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(payload)
            offset = 0
            while offset < len(view):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "Source write made no progress")
                offset += written
            os.fchmod(descriptor, 0o555 if executable else 0o444)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def _seal_tree(stage: Path) -> None:
    for directory, names, files in os.walk(stage, topdown=False, followlinks=False):
        if names is None or files is None:  # Defensive against nonstandard walk implementations.
            raise MaterializeError("UNSAFE_STAGE", "Source staging tree is malformed")
        descriptor = os.open(directory, _DIRECTORY_FLAGS)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise MaterializeError("UNSAFE_STAGE", "Source staging tree was altered")
            os.fchmod(descriptor, 0o555)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _unseal_for_cleanup(stage: Path) -> None:
    for directory, _, _ in os.walk(stage, topdown=False, followlinks=False):
        try:
            os.chmod(directory, 0o700, follow_symlinks=False)
        except OSError:
            pass


def _rename_no_replace(parent_fd: int, source_name: str, target_name: str) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "renameat2", None)
    if function is None:
        raise MaterializeError("ATOMIC_RENAME_UNAVAILABLE", "Atomic no-replace rename is unavailable")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                         ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    result = function(parent_fd, os.fsencode(source_name), parent_fd,
                      os.fsencode(target_name), _RENAME_NOREPLACE)
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise MaterializeError("MATERIALIZATION_EXISTS", "Source destination already exists")
        raise MaterializeError("ATOMIC_RENAME_FAILED", "Could not atomically seal source tree")


def materialize_snapshot(repo: Path, snapshot_id: str, commit: str, tree: str,
                         destination: Path) -> SnapshotInput:
    """Build a sealed read-only tree at a fresh exact destination.

    The destination parent is an owner-only directory separate from broker
    run_root. The caller retains this tree for the entire run and should remove
    it only after all container workers have stopped.
    """
    snapshot_id, commit, tree = _uuid(snapshot_id), _oid(commit), _oid(tree)
    if len(commit) != len(tree):
        raise MaterializeError("INVALID_GIT_OBJECT", "Commit and tree object formats differ")
    try:
        destination = _absolute(Path(destination))
    except UnsafePathError:
        raise MaterializeError("UNSAFE_DESTINATION", "Source destination path is unsafe") from None
    if destination == Path("/") or destination.name in {"", ".", ".."}:
        raise MaterializeError("UNSAFE_DESTINATION", "Source destination must be a fresh directory")
    try:
        ensure_private_directory(destination.parent)
    except (OSError, UnsafePathError):
        raise MaterializeError("UNSAFE_DESTINATION", "Source destination parent is not private") from None
    if destination.exists() or destination.is_symlink():
        raise MaterializeError("MATERIALIZATION_EXISTS", "Source destination already exists")
    stage: Path | None = None
    parent_fd = os.open(destination.parent, _DIRECTORY_FLAGS)
    try:
        with _pinned_repo(repo) as (pinned, inherited):
            try:
                _inspect_config(pinned, pass_fds=inherited)
                _reject_object_alternates(pinned, pass_fds=inherited)
            except GitError as error:
                raise MaterializeError(error.code, "Managed repository has an unsafe feature") from None
            _verify_identity(pinned, inherited, snapshot_id, commit, tree)
            entries = _listing(pinned, inherited, commit)
            stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.stage-",
                                          dir=destination.parent))
            root_fd = os.open(stage, _DIRECTORY_FLAGS)
            try:
                os.fchmod(root_fd, 0o700)
                for path, object_id, size, executable in entries:
                    payload = _blob(pinned, inherited, object_id, size)
                    _write_blob(root_fd, path, payload, executable=executable)
            finally:
                os.close(root_fd)
            _verify_identity(pinned, inherited, snapshot_id, commit, tree)
        _seal_tree(stage)
        _rename_no_replace(parent_fd, stage.name, destination.name)
        stage = None
        os.fsync(parent_fd)
    except (OSError, GitError) as error:
        raise MaterializeError("MATERIALIZATION_FAILED", "Could not build source snapshot") from error
    finally:
        os.close(parent_fd)
        if stage is not None:
            _unseal_for_cleanup(stage)
            shutil.rmtree(stage, ignore_errors=True)
    from runner_app.executors.broker import SnapshotInput
    return SnapshotInput(snapshot_id, commit, tree, destination)
