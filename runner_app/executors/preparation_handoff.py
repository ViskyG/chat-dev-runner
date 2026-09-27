"""Offline, byte-verified handoff from a finished preparation container.

This module never starts project code. The trusted coordinator must first
obtain a successful result from the rootless broker, then stage these files in a
separate quota-limited test volume. The broker can admit that copied tree
only with the same granted plan; staging alone does not authorize a test run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from .broker import BrokerResult
from .preparation_plan import PreparationError, PreparationPlan

_NAME = re.compile(r"[A-Za-z0-9_+@.-]{1,255}\Z")
_MAX_DEPTH = 32
_MAX_ENTRIES = 20_000
_MAX_FILE_BYTES = 128 * 1024 * 1024
_MAX_TREE_BYTES = 512 * 1024 * 1024
_OPEN_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_OPEN_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


@dataclass(frozen=True)
class BuildEntry:
    relative_path: str
    kind: Literal["directory", "file"]
    size_bytes: int
    sha256: str | None
    executable: bool


@dataclass(frozen=True)
class BuildManifest:
    plan_identity_sha256: str
    preparation_run_id: str
    preparation_profile_revision_sha256: str
    source_build: Path
    entries: tuple[BuildEntry, ...]
    total_bytes: int
    tree_sha256: str


def _unsafe() -> PreparationError:
    return PreparationError("PREPARATION_ARTIFACT_UNSAFE",
                            "Prepared build contains an unsafe artifact")


def _open_root(path: Path) -> int:
    """Pin each path component without following an ancestor symlink."""
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise _unsafe()
    parent = os.open("/", _OPEN_DIR)
    try:
        for component in path.parts[1:]:
            child = os.open(component, _OPEN_DIR, dir_fd=parent)
            os.close(parent)
            parent = child
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise _unsafe()
        return parent
    except OSError:
        os.close(parent)
        raise _unsafe() from None
    except BaseException:
        os.close(parent)
        raise


def _file_digest(directory: int, name: str, device: int) -> tuple[int, str, bool]:
    descriptor = os.open(name, _OPEN_FILE, dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_dev != device
                or before.st_uid != os.getuid() or before.st_nlink != 1
                or before.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | 0o022)
                or before.st_size > _MAX_FILE_BYTES):
            raise _unsafe()
        digest = hashlib.sha256()
        length = 0
        while chunk := os.read(descriptor, 65_536):
            length += len(chunk)
            if length > _MAX_FILE_BYTES:
                raise _unsafe()
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (length != before.st_size
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                    before.st_ctime_ns, before.st_mode) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                 after.st_ctime_ns, after.st_mode)):
            raise PreparationError("PREPARATION_ARTIFACT_CHANGED",
                                   "Prepared build changed during verification")
        return length, digest.hexdigest(), bool(before.st_mode & 0o111)
    finally:
        os.close(descriptor)


def _scan(root: Path) -> tuple[tuple[BuildEntry, ...], int]:
    descriptor = _open_root(root)
    entries: list[BuildEntry] = []
    total = 0
    root_before = os.fstat(descriptor)
    device = root_before.st_dev

    def walk(directory: int, prefix: str, depth: int) -> None:
        nonlocal total
        if depth > _MAX_DEPTH:
            raise _unsafe()
        with os.scandir(directory) as iterator:
            names = sorted(item.name for item in iterator)
        for name in names:
            relative = f"{prefix}/{name}" if prefix else name
            if (not _NAME.fullmatch(name) or name in {".", "..", ".git"}
                    or len(relative) > 1024 or len(entries) >= _MAX_ENTRIES):
                raise _unsafe()
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (info.st_dev != device or info.st_uid != os.getuid()
                    or info.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | 0o022)):
                raise _unsafe()
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, _OPEN_DIR, dir_fd=directory)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_mtime_ns,
                        opened.st_ctime_ns) != (info.st_dev, info.st_ino, info.st_mode,
                                               info.st_mtime_ns, info.st_ctime_ns):
                        raise PreparationError("PREPARATION_ARTIFACT_CHANGED",
                                               "Prepared build changed during verification")
                    entries.append(BuildEntry(relative, "directory", 0, None, True))
                    walk(child, relative, depth + 1)
                    after = os.fstat(child)
                    if (opened.st_dev, opened.st_ino, opened.st_mtime_ns,
                        opened.st_ctime_ns) != (after.st_dev, after.st_ino,
                                                after.st_mtime_ns, after.st_ctime_ns):
                        raise PreparationError("PREPARATION_ARTIFACT_CHANGED",
                                               "Prepared build changed during verification")
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                size, digest, executable = _file_digest(directory, name, device)
                total += size
                if total > _MAX_TREE_BYTES:
                    raise _unsafe()
                entries.append(BuildEntry(relative, "file", size, digest, executable))
            else:
                raise _unsafe()

    try:
        walk(descriptor, "", 1)
        root_after = os.fstat(descriptor)
        if (root_before.st_dev, root_before.st_ino, root_before.st_mode,
            root_before.st_mtime_ns, root_before.st_ctime_ns) != (
            root_after.st_dev, root_after.st_ino, root_after.st_mode,
            root_after.st_mtime_ns, root_after.st_ctime_ns):
            raise PreparationError("PREPARATION_ARTIFACT_CHANGED",
                                   "Prepared build changed during verification")
        return tuple(entries), total
    except OSError:
        raise _unsafe() from None
    finally:
        os.close(descriptor)


def _tree_digest(entries: tuple[BuildEntry, ...]) -> str:
    raw = json.dumps([asdict(entry) for entry in entries], sort_keys=True,
                     separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def attest_prepared_build(plan: PreparationPlan, result: BrokerResult,
                          source_build: Path) -> BuildManifest:
    """Accept only a complete, successful and cleaned preparation broker result."""
    try:
        canonical_run_id = str(UUID(result.run_id)) == result.run_id
    except (ValueError, TypeError, AttributeError):
        canonical_run_id = False
    capture = result.capture
    quota = result.containment.get("run_volume_hard_limit_bytes")
    if (not canonical_run_id or result.snapshot_id != plan.snapshot_id
            or result.snapshot_commit != plan.snapshot_commit
            or result.snapshot_tree != plan.snapshot_tree
            or result.profile_id != plan.preparation_profile_id
            or result.profile_revision_sha256 != plan.preparation_revision_sha256
            or result.image != plan.image
            or result.outcome != "PASSED" or result.exit_code != 0
            or capture.exit_code != 0 or capture.capture_fault is not None
            or capture.termination_reason is not None
            or capture.events_path is None or capture.events_sha256 is None
            or capture.metadata_path is None or capture.metadata_sha256 is None
            or capture.observed_events != capture.retained_events
            or len(capture.streams) != 2
            or {stream.name for stream in capture.streams} != {"stdout", "stderr"}
            or not all(stream.complete and stream.observed_bytes == stream.retained_bytes
                       for stream in capture.streams)
            or result.containment.get("verified") is not True
            or result.containment.get("backend") != "rootless_docker"
            or result.containment.get("image_repo_digest") != plan.image
            or type(quota) is not int or not 16 * 1024 * 1024 <= quota <= 4 * 1024 * 1024 * 1024
            or result.containment.get("daemon_rootless") is not True
            or result.containment.get("cleanup_verified") is not True
            or result.containment.get("network") != "none"
            or result.containment.get("read_only_source") is not True
            or result.containment.get("read_only_root") is not True
            or result.containment.get("no_new_privileges") is not True
            or result.containment.get("capabilities_dropped") != "ALL"
            or result.containment.get("mounts_verified_before_start") is not True
            or not isinstance(source_build, Path)
            or result.output_root != source_build.parent / "out"
            or source_build.name != "build" or source_build.parent.name != result.run_id):
        raise PreparationError("PREPARATION_RESULT_UNTRUSTED",
                               "Preparation result is incomplete or does not match the grant")
    parent = _open_root(source_build.parent)
    os.close(parent)
    entries, total = _scan(source_build)
    return BuildManifest(plan.identity_sha256, result.run_id,
                         result.profile_revision_sha256, source_build,
                         entries, total, _tree_digest(entries))


def _open_relative_directory(root: int, parts: tuple[str, ...]) -> int:
    descriptor = os.dup(root)
    try:
        for component in parts:
            child = os.open(component, _OPEN_DIR, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _copy_file(source_root: int, target_root: int, entry: BuildEntry) -> None:
    parts = tuple(entry.relative_path.split("/"))
    source_parent = _open_relative_directory(source_root, parts[:-1])
    target_parent = _open_relative_directory(target_root, parts[:-1])
    source = -1
    destination = -1
    try:
        source = os.open(parts[-1], _OPEN_FILE, dir_fd=source_parent)
        before = os.fstat(source)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_dev != os.fstat(source_root).st_dev
                or before.st_nlink != 1 or before.st_size != entry.size_bytes
                or before.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | 0o022)
                or bool(before.st_mode & 0o111) != entry.executable):
            raise PreparationError("PREPARATION_ARTIFACT_CHANGED",
                                   "Prepared build changed before handoff")
        destination = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                              os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                              dir_fd=target_parent)
        os.fchmod(destination, 0o700 if entry.executable else 0o600)
        digest = hashlib.sha256()
        length = 0
        while chunk := os.read(source, 65_536):
            length += len(chunk)
            if length > entry.size_bytes:
                raise PreparationError("PREPARATION_ARTIFACT_CHANGED",
                                       "Prepared build grew during handoff")
            digest.update(chunk)
            offset = 0
            while offset < len(chunk):
                written = os.write(destination, chunk[offset:])
                if written <= 0:
                    raise OSError("Prepared artifact copy made no progress")
                offset += written
        after = os.fstat(source)
        if (length != entry.size_bytes or digest.hexdigest() != entry.sha256
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                    before.st_ctime_ns, before.st_mode) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                 after.st_ctime_ns, after.st_mode)):
            raise PreparationError("PREPARATION_ARTIFACT_CHANGED",
                                   "Prepared build changed during handoff")
        os.fsync(destination)
    finally:
        if source >= 0:
            os.close(source)
        if destination >= 0:
            os.close(destination)
        os.close(source_parent)
        os.close(target_parent)


def stage_prepared_build(plan: PreparationPlan, manifest: BuildManifest,
                         target_build: Path) -> BuildManifest:
    """Copy into a separate empty private build root and verify both trees.

    Failure leaves the target quarantined for the caller to discard; no
    success manifest is returned and nothing becomes runnable.
    """
    if (manifest.plan_identity_sha256 != plan.identity_sha256
            or manifest.preparation_profile_revision_sha256 != plan.preparation_revision_sha256
            or manifest.preparation_run_id != manifest.source_build.parent.name
            or manifest.tree_sha256 != _tree_digest(manifest.entries)
            or not isinstance(target_build, Path) or target_build.name != "build"):
        raise PreparationError("PREPARATION_MANIFEST_MISMATCH",
                               "Prepared build manifest does not match the granted plan")
    target_parent = _open_root(target_build.parent)
    os.close(target_parent)
    source = _open_root(manifest.source_build)
    try:
        target = _open_root(target_build)
        try:
            if os.fstat(source).st_ino == os.fstat(target).st_ino and (
                    os.fstat(source).st_dev == os.fstat(target).st_dev):
                raise _unsafe()
            if os.listdir(target):
                raise PreparationError("PREPARATION_TARGET_NOT_EMPTY",
                                       "Handoff target must be empty")
            before_entries, before_bytes = _scan(manifest.source_build)
            if (before_entries != manifest.entries or before_bytes != manifest.total_bytes):
                raise PreparationError("PREPARATION_ARTIFACT_CHANGED",
                                       "Prepared build changed before handoff")
            for entry in manifest.entries:
                parts = tuple(entry.relative_path.split("/"))
                if entry.kind == "directory":
                    parent = _open_relative_directory(target, parts[:-1])
                    try:
                        os.mkdir(parts[-1], 0o700, dir_fd=parent)
                    finally:
                        os.close(parent)
                else:
                    _copy_file(source, target, entry)
            after_entries, after_bytes = _scan(manifest.source_build)
            copied_entries, copied_bytes = _scan(target_build)
            if (after_entries != manifest.entries or after_bytes != manifest.total_bytes
                    or copied_entries != manifest.entries or copied_bytes != manifest.total_bytes):
                raise PreparationError("PREPARATION_ARTIFACT_CHANGED",
                                       "Prepared build differs after handoff")
            os.fsync(target)
            return BuildManifest(
                manifest.plan_identity_sha256, manifest.preparation_run_id,
                manifest.preparation_profile_revision_sha256, target_build,
                copied_entries, copied_bytes, _tree_digest(copied_entries),
            )
        finally:
            os.close(target)
    except OSError:
        raise PreparationError("PREPARATION_HANDOFF_FAILED",
                               "Prepared build could not be staged") from None
    finally:
        os.close(source)


class OfflinePreparationCoordinator:
    """Compose finished-broker attestation and handoff without starting code."""

    def attest_and_stage(self, plan: PreparationPlan, result: BrokerResult,
                         source_build: Path, target_build: Path) -> BuildManifest:
        manifest = attest_prepared_build(plan, result, source_build)
        return stage_prepared_build(plan, manifest, target_build)
