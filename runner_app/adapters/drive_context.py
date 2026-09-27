"""Read-only committed-source context for an admitted EXPORT_CONTEXT operation.

No provider call, worktree-content read, upload, or repository mutation occurs here.
Selected committed excerpts remain local data requiring privacy review before delivery.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from runner_app.domain.commands import DomainError, validate_shape
from runner_app.git.argv import GitError, run_git, run_git_bytes, validate_branch
from runner_app.git.changes import ChangeError, safe_source_path
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.git.worktrees import _inspect_config, _reject_object_alternates, _workspace
from runner_app.storage.ledger import Ledger
from runner_app.storage.paths import UnsafePathError, _open_directory

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_MAX_FILES = 5_000
_MAX_PATH_BYTES = 1_000_000
_MAX_HASH_BLOB = 64 * 1024
_MAX_HASH_TOTAL = 4 * 1024 * 1024
_MAX_HASH_FILES = 256
_MAX_EXCERPT_TOTAL = 256 * 1024
_TEXT_SUFFIXES = frozenset({
    ".c", ".cc", ".cpp", ".css", ".go", ".h", ".html", ".ini", ".java", ".js",
    ".json", ".jsx", ".md", ".py", ".rs", ".sh", ".sql", ".toml", ".ts",
    ".tsx", ".txt", ".yaml", ".yml",
})
_TEXT_NAMES = frozenset({"Dockerfile", "LICENSE", "Makefile", "README"})


class ContextError(ValueError):
    """Stable export refusal; no source bytes, paths or Git stderr in messages."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _uuid(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as error:
        raise ContextError("INVALID_REQUEST_ID") from error
    return value


def _oid(value: str) -> str:
    if not isinstance(value, str) or _OID.fullmatch(value) is None:
        raise ContextError("INVALID_GIT_OBJECT")
    return value


def _path(raw: bytes) -> tuple[str, bool]:
    try:
        path = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise ContextError("UNSAFE_TREE_PATH") from error
    if (not path or len(path) > 500 or path.startswith("/") or "\\" in path or ":" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(ord(character) < 32 or ord(character) == 127 for character in path)):
        raise ContextError("UNSAFE_TREE_PATH")
    try:
        safe_source_path(path)
        return path, False
    except ChangeError as error:
        if error.code == "PROTECTED_PATH":
            return path, True
        raise ContextError("UNSAFE_TREE_PATH") from error


def _entries(repo: Path, inherited: tuple[int, ...], commit: str) -> list[dict[str, Any]]:
    raw = run_git_bytes(repo, ["--no-replace-objects", "ls-tree", "-r", "-z", "-l",
                               "--full-tree", commit],
                        pass_fds=inherited)
    if raw and not raw.endswith(b"\0"):
        raise ContextError("INVALID_GIT_TREE")
    entries: list[dict[str, Any]] = []
    paths: set[str] = set()
    directories: set[str] = set()
    path_bytes = 0
    for item in raw[:-1].split(b"\0") if raw else ():
        metadata, sep, raw_path = item.partition(b"\t")
        fields = metadata.split()
        if sep != b"\t" or len(fields) != 4:
            raise ContextError("INVALID_GIT_TREE")
        raw_mode, kind, raw_oid, raw_size = fields
        if raw_mode == b"160000":
            raise ContextError("UNSUPPORTED_TREE_ENTRY")
        if raw_mode not in {b"100644", b"100755", b"120000"} or kind != b"blob":
            raise ContextError("UNSUPPORTED_TREE_ENTRY")
        try:
            path, protected = _path(raw_path)
            oid = _oid(raw_oid.decode("ascii", "strict"))
            size = int(raw_size)
        except (UnicodeDecodeError, ValueError) as error:
            raise ContextError("INVALID_GIT_TREE") from error
        if size < 0 or len(oid) != len(commit) or path in paths or path in directories:
            raise ContextError("INVALID_GIT_TREE")
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in paths:
                raise ContextError("INVALID_GIT_TREE")
            directories.add(parent)
        paths.add(path)
        path_bytes += len(raw_path)
        if len(entries) >= _MAX_FILES or path_bytes > _MAX_PATH_BYTES:
            raise ContextError("CONTEXT_INVENTORY_LIMIT")
        classification = ("SYMLINK" if raw_mode == b"120000" else
                          "PROTECTED" if protected else
                          "LARGE" if size > _MAX_HASH_BLOB else "INDEX_ONLY")
        entries.append({
            "path": path, "git_oid": oid, "mode": raw_mode.decode("ascii"),
            "size_bytes": size, "sha256": None, "classification": classification,
        })
    return entries


