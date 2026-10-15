"""Strict JSON primitives for intent contracts; independent of v1 parsing rules."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, cast
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

from runner_app.domain.commands import DomainError
from runner_app.resources import load_schema

MAX_CONTRACT_BYTES = 262_144


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DomainError("DUPLICATE_JSON_KEY", "JSON contains a duplicate object key.")
        result[key] = value
    return result


def _number(_value: str) -> Any:
    raise DomainError("INVALID_JSON", "Intent contracts do not permit floating point numbers.")


def validate_json_values(value: Any, depth: int = 0) -> None:
    """Reject non-JSON Python values, floats and invalid Unicode before freezing."""
    if depth > 64:
        raise DomainError("INVALID_JSON", "JSON nesting exceeds the contract limit.")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise DomainError("INVALID_JSON", "JSON object keys must be strings.")
            validate_json_values(key, depth + 1)
            validate_json_values(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            validate_json_values(item, depth + 1)
    elif isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise DomainError("INVALID_UTF8", "JSON must contain valid Unicode.") from None
    elif value is not None and type(value) not in (bool, int):
        raise DomainError("INVALID_JSON", "Intent contracts permit only JSON integers.")


def parse_contract_json(raw: bytes, *, max_bytes: int = MAX_CONTRACT_BYTES) -> dict[str, Any]:
    """Parse untrusted intent bytes without permissive JSON extensions or coercion."""
    if not isinstance(raw, bytes):
        raise DomainError("INVALID_JSON", "Contract input must be UTF-8 bytes.")
    if len(raw) > max_bytes:
        raise DomainError("PAYLOAD_TOO_LARGE", "Contract exceeds the allowed byte budget.")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_pairs,
                           parse_float=_number, parse_constant=_number)
    except UnicodeDecodeError:
        raise DomainError("INVALID_UTF8", "Contract must be encoded as UTF-8.") from None
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, DomainError):
            raise
        raise DomainError("INVALID_JSON", "Contract contains invalid JSON.") from None
    if not isinstance(value, dict):
        raise DomainError("INVALID_JSON", "Contract must be a JSON object.")
    validate_json_values(value)
    return cast(dict[str, Any], value)


def canonical_bytes(value: dict[str, Any]) -> bytes:
    """Sorted compact UTF-8, preserving literal script text and filenames exactly."""
    validate_json_values(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


@lru_cache(maxsize=5)
def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(name), format_checker=FormatChecker())


def validate_schema(value: dict[str, Any], name: str) -> None:
    validate_json_values(value)
    if not _validator(name).is_valid(value):
        raise DomainError("INVALID_SCHEMA", "Contract does not match its versioned schema.")


def require_uuid(value: Any) -> str:
    if isinstance(value, str):
        try:
            if str(UUID(value)) == value:
                return value
        except ValueError:
            pass
    raise DomainError("INVALID_SCHEMA", "UUID must use canonical lowercase hyphenated spelling.")


def validate_relative_path(value: str, *, allow_root: bool = False) -> None:
    """Require a literal relative POSIX path, never a host or Git internal path."""
    if value == "." and allow_root:
        return
    if (not value or len(value) > 512 or value.startswith("/")
            or any(char in value for char in "\\:")
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise DomainError("UNSAFE_PATH", "Path must be a canonical relative POSIX path.")
    if any(part.casefold() == ".git" for part in value.split("/")):
        raise DomainError("PROTECTED_PATH", "Git internal paths are not task paths.")
