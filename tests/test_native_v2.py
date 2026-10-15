from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from runner_app.executors import native_v2
from runner_app.executors.native_v2 import NativeError, execute_native, shell_argv

pytestmark = pytest.mark.skipif(sys.platform not in {"linux", "darwin"} or os.geteuid() == 0,
                                reason="Real ordinary-user POSIX qualification only")


def execute(tmp_path: Path, script: str, **kwargs):
    options = {"env": {}, "output_dir": tmp_path / "evidence", "timeout_seconds": 3,
               "cancel": threading.Event(), "max_log_bytes": 8192}
    options.update(kwargs)
    return execute_native([sys.executable, "-c", script], cwd=tmp_path, **options)


def running(pid: int) -> bool:
    if sys.platform == "linux":
        try:
            return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0] != "Z"
        except FileNotFoundError:
            return False
    snapshot, complete = native_v2._snapshot()
    assert complete
    return pid in snapshot and not snapshot[pid].state.startswith("Z")


def test_real_exit_capture_private_routing_and_sanitized_explicit_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "inherited-test-value")
    monkeypatch.setenv("UNDECLARED_TEST_VALUE", "must-not-be-inherited")
    script = """import json, os, pathlib, sys
assert sys.stdin.read() == ''
out = pathlib.Path(os.environ['CDR_OUTPUT_DIR'])
(out / 'program.json').write_text('{"answer":42}')
print(json.dumps(dict(os.environ)), flush=True)
sys.stderr.buffer.write(b'raw\\xffstderr')
"""
    result = execute(tmp_path, script, env={"EXPLICIT_VALUE": "yes", "CDR_RUN_ID": "run-1",
                                          "GITHUB_TOKEN": "explicit-test-value",
                                          "TASK_PASSWORD": "explicit-test-value",
                                          "BASH_ENV": "/never/read"})
    assert result.outcome == "PASSED"
    assert result.exit_code == 0 and result.signal is None
    assert result.cleanup_verified and result.logs_persisted
    assert result.pid and result.process_start_identity
    record = json.loads((tmp_path / "evidence/process.json").read_bytes())
    assert record["pid"] == result.pid
    assert record["process_start_identity"] == result.process_start_identity
    assert result.host_os == {"linux": "linux", "darwin": "macos"}[sys.platform]
    assert result.host_arch
    observed_env = json.loads(result.stdout_path.read_bytes())
    assert observed_env["EXPLICIT_VALUE"] == "yes"
    assert observed_env["CDR_RUN_ID"] == "run-1"
    assert observed_env["HOME"] == str(tmp_path / "evidence/home")
    assert observed_env["TMPDIR"] == str(tmp_path / "evidence/tmp")
    assert set(observed_env).isdisjoint({"OPENAI_API_KEY", "UNDECLARED_TEST_VALUE", "GITHUB_TOKEN",
                                       "TASK_PASSWORD", "BASH_ENV"})
    assert result.stderr_path.read_bytes() == b"raw\xffstderr"
    assert (tmp_path / "evidence/program.json").read_bytes() == b'{"answer":42}'
    assert result.stdout_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("exit_code", [0, 7])
def test_real_exit_status_is_separate_from_cleanup(tmp_path, exit_code):
    result = execute(tmp_path, f"raise SystemExit({exit_code})")
    assert result.exit_code == exit_code
    assert result.outcome == ("PASSED" if exit_code == 0 else "FAILED")
    assert result.cleanup_verified
    assert result.error_code is None


def test_real_signal_is_reported_without_a_fabricated_exit_code(tmp_path):
    result = execute(tmp_path, "import os,signal; os.kill(os.getpid(), signal.SIGTERM)")
    assert result.outcome == "FAILED"
    assert result.exit_code is None and result.signal == signal.SIGTERM
    assert result.cleanup_verified


def test_large_concurrent_raw_streams_keep_draining_after_combined_disk_budget(tmp_path):
    script = """import os
for _ in range(32):
    os.write(1, b'o' * 65536)
    os.write(2, b'e' * 65536)
"""
    result = execute(tmp_path, script, max_log_bytes=1024)
    assert result.outcome == "PASSED" and result.cleanup_verified
    assert result.stdout_bytes == result.stderr_bytes == 2 * 1024 * 1024
    assert result.stdout_truncated and result.stderr_truncated
    assert result.stdout_saved_bytes + result.stderr_saved_bytes == 1024
    assert result.stdout_path.stat().st_size + result.stderr_path.stat().st_size == 1024


def tree_script(detach_child: bool = False) -> str:
    grandchild = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    child = f"""import json,os,pathlib,signal,subprocess,sys,time
{'os.setsid()' if detach_child else ''}
signal.signal(signal.SIGTERM, signal.SIG_IGN)
p = subprocess.Popen([sys.executable, '-c', {grandchild!r}])
pathlib.Path(os.environ['CDR_OUTPUT_DIR'], 'pids.json').write_text(
    json.dumps({{'child':os.getpid(), 'grandchild':p.pid}}))
time.sleep(30)
"""
    return f"""import subprocess,sys,time
p = subprocess.Popen([sys.executable, '-c', {child!r}])
time.sleep(30)
"""