def _blob(repo: Path, inherited: tuple[int, ...], entry: dict[str, Any]) -> bytes:
    try:
        raw = run_git_bytes(repo, ["--no-replace-objects", "cat-file", "blob",
                                   entry["git_oid"]],
                            pass_fds=inherited)
    except GitError as error:
        raise ContextError("CONTEXT_BLOB_UNAVAILABLE") from error
    if len(raw) != entry["size_bytes"]:
        raise ContextError("CONTEXT_BLOB_CHANGED")
    oid = entry["git_oid"]
    digest = hashlib.sha1 if len(oid) == 40 else hashlib.sha256
    if digest(f"blob {len(raw)}\0".encode("ascii") + raw).hexdigest() != oid:
        raise ContextError("CONTEXT_BLOB_CHANGED")
    return raw


def _commit_tree(repo: Path, inherited: tuple[int, ...], commit: str) -> str:
    raw = run_git_bytes(repo, ["--no-replace-objects", "cat-file", "commit", commit],
                        pass_fds=inherited)
    digest = hashlib.sha1 if len(commit) == 40 else hashlib.sha256
    if digest(f"commit {len(raw)}\0".encode("ascii") + raw).hexdigest() != commit:
        raise ContextError("CONTEXT_COMMIT_CHANGED")
    first_line = raw.split(b"\n", 1)[0]
    if not first_line.startswith(b"tree "):
        raise ContextError("INVALID_GIT_TREE")
    try:
        return _oid(first_line[5:].decode("ascii", "strict"))
    except UnicodeDecodeError as error:
        raise ContextError("INVALID_GIT_TREE") from error


def _text(raw: bytes) -> str | None:
    try:
        decoded = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return None
    if any(ord(character) < 32 and character not in "\t\n\r" or ord(character) == 127
           for character in decoded):
        return None
    return decoded


def _listed_text(path: str) -> bool:
    name = PurePosixPath(path).name
    return name in _TEXT_NAMES or PurePosixPath(path).suffix.lower() in _TEXT_SUFFIXES


@contextmanager
def _pinned(path: Path) -> Iterator[tuple[Path, tuple[int, ...]]]:
    try:
        descriptor = _open_directory(path, create=False)
    except (OSError, UnsafePathError) as error:
        raise ContextError("MANAGED_SOURCE_UNAVAILABLE") from error
    try:
        yield Path(f"/proc/self/fd/{descriptor}"), (descriptor,)
    finally:
        os.close(descriptor)


def _check_no_replacements(repo: Path, inherited: tuple[int, ...]) -> None:
    if run_git(repo, ["for-each-ref", "--format=%(refname)", "refs/replace/"],
               pass_fds=inherited):
        raise ContextError("REPLACEMENT_OBJECTS_UNSUPPORTED")


