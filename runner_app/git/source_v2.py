"""Freeze current v2 source into a private, filter-free per-request copy.

Bindings are controller configuration, never paths supplied by mailbox input.
The caller serializes access to the managed repository while fetch_head runs.
No checkout, source index or source ref is written. Local capture needs secure
no-follow descriptor primitives; unsupported hosts fail explicitly.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from .argv import GitError, run_git, run_git_bytes, validate_branch
from .changes import _PROTECTED_COMPONENTS, _SECRET_NAMES
from .materialize import _blob, _seal_tree, _unseal_for_cleanup, _write_blob
from .snapshots import _tracked_entries
from .worktrees import _inspect_config, _reject_object_alternates

_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z", re.ASCII)
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.ASCII)
_EXCLUSION_VERSION = "cdr-source-exclusions/1"
_RESERVED = (".runner/commands", ".runner/results", ".runner/state")
_SECRET_PARTS = _PROTECTED_COMPONENTS - {".github", ".gitattributes", ".gitmodules", ".lfsconfig"}
_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


class SourceError(RuntimeError):
    """Stable refusal without repository diagnostics, content or private paths."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SourceLimits:
    per_file_bytes: int = 8 * 1024 * 1024
    source_bytes: int = 128 * 1024 * 1024
    max_files: int = 20_000
    max_depth: int = 32


DEFAULT_SOURCE_LIMITS = SourceLimits()


@dataclass(frozen=True)
class LocalWorkspace:
    path: Path
    include_dirty: bool = False
    # Exact paths, or directory prefixes ending in '/'; ignored files also need approval.
    include_untracked: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceProject:
    repository: Path
    runs_root: Path
    workspaces: Mapping[str, LocalWorkspace] = field(default_factory=dict)
    # A successful call observes the exact CURRENT existing branch and imports its objects.
    # None means absent. An exception means fetch failed, never permission to use a cache.
    fetch_head: Callable[[Path, str], str | None] | None = None


@dataclass(frozen=True)
class SourceFile:
    path: str
    sha256: str
    bytes: int
    mode: int
    blob_oid: str
    # Local (dev, inode, mode, size, mtime_ns, ctime_ns, nlink); absent for Git objects.
    source_identity: tuple[int, ...] | None = None


@dataclass(frozen=True)
class SourceExclusion:
    path: str
    reason: str


@dataclass(frozen=True)
class SourceSnapshot:
    project_id: str
    request_id: str
    kind: str
    branch: str
    workspace_id: str | None
    path: Path
    source_commit: str
    source_tree: str
    workload_tree: str
    workload_snapshot_sha256: str
    files: tuple[SourceFile, ...]
    dirty: bool
    dirty_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    excluded_paths: tuple[SourceExclusion, ...]
    overlay_sha256: str | None
    manifest_path: Path
    manifest_sha256: str
    exclusion_version: str = _EXCLUSION_VERSION

    @property
    def commit(self) -> str:
        return self.source_commit

    @property
    def tree(self) -> str:
        """The filtered, actual workload tree for the existing rootless broker."""
        return self.workload_tree

    @property
    def workload_sha256(self) -> str:
        return self.workload_snapshot_sha256


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _plain(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path.resolve() != path:
        raise SourceError("UNSAFE_SOURCE_ROOT")
    for parent in (path, *path.parents):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise SourceError("UNSAFE_SOURCE_ROOT")
    return path


def _path(value: str, limits: SourceLimits) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise SourceError("UNSAFE_SOURCE_PATH")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise SourceError("UNSAFE_SOURCE_PATH") from None
    parts = value.split("/")
    if (not encoded or len(parts) > limits.max_depth or "\\" in value or ":" in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(p in {"", ".", ".."} or len(p.encode("utf-8")) > 255
                   or p.endswith((" ", ".")) for p in parts)):
        raise SourceError("UNSAFE_SOURCE_PATH")
    # Portable names must remain unambiguous on the native Windows profiles too.
    devices = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
               *(f"lpt{i}" for i in range(1, 10))}
    if any(p.split(".", 1)[0].casefold() in devices for p in parts):
        raise SourceError("UNSAFE_SOURCE_PATH")
    return value


