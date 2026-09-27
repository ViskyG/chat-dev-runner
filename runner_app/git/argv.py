"""Bounded, non-interactive Git invocation for runner-owned source operations."""

from __future__ import annotations

import os
import re
import selectors
import signal
import stat
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

_MAX_OUTPUT = 8 * 1024 * 1024
_MAX_ARGS = 128
_MAX_ARG_BYTES = 4096
_SAFE_CALLER_CONFIG = frozenset({"user.name", "user.email", "commit.gpgsign", "core.quotepath"})
_BASE_CONFIG = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.pager=cat",
    "core.attributesFile=/dev/null",
    "commit.gpgSign=false",
    "tag.gpgSign=false",
    "submodule.recurse=false",
    "fetch.recurseSubmodules=false",
    "protocol.allow=never",
    "protocol.file.allow=always",
    "filter.lfs.smudge=",
    "filter.lfs.process=",
)
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$", re.ASCII)


class GitError(RuntimeError):
    """A stable refusal or Git failure; raw Git output is deliberately unavailable."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def validate_branch(branch: str) -> str:
    """Validate a branch as one argument and as a Git heads ref component."""
    if not isinstance(branch, str) or not _BRANCH_RE.fullmatch(branch):
        raise GitError("INVALID_BRANCH", "Branch name is not an approved Git ref")
    if (".." in branch or "//" in branch or branch.endswith(("/", ".", ".lock"))
            or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))):
        raise GitError("INVALID_BRANCH", "Branch name is not an approved Git ref")
    if branch == "@" or "/." in branch:
        raise GitError("INVALID_BRANCH", "Branch name is not an approved Git ref")
    return branch


def _validate_args(args: Sequence[str]) -> list[str]:
    if isinstance(args, (str, bytes)) or not args or len(args) > _MAX_ARGS:
        raise GitError("INVALID_GIT_ARGV", "Git argument list is empty or too large")
    checked: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if not isinstance(arg, str) or "\x00" in arg or len(arg.encode("utf-8")) > _MAX_ARG_BYTES:
            raise GitError("INVALID_GIT_ARGV", "Git argument is invalid or too large")
        if arg == "-c":
            if index + 1 >= len(args):
                raise GitError("INVALID_GIT_ARGV", "Git configuration argument is incomplete")
            setting = args[index + 1]
            if not isinstance(setting, str) or "=" not in setting:
                raise GitError("INVALID_GIT_ARGV", "Git configuration argument is invalid")
            key, value = setting.split("=", 1)
            if key.lower() not in _SAFE_CALLER_CONFIG or any(c in value for c in "\r\n\x00"):
                raise GitError("UNSAFE_GIT_CONFIG", "Git configuration override is not approved")
        checked.append(arg)
        index += 1
    return checked


def _environment() -> dict[str, str]:
    # An allowlist prevents inherited Git config, tracing, credentials and askpass hooks.
    return {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "XDG_CONFIG_HOME": "/nonexistent",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_SSH_COMMAND": "/bin/false",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_NO_LAZY_FETCH": "1",
    }


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_git_bytes(cwd: Path, args: Sequence[str], timeout: float = 30,
                  *, pass_fds: tuple[int, ...] = (),
                  input_data: bytes | None = None) -> bytes:
    """Return bounded stdout bytes; never expose untrusted Git diagnostics or invoke a shell.

    A local repository is still untrusted. Callers inspect its configuration and tracked
    features before commands that check out files, and pass only policy-approved paths/refs.
    """
    cwd = Path(cwd)
    if len(pass_fds) > 4 or any(not isinstance(fd, int) or fd < 3 for fd in pass_fds):
        raise GitError("INVALID_GIT_FD", "Git inherited descriptor list is invalid")
    pinned = re.fullmatch(r"/proc/self/fd/([0-9]+)", str(cwd))
    pinned_fd = int(pinned.group(1)) if pinned else None
    if pinned_fd is not None:
        if pinned_fd not in pass_fds:
            raise GitError("INVALID_GIT_FD", "Pinned Git directory descriptor was not provided")
        try:
            valid_pinned = stat.S_ISDIR(os.fstat(pinned_fd).st_mode)
        except OSError:
            valid_pinned = False
        if not valid_pinned:
            raise GitError("INVALID_GIT_FD", "Pinned Git directory descriptor is not open")
    elif not cwd.is_absolute() or not cwd.is_dir() or cwd.is_symlink():
        raise GitError("INVALID_GIT_CWD", "Git working directory must be an existing absolute directory")
    if not 0 < timeout <= 300:
        raise GitError("INVALID_GIT_TIMEOUT", "Git timeout is outside the supported range")
    if input_data is not None and (not isinstance(input_data, bytes)
                                   or len(input_data) > 2 * 1024 * 1024):
        raise GitError("GIT_INPUT_LIMIT", "Git input must be at most 2 MiB")
    validated = _validate_args(args)
    command = ["git"]
    for setting in _BASE_CONFIG:
        command.extend(("-c", setting))
    command.extend(validated)
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=_environment(),
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            umask=0o077,
            pass_fds=pass_fds,
        )
    except OSError as error:
        raise GitError("GIT_START_FAILED", "Unable to start the local Git executable") from error
    output = bytearray()
    error_output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        assert process.stdout is not None and process.stderr is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, output)
            selector.register(process.stderr, selectors.EVENT_READ, error_output)
            pending = memoryview(input_data) if input_data is not None else None
            offset = 0
            if process.stdin is not None:
                if pending is not None and len(pending):
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    process.stdin.close()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GitError("GIT_TIMEOUT", "Git operation exceeded its time limit")
                for key, _ in selector.select(min(remaining, 0.25)):
                    if key.data == "stdin":
                        assert pending is not None and process.stdin is not None
                        try:
                            written = os.write(key.fd, pending[offset:offset + 65536])
                        except BrokenPipeError:
                            written = len(pending) - offset
                        offset += written
                        if offset >= len(pending):
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                        continue
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    buffer = key.data
                    buffer.extend(data)
                    if len(output) + len(error_output) > _MAX_OUTPUT:
                        raise GitError("GIT_OUTPUT_LIMIT", "Git output exceeded its size limit")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitError("GIT_TIMEOUT", "Git operation exceeded its time limit")
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise GitError("GIT_TIMEOUT", "Git operation exceeded its time limit") from error
        if return_code != 0:
            raise GitError("GIT_FAILED", f"Git operation failed with exit code {return_code}")
        return bytes(output)
    except BaseException:
        if process.poll() is None:
            _terminate(process)
        raise
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def run_git(cwd: Path, args: Sequence[str], timeout: float = 30,
            *, pass_fds: tuple[int, ...] = ()) -> str:
    """Text counterpart of run_git_bytes; NULs and non-UTF-8 filenames survive."""
    return run_git_bytes(cwd, args, timeout, pass_fds=pass_fds).decode(
        "utf-8", "surrogateescape")


def run_git_input(cwd: Path, args: Sequence[str], input_data: bytes,
                  timeout: float = 30) -> str:
    """Pass bounded binary content on stdin without exposing a mutable staging filename."""
    return run_git_bytes(cwd, args, timeout, input_data=input_data).decode(
        "utf-8", "surrogateescape")