class ContextExporter:
    def __init__(self, ledger: Ledger, registry: SourceRegistry) -> None:
        if ledger.database.path != registry.database.path:
            raise ValueError("Context exporter and source registry must share one database")
        self.ledger = ledger
        self.registry = registry

    def _command(self, request_id: str) -> dict[str, Any]:
        operation = self.ledger.get(_uuid(request_id))
        if operation is None or operation["status"] != "RUNNING":
            raise ContextError("OPERATION_NOT_RUNNING")
        command: dict[str, Any] = operation["command"]
        try:
            validate_shape(command)
        except DomainError as error:
            raise ContextError("INVALID_COMMAND") from error
        if command["action"] != "EXPORT_CONTEXT":
            raise ContextError("WRONG_CONTEXT_ACTION")
        project, workspace = command["project_id"], command["workspace_id"]
        with closing(self.registry.database.connect()) as connection:
            held = connection.execute(
                "SELECT 1 FROM locks WHERE request_id=? AND scope=?",
                (request_id, f"workspace:{project}/{workspace}"),
            ).fetchone()
        if held is None:
            raise ContextError("CONTEXT_LOCK_MISSING")
        if command["params"]["include_diff"]:
            # v1 has no pinned committed diff basis. Never read the live dirty diff.
            raise ContextError("COMMITTED_DIFF_BASIS_REQUIRED")
        _oid(command["expected_head"])
        return command

    @staticmethod
    def _state(worktree: Path, inherited: tuple[int, ...]) -> tuple[str, str, bool]:
        head = run_git(worktree, ["rev-parse", "HEAD"], pass_fds=inherited).strip()
        try:
            branch = run_git(worktree, ["symbolic-ref", "--quiet", "--short", "HEAD"],
                             pass_fds=inherited).strip()
        except GitError:
            raise ContextError("WORKSPACE_DETACHED") from None
        dirty = bool(run_git_bytes(
            worktree, ["--no-optional-locks", "status", "--porcelain=v1", "-z",
                       "--untracked-files=all", "--ignored=matching"], pass_fds=inherited,
        ))
        return head, branch, dirty

    def export(self, request_id: str) -> dict[str, Any]:
        """Build a bounded committed-tree index and selected local-only excerpts."""
        command = self._command(request_id)
        project_id, workspace_id = command["project_id"], command["workspace_id"]
        head = command["expected_head"]
        project = self.registry.get_project(project_id)
        workspace = self.registry.get_workspace(project_id, workspace_id)
        if project is None:
            raise ContextError("PROJECT_NOT_REGISTERED")
        if workspace is None or workspace["archived"]:
            raise ContextError("WORKSPACE_NOT_ACTIVE")
        if workspace["head"] != head:
            raise ContextError("WORKSPACE_CHANGED")
        repo_path = Path(project["repo_path"])
        worktree_path = Path(workspace["worktree_path"])
        try:
            self.registry._managed_path(
                repo_path, directory=True,
                expected=self.registry.managed_root / project_id / "repo.git",
            )
            self.registry._managed_path(
                worktree_path, directory=True,
                expected=self.registry.managed_root / project_id / "workspaces" / workspace_id,
            )
            _workspace(repo_path, worktree_path)
        except (SourceError, GitError) as error:
            raise ContextError("MANAGED_SOURCE_UNAVAILABLE") from error
        selected = command["params"]["paths"]
        with _pinned(repo_path) as (repo, repo_fds), _pinned(worktree_path) as (worktree, tree_fds):
            try:
                _inspect_config(repo, pass_fds=repo_fds)
                _inspect_config(worktree, pass_fds=tree_fds)
                _reject_object_alternates(repo, pass_fds=repo_fds)
                _check_no_replacements(repo, repo_fds)
                before_head, branch, dirty = self._state(worktree, tree_fds)
                if (before_head != head or branch != workspace["branch"]
                        or run_git(repo, ["rev-parse", "--verify", f"refs/heads/{branch}"],
                                   pass_fds=repo_fds).strip() != head):
                    raise ContextError("WORKSPACE_CHANGED")
                validate_branch(branch)
                if run_git(repo, ["--no-replace-objects", "cat-file", "-t", head],
                           pass_fds=repo_fds).strip() != "commit":
                    raise ContextError("INVALID_GIT_OBJECT")
                tree = _commit_tree(repo, repo_fds, head)
                observed_tree = _oid(run_git(
                    repo, ["--no-replace-objects", "rev-parse", "--verify", f"{head}^{{tree}}"],
                    pass_fds=repo_fds,
                ).strip())
                if observed_tree != tree:
                    raise ContextError("CONTEXT_COMMIT_CHANGED")
                inventory = _entries(repo, repo_fds, head)
                by_path = {entry["path"]: entry for entry in inventory}
                # Requested paths consume the bounded hashing budget first.
                ordered = [by_path[path] for path in dict.fromkeys(selected) if path in by_path]
                ordered.extend(entry for entry in inventory if entry["path"] not in selected)
                hashed_total = 0
                hashed_files = 0
                excerpt_total = 0
                excerpts: dict[str, str] = {}
                for entry in ordered:
                    if entry["classification"] in {"PROTECTED", "SYMLINK", "LARGE"}:
                        continue
                    if (hashed_files >= _MAX_HASH_FILES
                            or hashed_total + entry["size_bytes"] > _MAX_HASH_TOTAL):
                        entry["classification"] = "HASH_BUDGET"
                        continue
                    raw = _blob(repo, repo_fds, entry)
                    hashed_total += len(raw)
                    hashed_files += 1
                    entry["sha256"] = hashlib.sha256(raw).hexdigest()
                    decoded = _text(raw)
                    entry["classification"] = "TEXT" if decoded is not None else "BINARY"
                    if (entry["path"] in selected and decoded is not None
                            and _listed_text(entry["path"]) and
                            excerpt_total + len(raw) <= _MAX_EXCERPT_TOTAL):
                        excerpts[entry["path"]] = decoded
                        excerpt_total += len(raw)
                selection = []
                for path in selected:
                    selected_entry = by_path.get(path)
                    selection.append({
                        "path": path,
                        "status": "INCLUDED" if path in excerpts else
                                  "MISSING" if selected_entry is None else "INDEX_ONLY",
                        "reason": None if path in excerpts or selected_entry is None else (
                            "FORMAT_NOT_ALLOWLISTED" if selected_entry["classification"] == "TEXT"
                            and not _listed_text(path) else selected_entry["classification"]),
                        "text": excerpts.get(path),
                    })
                after_head, after_branch, after_dirty = self._state(worktree, tree_fds)
                _check_no_replacements(repo, repo_fds)
                current = self.registry.get_workspace(project_id, workspace_id)
                if (after_head != head or after_branch != branch or after_dirty != dirty
                        or current is None or current["head"] != head
                        or current["generation"] != workspace["generation"]
                        or current["branch"] != workspace["branch"] or current["archived"]):
                    raise ContextError("WORKSPACE_CHANGED")
            except GitError as error:
                raise ContextError(error.code) from error
        return {
            "schema_version": "1.0", "request_id": request_id,
            "project_id": project_id, "workspace_id": workspace_id,
            "base_commit": head, "tree_oid": tree, "branch": branch,
            "source_generation": workspace["generation"],
            "dirty_at_export": dirty, "source": "COMMITTED_TREE",
            "include_diff": False, "inventory": inventory, "selection": selection,
            "excerpt_bytes": excerpt_total, "privacy_review_required": bool(excerpts),
            "delivery_status": "LOCAL_ONLY",
        }