def _excluded(path: str) -> str | None:
    folded = path.casefold()
    if any(folded == p or folded.startswith(p + "/") for p in _RESERVED):
        return "controller_owned"
    for part in folded.split("/"):
        if part == ".git":
            return "git_metadata"
        if (part in _SECRET_PARTS or part in _SECRET_NAMES
                or part == ".env" or part.startswith(".env.")
                or part.endswith((".pem", ".p12", ".pfx", ".key", ".tfstate"))):
            return "credential_material"
    return None


def _feature(path: str, data: bytes | None = None) -> None:
    name = path.rsplit("/", 1)[-1].casefold()
    if name == ".gitmodules":
        raise SourceError("SUBMODULE_UNSUPPORTED")
    if name == ".lfsconfig":
        raise SourceError("LFS_UNSUPPORTED")
    if (name == ".gitattributes" and data is not None
            and (b"filter=" in data.lower() or b"working-tree-encoding=" in data.lower())):
        raise SourceError("FILTER_UNSUPPORTED")


def _oid(value: str) -> str:
    if not isinstance(value, str) or _OID.fullmatch(value) is None:
        raise SourceError("INVALID_SOURCE_OBJECT")
    return value


def _object(kind: str, data: bytes, length: int) -> str:
    algorithm = hashlib.sha1 if length == 40 else hashlib.sha256
    return algorithm(f"{kind} {len(data)}\0".encode("ascii") + data).hexdigest()


def _git_tree(files: tuple[SourceFile, ...], length: int) -> str:
    root: dict[str, object] = {}
    for item in files:
        node = root
        components = item.path.split("/")
        for component in components[:-1]:
            child = node.setdefault(component, {})
            if not isinstance(child, dict):
                raise SourceError("SOURCE_PATH_COLLISION")
            node = child
        node[components[-1]] = item

    def tree(node: dict[str, object]) -> str:
        entries: list[tuple[bytes, bytes]] = []
        for name, child in node.items():
            encoded = name.encode("utf-8")
            if isinstance(child, dict):
                mode, oid, key = b"40000", tree(child), encoded + b"/"
            else:
                assert isinstance(child, SourceFile)
                mode, oid, key = f"{child.mode:o}".encode("ascii"), child.blob_oid, encoded
            entries.append((key, mode + b" " + encoded + b"\0" + bytes.fromhex(oid)))
        return _object("tree", b"".join(data for _, data in sorted(entries)), length)
    return tree(root)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":")).encode("ascii") + b"\n"


def _listing(repo: Path, commit: str, limits: SourceLimits
             ) -> dict[str, tuple[int, str, int]]:
    raw = run_git_bytes(repo, ["ls-tree", "-r", "-z", "-l", "--full-tree", commit])
    if raw and not raw.endswith(b"\0"):
        raise SourceError("INVALID_SOURCE_TREE")
    result: dict[str, tuple[int, str, int]] = {}
    portable: dict[str, str] = {}
    for record in raw[:-1].split(b"\0") if raw else ():
        metadata, sep, raw_name = record.partition(b"\t")
        fields = metadata.split()
        if sep != b"\t" or len(fields) != 4:
            raise SourceError("INVALID_SOURCE_TREE")
        try:
            name = _path(raw_name.decode("utf-8", "strict"), limits)
            mode, oid = int(fields[0], 8), _oid(fields[2].decode("ascii"))
            size = int(fields[3]) if fields[3] != b"-" else 0
        except (UnicodeDecodeError, ValueError):
            raise SourceError("INVALID_SOURCE_TREE") from None
        if mode == 0o160000:
            raise SourceError("SUBMODULE_UNSUPPORTED")
        if mode == 0o120000:
            raise SourceError("SYMLINK_UNSUPPORTED")
        if mode not in {0o100644, 0o100755} or fields[1] != b"blob" or size < 0:
            raise SourceError("INVALID_SOURCE_TREE")
        _check_collision(name, portable)
        result[name] = (mode, oid, size)
        if len(result) > limits.max_files:
            raise SourceError("SOURCE_FILE_COUNT_LIMIT")
    return result


