"""Owner-approved, immutable execution profiles.

The profile directory and approval hashes are trusted *local* configuration.
Neither a project checkout nor a Drive command may supply either argument.
Every lookup reopens a private profile file and verifies its exact bytes, so a
profile changed after approval cannot gain authority by keeping the same ID.

This registry describes containment; it does not implement it. In particular,
container UID 0 is acceptable only if the broker independently proves that the
daemon is rootless and still drops capabilities and privileges.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal, cast

from runner_app.storage.paths import UnsafePathError, open_private_file

_MAX_PROFILE_BYTES = 65_536
_PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE = re.compile(
    r"[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?/[a-z0-9][a-z0-9._/-]*"
    r"@sha256:[0-9a-f]{64}\Z"
)
_OUTPUT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_FORMAT = re.compile(r"[a-z0-9][a-z0-9+./-]{0,63}\Z")
_USER = re.compile(r"(0|[1-9][0-9]{0,5}):(0|[1-9][0-9]{0,5})\Z")
_SAFE_OUTPUT_PATH = re.compile(r"[A-Za-z0-9_.*?/-]{1,256}\Z")

_PROFILE_KEYS = frozenset({
    "schema_version", "profile_id", "description", "execution_backend", "image", "argv",
    "cwd", "mounts", "user", "timeout_seconds", "network", "memory_mb", "cpu_limit",
    "pids_limit", "capabilities", "no_new_privileges", "read_only_root", "secrets",
    "output_budget_mb", "collect", "approval", "platform", "installed", "approved",
})
_MOUNT_LAYOUT = (
    ("snapshot", "/src", True),
    ("build", "/build", False),
    ("output", "/out", False),
    ("scratch", "/tmp", False),
)
_OUTPUT_KINDS = frozenset({
    "application_log", "test_result", "coverage", "report", "screenshot", "artifact",
})


class ProfileError(ValueError):
    """Safe, stable profile refusal; never includes file bytes or secret paths."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ProfileMount:
    """A broker-owned virtual source, never a host pathname from a profile."""

    source: Literal["snapshot", "build", "output", "scratch"]
    destination: str
    read_only: bool


@dataclass(frozen=True)
class ProfileOutput:
    id: str
    kind: str
    path: str
    format: str
    required: bool
    upload_policy: Literal["local_only", "sanitized_only"]


@dataclass(frozen=True)
class ApprovedProfile:
    profile_id: str
    revision_sha256: str
    execution_backend: Literal["rootless_container"]
    image: str
    argv: tuple[str, ...]
    cwd: str
    user: str
    mounts: tuple[ProfileMount, ...]
    timeout_seconds: int
    network: Literal["none"]
    memory_mb: int
    cpu_limit: float
    pids_limit: int
    output_budget_mb: int
    collect: tuple[ProfileOutput, ...]
    platform: Literal["linux/amd64", "linux/arm64"]

    @property
    def image_digest(self) -> str:
        return self.image.rsplit("@sha256:", 1)[1]


def _bad() -> ProfileError:
    return ProfileError("PROFILE_INVALID", "Локальный профиль исполнения некорректен.")


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise _bad()
        result[key] = value
    return result


def _constant(_value: str) -> Any:
    raise _bad()


def _int(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _bad()
    return value


def _scalar(value: str) -> bool:
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return False
    return True


def _float(value: Any, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float):
        raise _bad()
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise _bad() from error
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise _bad()
    return result


def _mounts(value: Any) -> tuple[ProfileMount, ...]:
    if value is None:
        value = [
            {"source": source, "destination": destination, "read_only": read_only}
            for source, destination, read_only in _MOUNT_LAYOUT
        ]
    if not isinstance(value, list) or len(value) != len(_MOUNT_LAYOUT):
        raise _bad()
    checked: list[ProfileMount] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"source", "destination", "read_only"}:
            raise _bad()
        source = item["source"]
        destination = item["destination"]
        read_only = item["read_only"]
        if not isinstance(source, str) or not isinstance(destination, str) or type(read_only) is not bool:
            raise _bad()
        checked.append(ProfileMount(cast(Any, source), destination, read_only))
    if tuple((m.source, m.destination, m.read_only) for m in checked) != _MOUNT_LAYOUT:
        raise _bad()
    return tuple(checked)


