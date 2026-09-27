"""Strict protocol validation and local-only, deterministic path rules."""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
import jsonschema

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"

class Rejected(ValueError):
    """A terminal refusal, not a fault in the runner service."""
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)

def canonical_digest(value: dict[str, Any]) -> str:
    """Protocol v1 has no float fields in commands; keep Unicode unchanged."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()

def validate_schema(value: Any, schema_name: str) -> None:
    try:
        schema = json.loads((SCHEMAS / f"{schema_name}.schema.json").read_text())
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(value)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(x) for x in exc.absolute_path) or "root"
        raise Rejected("INVALID_SCHEMA", f"{path}: {exc.message}") from exc

def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise Rejected("INVALID_TIME", "Timestamps require an explicit timezone")
    return result.astimezone(timezone.utc)

def validate_command(command: dict[str, Any], *, runner_id: str, epoch: str,
                     now: datetime | None = None, max_ttl: int = 900) -> str:
    validate_schema(command, "command")
    if command["dispatch"] != "READY":
        raise Rejected("NOT_READY", "Drafts are never executable")
    if command["runner_id"] != runner_id or command["epoch"] != epoch:
        raise Rejected("WRONG_RUNNER_EPOCH", "Command is not addressed to this armed session")
    now = now or datetime.now(timezone.utc)
    issued, expires = parse_time(command["issued_at"]), parse_time(command["expires_at"])
    ttl = (expires - issued).total_seconds()
    if not 0 < ttl <= max_ttl:
        raise Rejected("INVALID_TTL", "Command lifetime must be positive and within local policy")
    if issued > now and (issued - now).total_seconds() > 30:
        raise Rejected("FUTURE_COMMAND", "Clock skew exceeds 30 seconds")
    if expires <= now:
        raise Rejected("EXPIRED", "Issue a NEW request after reviewing current state")
    return canonical_digest(command)

def safe_relative_path(value: str) -> PurePosixPath:
    """Reject traversal, platform ambiguity, Git internals and credential paths."""
    if not value or len(value) > 500 or "\\" in value or ":" in value or "\x00" in value:
        raise Rejected("UNSAFE_PATH", "Ambiguous path")
    if any(ord(c) < 32 for c in value):
        raise Rejected("UNSAFE_PATH", "Control character in path")
    parts = value.split("/")
    if value.startswith("/") or any(x in {"", ".", ".."} for x in parts):
        raise Rejected("UNSAFE_PATH", "Path must be a canonical relative POSIX path")
    blocked = {".git", ".ssh", ".aws", ".gnupg", ".netrc", "secrets", ".runner-policy"}
    if any(p.lower() in blocked or p.lower() == ".env" or p.lower().startswith(".env.") for p in parts):
        raise Rejected("PROTECTED_PATH", "Protected path")
    if parts[:2] == [".github", "workflows"]:
        raise Rejected("POLICY_REQUIRED", "Workflow changes require a separate local policy capability")
    return PurePosixPath(value)

def resolve_regular(root: Path, value: str) -> Path:
    relative = safe_relative_path(value)
    root = root.resolve(strict=True)
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise Rejected("SYMLINK", "Symlinks cannot be used as changeset targets")
    # This reference check is not a replacement for openat/O_NOFOLLOW and a sandbox.
    if not path.resolve().is_relative_to(root):
        raise Rejected("UNSAFE_PATH", "Path escapes the workspace")
    if path.exists() and not path.is_file():
        raise Rejected("NOT_REGULAR_FILE", "Target is not a regular file")
    return path

def validate_branch(name: str) -> None:
    if (not name or len(name) > 120 or name.startswith(("-", "/")) or
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", name) or
        any(x in name for x in ("..", "//", "@{")) or
        name.endswith(("/", ".", ".lock")) or
        any(p.startswith(".") or p.endswith(".lock") for p in name.split("/"))):
        raise Rejected("INVALID_BRANCH", "Invalid Git branch name")
