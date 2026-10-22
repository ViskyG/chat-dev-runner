"""Finite, additive v2 result publication in the explicitly tested branch.

The controller supplies a managed bare repository, frozen evidence, its repository
lock, and a durable intent recorder. No project checkout, index or source ref is
changed. The production transport must update an *existing* ref with force=false;
creating a branch, PR or another delivery destination is outside this contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import tempfile
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from runner_app.git.argv import (
    _BASE_CONFIG,
    GitError,
    _environment,
    _terminate,
    _validate_args,
    run_git,
    run_git_bytes,
    validate_branch,
)
from runner_app.git.worktrees import _inspect_config, _reject_object_alternates

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z", re.ASCII)
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.ASCII)
_MANIFEST = ".runner-bundle.json"
COMMIT_AUTHOR = "Runner Results <runner-results@localhost>"


class ResultError(RuntimeError):
    """Stable, non-secret refusal; local result bytes are never removed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ResultPushRejected(ResultError):
    """A definite no-write response, distinguished from a lost response.

    Only a definite concurrent-ref conflict may be retried automatically. A
    generic HTTP/Git exception must never be translated to this exception.
    """

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.retryable = retryable


class ResultTransport(Protocol):
    """Controller-bound, authenticated transport for one approved repository.

    fetch_head observes the exact existing branch and installs its commit/tree
    objects in repo without changing a source ref or checkout. None means the
    branch is absent. push_commit updates that existing branch without force;
    it must never create a missing branch. A response lost after sending is an
    ordinary exception, not ResultPushRejected. No provider diagnostics escape.
    """

    def fetch_head(self, repo: Path, branch: str) -> str | None: ...

    def push_commit(self, repo: Path, commit: str, branch: str) -> None: ...


@dataclass(frozen=True)
class ResultLimits:
    per_file_bytes: int = 10 * 1024 * 1024
    bundle_bytes: int = 25 * 1024 * 1024
    max_files: int = 4096
    max_depth: int = 32


DEFAULT_RESULT_LIMITS = ResultLimits()


@dataclass(frozen=True)
class ResultPublishIntent:
    marker: str
    tested_branch: str
    runner_id: str
    request_id: str
    result_path: str
    bundle_sha256: str
    base_commit: str
    commit: str
    tree: str
    commit_timestamp: int
    commit_message: str


@dataclass(frozen=True)
class ResultPublication:
    status: str  # DELIVERED, LOCAL_ONLY, UNKNOWN
    code: str
    commit: str | None
    local_locator: str
    local_available: bool
    bundle_sha256: str | None = None
    remote_head: str | None = None
    marker: str | None = None


@dataclass(frozen=True)
class _Bundle:
    files: dict[str, bytes]
    digest: str


