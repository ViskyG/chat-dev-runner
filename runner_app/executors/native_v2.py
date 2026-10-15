"""Finite ordinary-user POSIX execution with bounded capture and owned-tree cleanup.

Native execution is not a sandbox. Session/group supervision and observed descendants
cover ordinary subprocess trees; deliberately escaping unobserved processes is not an
isolation guarantee. Platform qualification requires real checks on that platform.
"""
from __future__ import annotations

import json
import os
import platform
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import BinaryIO, Literal, cast

Outcome = Literal["PASSED", "FAILED", "TIMED_OUT", "CANCELLED", "NOT_RUN"]
_TERM_SECONDS = 0.3
_CLEANUP_SECONDS = 1.0
_POLL_SECONDS = 0.025
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SECRET_PREFIXES = ("AWS_", "AZURE_", "GOOGLE_", "GITHUB_", "GH_", "OPENAI_",
                    "ANTHROPIC_", "CODEX_", "CHATGPT_", "SLACK_", "CDR_CONTROLLER_")
_SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PRIVATE_KEY", "API_KEY")
_STARTUP_KEYS = {"BASH_ENV", "ENV", "ZDOTDIR", "PYTHONHOME", "PYTHONSTARTUP",
                 "SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_CONFIG", "GIT_CONFIG_COUNT",
                 "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"}


class NativeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class NativeResult:
    outcome: Outcome
    exit_code: int | None
    signal: int | None
    cleanup_verified: bool
    stdout_path: Path
    stderr_path: Path
    stdout_bytes: int
    stderr_bytes: int
    stdout_saved_bytes: int
    stderr_saved_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    logs_persisted: bool
    host_os: str
    host_arch: str
    duration_seconds: float
    pid: int | None
    process_start_identity: str | None
    error_code: str | None


@dataclass(frozen=True)
class _Process:
    pid: int
    ppid: int
    pgid: int
    start: str
    state: str


def _host_os() -> str:
    return {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(
        sys.platform, "unknown")


def shell_argv(dialect: str, script: str) -> list[str]:
    """Resolve exactly the named dialect, without translation or a hidden login shell."""
    if not isinstance(script, str) or not script or len(script) > 32768 or "\x00" in script:
        raise NativeError("INVALID_SCRIPT", "Shell script must be nonempty text without NUL.")
    if dialect == "cmd":
        raise NativeError("PLATFORM_UNAVAILABLE", "Windows Job Object execution is unavailable.")
    options = {"bash": ["--noprofile", "--norc", "-c"], "sh": ["-c"],
               "pwsh": ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]}
    if dialect not in options:
        raise NativeError("INVALID_DIALECT", "Shell dialect must be explicit and supported.")
    executable = shutil.which(dialect, path=os.defpath + os.pathsep + "/usr/local/bin")
    if executable is None:
        raise NativeError("TOOLCHAIN_UNAVAILABLE", "The selected shell is not installed.")
    return [str(Path(executable).absolute()), *options[dialect], script]


def _environment(env: Mapping[str, str], output_dir: Path) -> dict[str, str]:
    result = {"PATH": os.defpath, "LANG": "C.UTF-8"}
    for key, value in env.items():
        if (not isinstance(key, str) or not isinstance(value, str)
                or not _ENV_NAME.fullmatch(key) or "\x00" in value):
            raise NativeError("INVALID_ENV", "Environment must contain valid string assignments.")
        upper = key.upper()
        if (upper.startswith(_SECRET_PREFIXES + ("LD_", "DYLD_", "GIT_CONFIG_"))
                or any(part in upper for part in _SECRET_MARKERS) or upper in _STARTUP_KEYS):
            continue
        result[key] = value
    # No inherited account home, credential caches or controller scratch directories.
    result.update(HOME=str(output_dir / "home"), TMPDIR=str(output_dir / "tmp"),
                  CDR_OUTPUT_DIR=str(output_dir))
    return result


