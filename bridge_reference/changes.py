"""Checked UTF-8 file replacement for a disposable worktree; no folder sync."""
from __future__ import annotations
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any
from .protocol import Rejected, resolve_regular, validate_schema

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def apply_changeset(root: Path, change: dict[str, Any], *, expected_head: str,
                    max_bytes: int = 2 * 1024 * 1024) -> list[dict[str, Any]]:
    validate_schema(change, "changeset")
    if change["base_head"] != expected_head:
        raise Rejected("STALE_BASE", "Changeset base differs from the captured Git HEAD")
    plan, seen, total = [], set(), 0
    for item in change["files"]:
        name = item["path"]
        if name in seen:
            raise Rejected("DUPLICATE_PATH", "One changeset may touch a path only once")
        seen.add(name)
        target = resolve_regular(root, name)
        original = target.read_bytes() if target.exists() else None
        actual = digest(original) if original is not None else None
        if actual != item["expected_sha256"]:
            raise Rejected("CONTENT_CONFLICT", f"Current bytes differ for {name}")
        if item["op"] == "delete" and original is None:
            raise Rejected("MISSING_DELETE_TARGET", f"Cannot delete missing file {name}")
        content = item["content_utf8"].encode("utf-8") if item["op"] == "upsert" else None
        total += len(content) if content is not None else 0
        if total > max_bytes:
            raise Rejected("CHANGESET_TOO_LARGE", "Use explicit blob transport for larger changes")
        plan.append((item, target, original, content))
    # Validation precedes all writes. Multi-file FS replacement is not transactional;
    # rollback is best-effort, and live implementation MUST use a disposable staging snapshot.
    changed = []
    try:
        for item, target, original, content in plan:
            target.parent.mkdir(parents=True, exist_ok=True)
            if content is None:
                target.unlink()
            else:
                fd, temp = tempfile.mkstemp(prefix=".cdr-", dir=target.parent)
                try:
                    with os.fdopen(fd, "wb") as output:
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temp, target)
                finally:
                    if os.path.exists(temp):
                        os.unlink(temp)
            changed.append((target, original))
    except OSError:
        for target, original in reversed(changed):
            if original is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(original)
        raise
    return [{"path": i["path"], "op": i["op"], "sha256": digest(content) if content is not None else None}
            for i, _, _, content in plan]
