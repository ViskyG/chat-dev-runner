"""Observe an enrolled tool's version through one finite, journaled native probe.

The caller owns per-run deduplication, preparation retries and the remaining time
budget. Missing local tool files defer probing; no install or fallback occurs.
Raw native capture stays authoritative when text cannot establish a version.
"""
from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Literal

from runner_app.storage.paths import UnsafePathError, open_private_file

from .native_v2 import NativeResult, execute_native
from .task_plan_v2 import ToolProvenance

_MAX_CAPTURE_BYTES = 16 * 1024
_MAX_VERSION_CHARS = 1024
_ANSI = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|\x1b[ -/]*[@-~]"
)


@dataclass(frozen=True)
class ToolObservation:
    id: str
    state: Literal["OBSERVED", "NOT_READY", "UNKNOWN"]
    version: str | None
    execution: NativeResult | None


def _read_capture(path: Path) -> bytes:
    descriptor = open_private_file(path)
    try:
        before = os.fstat(descriptor)
        data = os.read(descriptor, _MAX_CAPTURE_BYTES + 1)
        after = os.fstat(descriptor)
        def identity(info: os.stat_result) -> tuple[int, ...]:
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if (len(data) != before.st_size or len(data) > _MAX_CAPTURE_BYTES
                or identity(before) != identity(after)):
            raise ValueError("Capture changed or exceeded its bound")
        return data
    finally:
        os.close(descriptor)


def _first_line(raw: bytes) -> str | None:
    text = _ANSI.sub("", raw.decode("utf-8", "strict"))
    for line in text.splitlines():
        clean = "".join(c for c in line if not unicodedata.category(c).startswith("C")).strip()
        if clean:
            if len(clean) > _MAX_VERSION_CHARS:
                raise ValueError("Version line exceeds its bound")
            return clean
    return None


def observe_tool(tool: ToolProvenance, *, cwd: Path, env: Mapping[str, str],
                 capture_dir: Path, artifact_dir: Path, timeout_seconds: float,
                 cancel: Event,
                 record_intent: Callable[[dict[str, Any]], None]) -> ToolObservation:
    """Probe the exact declared argv once; a journal failure prevents all spawning.

    NOT_READY has no process and may be retried by the caller after preparation.
    UNKNOWN preserves the actual process result, including any uncertain cleanup.
    The timeout is forwarded unchanged, including fractional caller-owned budgets.
    """
    if any(Path(arg).is_absolute() and not Path(arg).is_file() for arg in tool.argv_prefix):
        return ToolObservation(tool.id, "NOT_READY", None, None)
    record_intent({
        "kind": "TOOL_VERSION", "tool_id": tool.id,
        "argv": list(tool.version_probe_argv), "cwd": str(cwd),
        "capture_dir": str(capture_dir), "timeout_seconds": timeout_seconds,
        "max_log_bytes": _MAX_CAPTURE_BYTES,
    })
    execution = execute_native(
        list(tool.version_probe_argv), cwd=cwd, env=env, output_dir=capture_dir,
        artifact_dir=artifact_dir,
        timeout_seconds=timeout_seconds, cancel=cancel,
        max_log_bytes=_MAX_CAPTURE_BYTES)
    unknown = ToolObservation(tool.id, "UNKNOWN", None, execution)
    if (execution.outcome != "PASSED" or execution.exit_code != 0
            or not execution.cleanup_verified or not execution.logs_persisted
            or execution.error_code is not None
            or execution.stdout_truncated or execution.stderr_truncated):
        return unknown
    try:
        stdout = _read_capture(execution.stdout_path)
        stderr = _read_capture(execution.stderr_path)
        version = _first_line(stdout) or _first_line(stderr)
    except (OSError, UnsafePathError, ValueError):
        return unknown
    return ToolObservation(tool.id, "OBSERVED", version, execution) if version else unknown
