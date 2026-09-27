"""Best-effort local export redaction; never a credential-safety guarantee."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runner_app.outputs.capture import safe_text
from runner_app.outputs.collection import OutputError
from runner_app.storage.paths import ensure_private_directory

_NOTICE = "Sanitized export (best effort; inspect before sharing).\n\n"
_PATTERNS = (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}"),
    re.compile(r"(?i)\b(password|token|api[_-]?key|secret)\s*[:=]\s*['\"]?[^\s'\"]+"),
    re.compile(r"(?i)\b(https?://)[^\s/@:]+:[^\s/@]+@"),
)


@dataclass(frozen=True)
class RedactedExport:
    output: dict[str, Any]
    file: Path
    raw_sha256: str
    export_sha256: str
    replacements: int
    quarantined: bool
    caveat: str = "Best effort only; human/privacy review is still required before upload"


def redact_text_export(
    raw_output: dict[str, Any],
    raw_file: Path,
    export_root: Path,
    *,
    allowlisted: bool,
    reviewed_suspicious: bool = False,
    max_bytes: int = 2 * 1024 * 1024,
) -> RedactedExport:
    """Create a distinct private text variant for an explicitly allowlisted output.

    This function performs no network write. Binary/active formats cannot be
    converted by an unreviewed text replacement pass.
    """
    if not allowlisted:
        raise OutputError("EXPORT_NOT_ALLOWLISTED", "Profile has not allowed this output for export")
    if max_bytes < 1 or raw_file.stat().st_size > max_bytes:
        raise OutputError("EXPORT_BYTE_LIMIT", "Output exceeds export redaction limit")
    media_type = raw_output["media_type"].split(";", 1)[0].lower()
    if media_type not in (
        "text/plain", "text/markdown", "text/csv", "application/json",
        "application/x-ndjson", "application/xml", "text/xml",
    ):
        raise OutputError("EXPORT_FORMAT_UNSUPPORTED", "Output cannot be safely text-redacted")
    if not raw_output["complete"]:
        raise OutputError("EXPORT_INCOMPLETE", "Incomplete output cannot be exported automatically")
    raw = raw_file.read_bytes()
    if hashlib.sha256(raw).hexdigest() != raw_output["sha256"]:
        raise OutputError("EXPORT_SOURCE_CHANGED", "Raw output no longer matches frozen hash")
    text = safe_text(raw)
    replacements = 0
    for pattern in _PATTERNS:
        text, count = pattern.subn("[REDACTED]", text)
        replacements += count
    encoded = (_NOTICE + text).encode("utf-8")
    ensure_private_directory(export_root)
    base_id = str(raw_output["output_id"])
    output_id = f"{base_id[:41]}-export"
    destination = export_root / f"{output_id}.txt"
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600)
    try:
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(encoded):
            count = os.write(fd, encoded[offset:])
            if count <= 0:
                raise OSError("Export storage made no progress")
            offset += count
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        destination.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)
    digest = hashlib.sha256(encoded).hexdigest()
    return RedactedExport(
        output={
            "output_id": output_id, "kind": "report", "format": "redacted-text/v1",
            "media_type": "text/plain; charset=utf-8",
            "relative_path": f"{export_root.name}/{destination.name}",
            "size_bytes": len(encoded), "sha256": digest, "complete": True,
            "truncated_reason": None,
            "publication": (
                "local_only" if replacements and not reviewed_suspicious else "redacted_upload"
            ),
        },
        file=destination, raw_sha256=raw_output["sha256"],
        export_sha256=digest, replacements=replacements,
        quarantined=bool(replacements and not reviewed_suspicious),
    )
