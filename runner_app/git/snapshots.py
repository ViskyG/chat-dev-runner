"""Immutable source snapshots from a private Git index, without checkout filters.

Source workspaces are read only. A no-checkout worktree gives every snapshot a private
index; checked blobs go directly into Git's object store, then a private ref retains
the commit. Intent and manifest files make interruption discoverable on disk.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .argv import GitError, run_git, run_git_bytes, run_git_input
from .changes import (
    MAX_CHANGESET_BYTES,
    ChangeError,
    ChangePlan,
    safe_source_path,
    validate_changeset,
)

MAX_EXISTING_BLOB_BYTES = 8 * 1024 * 1024
MAX_FINGERPRINT_FILE_BYTES = 128 * 1024 * 1024
_HEX = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    project_id: str
    workspace_id: str
    base_head: str
    commit: str
    tree: str
    source_generation: int
    capture_fingerprint: str
    provenance: str
    dirty_at_capture: bool
    included_paths: tuple[str, ...]
    approved_untracked_paths: tuple[str, ...]
    manifest_path: Path
    manifest_sha256: str


def _snapshot_id(value: str | None) -> str:
    if value is None:
        return str(uuid.uuid4())
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ChangeError("INVALID_SNAPSHOT_ID", "Snapshot ID must be a canonical UUID") from exc
    if canonical != value:
        raise ChangeError("INVALID_SNAPSHOT_ID", "Snapshot ID must be a canonical UUID")
    return canonical


def _hex_oid(value: str) -> str:
    oid = value.strip()
    if not _HEX.fullmatch(oid):
        raise GitError("INVALID_GIT_OBJECT", "Git returned an invalid object ID")
    return oid


def _frame(value: bytes) -> bytes:
    return len(value).to_bytes(8, "big") + value


def _split_z(raw: bytes) -> list[bytes]:
    if raw and not raw.endswith(b"\0"):
        raise GitError("INVALID_GIT_OUTPUT", "Git returned a truncated path list")
    return raw[:-1].split(b"\0") if raw else []


def _decode_path(raw: bytes) -> str:
    try:
        path = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ChangeError("UNSUPPORTED_PATH", "Source path is not UTF-8") from exc
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise ChangeError("UNSUPPORTED_PATH", "Repository contains an ambiguous source path")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise ChangeError("UNSUPPORTED_PATH", "Repository contains an ambiguous source path")
    return path


def _parent_fd(root: Path, path: str) -> tuple[int, str]:
    """Open a path's parent one component at a time, never following a symlink."""
    parts = path.split("/")
    try:
        fd = os.open(root, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    except OSError as exc:
        raise ChangeError("UNSAFE_ROOT", "Source root is unavailable or is a symlink") from exc
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd, parts[-1]
    except OSError as exc:
        os.close(fd)
        if exc.errno == errno.ENOENT:
            raise ChangeError("MISSING_PARENT", "Source parent is absent") from exc
        raise ChangeError("SYMLINK", "Source parent is a link or non-directory") from exc


def _file_state(root: Path, path: str, *, max_bytes: int,
                include_bytes: bool) -> tuple[str, int, bytes | None, str, str] | None:
    """Hash regular content through directory FDs; do not follow a final symlink."""
    try:
        parent_fd, name = _parent_fd(root, path)
    except ChangeError as exc:
        if exc.code == "MISSING_PARENT":
            return None
        raise
    try:
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            if include_bytes:
                raise ChangeError("SYMLINK", "Source path is a symbolic link")
            target = os.readlink(name, dir_fd=parent_fd).encode("utf-8", "surrogateescape")
            header = f"blob {len(target)}\0".encode("ascii")
            return (hashlib.sha256(b"link\0" + target).hexdigest(), 0o120000, None,
                    hashlib.sha1(header + target).hexdigest(),
                    hashlib.sha256(header + target).hexdigest())
        if not stat.S_ISREG(info.st_mode):
            raise ChangeError("NOT_REGULAR_FILE", "Source path is not a regular file")
        if info.st_size > max_bytes:
            raise ChangeError("FILE_TOO_LARGE", "Source file exceeds the local byte budget")
        fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW, dir_fd=parent_fd)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > max_bytes:
                raise ChangeError("NOT_REGULAR_FILE", "Source path changed during inspection")
            digest = hashlib.sha256()
            header = f"blob {opened.st_size}\0".encode("ascii")
            git_sha1 = hashlib.sha1(header)
            git_sha256 = hashlib.sha256(header)
            payload = bytearray() if include_bytes else None
            count = 0
            while True:
                chunk = os.read(fd, min(1024 * 1024, max_bytes + 1 - count))
                if not chunk:
                    break
                count += len(chunk)
                if count > max_bytes:
                    raise ChangeError("FILE_TOO_LARGE", "Source file exceeds the local byte budget")
                digest.update(chunk)
                git_sha1.update(chunk)
                git_sha256.update(chunk)
                if payload is not None:
                    payload.extend(chunk)
            after = os.fstat(fd)
            if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns
            ):
                raise ChangeError("WORKSPACE_CHANGED", "Source file changed during inspection")
            mode = 0o100755 if opened.st_mode & stat.S_IXUSR else 0o100644
            return (digest.hexdigest(), mode, bytes(payload) if payload is not None else None,
                    git_sha1.hexdigest(), git_sha256.hexdigest())
        finally:
            os.close(fd)
    except OSError as exc:
        raise ChangeError("WORKSPACE_CHANGED", "Source file changed during inspection") from exc
    finally:
        os.close(parent_fd)