def _snapshot() -> tuple[dict[int, _Process], bool]:
    processes: dict[int, _Process] = {}
    if sys.platform == "linux":
        try:
            entries = list(Path("/proc").iterdir())
        except OSError:
            return processes, False
        complete = True
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                fields = (entry / "stat").read_text().rpartition(")")[2].split()
                pid = int(entry.name)
                processes[pid] = _Process(pid, int(fields[1]), int(fields[2]),
                                          fields[19], fields[0])
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (OSError, ValueError, IndexError):
                complete = False
        return processes, complete
    if sys.platform == "darwin":
        try:
            listing = subprocess.run(
                ["/bin/ps", "-axo", "pid=,ppid=,pgid=,stat=,lstart="],
                env={"PATH": os.defpath, "LC_ALL": "C"}, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=0.3, check=False)
            if listing.returncode != 0 or len(listing.stdout) > 4 * 1024 * 1024:
                return processes, False
            for line in listing.stdout.decode("ascii", errors="replace").splitlines():
                fields = line.split(None, 4)
                if len(fields) == 5:
                    pid = int(fields[0])
                    processes[pid] = _Process(pid, int(fields[1]), int(fields[2]),
                                              fields[4], fields[3])
            return processes, True
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return processes, False
    return processes, False


def _owned(snapshot: dict[int, _Process], identities: dict[int, str], pgid: int) -> list[_Process]:
    """Track group members and descendants while preserving each process start identity."""
    members = {pid for pid, proc in snapshot.items()
               if identities.get(pid) == proc.start or proc.pgid == pgid}
    while True:
        descendants = {pid for pid, proc in snapshot.items() if proc.ppid in members}
        added = descendants - members
        if not added:
            break
        members.update(added)
    for pid in members:
        identities[pid] = snapshot[pid].start
    return [snapshot[pid] for pid in members if not snapshot[pid].state.startswith(("Z", "X"))]


