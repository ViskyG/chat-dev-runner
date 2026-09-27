"""Bounded pipe capture for a process already started by a trusted executor.

This module does not start or confine project code.  The executor supplies the
process and termination callbacks for its own container/scope.  Selector order
is an observation order, never a claim about exact stdout/stderr interleaving.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import subprocess
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, BinaryIO, cast


@dataclass(frozen=True)
class StreamCapture:
    name: str
    raw_path: Path
    view_path: Path | None
    observed_bytes: int
    retained_bytes: int
    raw_sha256: str
    view_sha256: str | None
    view_bytes: int
    complete: bool
    truncated_reason: str | None


@dataclass(frozen=True)
class CaptureResult:
    started_at: str
    ended_at: str
    duration_seconds: float
    exit_code: int | None
    termination_reason: str | None
    capture_fault: str | None
    streams: tuple[StreamCapture, StreamCapture]
    events_path: Path | None
    events_sha256: str | None
    metadata_path: Path | None
    metadata_sha256: str | None
    observed_events: int
    retained_events: int

    @property
    def outcome_hint(self) -> str:
        if self.termination_reason in ("CANCELLED", "TIMED_OUT"):
            return self.termination_reason
        if self.termination_reason is not None:
            return "UNKNOWN"
        if self.exit_code is None:
            return "UNKNOWN"
        return "PASSED" if self.exit_code == 0 else "FAILED"

    @property
    def outcome(self) -> str:
        """Execution-axis hint; a capture fault is a separate infrastructure finding."""
        return self.outcome_hint

    def files(self) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for stream in self.streams:
            result[f"{stream.name}-raw"] = stream.raw_path
            if stream.view_path is not None:
                result[f"{stream.name}-view"] = stream.view_path
        if self.events_path is not None:
            result["capture-events"] = self.events_path
        if self.metadata_path is not None:
            result["capture-metadata"] = self.metadata_path
        return result

    def outputs(self, *, relative_prefix: str) -> tuple[dict[str, Any], ...]:
        """Return schema-v1 descriptors for files kept under an evidence root."""
        if not relative_prefix or relative_prefix.startswith("/") or ".." in Path(relative_prefix).parts:
            raise ValueError("relative_prefix must be a canonical relative directory")
        outputs: list[dict[str, Any]] = []
        for stream in self.streams:
            outputs.append(_output(
                f"{stream.name}-raw", "raw-bytes-v1", "application/octet-stream",
                f"{relative_prefix}/{stream.name}.bin", stream.retained_bytes,
                stream.raw_sha256, stream.complete, stream.truncated_reason,
            ))
            if stream.view_path is not None and stream.view_sha256 is not None:
                outputs.append(_output(
                    f"{stream.name}-view", "utf8-safe-view-v1", "text/plain; charset=utf-8",
                    f"{relative_prefix}/{stream.name}.txt", stream.view_bytes,
                    stream.view_sha256, stream.complete, stream.truncated_reason,
                ))
        if self.events_path is not None and self.events_sha256 is not None:
            events_complete = self.observed_events == self.retained_events
            outputs.append(_output(
                "capture-events", "stream-observation/v1", "application/x-ndjson",
                f"{relative_prefix}/events.jsonl", self.events_path.stat().st_size,
                self.events_sha256, events_complete,
                None if events_complete else "event_limit",
                kind="runner_log",
            ))
        if self.metadata_path is not None and self.metadata_sha256 is not None:
            outputs.append(_output(
                "capture-metadata", "capture-metadata/v1", "application/json",
                f"{relative_prefix}/capture.json", self.metadata_path.stat().st_size,
                self.metadata_sha256, True, None, kind="runner_log",
            ))
        return tuple(outputs)


def _output(output_id: str, format_name: str, media_type: str, relative_path: str,
            size: int, digest: str, complete: bool, reason: str | None,
            *, kind: str = "console") -> dict[str, Any]:
    return {
        "output_id": output_id, "kind": kind, "format": format_name,
        "media_type": media_type, "relative_path": relative_path,
        "size_bytes": size, "sha256": digest, "complete": complete,
        "truncated_reason": reason, "publication": "local_only",
    }


def safe_text(raw: bytes) -> str:
    """Decode with replacement and visibly escape controls, including ANSI/C1."""
    decoded = raw.decode("utf-8", errors="replace")
    result: list[str] = []
    for character in decoded:
        if character in ("\n", "\t"):
            result.append(character)
        elif unicodedata.category(character) in ("Cc", "Cf"):
            code = ord(character)
            result.append(f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}")
        else:
            result.append(character)
    return "".join(result)


def _open_new(path: Path) -> BinaryIO:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "wb", buffering=0)


def _write_all(destination: BinaryIO, data: bytes) -> int:
    written = 0
    while written < len(data):
        count = destination.write(data[written:])
        if count is None or count <= 0:
            raise OSError("Capture destination made no progress")
        written += count
    return written


def capture_process(
    process: subprocess.Popen[bytes],
    output_dir: Path,
    *,
    timeout_seconds: float,
    max_stream_bytes: int,
    max_total_bytes: int,
    terminate: Callable[[], None],
    kill: Callable[[], None],
    cancel: Event | None = None,
    observe: Callable[[], str | None] | None = None,
    termination_grace_seconds: float = 0.5,
    drain_grace_seconds: float = 2.0,
    max_event_records: int = 10_000,
) -> CaptureResult:
    """Drain both binary pipes while bounding retained bytes and termination time.

    The caller owns final container/scope cleanup even when this function reports
    a capture fault.  A storage write failure stops retention, not pipe draining.
    """
    if (process.stdout is None or process.stderr is None or timeout_seconds <= 0
            or max_stream_bytes < 1 or max_total_bytes < 1 or max_event_records < 1
            or termination_grace_seconds <= 0 or drain_grace_seconds <= 0):
        raise ValueError("Binary pipes and positive capture limits are required")
    output_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(output_dir, 0o700)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    selector = selectors.DefaultSelector()
    state: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    observed_events = 0
    total_retained = 0
    termination_reason: str | None = None
    capture_fault: str | None = None
    termination_at: float | None = None
    killed_at: float | None = None
    exited_at: float | None = None
    try:
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            destination = _open_new(output_dir / f"{name}.bin")
            state[name] = {
                "file": destination, "observed": 0, "retained": 0,
                "hash": hashlib.sha256(), "reason": None,
            }
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        while selector.get_map() or process.poll() is None:
            now = time.monotonic()
            if process.poll() is not None and exited_at is None:
                exited_at = now
            if process.poll() is None:
                if termination_reason is None and cancel is not None and cancel.is_set():
                    termination_reason = "CANCELLED"
                elif termination_reason is None and now - started >= timeout_seconds:
                    termination_reason = "TIMED_OUT"
            if termination_reason is None and observe is not None:
                try:
                    resource_code = observe()
                except Exception as error:  # noqa: BLE001 - monitor failure is infrastructure fault
                    resource_code = None
                    termination_reason = "RESOURCE_LIMIT"
                    capture_fault = f"RESOURCE_MONITOR_FAILED:{type(error).__name__}"
                if resource_code is not None:
                    if not isinstance(resource_code, str) or not re.fullmatch(
                        r"[A-Z][A-Z0-9_]{0,63}", resource_code
                    ):
                        resource_code = "INVALID_MONITOR_CODE"
                    termination_reason = "RESOURCE_LIMIT"
                    capture_fault = f"RESOURCE_LIMIT:{resource_code}"
            if termination_reason is not None and termination_at is None:
                termination_at = now
                try:
                    terminate()
                except Exception as error:  # noqa: BLE001 - executor callback failure is evidence
                    capture_fault = f"TERMINATE_FAILED:{type(error).__name__}"
            if (termination_at is not None and killed_at is None
                    and now - termination_at >= termination_grace_seconds):
                killed_at = now
                try:
                    kill()
                except Exception as error:  # noqa: BLE001 - executor callback failure is evidence
                    capture_fault = f"KILL_FAILED:{type(error).__name__}"
            if (killed_at is not None and now - killed_at >= drain_grace_seconds
                    and process.poll() is None):
                capture_fault = capture_fault or "PROCESS_TERMINATION_UNCONFIRMED"
                for key in list(selector.get_map().values()):
                    state[key.data]["reason"] = state[key.data]["reason"] or "drain_timeout"
                    selector.unregister(key.fileobj)
                    cast(BinaryIO, key.fileobj).close()
                break
            if (exited_at is not None and selector.get_map()
                    and now - exited_at >= drain_grace_seconds):
                capture_fault = capture_fault or "DRAIN_TIMEOUT"
                if killed_at is None:
                    try:
                        kill()
                    except Exception as error:  # noqa: BLE001 - executor callback failure is evidence
                        capture_fault = f"KILL_FAILED:{type(error).__name__}"
                    killed_at = now
                for key in list(selector.get_map().values()):
                    state[key.data]["reason"] = state[key.data]["reason"] or "drain_timeout"
                    selector.unregister(key.fileobj)
                    cast(BinaryIO, key.fileobj).close()
                break
            for key, _ in selector.select(0.05):
                try:
                    chunk = os.read(cast(BinaryIO, key.fileobj).fileno(), 65_536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    cast(BinaryIO, key.fileobj).close()
                    continue
                entry = state[key.data]
                entry["observed"] += len(chunk)
                allowed = min(len(chunk), max_stream_bytes - entry["retained"],
                              max_total_bytes - total_retained)
                retained = 0
                if allowed > 0 and capture_fault is None:
                    try:
                        while retained < allowed:
                            piece = chunk[retained:allowed]
                            count = entry["file"].write(piece)
                            if count is None or count <= 0:
                                raise OSError("Capture destination made no progress")
                            entry["hash"].update(piece[:count])
                            entry["retained"] += count
                            total_retained += count
                            retained += count
                    except OSError as error:
                        capture_fault = f"STORAGE_WRITE_FAILED:{type(error).__name__}"
                        entry["reason"] = "storage_error"
                if len(chunk) > retained and entry["reason"] is None:
                    entry["reason"] = (
                        "storage_error" if capture_fault is not None else
                        "stream_limit" if entry["retained"] >= max_stream_bytes else "run_limit"
                    )
                observed_events += 1
                if len(events) < max_event_records:
                    events.append({
                        "sequence": observed_events, "stream": key.data,
                        "monotonic_seconds": round(time.monotonic() - started, 9),
                        "observed_bytes": len(chunk), "retained_bytes": retained,
                    })
        try:
            exit_code: int | None = process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                kill()
                exit_code = process.wait(timeout=1)
            except Exception:  # noqa: BLE001 - failed cleanup is an infrastructure finding
                exit_code = None
                capture_fault = "PROCESS_NOT_REAPED"
    finally:
        for key in list(selector.get_map().values()):
            selector.unregister(key.fileobj)
            cast(BinaryIO, key.fileobj).close()
        selector.close()
        for entry in state.values():
            try:
                os.fsync(entry["file"].fileno())
            except OSError as error:
                capture_fault = capture_fault or f"RAW_FSYNC_FAILED:{type(error).__name__}"
                entry["reason"] = "storage_error"
            finally:
                entry["file"].close()
    ended_at = datetime.now(timezone.utc).isoformat()
    duration = round(time.monotonic() - started, 9)
    streams: list[StreamCapture] = []
    for name in ("stdout", "stderr"):
        entry = state[name]
        raw_path = output_dir / f"{name}.bin"
        view_path: Path | None = None
        view_sha256: str | None = None
        view_bytes = 0
        try:
            safe = safe_text(raw_path.read_bytes()).encode("utf-8")
            view_path = output_dir / f"{name}.txt"
            with _open_new(view_path) as destination:
                _write_all(destination, safe)
                os.fsync(destination.fileno())
            view_bytes = len(safe)
            view_sha256 = hashlib.sha256(safe).hexdigest()
        except OSError as error:
            capture_fault = capture_fault or f"VIEW_WRITE_FAILED:{type(error).__name__}"
            if view_path is not None:
                view_path.unlink(missing_ok=True)
            view_path = None
        reason = entry["reason"]
        if capture_fault is not None and entry["observed"] > entry["retained"]:
            reason = reason or "storage_error"
        streams.append(StreamCapture(
            name=name, raw_path=raw_path, view_path=view_path,
            observed_bytes=entry["observed"], retained_bytes=entry["retained"],
            raw_sha256=entry["hash"].hexdigest(), view_sha256=view_sha256,
            view_bytes=view_bytes, complete=reason is None, truncated_reason=reason,
        ))
    events_path: Path | None = None
    events_digest: str | None = None
    try:
        events_path = output_dir / "events.jsonl"
        encoded = b"".join(json.dumps(item, separators=(",", ":")).encode("utf-8") + b"\n"
                           for item in events)
        with _open_new(events_path) as destination:
            _write_all(destination, encoded)
            os.fsync(destination.fileno())
        events_digest = hashlib.sha256(encoded).hexdigest()
    except OSError as error:
        capture_fault = capture_fault or f"EVENT_WRITE_FAILED:{type(error).__name__}"
        if events_path is not None:
            events_path.unlink(missing_ok=True)
        events_path = None
    metadata_path: Path | None = None
    metadata_digest: str | None = None
    metadata = {
        "schema_version": "1.0", "started_at": started_at, "ended_at": ended_at,
        "duration_seconds": duration, "exit_code": exit_code,
        "termination_reason": termination_reason, "capture_fault": capture_fault,
        "stream_order": "selector-observation-only; no exact cross-stream interleaving claim",
        "streams": {
            stream.name: {
                "observed_bytes": stream.observed_bytes,
                "retained_bytes": stream.retained_bytes,
                "sha256": stream.raw_sha256,
                "complete": stream.complete,
                "truncated_reason": stream.truncated_reason,
            }
            for stream in streams
        },
        "events": {"observed": observed_events, "retained": len(events)},
    }
    try:
        metadata_path = output_dir / "capture.json"
        encoded_metadata = (json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        with _open_new(metadata_path) as destination:
            _write_all(destination, encoded_metadata)
            os.fsync(destination.fileno())
        metadata_digest = hashlib.sha256(encoded_metadata).hexdigest()
    except OSError as error:
        capture_fault = capture_fault or f"METADATA_WRITE_FAILED:{type(error).__name__}"
        if metadata_path is not None:
            metadata_path.unlink(missing_ok=True)
        metadata_path = None
    return CaptureResult(
        started_at=started_at, ended_at=ended_at, duration_seconds=duration,
        exit_code=exit_code, termination_reason=termination_reason, capture_fault=capture_fault,
        streams=(streams[0], streams[1]), events_path=events_path,
        events_sha256=events_digest, metadata_path=metadata_path,
        metadata_sha256=metadata_digest, observed_events=observed_events,
        retained_events=len(events),
    )
