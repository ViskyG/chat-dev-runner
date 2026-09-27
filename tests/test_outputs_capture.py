"""Trusted generated subprocess fixtures; these tests do not claim host sandboxing."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from threading import Event

from jsonschema import Draft202012Validator

from runner_app.outputs import capture as capture_module
from runner_app.outputs.capture import capture_process, safe_text
from runner_app.resources import load_schema


def _toy(code: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", code], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )


def _callbacks(process: subprocess.Popen[bytes]):
    def terminate() -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def kill() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    return terminate, kill


def test_capture_exact_bytes_safe_view_and_schema(tmp_path: Path) -> None:
    process = _toy(
        "import os;os.write(1,bytes([65,255,27,91,51,49,109,10]));os.write(2,b'error\\n')"
    )
    terminate, kill = _callbacks(process)
    result = capture_process(
        process, tmp_path / "capture", timeout_seconds=2, max_stream_bytes=1024,
        max_total_bytes=2048, terminate=terminate, kill=kill,
    )
    stdout = result.streams[0]
    assert result.outcome == "PASSED" and result.exit_code == 0
    assert stdout.raw_path.read_bytes() == bytes([65, 255, 27, 91, 51, 49, 109, 10])
    assert stdout.view_path is not None
    assert stdout.view_path.read_text() == "A�\\x1b[31m\n"
    assert stdout.raw_sha256 == hashlib.sha256(stdout.raw_path.read_bytes()).hexdigest()
    assert result.streams[1].raw_path.read_bytes() == b"error\n"
    assert result.events_path is not None
    events = [json.loads(line) for line in result.events_path.read_text().splitlines()]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert {event["stream"] for event in events} == {"stdout", "stderr"}
    assert all(event["monotonic_seconds"] >= 0 for event in events)
    assert result.metadata_path is not None
    metadata = json.loads(result.metadata_path.read_text())
    assert metadata["streams"]["stdout"]["observed_bytes"] == 8
    assert "no exact cross-stream interleaving" in metadata["stream_order"]
    assert set(result.files()) == {
        "stdout-raw", "stdout-view", "stderr-raw", "stderr-view", "capture-events",
        "capture-metadata",
    }
    validator = Draft202012Validator(load_schema("output"))
    for output in result.outputs(relative_prefix="capture"):
        validator.validate(output)
        assert (tmp_path / output["relative_path"]).is_file()


def test_capture_drains_after_stream_cap_without_hanging(tmp_path: Path) -> None:
    process = _toy("import os;os.write(1,b'x'*200000)")
    terminate, kill = _callbacks(process)
    result = capture_process(
        process, tmp_path / "capture", timeout_seconds=3, max_stream_bytes=113,
        max_total_bytes=500, terminate=terminate, kill=kill, max_event_records=1,
    )
    stdout = result.streams[0]
    assert result.outcome == "PASSED"
    assert stdout.observed_bytes == 200000
    assert stdout.retained_bytes == 113
    assert stdout.raw_path.read_bytes() == b"x" * 113
    assert not stdout.complete and stdout.truncated_reason == "stream_limit"
    assert result.observed_events > result.retained_events
    assert next(o for o in result.outputs(relative_prefix="capture")
                if o["output_id"] == "capture-events")["complete"] is False


def test_capture_timeout_and_cancel_are_distinct(tmp_path: Path) -> None:
    for name, cancel in (("timeout", None), ("cancel", Event())):
        process = _toy("import time;time.sleep(30)")
        terminate, kill = _callbacks(process)
        if cancel is not None:
            cancel.set()
        result = capture_process(
            process, tmp_path / name, timeout_seconds=0.1,
            max_stream_bytes=100, max_total_bytes=200,
            terminate=terminate, kill=kill, cancel=cancel,
            termination_grace_seconds=0.1, drain_grace_seconds=0.5,
        )
        assert result.outcome == ("CANCELLED" if cancel is not None else "TIMED_OUT")
        assert result.duration_seconds < 3
        assert process.poll() is not None


def test_partial_storage_failure_retains_correct_hash_and_drains(
    tmp_path: Path, monkeypatch,
) -> None:
    original_open = capture_module._open_new

    class FailingWriter:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.first = True

        def write(self, data: bytes) -> int:
            if self.first:
                self.first = False
                return self.wrapped.write(data[:5])
            raise OSError(errno.ENOSPC, "simulated disk full")

        def fileno(self) -> int:
            return self.wrapped.fileno()

        def close(self) -> None:
            self.wrapped.close()

    def fake_open(path: Path):
        wrapped = original_open(path)
        return FailingWriter(wrapped) if path.name == "stdout.bin" else wrapped

    monkeypatch.setattr(capture_module, "_open_new", fake_open)
    process = _toy("import os;os.write(1,b'abcdefghijklmnop')")
    terminate, kill = _callbacks(process)
    result = capture_process(
        process, tmp_path / "capture", timeout_seconds=2,
        max_stream_bytes=100, max_total_bytes=200,
        terminate=terminate, kill=kill,
    )
    stdout = result.streams[0]
    assert result.outcome == "PASSED"
    assert result.capture_fault == "STORAGE_WRITE_FAILED:OSError"
    assert stdout.observed_bytes == 16 and stdout.retained_bytes == 5
    assert stdout.raw_path.read_bytes() == b"abcde"
    assert stdout.raw_sha256 == hashlib.sha256(b"abcde").hexdigest()
    assert stdout.truncated_reason == "storage_error"


def test_safe_text_escapes_terminal_and_bidi_controls() -> None:
    assert safe_text(b"ok\x1b[31m\r\xe2\x80\xae") == "ok\\x1b[31m\\x0d\\u202e"


def test_exited_parent_with_open_descendant_pipe_is_bounded(tmp_path: Path) -> None:
    process = _toy(
        "import subprocess,sys;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])"
    )
    terminate, kill = _callbacks(process)
    result = capture_process(
        process, tmp_path / "capture", timeout_seconds=3,
        max_stream_bytes=100, max_total_bytes=200,
        terminate=terminate, kill=kill, drain_grace_seconds=0.1,
    )
    assert result.exit_code == 0
    assert result.capture_fault == "DRAIN_TIMEOUT"
    assert result.termination_reason is None
    assert result.duration_seconds < 2
    assert any(stream.truncated_reason == "drain_timeout" for stream in result.streams)


def test_external_resource_observer_stops_process_as_infrastructure_fault(tmp_path: Path) -> None:
    process = _toy("import time;time.sleep(30)")
    terminate, kill = _callbacks(process)
    result = capture_process(
        process, tmp_path / "capture", timeout_seconds=5,
        max_stream_bytes=100, max_total_bytes=200,
        terminate=terminate, kill=kill, observe=lambda: "OUTPUT_BYTES",
        termination_grace_seconds=0.1, drain_grace_seconds=0.2,
    )
    assert result.outcome == "UNKNOWN"
    assert result.termination_reason == "RESOURCE_LIMIT"
    assert result.capture_fault == "RESOURCE_LIMIT:OUTPUT_BYTES"
    assert process.poll() is not None


def test_failed_resource_observer_stops_unmonitored_process(tmp_path: Path) -> None:
    process = _toy("import time;time.sleep(30)")
    terminate, kill = _callbacks(process)

    def fail() -> str | None:
        raise RuntimeError("toy monitor fault")

    result = capture_process(
        process, tmp_path / "capture", timeout_seconds=5,
        max_stream_bytes=100, max_total_bytes=200,
        terminate=terminate, kill=kill, observe=fail,
        termination_grace_seconds=0.1, drain_grace_seconds=0.2,
    )
    assert result.outcome == "UNKNOWN"
    assert result.capture_fault == "RESOURCE_MONITOR_FAILED:RuntimeError"
    assert process.poll() is not None