def _identifier(value: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ResultError("INVALID_RESULT_ID")
    return value


def _oid(value: str) -> str:
    if not isinstance(value, str) or _OID.fullmatch(value) is None:
        raise ResultError("INVALID_REMOTE_HEAD")
    return value


def _plain_directory(path: Path) -> Path:
    if (not path.is_absolute() or ".." in path.parts
            or path.resolve() != path or not path.is_dir()):
        raise ResultError("UNSAFE_RESULT_PATH")
    for part in (path, *path.parents):
        info = part.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise ResultError("UNSAFE_RESULT_PATH")
    return path


def _managed_repo(repo: Path) -> Path:
    repo = _plain_directory(Path(repo))
    _inspect_config(repo)
    _reject_object_alternates(repo)
    if run_git(repo, ["rev-parse", "--is-bare-repository"]).strip() != "true":
        raise ResultError("MANAGED_BARE_REPOSITORY_REQUIRED")
    if run_git(repo, ["for-each-ref", "--format=%(refname)", "refs/replace"]).strip():
        raise ResultError("REWRITTEN_HISTORY_UNSUPPORTED")
    grafts = Path(run_git(repo, ["rev-parse", "--git-path", "info/grafts"]).strip())
    if not grafts.is_absolute():
        grafts = repo / grafts
    if grafts.exists() or grafts.is_symlink():
        raise ResultError("REWRITTEN_HISTORY_UNSUPPORTED")
    return repo


def _component(name: str) -> str:
    if (name in {"", ".", ".."} or any(c in name for c in "/\\:")
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or len(name.encode("utf-8")) > 255):
        raise ResultError("UNSAFE_RESULT_PATH")
    return name


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _read_bundle(result_dir: Path, runner_id: str, request_id: str,
                 limits: ResultLimits) -> _Bundle:
    """Freeze bounded bytes using no-follow, descriptor-relative reads.

    Unsupported secure filesystem primitives are an explicit local blocker.
    The input is already controller-sealed; these checks also reject linked or
    changed paths instead of trusting a pathname stat followed by an open.
    """
    _plain_directory(result_dir)
    if (not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
            or os.open not in os.supports_dir_fd or os.scandir not in os.supports_fd):
        raise ResultError("UNSUPPORTED_SECURE_FILESYSTEM")
    if any(not isinstance(n, int) or isinstance(n, bool) or n <= 0 for n in (
        limits.per_file_bytes, limits.bundle_bytes, limits.max_files, limits.max_depth,
    )):
        raise ResultError("INVALID_RESULT_LIMITS")
    files: dict[str, bytes] = {}
    portable_paths: set[str] = set()
    total = 0
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(result_dir.anchor, directory_flags)
    try:
        for component in result_dir.parts[1:]:
            next_fd = os.open(component, directory_flags, dir_fd=root_fd)
            os.close(root_fd)
            root_fd = next_fd

        def visit(directory_fd: int, prefix: str, depth: int) -> None:
            nonlocal total
            if depth > limits.max_depth:
                raise ResultError("RESULT_PATH_DEPTH_LIMIT")
            before = os.fstat(directory_fd)
            names: list[str] = []
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    names.append(entry.name)
                    if len(names) > limits.max_files * (limits.max_depth + 1):
                        raise ResultError("RESULT_FILE_COUNT_LIMIT")
            names.sort()
            for name in names:
                _component(name)
                relative = f"{prefix}/{name}" if prefix else name
                if relative == _MANIFEST:
                    raise ResultError("RESERVED_BUNDLE_MANIFEST")
                portable = unicodedata.normalize("NFC", relative).casefold()
                if portable in portable_paths:
                    raise ResultError("RESULT_PATH_COLLISION")
                portable_paths.add(portable)
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (stat.S_ISLNK(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & 0x400):
                    raise ResultError("UNSAFE_RESULT_PATH")
                if relative == "delivery.json":
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ResultError("UNSAFE_RESULT_PATH")
                    continue  # Mutable delivery view is not immutable evidence.
                if stat.S_ISDIR(info.st_mode):
                    child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                    try:
                        if _identity(info) != _identity(os.fstat(child_fd)):
                            raise ResultError("RESULT_BUNDLE_CHANGED")
                        visit(child_fd, relative, depth + 1)
                    finally:
                        os.close(child_fd)
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ResultError("UNSAFE_RESULT_PATH")
                if len(files) >= limits.max_files:
                    raise ResultError("RESULT_FILE_COUNT_LIMIT")
                if info.st_size > limits.per_file_bytes:
                    raise ResultError("POLICY_SIZE_LIMIT")
                if total + info.st_size > limits.bundle_bytes:
                    raise ResultError("POLICY_SIZE_LIMIT")
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                with os.fdopen(descriptor, "rb") as stream:
                    if _identity(info) != _identity(os.fstat(stream.fileno())):
                        raise ResultError("RESULT_BUNDLE_CHANGED")
                    data = stream.read(limits.per_file_bytes + 1)
                    after = os.fstat(stream.fileno())
                    if _identity(info) != _identity(after) or len(data) != info.st_size:
                        raise ResultError("RESULT_BUNDLE_CHANGED")
                files[relative] = data
                total += len(data)
            after = os.fstat(directory_fd)
            if (before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_mtime_ns, after.st_ctime_ns):
                raise ResultError("RESULT_BUNDLE_CHANGED")

        visit(root_fd, "", 0)
    finally:
        os.close(root_fd)
    if not files:
        raise ResultError("RESULT_BUNDLE_EMPTY")
    manifest = json.dumps({
        "schema_version": 1, "runner_id": runner_id, "request_id": request_id,
        "files": [{"path": name, "bytes": len(data),
                   "sha256": hashlib.sha256(data).hexdigest()}
                  for name, data in sorted(files.items())],
    }, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    if (len(manifest) > limits.per_file_bytes
            or total + len(manifest) > limits.bundle_bytes
            or len(files) + 1 > limits.max_files):
        raise ResultError("POLICY_SIZE_LIMIT")
    files[_MANIFEST] = manifest
    return _Bundle(files, hashlib.sha256(manifest).hexdigest())


def _write_object(repo: Path, kind: str, data: bytes) -> str:
    if len(data) <= 2 * 1024 * 1024:
        return _oid(run_git_bytes(
            repo, ["hash-object", "-t", kind, "-w", "--stdin"], input_data=data,
        ).decode("ascii").strip())
    # Only frozen result bytes enter this temporary private file, never source.
    with tempfile.TemporaryDirectory(prefix="runner-result-blob-") as temporary:
        path = Path(temporary) / "blob"
        path.write_bytes(data)
        return _oid(run_git(repo, ["hash-object", "-t", kind, "-w", "--no-filters",
                                   "--", str(path)]).strip())


def _tree(repo: Path, entries: dict[bytes, tuple[bytes, bytes, str]]) -> str:
    raw = b"".join(mode + b" " + kind + b" " + oid.encode("ascii") + b"\t" + name + b"\0"
                   for name, (mode, kind, oid) in sorted(entries.items()))
    return _oid(run_git_bytes(repo, ["mktree", "-z"], input_data=raw).decode("ascii").strip())


def _bundle_tree(repo: Path, bundle: _Bundle) -> str:
    paths = {name: _write_object(repo, "blob", data) for name, data in bundle.files.items()}

    def build(prefix: str) -> str:
        entries: dict[bytes, tuple[bytes, bytes, str]] = {}
        for path, oid in paths.items():
            if not path.startswith(prefix):
                continue
            child, separator, _ = path[len(prefix):].partition("/")
            name = child.encode("utf-8")
            if name in entries:
                continue
            entries[name] = ((b"040000", b"tree", build(prefix + child + "/"))
                             if separator else (b"100644", b"blob", oid))
        return _tree(repo, entries)

    return build("")


def _entries(repo: Path, tree: str) -> dict[bytes, tuple[bytes, bytes, str]]:
    result: dict[bytes, tuple[bytes, bytes, str]] = {}
    for raw in run_git_bytes(repo, ["ls-tree", "-z", _oid(tree)]).split(b"\0"):
        if not raw:
            continue
        header, name = raw.split(b"\t", 1)
        mode, kind, oid = header.split(b" ")
        result[name] = (mode, kind, _oid(oid.decode("ascii")))
    return result


def _lookup(repo: Path, tree: str, parts: tuple[bytes, ...]) -> str | None:
    for part in parts:
        entry = _entries(repo, tree).get(part)
        if entry is None:
            return None
        mode, kind, tree = entry
        if (mode, kind) != (b"040000", b"tree"):
            raise ResultError("RESULT_PATH_CONFLICT")
    return tree


def _insert(repo: Path, tree: str | None, parts: tuple[bytes, ...], subtree: str) -> str:
    if not parts:
        return subtree
    entries = _entries(repo, tree) if tree else {}
    name = parts[0]
    old = entries.get(name)
    if old is not None and old[:2] != (b"040000", b"tree"):
        raise ResultError("RESULT_PATH_CONFLICT")
    child = _insert(repo, old[2] if old else None, parts[1:], subtree)
    entries[name] = (b"040000", b"tree", child)
    return _tree(repo, entries)


def _commit_tree(repo: Path, commit: str) -> str:
    return _oid(run_git(repo, ["rev-parse", "--verify", f"{_oid(commit)}^{{tree}}"] ).strip())


def _proof_commit(repo: Path, head: str, result_path: str, intended: str | None) -> str:
    if intended is not None:
        try:
            run_git(repo, ["merge-base", "--is-ancestor", intended, head])
            return intended
        except GitError:
            pass
    try:
        return _oid(run_git(repo, ["rev-list", "-1", head, "--", result_path]).strip())
    except GitError:
        # A transport may have installed the observed head/tree without its
        # complete ancestry. That exact fetched head already proves delivery.
        return head


def bundle_digest(result_dir: Path, *, runner_id: str, request_id: str,
                  limits: ResultLimits = DEFAULT_RESULT_LIMITS) -> str:
    """Verify the immutable local bundle identity, including on a local-only run."""
    return _read_bundle(result_dir, _identifier(runner_id), _identifier(request_id), limits).digest


def publish_results(
    repo: Path, transport: ResultTransport, *, tested_branch: str,
    runner_id: str, request_id: str, result_dir: Path,
    record_intent: Callable[[ResultPublishIntent], None],
    limits: ResultLimits = DEFAULT_RESULT_LIMITS, max_conflict_retries: int = 3,
    publication_marker: str | None = None, commit_timestamp: int | None = None,
    expected_bundle_sha256: str | None = None, reconcile_only: bool = False,
) -> ResultPublication:
    """Publish one sealed bundle; keep every failure local and finitely bounded.

    record_intent must synchronously persist the exact candidate before returning.
    An UNKNOWN candidate is reconciled only by readback in this call, never pushed
    again under a different commit identity. The controller writes its local
    delivery record from this response; delivery.json is excluded from hashing.
    Restart recovery uses reconcile_only after a durable prior publication intent:
    absence cannot authorize replay of a possibly submitted ref update.
    """
    result_dir = Path(result_dir)
    locator = str(result_dir.absolute())
    bundle: _Bundle | None = None
    intended: str | None = None
    head: str | None = None
    marker = publication_marker or uuid4().hex
    local_available = False

    def answer(status: str, code: str, commit: str | None = None) -> ResultPublication:
        # After a prior durable intent, absence, conflicts and failed readback
        # cannot prove that its ref write never applied. Only the exact fetched
        # subtree can resolve that uncertainty, including on local evidence errors.
        if reconcile_only and status == "LOCAL_ONLY":
            status = "UNKNOWN"
        return ResultPublication(status, code, commit, locator, local_available,
                                 bundle.digest if bundle else None, head, marker)

    try:
        tested_branch = validate_branch(tested_branch)
        _identifier(runner_id)
        _identifier(request_id)
        _identifier(marker)
        if (not isinstance(max_conflict_retries, int) or isinstance(max_conflict_retries, bool)
                or not 0 <= max_conflict_retries <= 3):
            raise ResultError("INVALID_RETRY_BUDGET")
        timestamp = int(time.time()) if commit_timestamp is None else commit_timestamp
        if (not isinstance(timestamp, int) or isinstance(timestamp, bool)
                or not 0 <= timestamp <= 253402300799):
            raise ResultError("INVALID_COMMIT_TIMESTAMP")
        repo = _managed_repo(Path(repo))
        _plain_directory(result_dir)
        if repo == result_dir or repo in result_dir.parents or result_dir in repo.parents:
            raise ResultError("RESULT_SOURCE_PATH_OVERLAP")
        bundle = _read_bundle(result_dir, runner_id, request_id, limits)
        local_available = True
        if expected_bundle_sha256 is not None and bundle.digest != expected_bundle_sha256:
            return answer("LOCAL_ONLY", "RESULT_BUNDLE_CHANGED")
        subtree = _bundle_tree(repo, bundle)
        result_path = f".runner/results/{runner_id}/{request_id}"
        parts = tuple(part.encode("ascii") for part in result_path.split("/"))
        message = (f"Publish runner results: {runner_id}/{request_id}\n\n"
                   f"Bundle-SHA256: {bundle.digest}\nPublication-Marker: {marker}\n")

        for attempt in range(max_conflict_retries + 1):
            head = transport.fetch_head(repo, tested_branch)
            if head is None:
                return answer("LOCAL_ONLY", "TARGET_BRANCH_MISSING")
            head = _oid(head)
            base_tree = _commit_tree(repo, head)
            existing = _lookup(repo, base_tree, parts)
            if existing is not None:
                if existing != subtree:
                    return answer("LOCAL_ONLY", "RESULT_PATH_CONFLICT")
                # This fetched branch head proves the complete exact tree, not
                # merely an untrusted manifest's asserted bundle digest.
                return answer("DELIVERED", "ALREADY_DELIVERED",
                              _proof_commit(repo, head, result_path, None))
            if reconcile_only:
                return answer("UNKNOWN", "DELIVERY_UNKNOWN")
            tree = _insert(repo, base_tree, parts, subtree)
            identity = f"{COMMIT_AUTHOR} {timestamp} +0000"
            raw_commit = (f"tree {tree}\nparent {head}\nauthor {identity}\n"
                          f"committer {identity}\n\n{message}").encode()
            intended = _write_object(repo, "commit", raw_commit)
            intent = ResultPublishIntent(marker, tested_branch, runner_id, request_id,
                                         result_path, bundle.digest, head, intended, tree,
                                         timestamp, message)
            try:
                record_intent(intent)
            except Exception:  # noqa: BLE001 - persistence details are local/private
                return answer("LOCAL_ONLY", "INTENT_PERSISTENCE_FAILED", intended)
            rejected: ResultPushRejected | None = None
            ambiguous = False
            try:
                transport.push_commit(repo, intended, tested_branch)
            except ResultPushRejected as error:
                rejected = error
            except Exception:  # noqa: BLE001 - credentials/provider diagnostics stay private
                ambiguous = True
            try:
                observed = transport.fetch_head(repo, tested_branch)
                head = _oid(observed) if observed is not None else None
                existing = _lookup(repo, _commit_tree(repo, head), parts) if head else None
            except Exception:  # noqa: BLE001 - no readback means no delivery claim
                if rejected is not None:
                    return answer("LOCAL_ONLY", rejected.code, intended)
                return answer("UNKNOWN", "DELIVERY_UNKNOWN", intended)
            if existing == subtree:
                assert head is not None
                return answer("DELIVERED", "DELIVERED_RECONCILED" if ambiguous else "DELIVERED",
                              _proof_commit(repo, head, result_path, intended))
            if rejected is None:
                return answer("UNKNOWN", "DELIVERY_UNKNOWN", intended)
            if existing is not None:
                return answer("LOCAL_ONLY", "RESULT_PATH_CONFLICT", intended)
            if head is None:
                return answer("LOCAL_ONLY", "TARGET_BRANCH_MISSING", intended)
            if not rejected.retryable:
                return answer("LOCAL_ONLY", rejected.code, intended)
            if attempt == max_conflict_retries:
                return answer("LOCAL_ONLY", "RESULT_CONFLICT_RETRY_LIMIT", intended)
        raise AssertionError("Bounded publication loop did not return")
    except (ResultError, GitError) as error:
        if error.code in {"POLICY_SIZE_LIMIT", "RESULT_FILE_COUNT_LIMIT", "RESULT_PATH_DEPTH_LIMIT"}:
            local_available = True  # Verified directory retained; no upload was attempted.
        return answer("LOCAL_ONLY", error.code, intended)
    except (OSError, UnicodeError, ValueError, TypeError):
        return answer("LOCAL_ONLY", "LOCAL_PERSISTENCE_FAILED", intended)
    except Exception:  # noqa: BLE001 - no transport diagnostic/secret reaches result
        return answer("LOCAL_ONLY", "PUBLICATION_FAILED", intended)


def _local_push(repo: Path, arguments: Sequence[str]) -> tuple[int, bytes]:
    """Bounded porcelain status without discarding a definite rejected response."""
    command = ["git"]
    for setting in _BASE_CONFIG:
        command.extend(("-c", setting))
    command.extend(_validate_args(arguments))
    process = subprocess.Popen(
        command, cwd=repo, env=_environment(), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, umask=0o077,
    )
    output = bytearray()
    discarded = bytearray()
    deadline = time.monotonic() + 120
    try:
        assert process.stdout is not None and process.stderr is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, output)
            selector.register(process.stderr, selectors.EVENT_READ, discarded)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GitError("GIT_TIMEOUT", "Local result push timed out")
                for key, _ in selector.select(min(remaining, 0.25)):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                    else:
                        key.data.extend(data)
                    if len(output) + len(discarded) > 1024 * 1024:
                        raise GitError("GIT_OUTPUT_LIMIT", "Local result push exceeded output budget")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitError("GIT_TIMEOUT", "Local result push timed out")
        try:
            status = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise GitError("GIT_TIMEOUT", "Local result push timed out") from None
        return status, bytes(output)
    finally:
        if process.poll() is None:
            _terminate(process)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


class LocalBareResultTransport:
    """Real normal-Git receiver transport for LOCAL TESTS ONLY.

    Ordinary push can recreate a branch deleted between its ref advertisement
    and write. The existence precheck below narrows that race but cannot close
    it. Production must implement ResultTransport using an existing-ref update
    (for example GitHub PATCH refs force=false), never a create-ref operation.
    """

    def __init__(self, receiver: Path) -> None:
        self.receiver = _managed_repo(Path(receiver))
        hooks = self.receiver / "hooks"
        if hooks.is_symlink() or (hooks.exists() and (not hooks.is_dir() or any(
            child.is_symlink() or child.suffix != ".sample" for child in hooks.iterdir()
        ))):
            raise ResultError("UNSAFE_LOCAL_RECEIVER")

    def _read_head(self, repo: Path, branch: str) -> str | None:
        branch = validate_branch(branch)
        ref = f"refs/heads/{branch}"
        raw = run_git(repo, ["ls-remote", "--refs", "--heads", str(self.receiver), ref])
        lines = raw.splitlines()
        if not lines:
            return None
        if len(lines) != 1:
            raise ResultError("INVALID_REMOTE_HEAD")
        oid, separator, returned_ref = lines[0].partition("\t")
        if not separator or returned_ref != ref:
            raise ResultError("INVALID_REMOTE_HEAD")
        return _oid(oid)

    def fetch_head(self, repo: Path, branch: str) -> str | None:
        if self._read_head(repo, branch) is None:
            return None
        ref = f"refs/cdr/result-readback/{uuid4().hex}"
        try:
            run_git(repo, ["fetch", "--no-tags", "--no-recurse-submodules", "--no-auto-gc",
                           "--no-write-fetch-head", "--no-progress", str(self.receiver),
                           f"refs/heads/{validate_branch(branch)}:{ref}"], timeout=120)
            return _oid(run_git(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"] ).strip())
        except GitError:
            if self._read_head(repo, branch) is None:
                return None
            raise
        finally:
            run_git(repo, ["update-ref", "-d", ref])

    def push_commit(self, repo: Path, commit: str, branch: str) -> None:
        commit = _oid(commit)
        branch = validate_branch(branch)
        if self._read_head(repo, branch) is None:
            raise ResultPushRejected("TARGET_BRANCH_MISSING")
        ref = f"refs/heads/{branch}"
        status, output = _local_push(repo, [
            "push", "--porcelain", "--no-verify", "--no-follow-tags",
            "--recurse-submodules=no", str(self.receiver), f"{commit}:{ref}",
        ])
        if status == 0:
            return
        for line in output.splitlines():
            fields = line.split(b"\t")
            if len(fields) != 3 or fields[0] != b"!":
                continue
            if fields[1] != f"{commit}:{ref}".encode("ascii"):
                continue
            retryable = fields[2] in {
                b"[rejected] (fetch first)", b"[rejected] (non-fast-forward)",
            }
            raise ResultPushRejected("REMOTE_CHANGED" if retryable else "PUSH_REJECTED",
                                     retryable=retryable)
        raise GitError("GIT_PUSH_UNKNOWN", "Local result push has no definite response")