def _output_path(value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_OUTPUT_PATH.fullmatch(value):
        raise _bad()
    if value.startswith("/") or "//" in value or "**" in value:
        raise _bad()
    parts = value.split("/")
    if any(part in {"", ".", "..", ".git"} or part.startswith(".") for part in parts):
        raise _bad()
    return value


def _collect(value: Any) -> tuple[ProfileOutput, ...]:
    if not isinstance(value, list) or len(value) > 64:
        raise _bad()
    outputs: list[ProfileOutput] = []
    ids: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or not {"id", "kind", "path", "format", "required"} <= set(item):
            raise _bad()
        if set(item) - {"id", "kind", "path", "format", "required", "upload_policy"}:
            raise _bad()
        output_id = item["id"]
        kind = item["kind"]
        fmt = item["format"]
        required = item["required"]
        upload = item.get("upload_policy", "local_only")
        if (not isinstance(output_id, str) or not _OUTPUT_ID.fullmatch(output_id)
                or output_id in ids or not isinstance(kind, str) or kind not in _OUTPUT_KINDS
                or not isinstance(fmt, str) or not _FORMAT.fullmatch(fmt)
                or type(required) is not bool or not isinstance(upload, str)
                or upload not in {"local_only", "sanitized_only"}):
            raise _bad()
        ids.add(output_id)
        outputs.append(ProfileOutput(output_id, kind, _output_path(item["path"]), fmt,
                                     required, cast(Any, upload)))
    return tuple(outputs)


def _validate(raw: bytes, *, requested_id: str, revision_sha256: str) -> ApprovedProfile:
    try:
        value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_pairs,
                           parse_constant=_constant)
    except ProfileError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise _bad() from error
    if not isinstance(value, dict) or set(value) - _PROFILE_KEYS:
        raise _bad()
    if value.get("schema_version") != "1.0" or value.get("profile_id") != requested_id:
        raise _bad()
    if value.get("installed") is not True or value.get("approved") is not True:
        raise ProfileError("PROFILE_UNAVAILABLE", "Профиль не установлен и не разрешён владельцем.")
    if value.get("execution_backend") != "rootless_container":
        raise _bad()
    image = value.get("image")
    if (not isinstance(image, str) or not _IMAGE.fullmatch(image)
            or "//" in image or ".." in image):
        raise _bad()
    argv = value.get("argv")
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 64
            or any(not isinstance(arg, str) or not 1 <= len(arg) <= 4096
                   or not _scalar(arg)
                   or any(ord(char) < 32 or ord(char) == 127 for char in arg)
                   for arg in argv)):
        raise _bad()
    cwd = value.get("cwd", "/src")
    if (not isinstance(cwd, str) or len(cwd) > 256 or not _scalar(cwd) or "\\" in cwd
            or any(part in {".", ".."} for part in cwd.split("/"))
            or str(PurePosixPath(cwd)) != cwd
            or not (cwd == "/src" or cwd.startswith("/src/"))):
        raise _bad()
    user = value.get("user", "0:0")
    if not isinstance(user, str) or not _USER.fullmatch(user):
        raise _bad()
    uid, gid = (int(part) for part in user.split(":"))
    if uid > 65535 or gid > 65535:
        raise _bad()
    if (value.get("network") != "none" or value.get("capabilities") != "drop_all"
            or value.get("no_new_privileges") is not True
            or value.get("read_only_root") is not True or value.get("secrets") != []):
        raise _bad()
    if not isinstance(value.get("platform"), str) or value["platform"] not in {
        "linux/amd64", "linux/arm64"
    }:
        raise _bad()
    for key in ("description", "approval"):
        if key in value and (not isinstance(value[key], str) or len(value[key]) > 1024
                             or not _scalar(value[key])):
            raise _bad()
    return ApprovedProfile(
        profile_id=requested_id,
        revision_sha256=revision_sha256,
        execution_backend="rootless_container",
        image=image,
        argv=tuple(argv),
        cwd=cwd,
        user=user,
        mounts=_mounts(value.get("mounts")),
        timeout_seconds=_int(value.get("timeout_seconds"), 1, 3600),
        network="none",
        memory_mb=_int(value.get("memory_mb"), 16, 32768),
        cpu_limit=_float(value.get("cpu_limit"), 0.1, 32.0),
        pids_limit=_int(value.get("pids_limit"), 8, 4096),
        output_budget_mb=_int(value.get("output_budget_mb"), 1, 1024),
        collect=_collect(value.get("collect")),
        platform=cast(Any, value["platform"]),
    )


class ProfileRegistry:
    """Deny-by-default profile reader configured only by trusted local owner state.

    ``approved_hashes`` contains exact SHA-256 hashes of 0600 files named
    ``<profile_id>.json`` in a 0700 owner directory. Listing a profile in source
    code, a project policy, or a remote command is not a grant.
    """

    def __init__(self, profile_dir: Path, approved_hashes: Mapping[str, str] | None = None) -> None:
        directory = Path(profile_dir)
        if not directory.is_absolute() or ".." in directory.parts:
            raise ProfileError("PROFILE_CONFIG_UNSAFE", "Каталог профилей должен быть абсолютным.")
        grants = dict(approved_hashes or {})
        if any(not isinstance(key, str) or not _PROFILE_ID.fullmatch(key)
               or not isinstance(digest, str) or not _SHA256.fullmatch(digest)
               for key, digest in grants.items()):
            raise ProfileError("PROFILE_CONFIG_UNSAFE", "Локальное разрешение профиля некорректно.")
        self._profile_dir = directory
        self._approved_hashes = MappingProxyType(grants)

    @property
    def granted_ids(self) -> frozenset[str]:
        """Configured identifiers only; use ``available_ids`` for live UI choices."""
        return frozenset(self._approved_hashes)

    def available_ids(self) -> frozenset[str]:
        """Only profiles that still pass the complete file and approval validation."""
        available: set[str] = set()
        for profile_id in self._approved_hashes:
            try:
                self.require(profile_id)
            except ProfileError:
                continue
            available.add(profile_id)
        return frozenset(available)

    def require(self, profile_id: str) -> ApprovedProfile:
        if not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id):
            raise ProfileError("PROFILE_UNAVAILABLE", "Профиль не разрешён владельцем.")
        approved_hash = self._approved_hashes.get(profile_id)
        if approved_hash is None:
            raise ProfileError("PROFILE_UNAVAILABLE", "Профиль не разрешён владельцем.")
        try:
            descriptor = open_private_file(self._profile_dir / f"{profile_id}.json")
            with os.fdopen(descriptor, "rb") as profile_file:
                raw = profile_file.read(_MAX_PROFILE_BYTES + 1)
        except (OSError, UnsafePathError) as error:
            raise ProfileError("PROFILE_UNAVAILABLE", "Локальный профиль недоступен.") from error
        if len(raw) > _MAX_PROFILE_BYTES:
            raise _bad()
        revision_sha256 = hashlib.sha256(raw).hexdigest()
        if revision_sha256 != approved_hash:
            raise ProfileError("PROFILE_CHANGED", "Профиль изменился после разрешения владельцем.")
        return _validate(raw, requested_id=profile_id, revision_sha256=revision_sha256)
