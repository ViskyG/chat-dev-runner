"""Bounded capture for TRUSTED toy processes only. Not an execution sandbox."""
from __future__ import annotations
import hashlib
import json
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path
from threading import Event
from typing import Any

def capture(argv: list[str], cwd: Path, output_dir: Path, *, timeout: float = 10,
            max_stream_bytes: int = 1024 * 1024, cancel: Event | None = None) -> dict[str, Any]:
    if not argv or timeout <= 0 or max_stream_bytes < 1:
        raise ValueError("Nonempty argv and positive limits required")
    output_dir.mkdir(parents=True, exist_ok=False)
    home = output_dir / "empty-home"
    home.mkdir()
    # No cloud credentials inherited. This is NOT containment of malicious code.
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
           "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    start = time.monotonic()
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, shell=False)
    selector = selectors.DefaultSelector()
    streams = {}
    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        assert stream is not None
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
        streams[name] = {"file": (output_dir / f"{name}.bin").open("wb"), "seen": 0,
                         "saved": 0, "truncated": False}
    reason, terminate_at, hard_killed = None, None, False
    try:
        while selector.get_map() or process.poll() is None:
            elapsed = time.monotonic() - start
            if reason is None and ((cancel and cancel.is_set()) or elapsed > timeout):
                reason = "CANCELLED" if cancel and cancel.is_set() else "TIMED_OUT"
                terminate_at = time.monotonic()
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if terminate_at is not None and time.monotonic() - terminate_at > .3 and not hard_killed:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                hard_killed = True
            for key, _ in selector.select(.05):
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                info = streams[key.data]
                info["seen"] += len(chunk)
                writable = chunk[:max(0, max_stream_bytes - info["saved"])]
                info["file"].write(writable)
                info["saved"] += len(writable)
                info["truncated"] = info["seen"] > info["saved"]
            # A daemonized descendant with escaped process group must not hang the harness.
            if terminate_at is not None and time.monotonic() - terminate_at > 2:
                for key in list(selector.get_map().values()):
                    streams[key.data]["truncated"] = True
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                break
        code = process.wait(timeout=3)
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
        for info in streams.values():
            info["file"].close()
        selector.close()
    result: dict[str, Any] = {"outcome": reason or ("PASSED" if code == 0 else "FAILED"),
                               "exit_code": code, "duration_seconds": round(time.monotonic() - start, 6),
                               "streams": {}}
    for name, info in streams.items():
        raw = (output_dir / f"{name}.bin").read_bytes()
        (output_dir / f"{name}.txt").write_text(raw.decode("utf-8", errors="replace"), encoding="utf-8")
        result["streams"][name] = {"observed_bytes": info["seen"], "stored_bytes": len(raw),
                                    "complete": not info["truncated"],
                                    "sha256": hashlib.sha256(raw).hexdigest(), "raw_path": f"{name}.bin",
                                    "view_path": f"{name}.txt", "privacy": "local_only"}
    (output_dir / "capture.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
