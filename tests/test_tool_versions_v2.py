from __future__ import annotations

import os
import sys
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from runner_app.executors.task_plan_v2 import ToolProvenance
from runner_app.executors.tool_versions_v2 import observe_tool

pytestmark = pytest.mark.skipif(sys.platform not in {"linux", "darwin"} or os.geteuid() == 0,
                                reason="Real ordinary-user POSIX tool observation")


def tool(*arguments: str) -> ToolProvenance:
    return ToolProvenance("python", (sys.executable,), None, "NOT_PROBED",
                          (sys.executable, *arguments), "PRESENT")


def test_real_python_probe_is_journaled_before_spawn_and_retains_raw_bytes(tmp_path: Path) -> None:
    capture = tmp_path / "probe"
    intents: list[dict[str, Any]] = []

    def record(detail: dict[str, Any]) -> None:
        assert not capture.exists()
        intents.append(detail)

    observed = observe_tool(tool("-V"), cwd=tmp_path, env={}, capture_dir=capture,
                            artifact_dir=tmp_path, timeout_seconds=2, cancel=Event(),
                            record_intent=record)
    assert observed.id == "python" and observed.state == "OBSERVED"
    assert observed.version == f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    assert observed.execution and observed.execution.cleanup_verified
    assert observed.execution.exit_code == 0 and observed.execution.logs_persisted
    assert intents[0]["argv"] == [sys.executable, "-V"]
    assert intents[0]["max_log_bytes"] == 16 * 1024
    assert observed.version.encode() in (capture / "stdout.bin").read_bytes() + (capture / "stderr.bin").read_bytes()


def test_local_tool_before_preparation_is_deferred_without_process_or_intent(tmp_path: Path) -> None:
    entry = tmp_path / "node_modules/vitest/vitest.mjs"
    deferred = ToolProvenance("vitest", (sys.executable, str(entry)), None, "NOT_PROBED",
                              (sys.executable, str(entry), "--version"), "AFTER_PREPARATION")
    intents: list[dict[str, Any]] = []
    observed = observe_tool(deferred, cwd=tmp_path, env={}, capture_dir=tmp_path / "probe",
                            artifact_dir=tmp_path, timeout_seconds=2, cancel=Event(),
                            record_intent=intents.append)
    assert observed.state == "NOT_READY" and observed.version is None
    assert observed.execution is None and not intents
    assert not (tmp_path / "probe").exists()


def test_nonzero_probe_never_reports_its_output_as_an_observed_version(tmp_path: Path) -> None:
    observed = observe_tool(tool("-c", "print('unreliable version');raise SystemExit(7)"),
                            cwd=tmp_path, env={}, capture_dir=tmp_path / "probe",
                            artifact_dir=tmp_path, timeout_seconds=2, cancel=Event(),
                            record_intent=lambda _detail: None)
    assert observed.state == "UNKNOWN" and observed.version is None
    assert observed.execution and observed.execution.exit_code == 7
    assert observed.execution.stdout_path.read_bytes() == b"unreliable version\n"


def test_journal_failure_prevents_spawn(tmp_path: Path) -> None:
    def refuse(_detail: dict[str, Any]) -> None:
        raise RuntimeError("durable journal unavailable")

    with pytest.raises(RuntimeError, match="durable journal unavailable"):
        observe_tool(tool("-V"), cwd=tmp_path, env={}, capture_dir=tmp_path / "probe",
                     artifact_dir=tmp_path, timeout_seconds=2, cancel=Event(), record_intent=refuse)
    assert not (tmp_path / "probe").exists()


def test_stderr_version_strips_ansi_and_control_characters(tmp_path: Path) -> None:
    command = "import os;os.write(2,b'\\n\\x1b[32mExample\\x00 1.2.3\\x1b[0m\\nsecond line\\n')"
    observed = observe_tool(tool("-c", command), cwd=tmp_path, env={}, capture_dir=tmp_path / "probe",
                            artifact_dir=tmp_path, timeout_seconds=2, cancel=Event(),
                            record_intent=lambda _detail: None)
    assert observed.state == "OBSERVED" and observed.version == "Example 1.2.3"
    assert observed.execution and b"\x1b[32m" in observed.execution.stderr_path.read_bytes()


def test_overlong_first_line_is_unknown_and_does_not_select_another_stream(tmp_path: Path) -> None:
    command = "import sys;print('v'*1025);print('other version',file=sys.stderr)"
    observed = observe_tool(tool("-c", command), cwd=tmp_path, env={}, capture_dir=tmp_path / "probe",
                            artifact_dir=tmp_path, timeout_seconds=2, cancel=Event(),
                            record_intent=lambda _detail: None)
    assert observed.state == "UNKNOWN" and observed.version is None
    assert observed.execution and observed.execution.exit_code == 0
    assert observed.execution.stdout_path.read_bytes().startswith(b"v" * 1025)