@pytest.mark.parametrize("reason", ["cancel", "timeout"])
def test_real_child_and_grandchild_are_killed_on_cancel_or_timeout(tmp_path, reason):
    cancel = threading.Event()
    ready = tmp_path / "evidence/pids.json"
    watcher = None
    if reason == "cancel":
        def cancel_when_started():
            deadline = time.monotonic() + 2
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.05)
            cancel.set()
        watcher = threading.Thread(target=cancel_when_started)
        watcher.start()
    result = execute(tmp_path, tree_script(), cancel=cancel,
                     timeout_seconds=1 if reason == "timeout" else 3)
    if watcher is not None:
        watcher.join(timeout=2)
        assert not watcher.is_alive()
    assert result.outcome == ("CANCELLED" if reason == "cancel" else "TIMED_OUT")
    assert result.cleanup_verified and result.error_code is None
    assert result.duration_seconds < 2.5
    pids = json.loads(ready.read_bytes())
    assert not running(result.pid)
    assert all(not running(pid) for pid in pids.values())


def test_observed_descendant_in_another_session_is_also_cleaned(tmp_path):
    cancel = threading.Event()
    timer = threading.Timer(0.3, cancel.set)
    timer.start()
    try:
        result = execute(tmp_path, tree_script(detach_child=True), cancel=cancel)
    finally:
        timer.cancel()
        timer.join()
    assert result.outcome == "CANCELLED" and result.cleanup_verified
    pids = json.loads((tmp_path / "evidence/pids.json").read_bytes())
    assert all(not running(pid) for pid in pids.values())


def test_leader_exit_does_not_hang_on_inherited_child_pipe_handles(tmp_path):
    script = """import subprocess,sys
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
print('leader done', flush=True)
"""
    result = execute(tmp_path, script)
    assert result.outcome == "PASSED" and result.cleanup_verified
    assert result.duration_seconds < 1.5
    assert result.stdout_path.read_bytes() == b"leader done\n"


def test_unknown_process_observation_does_not_claim_verified_cleanup(tmp_path, monkeypatch):
    original = native_v2._snapshot
    def incomplete():
        snapshot, _ = original()
        return snapshot, False
    monkeypatch.setattr(native_v2, "_snapshot", incomplete)
    result = execute(tmp_path, "print('done')")
    assert result.outcome == "PASSED"
    assert not result.cleanup_verified
    assert result.error_code == "CLEANUP_UNCERTAIN"


def test_explicit_shell_dialect_executes_without_login_startup_files(tmp_path):
    argv = shell_argv("sh", "printf '%s' literal; printf '%s' error >&2")
    assert Path(argv[0]).is_absolute()
    assert argv[1:] == ["-c", "printf '%s' literal; printf '%s' error >&2"]
    result = execute_native(argv, cwd=tmp_path, env={}, output_dir=tmp_path / "evidence",
                            timeout_seconds=3, cancel=threading.Event(), max_log_bytes=100)
    assert result.outcome == "PASSED" and result.cleanup_verified
    assert result.stdout_path.read_bytes() == b"literal"
    assert result.stderr_path.read_bytes() == b"error"
    with pytest.raises(NativeError) as exc:
        shell_argv("automatic", "true")
    assert exc.value.code == "INVALID_DIALECT"


def test_existing_evidence_is_never_overwritten(tmp_path):
    output = tmp_path / "evidence"
    output.mkdir()
    (output / "stdout.bin").write_bytes(b"existing evidence")
    result = execute(tmp_path, "print('must not start')")
    assert result.outcome == "NOT_RUN" and result.pid is None
    assert result.error_code == "LOCAL_PERSISTENCE_FAILED"
    assert (output / "stdout.bin").read_bytes() == b"existing evidence"


def test_missing_executable_and_pre_cancel_never_start_work(tmp_path):
    result = execute_native([str(tmp_path / "missing")], cwd=tmp_path, env={},
                            output_dir=tmp_path / "missing-evidence", timeout_seconds=1,
                            cancel=threading.Event(), max_log_bytes=0)
    assert result.outcome == "NOT_RUN" and result.error_code == "SPAWN_FAILED"
    assert result.cleanup_verified and result.pid is None
    cancel = threading.Event()
    cancel.set()
    result = execute(tmp_path, "raise SystemExit(99)", cancel=cancel)
    assert result.outcome == "CANCELLED" and result.cleanup_verified and result.pid is None


def test_windows_backend_is_truthfully_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(native_v2, "_host_os", lambda: "windows")
    result = execute(tmp_path, "print('must not start')")
    assert result.outcome == "NOT_RUN"
    assert result.error_code == "PLATFORM_UNAVAILABLE" and result.pid is None
    with pytest.raises(NativeError) as exc:
        shell_argv("cmd", "echo must-not-start")
    assert exc.value.code == "PLATFORM_UNAVAILABLE"


def test_phases_have_separate_capture_dirs_and_one_shared_artifact_root(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    script = """import os,pathlib
p = pathlib.Path(os.environ['CDR_OUTPUT_DIR']) / 'shared.txt'
with p.open('a') as stream:
    stream.write('phase\\n')
print('captured')
"""
    first = execute(tmp_path, script, output_dir=tmp_path / "phase-1", artifact_dir=artifacts)
    second = execute(tmp_path, script, output_dir=tmp_path / "phase-2", artifact_dir=artifacts)
    assert first.outcome == second.outcome == "PASSED"
    assert first.cleanup_verified and second.cleanup_verified
    assert first.stdout_path != second.stdout_path
    assert first.stdout_path.read_bytes() == second.stdout_path.read_bytes() == b"captured\n"
    assert (artifacts / "shared.txt").read_bytes() == b"phase\nphase\n"


@pytest.mark.parametrize("argv", [[], ["python", "-V"], [sys.executable, "bad\x00arg"]])
def test_invalid_argv_never_starts_a_process(tmp_path, argv):
    result = execute_native(argv, cwd=tmp_path, env={}, output_dir=tmp_path / "evidence",
                            timeout_seconds=1, cancel=threading.Event(), max_log_bytes=0)
    assert result.outcome == "NOT_RUN" and result.error_code == "INVALID_EXECUTION"
