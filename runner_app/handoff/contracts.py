"""Proposed cdr-handoff/1 payload: data, never a host-path or execution grant."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, fields
from typing import Any, cast
from uuid import UUID


class HandoffError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Limits:
    max_files: int = 16
    file_bytes: int = 64 * 1024 * 1024
    total_bytes: int = 128 * 1024 * 1024
    expanded_bytes: int = 512 * 1024 * 1024
    zip_entries: int = 4096
    zip_ratio: int = 200
    max_depth: int = 16
    max_path_bytes: int = 512
    max_manifest_bytes: int = 64 * 1024
    zip_central_directory_bytes: int = 4 * 1024 * 1024
    minimum_free_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            minimum = 0 if field.name == 'minimum_free_bytes' else 1
            if type(value) is not int or value < minimum:
                raise HandoffError('INVALID_LIMITS')


DEFAULT_LIMITS = Limits()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise HandoffError('DUPLICATE_JSON_KEY')
        out[k] = v
    return out


def parse(raw: bytes, limits: Limits = DEFAULT_LIMITS) -> dict[str, Any]:
    if len(raw) > limits.max_manifest_bytes:
        raise HandoffError('MANIFEST_TOO_LARGE')
    def bad_constant(_value: str) -> Any:
        raise HandoffError('INVALID_JSON')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=bad_constant)
    except HandoffError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise HandoffError('INVALID_JSON') from None
    return validate(value, limits)


def _keys(value: Any, keys: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise HandoffError('INVALID_MANIFEST')


def slug(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', value):
        raise HandoffError('INVALID_ID')
    return value


def uuid(value: Any) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise HandoffError('INVALID_UUID') from None
    return value


def relative(value: Any, limits: Limits = DEFAULT_LIMITS) -> str:
    """Reject rather than sanitize; the accepted name is the installed name."""
    if (not isinstance(value, str) or not value or value.startswith('/')
            or '\\' in value or unicodedata.normalize('NFC', value) != value):
        raise HandoffError('UNSAFE_PATH')
    try:
        if len(value.encode('utf-8')) > limits.max_path_bytes:
            raise HandoffError('UNSAFE_PATH')
    except UnicodeError:
        raise HandoffError('UNSAFE_PATH') from None
    parts = value.split('/')
    if len(parts) > limits.max_depth:
        raise HandoffError('UNSAFE_PATH')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)),
                *(f'LPT{i}' for i in range(10))}
    for part in parts:
        if (part in {'', '.', '..'} or part.casefold() == '.git'
                or part.endswith(('.', ' ')) or ':' in part
                or any(unicodedata.category(c).startswith('C') for c in part)
                or any(c in part for c in '<>"|?*')
                or part.split('.')[0].upper() in reserved):
            raise HandoffError('UNSAFE_PATH')
    return value


def validate(value: Any, limits: Limits = DEFAULT_LIMITS) -> dict[str, Any]:
    _keys(value, {'protocol', 'handoff_id', 'project_id', 'target_id', 'purpose',
                  'title', 'base_commit', 'artifacts', 'entrypoint'})
    if value['protocol'] != 'cdr-handoff/1':
        raise HandoffError('INVALID_PROTOCOL')
    uuid(value['handoff_id'])
    slug(value['project_id']); slug(value['target_id'])
    if not isinstance(value['purpose'], str) or value['purpose'] not in {'design', 'roadmap', 'implementation', 'assets', 'reference'}:
        raise HandoffError('INVALID_PURPOSE')
    title = value['title']
    if (not isinstance(title, str) or not 1 <= len(title) <= 160
            or any(unicodedata.category(c).startswith('C') for c in title)):
        raise HandoffError('INVALID_TITLE')
    base = value['base_commit']
    if base is not None and (not isinstance(base, str) or
                            not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', base)):
        raise HandoffError('INVALID_BASE_COMMIT')
    artifacts = value['artifacts']
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= limits.max_files:
        raise HandoffError('INVALID_ARTIFACTS')
    ids: dict[str, dict[str, Any]] = {}
    total = 0
    for artifact in artifacts:
        _keys(artifact, {'artifact_id', 'drive_file_id', 'drive_version', 'filename',
                         'size_bytes', 'sha256', 'media_type', 'unpack'})
        key = slug(artifact['artifact_id'])
        if key in ids:
            raise HandoffError('DUPLICATE_ARTIFACT')
        ids[key] = artifact
        for field in ('drive_file_id', 'drive_version'):
            v = artifact[field]
            pat = r'[A-Za-z0-9_-]{1,200}' if field == 'drive_file_id' else r'[0-9]{1,30}'
            if not isinstance(v, str) or not re.fullmatch(pat, v):
                raise HandoffError('INVALID_DRIVE_IDENTITY')
        if '/' in relative(artifact['filename'], limits):
            raise HandoffError('UNSAFE_FILENAME')
        n = artifact['size_bytes']
        if type(n) is not int or not 0 <= n <= limits.file_bytes:
            raise HandoffError('ARTIFACT_TOO_LARGE')
        total += n
        if not isinstance(artifact['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', artifact['sha256']):
            raise HandoffError('INVALID_DIGEST')
        mime = artifact['media_type']
        if (not isinstance(mime, str) or not re.fullmatch(r'[a-z0-9.+-]+/[a-z0-9.+-]+', mime)
                or mime.startswith('application/vnd.google-apps.')):
            raise HandoffError('BLOB_REQUIRED')
        if not isinstance(artifact['unpack'], str) or artifact['unpack'] not in {'none', 'zip'}:
            raise HandoffError('UNPACK_UNSUPPORTED')
        if artifact['unpack'] == 'zip' and mime != 'application/zip':
            raise HandoffError('ZIP_MEDIA_REQUIRED')
    if total > limits.total_bytes:
        raise HandoffError('TRANSFER_TOO_LARGE')
    entry = value['entrypoint']
    if entry is not None:
        _keys(entry, {'artifact_id', 'path'})
        slug(entry['artifact_id'])
        if entry['artifact_id'] not in ids:
            raise HandoffError('ENTRYPOINT_UNKNOWN')
        artifact = ids[entry['artifact_id']]
        if entry['path'] is not None:
            relative(entry['path'], limits)
            if artifact['unpack'] != 'zip':
                raise HandoffError('ENTRYPOINT_NOT_EXTRACTED')
    try:
        if len(canonical(value)) > limits.max_manifest_bytes:
            raise HandoffError('MANIFEST_TOO_LARGE')
    except (UnicodeError, ValueError, RecursionError):
        raise HandoffError('INVALID_MANIFEST') from None
    # Detached JSON representation, no mutation by the caller during transfer.
    return cast(dict[str, Any], json.loads(canonical(value)))