def _tracked_entries(workspace: Path) -> tuple[bytes, dict[str, tuple[int, str]]]:
    raw = run_git_bytes(workspace, ["ls-files", "--stage", "-z", "--"])
    paths: dict[str, tuple[int, str]] = {}
    for entry in _split_z(raw):
        metadata, separator, path = entry.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or fields[2] != b"0":
            raise ChangeError("UNMERGED_INDEX", "Workspace index is unmerged or malformed")
        name = _decode_path(path)
        if name in paths:
            raise ChangeError("UNMERGED_INDEX", "Workspace index has duplicate paths")
        try:
            mode = int(fields[0], 8)
            oid = _hex_oid(fields[1].decode("ascii"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ChangeError("UNMERGED_INDEX", "Workspace index entry is malformed") from exc
        paths[name] = (mode, oid)
    return raw, paths


def _untracked_paths(workspace: Path) -> list[str]:
    raw = run_git_bytes(workspace, ["ls-files", "--others", "--exclude-standard", "-z", "--"])
    return sorted(_decode_path(item) for item in _split_z(raw))


def workspace_fingerprint(workspace: Path, *,
                          approved_untracked: Sequence[str] = ()) -> str:
    """Hash HEAD, index, every tracked file, and names of all visible untracked files.

    Approved untracked files are also content-hashed. Unknown untracked content is
    never read into the runner; its presence still changes the fingerprint.
    """
    head = _hex_oid(run_git(workspace, ["rev-parse", "HEAD"]))
    index_bytes, tracked = _tracked_entries(workspace)
    untracked = _untracked_paths(workspace)
    approved = set(approved_untracked)
    digest = hashlib.sha256(b"cdr-source-fingerprint-v1\0")
    digest.update(_frame(head.encode("ascii")))
    digest.update(_frame(index_bytes))
    for path in sorted(tracked):
        state = _file_state(workspace, path, max_bytes=MAX_FINGERPRINT_FILE_BYTES,
                            include_bytes=False)
        encoded = path.encode("utf-8")
        digest.update(_frame(encoded))
        digest.update(_frame(json.dumps(state[:2] if state else None,
                                        separators=(",", ":")).encode("ascii")))
    for path in untracked:
        digest.update(_frame(b"untracked:" + path.encode("utf-8")))
        if path in approved:
            safe_source_path(path)
            state = _file_state(workspace, path, max_bytes=MAX_FINGERPRINT_FILE_BYTES,
                                include_bytes=False)
            digest.update(_frame(json.dumps(state[:2] if state else None,
                                            separators=(",", ":")).encode("ascii")))
    return digest.hexdigest()


def _workspace_repo_matches(repo: Path, workspace: Path) -> None:
    def common(path: Path) -> Path:
        raw = run_git(path, ["rev-parse", "--git-common-dir"]).strip()
        common_dir = Path(raw)
        return (common_dir if common_dir.is_absolute() else path / common_dir).resolve(strict=True)
    if common(repo) != common(workspace):
        raise ChangeError("WRONG_REPOSITORY", "Workspace is not linked to the trusted repository")


def _require_managed_layout(repo: Path, workspace: Path, stage_root: Path,
                            project_id: str, workspace_id: str) -> None:
    project = repo.parent
    if (repo.name != "repo.git" or project.name != project_id
            or workspace != project / "workspaces" / workspace_id
            or stage_root != project / "snapshots"):
        raise ChangeError("WRONG_WORKSPACE", "Source paths do not match managed identity")


def _require_head(repo: Path, workspace: Path, expected_head: str) -> None:
    _workspace_repo_matches(repo, workspace)
    if _hex_oid(run_git(workspace, ["rev-parse", "HEAD"])) != expected_head:
        raise ChangeError("STALE_BASE", "Workspace HEAD changed")
    if run_git(repo, ["cat-file", "-t", expected_head]).strip() != "commit":
        raise ChangeError("INVALID_HEAD", "Base Git object is not a commit")


def _require_clean_workspace(workspace: Path) -> None:
    """Verify index and every tracked working file, including assume-unchanged files."""
    if _untracked_paths(workspace):
        raise ChangeError("DIRTY_WORKSPACE", "Automatic delivery requires a clean workspace")
    if run_git_bytes(workspace, ["diff", "--cached", "--name-only", "-z", "HEAD", "--"]):
        raise ChangeError("DIRTY_WORKSPACE", "Automatic delivery requires a clean workspace")
    _, entries = _tracked_entries(workspace)
    for path, (index_mode, index_oid) in entries.items():
        state = _file_state(workspace, path, max_bytes=MAX_FINGERPRINT_FILE_BYTES,
                            include_bytes=False)
        calculated = state[3 if len(index_oid) == 40 else 4] if state is not None else None
        if state is None or state[1] != index_mode or calculated != index_oid:
            raise ChangeError("DIRTY_WORKSPACE", "Automatic delivery requires a clean workspace")


def _tree_entry(repo: Path, base_head: str, path: str) -> tuple[int, str] | None:
    raw = run_git_bytes(repo, ["--literal-pathspecs", "ls-tree", "-z", base_head,
                               "--", path])
    matches: list[tuple[int, str]] = []
    for item in _split_z(raw):
        meta, tab, found = item.partition(b"\t")
        if not tab:
            raise GitError("INVALID_GIT_OUTPUT", "Git tree response is malformed")
        if found != path.encode("utf-8"):
            continue
        fields = meta.split()
        if len(fields) != 3:
            raise GitError("INVALID_GIT_OUTPUT", "Git tree response is malformed")
        mode = int(fields[0], 8)
        oid = _hex_oid(fields[2].decode("ascii"))
        matches.append((mode, oid))
    if len(matches) > 1:
        raise GitError("INVALID_GIT_OUTPUT", "Git tree has duplicate entries")
    return matches[0] if matches else None


def _base_file(repo: Path, base_head: str, path: str) -> tuple[int, str] | None:
    components = path.split("/")
    for index in range(1, len(components)):
        parent = _tree_entry(repo, base_head, "/".join(components[:index]))
        if parent is not None and parent[0] != 0o40000:
            code = "SYMLINK" if parent[0] == 0o120000 else "NOT_DIRECTORY"
            raise ChangeError(code, "Changeset path has a non-directory parent")
    target = _tree_entry(repo, base_head, path)
    if target is None:
        return None
    if target[0] == 0o120000:
        raise ChangeError("SYMLINK", "Changeset cannot replace a symbolic link")
    if target[0] not in {0o100644, 0o100755}:
        raise ChangeError("NOT_REGULAR_FILE", "Changeset target is not a regular Git file")
    return target


def _base_blob_bytes(repo: Path, oid: str) -> bytes:
    try:
        size = int(run_git(repo, ["cat-file", "-s", oid]).strip())
    except ValueError as exc:
        raise GitError("INVALID_GIT_OUTPUT", "Git blob size is malformed") from exc
    if size > MAX_EXISTING_BLOB_BYTES:
        raise ChangeError("FILE_TOO_LARGE", "Existing file exceeds the byte budget")
    return run_git_bytes(repo, ["cat-file", "blob", oid])


def _preflight_plan(repo: Path, plan: ChangePlan) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in plan.files:
        base_file = _base_file(repo, plan.base_head, item.path)
        original = (_base_blob_bytes(repo, base_file[1])
                    if base_file is not None else None)
        if original is not None and len(original) > MAX_EXISTING_BLOB_BYTES:
            raise ChangeError("FILE_TOO_LARGE", "Existing file exceeds the byte budget")
        actual = hashlib.sha256(original).hexdigest() if original is not None else None
        if actual != item.expected_sha256:
            raise ChangeError("CONTENT_CONFLICT", "A file differs from the expected base hash")
        if item.op == "delete" and original is None:
            raise ChangeError("MISSING_DELETE_TARGET", "Delete target is absent from base")
        output.append({"path": item.path, "op": item.op, "base_sha256": actual,
                       "sha256": hashlib.sha256(item.content).hexdigest()
                       if item.content is not None else None,
                       "mode": base_file[0] if base_file is not None else 0o100644})
    return output


def _atomic_json(path: Path, value: Mapping[str, Any]) -> str:
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temp.exists():
            temp.unlink()
    return hashlib.sha256(payload).hexdigest()


def _prepare_operation(stage_root: Path, snapshot_id: str,
                       intent: Mapping[str, Any]) -> tuple[Path, Path]:
    if not stage_root.is_absolute() or ".." in stage_root.parts:
        raise ChangeError("UNSAFE_ROOT", "Snapshot root must be canonical and absolute")
    stage_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_info = stage_root.lstat()
    if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid()
            or root_info.st_mode & 0o077):
        raise ChangeError("UNSAFE_ROOT", "Snapshot root must be an owner-only directory")
    # A restrictive caller umask may have removed our own directory permissions.
    os.chmod(stage_root, 0o700)
    root_fd = os.open(stage_root, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    try:
        os.fchmod(root_fd, 0o700)
        try:
            os.mkdir(snapshot_id, mode=0o700, dir_fd=root_fd)
        except FileExistsError as exc:
            raise ChangeError("SNAPSHOT_EXISTS", "Snapshot ID already has retained staging state") from exc
        os.chmod(snapshot_id, 0o700, dir_fd=root_fd)
        operation_fd = os.open(snapshot_id, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                               dir_fd=root_fd)
        try:
            os.fchmod(operation_fd, 0o700)
            os.fsync(operation_fd)
            os.fsync(root_fd)
        finally:
            os.close(operation_fd)
    finally:
        os.close(root_fd)
    operation = stage_root / snapshot_id
    _atomic_json(operation / "intent.json", intent)
    return operation, operation / "checkout"


def _write_blob(checkout: Path, content: bytes) -> str:
    """Feed bytes directly to Git, avoiding a reopenable temporary path."""
    return _hex_oid(run_git_input(checkout, ["hash-object", "-w", "--stdin",
                                             "--no-filters"], content))


def _snapshot_from_files(
    repo: Path, stage_root: Path, *, snapshot_id: str, project_id: str,
    workspace_id: str, base_head: str, source_generation: int,
    capture_fingerprint: str, provenance: str, dirty_at_capture: bool,
    approved_untracked_paths: Sequence[str], files: Sequence[tuple[str, str, bytes | None, int]],
    manifest_files: Sequence[Mapping[str, Any]],
) -> Snapshot:
    intent: dict[str, Any] = {
        "schema_version": "1.0", "snapshot_id": snapshot_id,
        "project_id": project_id, "workspace_id": workspace_id,
        "base_head": base_head, "source_generation": source_generation,
        "capture_fingerprint": capture_fingerprint, "provenance": provenance,
        "state": "PREPARING",
    }
    operation, checkout = _prepare_operation(stage_root, snapshot_id, intent)
    run_git(repo, ["worktree", "add", "--no-checkout", "--detach", str(checkout), base_head])
    run_git(checkout, ["read-tree", base_head])
    for path, op, content, mode in files:
        if op == "delete":
            run_git(checkout, ["--literal-pathspecs", "update-index",
                               "--force-remove", "--", path])
            continue
        if content is None:
            raise ChangeError("INVALID_PLAN", "Upsert has no content")
        oid = _write_blob(checkout, content)
        run_git(checkout, ["update-index", "--add", "--cacheinfo",
                           f"{mode:o},{oid},{path}"])
    tree = _hex_oid(run_git(checkout, ["write-tree"]))
    base_tree = _hex_oid(run_git(repo, ["rev-parse", f"{base_head}^{{tree}}"] ))
    if tree == base_tree:
        commit = base_head
    else:
        commit = _hex_oid(run_git(checkout, [
            "-c", "user.name=Chat-Dev Runner", "-c", "user.email=runner@local.invalid",
            "commit-tree", tree, "-p", base_head, "-m", f"Runner snapshot {snapshot_id}",
        ]))
    ref = f"refs/cdr/snapshots/{snapshot_id}"
    intent.update({"state": "COMMIT_CREATED", "commit": commit, "tree": tree, "ref": ref})
    _atomic_json(operation / "intent.json", intent)
    run_git(repo, ["update-ref", ref, commit, "0" * len(commit)])
    manifest: dict[str, Any] = {
        "schema_version": "1.0", "snapshot_id": snapshot_id,
        "project_id": project_id, "workspace_id": workspace_id,
        "base_head": base_head, "commit": commit, "tree": tree,
        "source_generation": source_generation,
        "capture_fingerprint": capture_fingerprint,
        "provenance": provenance, "dirty_at_capture": dirty_at_capture,
        "included_paths": [item[0] for item in files],
        "approved_untracked_paths": list(approved_untracked_paths),
        "files": list(manifest_files), "retained_ref": ref,
    }
    manifest_path = operation / "manifest.json"
    manifest_sha256 = _atomic_json(manifest_path, manifest)
    return Snapshot(
        snapshot_id=snapshot_id, project_id=project_id, workspace_id=workspace_id,
        base_head=base_head, commit=commit, tree=tree,
        source_generation=source_generation, capture_fingerprint=capture_fingerprint,
        provenance=provenance, dirty_at_capture=dirty_at_capture,
        included_paths=tuple(item[0] for item in files),
        approved_untracked_paths=tuple(approved_untracked_paths),
        manifest_path=manifest_path, manifest_sha256=manifest_sha256,
    )


def capture_clean_verify(
    repo: Path, workspace: Path, stage_root: Path, *, project_id: str,
    workspace_id: str, expected_head: str, source_generation: int,
    snapshot_id: str | None = None,
) -> Snapshot:
    """Retain the exact clean HEAD for VERIFY without changing the editor checkout."""
    selected_id = _snapshot_id(snapshot_id)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", project_id) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,47}", workspace_id
    ):
        raise ChangeError("INVALID_IDENTITY", "Trusted project/workspace identity is invalid")
    if type(source_generation) is not int or source_generation < 0:
        raise ChangeError("INVALID_GENERATION", "Source generation is invalid")
    if not _HEX.fullmatch(expected_head):
        raise ChangeError("INVALID_HEAD", "Expected head must be a full commit ID")
    _require_managed_layout(repo, workspace, stage_root, project_id, workspace_id)
    _require_head(repo, workspace, expected_head)
    _require_clean_workspace(workspace)
    capture_fingerprint = workspace_fingerprint(workspace)
    _require_clean_workspace(workspace)
    return _snapshot_from_files(
        repo, stage_root, snapshot_id=selected_id, project_id=project_id,
        workspace_id=workspace_id, base_head=expected_head,
        source_generation=source_generation, capture_fingerprint=capture_fingerprint,
        provenance="CLEAN_VERIFY", dirty_at_capture=False,
        approved_untracked_paths=(), files=(), manifest_files=(),
    )


def stage_changeset(
    repo: Path, workspace: Path, stage_root: Path, change: Mapping[str, Any], *,
    project_id: str, workspace_id: str, expected_head: str,
    source_generation: int, snapshot_id: str | None = None,
) -> Snapshot:
    """Apply a full-base v1 changeset only to an immutable private Git snapshot."""
    selected_id = _snapshot_id(snapshot_id)
    plan = validate_changeset(change, project_id=project_id,
                              workspace_id=workspace_id, expected_head=expected_head)
    if type(source_generation) is not int or source_generation < 0:
        raise ChangeError("INVALID_GENERATION", "Source generation is invalid")
    _require_managed_layout(repo, workspace, stage_root, project_id, workspace_id)
    _require_head(repo, workspace, expected_head)
    _require_clean_workspace(workspace)
    capture_fingerprint = workspace_fingerprint(workspace)
    manifest_files = _preflight_plan(repo, plan)
    staged = [(item.path, item.op, item.content, int(metadata["mode"]))
              for item, metadata in zip(plan.files, manifest_files)]
    return _snapshot_from_files(
        repo, stage_root, snapshot_id=selected_id, project_id=project_id,
        workspace_id=workspace_id, base_head=expected_head,
        source_generation=source_generation, capture_fingerprint=capture_fingerprint,
        provenance="CHANGESET_V1", dirty_at_capture=False,
        approved_untracked_paths=(), files=staged, manifest_files=manifest_files,
    )


def capture_dirty_verify(
    repo: Path, workspace: Path, stage_root: Path, *, project_id: str,
    workspace_id: str, expected_head: str, source_generation: int,
    approved_files: Mapping[str, str | None], snapshot_id: str | None = None,
) -> Snapshot:
    """Freeze the explicitly approved dirty tree without mutating its editor checkout.

    ``approved_files`` maps each displayed dirty path to its current SHA-256, or
    ``None`` for an approved deletion. All visible dirty and untracked paths must
    be listed. Unsafe paths and symlinks are refused even when explicitly listed.
    """
    selected_id = _snapshot_id(snapshot_id)
    if type(source_generation) is not int or source_generation < 0:
        raise ChangeError("INVALID_GENERATION", "Source generation is invalid")
    if not _HEX.fullmatch(expected_head):
        raise ChangeError("INVALID_HEAD", "Expected head must be a full commit ID")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", project_id) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,47}", workspace_id
    ):
        raise ChangeError("INVALID_IDENTITY", "Trusted project/workspace identity is invalid")
    _require_managed_layout(repo, workspace, stage_root, project_id, workspace_id)
    _require_head(repo, workspace, expected_head)
    dirty_raw = run_git_bytes(workspace, ["diff", "--name-only", "-z", "--no-renames",
                                          "HEAD", "--"])
    tracked_dirty = {_decode_path(item) for item in _split_z(dirty_raw)}
    untracked = set(_untracked_paths(workspace))
    actual_paths = tracked_dirty | untracked
    if not actual_paths:
        raise ChangeError("NOT_DIRTY", "Workspace has no changes to capture")
    if set(approved_files) != actual_paths:
        raise ChangeError("DIRTY_SET_CHANGED", "Displayed dirty paths changed or lack approval")
    for path in actual_paths:
        safe_source_path(path)
    approved_untracked = sorted(untracked)
    capture_fingerprint = workspace_fingerprint(
        workspace, approved_untracked=approved_untracked
    )
    files: list[tuple[str, str, bytes | None, int]] = []
    manifest_files: list[dict[str, Any]] = []
    total = 0
    for path in sorted(actual_paths):
        expected = approved_files[path]
        if expected is not None and not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ChangeError("INVALID_HASH", "Approved file hash is invalid")
        state = _file_state(workspace, path, max_bytes=MAX_CHANGESET_BYTES,
                            include_bytes=True)
        actual = state[0] if state is not None else None
        if actual != expected:
            raise ChangeError("CONTENT_CONFLICT", "Approved dirty file bytes changed")
        base = _base_file(repo, expected_head, path)
        base_digest = (hashlib.sha256(_base_blob_bytes(repo, base[1])).hexdigest()
                       if base is not None else None)
        if state is None:
            if base is None:
                raise ChangeError("MISSING_DELETE_TARGET", "Deleted path is absent from base")
            files.append((path, "delete", None, base[0]))
            manifest_files.append({"path": path, "op": "delete", "base_sha256": base_digest,
                                   "sha256": None, "mode": base[0]})
        else:
            content = state[2]
            if content is None:
                raise ChangeError("INVALID_PLAN", "Dirty file could not be read")
            total += len(content)
            if total > MAX_CHANGESET_BYTES:
                raise ChangeError("CHANGESET_TOO_LARGE", "Dirty capture exceeds byte budget")
            files.append((path, "upsert", content, state[1]))
            manifest_files.append({"path": path, "op": "upsert", "base_sha256": base_digest,
                                   "sha256": actual, "mode": state[1]})
    if workspace_fingerprint(workspace, approved_untracked=approved_untracked) != capture_fingerprint:
        raise ChangeError("WORKSPACE_CHANGED", "Workspace changed while freezing dirty files")
    return _snapshot_from_files(
        repo, stage_root, snapshot_id=selected_id, project_id=project_id,
        workspace_id=workspace_id, base_head=expected_head,
        source_generation=source_generation, capture_fingerprint=capture_fingerprint,
        provenance="LOCAL_DIRTY_VERIFY", dirty_at_capture=True,
        approved_untracked_paths=approved_untracked, files=files,
        manifest_files=manifest_files,
    )


def snapshot_is_stale(snapshot: Snapshot, workspace: Path, *,
                      current_generation: int) -> bool:
    """Detect editor/index/HEAD drift before promotion or publication."""
    if current_generation != snapshot.source_generation:
        return True
    try:
        return workspace_fingerprint(
            workspace, approved_untracked=snapshot.approved_untracked_paths
        ) != snapshot.capture_fingerprint
    except (ChangeError, GitError, OSError):
        return True