def _check_collision(path: str, seen: dict[str, str]) -> None:
    parts = path.split("/")
    for index in range(1, len(parts) + 1):
        name = "/".join(parts[:index])
        key = unicodedata.normalize("NFC", name).casefold()
        if key in seen and seen[key] != name:
            raise SourceError("SOURCE_PATH_COLLISION")
        seen[key] = name


def _repo_guard(repo: Path, *, bare: bool) -> None:
    _plain(repo)
    metadata = repo / ".git"
    if metadata.is_symlink():
        raise SourceError("UNSAFE_GIT_METADATA")
    _inspect_config(repo)
    _reject_object_alternates(repo)
    if bare and run_git(repo, ["rev-parse", "--is-bare-repository"]).strip() != "true":
        raise SourceError("MANAGED_BARE_REPOSITORY_REQUIRED")
    if not bare and Path(run_git(repo, ["rev-parse", "--show-toplevel"]).strip()) != repo:
        raise SourceError("UNREGISTERED_SOURCE_ROOT")
    if run_git(repo, ["for-each-ref", "--format=%(refname)", "refs/replace"]).strip():
        raise SourceError("REWRITTEN_HISTORY_UNSUPPORTED")
    grafts = Path(run_git(repo, ["rev-parse", "--git-path", "info/grafts"]).strip())
    grafts = grafts if grafts.is_absolute() else repo / grafts
    if grafts.exists() or grafts.is_symlink():
        raise SourceError("REWRITTEN_HISTORY_UNSUPPORTED")


def _branch(repo: Path) -> str:
    try:
        return run_git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"]).strip()
    except GitError:
        raise SourceError("BRANCH_REQUIRED") from None


