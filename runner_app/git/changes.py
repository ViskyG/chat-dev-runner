"""Bounded, policy-checked wire changesets for private Git snapshot staging.

This module intentionally does not write an editable workspace. A validated plan is
consumed by snapshots.py against an immutable base commit and a private Git index.
"""
from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Any

import jsonschema

MAX_CHANGESET_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 262_144
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_HEAD = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_PROTECTED_COMPONENTS = frozenset(
    {".git", ".github", ".ssh", ".aws", ".gnupg", ".kube", ".docker",
     ".netrc", ".npmrc", ".pypirc", ".lfsconfig", ".gitmodules",
     ".gitattributes", ".runner-policy", "secrets", "credentials"}
)
_SECRET_NAMES = frozenset(
    {"id_rsa", "id_ed25519", "authorized_keys", "known_hosts", "token", "tokens",
     "credentials.json", "service-account.json", "service_account.json"}
)


class ChangeError(ValueError):
    """Terminal changeset refusal with a stable, non-secret code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class FileChange:
    path: str
    op: str
    expected_sha256: str | None
    content: bytes | None


@dataclass(frozen=True)
class ChangePlan:
    change_id: str
    project_id: str
    workspace_id: str
    base_head: str
    files: tuple[FileChange, ...]
    content_bytes: int


def safe_source_path(value: str) -> PurePosixPath:
    """Require an unambiguous relative source path and enforce protected-file policy."""
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ChangeError("UNSAFE_PATH", "Source path is empty or too long")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise ChangeError("UNSAFE_PATH", "Source path must be valid UTF-8") from exc
    if value.startswith("/") or "\\" in value or ":" in value:
        raise ChangeError("UNSAFE_PATH", "Source path must be a relative POSIX path")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ChangeError("UNSAFE_PATH", "Control characters are forbidden in source paths")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ChangeError("UNSAFE_PATH", "Source path is not canonical")
    if any(value.casefold() == root or value.casefold().startswith(root + "/")
           for root in ("handoffs", ".runner/handoffs")):
        raise ChangeError("PROTECTED_PATH", "Received handoffs are separate from project source")
    for part in parts:
        lower = part.casefold()
        if (lower in _PROTECTED_COMPONENTS or lower in _SECRET_NAMES
                or lower == ".env" or lower.startswith(".env.")
                or lower.endswith((".pem", ".p12", ".pfx", ".key", ".tfstate"))):
            raise ChangeError("PROTECTED_PATH", "Source path requires separate local authority")
    return PurePosixPath(value)


def _schema() -> dict[str, Any]:
    raw = files("schemas").joinpath("changeset.schema.json").read_text(encoding="utf-8")
    value: dict[str, Any] = json.loads(raw)
    return value


def validate_changeset(
    change: Mapping[str, Any], *, project_id: str, workspace_id: str,
    expected_head: str, max_bytes: int = MAX_CHANGESET_BYTES,
) -> ChangePlan:
    """Validate the complete v1 envelope before any Git index/object mutation."""
    try:
        jsonschema.Draft202012Validator(
            _schema(), format_checker=jsonschema.FormatChecker()
        ).validate(change)
    except jsonschema.ValidationError as exc:
        raise ChangeError("INVALID_SCHEMA", "Changeset does not match schema v1") from exc
    if not _SLUG.fullmatch(project_id) or not _SLUG.fullmatch(workspace_id):
        raise ChangeError("INVALID_IDENTITY", "Trusted project/workspace identity is invalid")
    if not _HEAD.fullmatch(expected_head):
        raise ChangeError("INVALID_HEAD", "Expected head must be a full commit ID")
    if change["project_id"] != project_id or change["workspace_id"] != workspace_id:
        raise ChangeError("WRONG_WORKSPACE", "Changeset addresses another project or workspace")
    if change["base_head"] != expected_head:
        raise ChangeError("STALE_BASE", "Changeset base differs from expected workspace HEAD")
    try:
        change_id = str(uuid.UUID(change["change_id"]))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ChangeError("INVALID_SCHEMA", "Change ID must be a UUID") from exc
    if change_id != change["change_id"]:
        raise ChangeError("INVALID_SCHEMA", "Change ID must be a canonical UUID")
    if max_bytes <= 0 or max_bytes > MAX_CHANGESET_BYTES:
        raise ChangeError("INVALID_LIMIT", "Changeset budget exceeds local policy")
    seen: set[str] = set()
    plan: list[FileChange] = []
    total = 0
    for item in change["files"]:
        path = str(safe_source_path(item["path"]))
        if path in seen:
            raise ChangeError("DUPLICATE_PATH", "One changeset may touch each path once")
        seen.add(path)
        content: bytes | None = None
        if item["op"] == "upsert":
            try:
                content = item["content_utf8"].encode("utf-8", "strict")
            except UnicodeEncodeError as exc:
                raise ChangeError("INVALID_UTF8", "Content is not valid UTF-8") from exc
            if len(content) > MAX_FILE_BYTES:
                raise ChangeError("FILE_TOO_LARGE", "One changed file exceeds the byte budget")
            total += len(content)
        if total > max_bytes:
            raise ChangeError("CHANGESET_TOO_LARGE", "Changeset exceeds the byte budget")
        expected = item["expected_sha256"]
        if expected is not None and not _HEX.fullmatch(expected):
            raise ChangeError("INVALID_SCHEMA", "Expected file hash is invalid")
        plan.append(FileChange(path=path, op=item["op"],
                               expected_sha256=expected, content=content))
    for path in seen:
        for parent in PurePosixPath(path).parents:
            if str(parent) in seen:
                raise ChangeError("PATH_COLLISION", "Changeset has file/directory path collision")
    return ChangePlan(change_id=change_id, project_id=project_id,
                      workspace_id=workspace_id, base_head=expected_head,
                      files=tuple(plan), content_bytes=total)