def _terminate_owned(process: subprocess.Popen[bytes], identities: dict[int, str],
                     sig: int) -> bool:
    snapshot, complete = _snapshot()
    members = _owned(snapshot, identities, process.pid)
    if process.poll() is None or any(member.pgid == process.pid for member in members):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            complete = False
    # Observed descendants that create another process group still belong to this run.
    escaped = [member for member in members if member.pgid != process.pid]
    if escaped:
        current, verified = _snapshot()
        complete &= verified
        for member in escaped:
            if member.pid not in current or current[member.pid].start != member.start:
                continue
            try:
                os.kill(member.pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                complete = False
    return complete


def execute_native(argv: list[str], *, cwd: Path, env: Mapping[str, str], output_dir: Path,
                   timeout_seconds: int, cancel: Event, max_log_bytes: int) -> NativeResult:
    """Run an absolute executable; max_log_bytes bounds both raw logs combined.

    The timeout bounds workload time. TERM/KILL/capture receive at most one further
    second. A false cleanup_verified result must block the caller's next local lane.
    No global environment is copied; only sanitized explicit assignments reach code.
    """
    started = time.monotonic()
    host = _host_os()
    arch = platform.machine().lower() or "unknown"
    stdout_path, stderr_path = output_dir / "stdout.bin", output_dir / "stderr.bin"
    observed = {"stdout": 0, "stderr": 0}
    saved = {"stdout": 0, "stderr": 0}
    logs_ok = False
    process: subprocess.Popen[bytes] | None = None
    identity: str | None = None

    def result(outcome: Outcome, cleanup: bool, error: str | None = None) -> NativeResult:
        rc = process.returncode if process is not None else None
        return NativeResult(
            outcome, rc if rc is not None and rc >= 0 else None,
            -rc if rc is not None and rc < 0 else None, cleanup,
            stdout_path, stderr_path, observed["stdout"], observed["stderr"],
            saved["stdout"], saved["stderr"], observed["stdout"] > saved["stdout"],
            observed["stderr"] > saved["stderr"], logs_ok, host, arch,
            time.monotonic() - started, process.pid if process is not None else None,
            identity, error)

    if host not in {"linux", "macos"} or os.name != "posix":
        return result("NOT_RUN", True, "PLATFORM_UNAVAILABLE")
    if os.geteuid() == 0:
        return result("NOT_RUN", True, "ELEVATED_EXECUTION_UNAVAILABLE")
    if (not isinstance(argv, list) or not argv or len(argv) > 128
            or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv)
            or not Path(argv[0]).is_absolute()
            or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 86400
            or type(max_log_bytes) is not int or not 0 <= max_log_bytes <= 100 * 1024 * 1024):
        return result("NOT_RUN", True, "INVALID_EXECUTION")
    if (not cwd.is_absolute() or not cwd.is_dir() or cwd.resolve() != cwd
            or not output_dir.is_absolute() or output_dir.resolve() != output_dir):
        return result("NOT_RUN", True, "INVALID_EXECUTION_PATH")
    try:
        child_env = _environment(env, output_dir)
    except NativeError as exc:
        return result("NOT_RUN", True, exc.code)
    if cancel.is_set():
        return result("CANCELLED", True)
    logs: dict[str, BinaryIO] = {}
    try:
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("home", "tmp"):
            (output_dir / name).mkdir(mode=0o700, exist_ok=False)
        logs["stdout"] = stdout_path.open("xb")
        logs["stderr"] = stderr_path.open("xb")
        for log in logs.values():
            os.fchmod(log.fileno(), 0o600)
        logs_ok = True
    except OSError:
        for log in logs.values():
            log.close()
        return result("NOT_RUN", True, "LOCAL_PERSISTENCE_FAILED")

    selector = selectors.DefaultSelector()
    identities: dict[int, str] = {}
    outcome: Outcome | None = None
    error: str | None = None
    cleanup_started: float | None = None
    killed = False
    cleanup_verified = False
    observation_complete = True
    try:
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True, close_fds=True, umask=0o077)
        except OSError:
            raise NativeError("SPAWN_FAILED", "Native process could not be started.") from None
        snapshot, complete = _snapshot()
        observation_complete &= complete
        identities.update({member.pid: member.start
                           for member in _owned(snapshot, identities, process.pid)})
        identity = identities.get(process.pid)
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            assert stream is not None
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        try:
            with (output_dir / "process.json").open("x", encoding="utf-8") as record:
                os.fchmod(record.fileno(), 0o600)
                json.dump({"pid": process.pid, "pgid": process.pid,
                           "process_start_identity": identity, "host_os": host}, record)
                record.flush()
                os.fsync(record.fileno())
        except OSError:
            logs_ok = False
            raise NativeError("LOCAL_PERSISTENCE_FAILED", "Process identity was not saved.") from None
        while True:
            now = time.monotonic()
            rc = process.poll()
            snapshot, complete = _snapshot()
            observation_complete &= complete
            members = _owned(snapshot, identities, process.pid)
            if cleanup_started is None:
                if cancel.is_set():
                    outcome = "CANCELLED"
                elif now - started >= timeout_seconds:
                    outcome = "TIMED_OUT"
                elif rc is not None:
                    outcome = "PASSED" if rc == 0 else "FAILED"
                if outcome is not None:
                    cleanup_started = now
                    observation_complete &= _terminate_owned(process, identities, signal.SIGTERM)
            elif not killed and now - cleanup_started >= _TERM_SECONDS:
                observation_complete &= _terminate_owned(process, identities, signal.SIGKILL)
                killed = True
            for key, _mask in selector.select(_POLL_SECONDS):
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    cast(BinaryIO, key.fileobj).close()
                    continue
                name = key.data
                observed[name] += len(chunk)
                remaining = max_log_bytes - sum(saved.values())
                if logs_ok and remaining > 0:
                    try:
                        logs[name].write(chunk[:remaining])
                        saved[name] += min(remaining, len(chunk))
                    except OSError:
                        logs_ok = False
                        error = "LOCAL_PERSISTENCE_FAILED"
            if cleanup_started is not None:
                if rc is not None and not members and not selector.get_map():
                    cleanup_verified = observation_complete
                    break
                if now - cleanup_started >= _CLEANUP_SECONDS:
                    break
        process.poll()
        if not cleanup_verified:
            error = error or "CLEANUP_UNCERTAIN"
        assert outcome is not None
    except NativeError as exc:
        outcome = "NOT_RUN" if process is None else "FAILED"
        cleanup_verified = process is None
        error = exc.code
    except (OSError, ValueError):
        outcome = outcome or "FAILED"
        error = error or "SUPERVISION_FAILED"
    finally:
        if process is not None and not cleanup_verified:
            _terminate_owned(process, identities, signal.SIGKILL)
            if process.poll() is None:
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    error = error or "CLEANUP_UNCERTAIN"
        for key in list(selector.get_map().values()):
            cast(BinaryIO, key.fileobj).close()
        selector.close()
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        for log in logs.values():
            try:
                log.flush()
                os.fsync(log.fileno())
                log.close()
            except OSError:
                logs_ok = False
                error = "LOCAL_PERSISTENCE_FAILED"
                try:
                    log.close()
                except OSError:
                    pass
    assert outcome is not None
    return result(outcome, cleanup_verified, error)