class SourceResolver:
    """Controller-bound resolver; resolve only when the request enters execution."""

    def __init__(self, projects: Mapping[str, SourceProject], *,
                 limits: SourceLimits = DEFAULT_SOURCE_LIMITS) -> None:
        self.projects = dict(projects)
        self.limits = limits
        if any(type(value) is not int or value <= 0 for value in (
            limits.per_file_bytes, limits.source_bytes, limits.max_files, limits.max_depth,
        )):
            raise SourceError("INVALID_SOURCE_LIMITS")

    def resolve_current(self, project_id: str, request_id: str,
                        source_selector: Mapping[str, object]) -> SourceSnapshot:
        if not isinstance(project_id, str) or _ID.fullmatch(project_id) is None:
            raise SourceError("INVALID_PROJECT_ID")
        try:
            if str(UUID(request_id)) != request_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise SourceError("INVALID_REQUEST_ID") from None
        if project_id not in self.projects:
            raise SourceError("PROJECT_NOT_REGISTERED")
        selector = dict(source_selector)
        if set(selector) - {"kind", "branch", "workspace_id"}:
            raise SourceError("INVALID_SOURCE_SELECTOR")
        kind, branch = selector.get("kind"), selector.get("branch")
        if not isinstance(branch, str) or not branch:
            raise SourceError("BRANCH_REQUIRED")
        branch = validate_branch(branch)
        if kind not in {"github_branch", "local_workspace"}:
            raise SourceError("INVALID_SOURCE_SELECTOR")
        project = self.projects[project_id]
        repo = _plain(project.repository)
        _repo_guard(repo, bare=True)
        workspace_id: str | None = None
        workspace: LocalWorkspace | None = None
        if kind == "local_workspace":
            selected = selector.get("workspace_id")
            if not isinstance(selected, str) or selected not in project.workspaces:
                raise SourceError("WORKSPACE_NOT_REGISTERED")
            workspace_id, workspace = selected, project.workspaces[selected]
            original = _plain(workspace.path)
            _repo_guard(original, bare=False)
        else:
            original = repo
        # Runs may not be inside ANY original source, including another registered workspace.
        root = Path(project.runs_root)
        sources = [repo, *(Path(item.path) for item in project.workspaces.values())]
        if not root.is_absolute() or any(root.is_relative_to(path) for path in sources):
            raise SourceError("UNSAFE_RUNS_ROOT")
        if root.resolve() != root:
            raise SourceError("UNSAFE_RUNS_ROOT")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _plain(root)
        if root.stat().st_uid != os.geteuid() or root.stat().st_mode & 0o077:
            raise SourceError("UNSAFE_RUNS_ROOT")
        operation = root / request_id
        if any(operation.is_relative_to(path) or path.is_relative_to(operation)
               for path in sources):
            raise SourceError("UNSAFE_RUNS_ROOT")
        try:
            operation.mkdir(mode=0o700)
        except FileExistsError:
            raise SourceError("SOURCE_SNAPSHOT_EXISTS") from None
        destination = operation / "source"
        destination.mkdir(mode=0o700)
        root_fd = os.open(destination, _FLAGS)
        try:
            if workspace is None:
                if project.fetch_head is None:
                    raise SourceError("SOURCE_FETCH_UNAVAILABLE")
                try:
                    commit = project.fetch_head(repo, branch)
                except Exception:  # noqa: BLE001 -- provider diagnostics must never escape.
                    raise SourceError("SOURCE_FETCH_FAILED") from None
                if commit is None:
                    raise SourceError("SOURCE_BRANCH_MISSING")
                commit = _oid(commit)
                _repo_guard(repo, bare=True)
            else:
                commit = _oid(run_git(original, ["rev-parse", "HEAD"]).strip())
                observed_branch = _branch(original)
                if observed_branch != branch:
                    raise SourceError("SOURCE_BRANCH_MISMATCH")
            if run_git(original, ["cat-file", "-t", commit]).strip() != "commit":
                raise SourceError("INVALID_SOURCE_OBJECT")
            source_tree = _oid(run_git(original, ["rev-parse", f"{commit}^{{tree}}"] ).strip())
            entries = _listing(original, commit, self.limits)
            if workspace is None:
                files, exclusions = self._from_git(repo, entries, root_fd)
                dirty_paths: tuple[str, ...] = ()
                untracked: tuple[str, ...] = ()
            else:
                files, exclusions, dirty_paths, untracked = self._from_local(
                    original, workspace, commit, branch, entries, root_fd)
            workload_tree = _git_tree(files, len(commit))
            file_index = [{"path": item.path, "sha256": item.sha256,
                           "bytes": item.bytes, "mode": item.mode} for item in files]
            workload_digest = hashlib.sha256(_canonical(file_index)).hexdigest()
            overlay = hashlib.sha256(_canonical({
                "base": commit, "workload": workload_digest, "dirty_paths": dirty_paths,
                "untracked_paths": untracked,
            })).hexdigest() if dirty_paths or untracked else None
            manifest = {"schema_version": "cdr-source-snapshot/2", "project_id": project_id,
                        "request_id": request_id, "kind": kind, "branch": branch,
                        "workspace_id": workspace_id, "source_commit": commit,
                        "source_tree": source_tree, "workload_tree": workload_tree,
                        "workload_snapshot_sha256": workload_digest, "files": file_index,
                        "dirty": bool(dirty_paths or untracked), "dirty_paths": dirty_paths,
                        "untracked_paths": untracked, "overlay_sha256": overlay,
                        "source_identities": {item.path: item.source_identity for item in files
                                              if item.source_identity is not None},
                        "exclusion_version": _EXCLUSION_VERSION,
                        "excluded_paths": [{"path": item.path, "reason": item.reason}
                                           for item in exclusions]}
            payload = _canonical(manifest)
            manifest_path = operation / "source-manifest.json"
            with manifest_path.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            manifest_path.chmod(0o400)
            _seal_tree(destination)
            return SourceSnapshot(
                project_id, request_id, str(kind), branch, workspace_id, destination,
                commit, source_tree, workload_tree, workload_digest, files,
                bool(dirty_paths or untracked), dirty_paths, untracked, exclusions, overlay,
                manifest_path, hashlib.sha256(payload).hexdigest())
        except BaseException:
            _unseal_for_cleanup(destination)
            shutil.rmtree(operation)
            raise
        finally:
            os.close(root_fd)

    def _from_git(self, repo: Path, entries: dict[str, tuple[int, str, int]], root_fd: int
                  ) -> tuple[tuple[SourceFile, ...], tuple[SourceExclusion, ...]]:
        files: list[SourceFile] = []
        exclusions: list[SourceExclusion] = []
        total = 0
        for path, (mode, oid, size) in sorted(entries.items()):
            reason = _excluded(path)
            if reason:
                exclusions.append(SourceExclusion(path, reason))
                continue
            _feature(path)
            total += size
            if size > self.limits.per_file_bytes or total > self.limits.source_bytes:
                raise SourceError("SOURCE_BYTE_LIMIT")
            data = _blob(repo, (), oid, size)
            _feature(path, data)
            _write_blob(root_fd, path, data, executable=mode == 0o100755)
            files.append(SourceFile(path, hashlib.sha256(data).hexdigest(), size, mode, oid))
        return tuple(files), tuple(exclusions)

    def _from_local(self, original: Path, workspace: LocalWorkspace, commit: str, branch: str,
                    entries: dict[str, tuple[int, str, int]], root_fd: int
                    ) -> tuple[tuple[SourceFile, ...], tuple[SourceExclusion, ...],
                               tuple[str, ...], tuple[str, ...]]:
        if (not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
                or os.scandir not in os.supports_fd or os.open not in os.supports_dir_fd):
            raise SourceError("UNSUPPORTED_SECURE_FILESYSTEM")
        approved = tuple(_path(p.rstrip("/"), self.limits) + ("/" if p.endswith("/") else "")
                         for p in workspace.include_untracked)
        index_before, indexed = _tracked_entries(original)
        for path, (mode, _) in indexed.items():
            _path(path, self.limits)
            if mode == 0o160000:
                raise SourceError("SUBMODULE_UNSUPPORTED")
            if mode not in {0o100644, 0o100755}:
                raise SourceError("UNSAFE_SOURCE_FILE")
        included_directories: set[str] = set()
        for path in (*entries, *indexed, *(item.rstrip("/") for item in approved)):
            parts = path.split("/")
            included_directories.update("/".join(parts[:i]) for i in range(1, len(parts)))
        local_fd = os.open(original, _FLAGS)
        files: list[SourceFile] = []
        exclusions: list[SourceExclusion] = []
        seen: dict[str, str] = {}
        observed: dict[str, tuple[int, ...]] = {}
        directories: dict[str, tuple[int, ...]] = {}
        child_directories: dict[str, list[str]] = {}
        child_files: dict[str, list[str]] = {}
        dirty = {path for path in set(entries) | set(indexed)
                 if not _excluded(path) and entries.get(path, ())[:2] != indexed.get(path)}
        untracked: set[str] = set()
        nodes = total = 0

        def visit(fd: int, prefix: str) -> None:
            nonlocal nodes, total
            before = _identity(os.fstat(fd))
            directories[prefix] = before
            child_directories[prefix] = []
            child_files[prefix] = []
            names: list[str] = []
            with os.scandir(fd) as iterator:
                for item in iterator:
                    names.append(item.name)
                    if len(names) + nodes > self.limits.max_files:
                        raise SourceError("SOURCE_FILE_COUNT_LIMIT")
            names.sort()
            for name in names:
                nodes += 1
                if nodes > self.limits.max_files:
                    raise SourceError("SOURCE_FILE_COUNT_LIMIT")
                path = _path(f"{prefix}/{name}" if prefix else name, self.limits)
                _check_collision(path, seen)
                reason = _excluded(path)
                if reason:
                    exclusions.append(SourceExclusion(path, reason))
                    continue
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                        or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
                    raise SourceError("UNSAFE_SOURCE_FILE")
                if stat.S_ISDIR(info.st_mode):
                    if (path not in included_directories
                            and not any(p.endswith("/") and (path + "/").startswith(p)
                                        for p in approved)):
                        exclusions.append(SourceExclusion(path, "unapproved_untracked"))
                        continue
                    child_directories[prefix].append(path)
                    child_fd = os.open(name, _FLAGS, dir_fd=fd)
                    try:
                        if _identity(info) != _identity(os.fstat(child_fd)):
                            raise SourceError("INPUT_CHANGED")
                        visit(child_fd, path)
                    finally:
                        os.close(child_fd)
                    continue
                _feature(path)
                if path not in entries and path not in indexed and not any(path == p or (p.endswith("/")
                                                                 and path.startswith(p))
                                                   for p in approved):
                    exclusions.append(SourceExclusion(path, "unapproved_untracked"))
                    continue
                if info.st_nlink != 1:
                    raise SourceError("UNSAFE_SOURCE_FILE")
                if info.st_size > self.limits.per_file_bytes or total + info.st_size > self.limits.source_bytes:
                    raise SourceError("SOURCE_BYTE_LIMIT")
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=fd)
                with os.fdopen(descriptor, "rb") as stream:
                    if _identity(info) != _identity(os.fstat(stream.fileno())):
                        raise SourceError("INPUT_CHANGED")
                    data = stream.read(self.limits.per_file_bytes + 1)
                    if _identity(info) != _identity(os.fstat(stream.fileno())) or len(data) != info.st_size:
                        raise SourceError("INPUT_CHANGED")
                _feature(path, data)
                mode = 0o100755 if info.st_mode & 0o111 else 0o100644
                oid = _object("blob", data, len(commit))
                if path not in entries and path not in indexed:
                    untracked.add(path)
                elif (mode, oid) != entries.get(path, ())[:2]:
                    dirty.add(path)
                total += len(data)
                observed[path] = _identity(info)
                child_files[prefix].append(path)
                _write_blob(root_fd, path, data, executable=mode == 0o100755)
                files.append(SourceFile(path, hashlib.sha256(data).hexdigest(), len(data),
                                        mode, oid, _identity(info)))
            if before != _identity(os.fstat(fd)):
                raise SourceError("INPUT_CHANGED")

        def verify(fd: int, prefix: str) -> None:
            if directories[prefix] != _identity(os.fstat(fd)):
                raise SourceError("INPUT_CHANGED")
            for path in child_directories[prefix]:
                child = os.open(path.rsplit("/", 1)[-1], _FLAGS, dir_fd=fd)
                try:
                    verify(child, path)
                finally:
                    os.close(child)
            for path in child_files[prefix]:
                actual = os.stat(path.rsplit("/", 1)[-1], dir_fd=fd, follow_symlinks=False)
                if observed[path] != _identity(actual):
                    raise SourceError("INPUT_CHANGED")

        try:
            visit(local_fd, "")
            verify(local_fd, "")
            for path in entries:
                if path not in observed and not _excluded(path):
                    dirty.add(path)
            if dirty and not workspace.include_dirty:
                raise SourceError("DIRTY_SOURCE_NOT_APPROVED")
            if (run_git(original, ["rev-parse", "HEAD"]).strip() != commit
                    or _branch(original) != branch
                    or _tracked_entries(original)[0] != index_before
                    or _identity(original.stat()) != _identity(os.fstat(local_fd))):
                raise SourceError("INPUT_CHANGED")
            _repo_guard(original, bare=False)
        except OSError:
            raise SourceError("INPUT_CHANGED") from None
        finally:
            os.close(local_fd)
        return (tuple(sorted(files, key=lambda item: item.path)),
                tuple(sorted(exclusions, key=lambda item: item.path)),
                tuple(sorted(dirty)), tuple(sorted(untracked)))
