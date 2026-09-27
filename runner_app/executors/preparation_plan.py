"""Offline identity planning for a future contained dependency-preparation phase.

A plan grants no execution authority. Both profiles and their exact revisions come
from owner-private local configuration; the source snapshot is verified against
its Git tree, and the lockfile is read without following symlinks. Package hooks
must remain unavailable until a separate rootless preparation broker, artifact
handoff, and evidence contract are implemented and accepted on the target host.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn
from uuid import UUID

from runner_app.git.changes import ChangeError, safe_source_path

from .broker import BrokerError, SnapshotInput, _snapshot_tree
from .profiles import ProfileError, ProfileRegistry

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_MAX_LOCKFILE_BYTES = 1_048_576


class PreparationError(ValueError):
    """A stable refusal that never includes profile, source, or lockfile bytes."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class PreparationGrant:
    """Owner-side binding; never construct from a Drive command or project file."""

    template_id: str
    execution_profile_id: str
    execution_revision_sha256: str
    preparation_profile_id: str
    preparation_revision_sha256: str
    lockfile_path: str


@dataclass(frozen=True)
class PreparationPlan:
    """Read-only identity evidence, not a runnable environment or cache entry."""

    template_id: str
    snapshot_id: str
    snapshot_commit: str
    snapshot_tree: str
    lockfile_path: str
    lockfile_sha256: str
    execution_profile_id: str
    execution_revision_sha256: str
    preparation_profile_id: str
    preparation_revision_sha256: str
    image: str
    platform: str
    identity_sha256: str


def _validate_grant(grant: PreparationGrant) -> tuple[str, ...]:
    values = (grant.template_id, grant.execution_profile_id, grant.preparation_profile_id)
    if (not all(isinstance(value, str) and _SLUG.fullmatch(value) for value in values)
            or grant.execution_profile_id == grant.preparation_profile_id
            or not all(isinstance(value, str) and _SHA256.fullmatch(value) for value in (
                grant.execution_revision_sha256, grant.preparation_revision_sha256,
            ))):
        raise PreparationError("PREPARATION_GRANT_INVALID", "Local preparation grant is invalid")
    try:
        relative = safe_source_path(grant.lockfile_path)
    except (ChangeError, TypeError, UnicodeError):
        raise PreparationError("PREPARATION_GRANT_INVALID", "Local lockfile grant is invalid") from None
    return relative.parts


def _validate_snapshot(snapshot: SnapshotInput) -> None:
    try:
        if (str(UUID(snapshot.snapshot_id)) != snapshot.snapshot_id
                or not _OID.fullmatch(snapshot.commit)
                or not _OID.fullmatch(snapshot.tree)
                or len(snapshot.commit) != len(snapshot.tree)
                or not isinstance(snapshot.path, Path)):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise PreparationError("SNAPSHOT_INVALID", "Frozen snapshot identity is invalid") from None
    try:
        _snapshot_tree(snapshot.path, snapshot.tree)
    except (BrokerError, OSError):
        raise PreparationError("SNAPSHOT_INVALID", "Frozen snapshot cannot be verified") from None


def _lockfile_sha256(root: Path, parts: tuple[str, ...]) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(root, flags | os.O_DIRECTORY)
        for part in parts[:-1]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=descriptor)
        try:
            before = os.fstat(file_descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_nlink != 1 or before.st_mode & 0o222
                    or not 1 <= before.st_size <= _MAX_LOCKFILE_BYTES):
                raise PreparationError("LOCKFILE_UNSAFE", "Frozen lockfile is unsafe or too large")
            digest = hashlib.sha256()
            count = 0
            while chunk := os.read(file_descriptor, 65_536):
                count += len(chunk)
                if count > _MAX_LOCKFILE_BYTES:
                    raise PreparationError("LOCKFILE_UNSAFE", "Frozen lockfile is too large")
                digest.update(chunk)
            after = os.fstat(file_descriptor)
            if (count != before.st_size or (before.st_dev, before.st_ino, before.st_size,
                    before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                    after.st_ctime_ns)):
                raise PreparationError("LOCKFILE_CHANGED", "Frozen lockfile changed during read")
            return digest.hexdigest()
        finally:
            os.close(file_descriptor)
    except FileNotFoundError:
        raise PreparationError("LOCKFILE_REQUIRED", "Approved lockfile is absent") from None
    except (OSError, NotADirectoryError):
        raise PreparationError("LOCKFILE_UNSAFE", "Frozen lockfile cannot be read safely") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def plan_preparation(snapshot: SnapshotInput, *, template_id: str,
                     execution_profile_id: str, grant: PreparationGrant,
                     profiles: ProfileRegistry) -> PreparationPlan:
    """Bind one frozen source to exact owner-approved preparation/test profiles.

    The two caller-provided IDs only select a local grant. No remote field can
    supply an argv, image, mount, network mode, lockfile path, or host path.
    """
    parts = _validate_grant(grant)
    if (template_id != grant.template_id
            or execution_profile_id != grant.execution_profile_id):
        raise PreparationError("PREPARATION_NOT_GRANTED", "Template/profile pairing is not granted")
    try:
        execution = profiles.require(grant.execution_profile_id)
        preparation = profiles.require(grant.preparation_profile_id)
    except ProfileError:
        raise PreparationError("PREPARATION_PROFILE_UNAVAILABLE",
                               "Owner-approved preparation profile is unavailable") from None
    if (execution.revision_sha256 != grant.execution_revision_sha256
            or preparation.revision_sha256 != grant.preparation_revision_sha256
            or execution.image != preparation.image or execution.platform != preparation.platform
            or execution.network != "none" or preparation.network != "none"
            or execution.user != "0:0" or preparation.user != "0:0"):
        raise PreparationError("PREPARATION_GRANT_CHANGED",
                               "Preparation profile no longer matches the local grant")
    _validate_snapshot(snapshot)
    lockfile_sha256 = _lockfile_sha256(snapshot.path, parts)
    identity = {
        "template_id": grant.template_id,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_commit": snapshot.commit,
        "snapshot_tree": snapshot.tree,
        "lockfile_path": grant.lockfile_path,
        "lockfile_sha256": lockfile_sha256,
        "execution_profile_id": execution.profile_id,
        "execution_revision_sha256": execution.revision_sha256,
        "preparation_profile_id": preparation.profile_id,
        "preparation_revision_sha256": preparation.revision_sha256,
        "image": execution.image,
        "platform": execution.platform,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return PreparationPlan(**identity, identity_sha256=hashlib.sha256(encoded).hexdigest())


def execute_preparation(_plan: PreparationPlan) -> NoReturn:
    """Fail closed until a separately contained preparation phase exists."""
    raise PreparationError("PREPARATION_UNAVAILABLE",
                           "Contained dependency preparation is not installed")
