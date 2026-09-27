"""Owner-only local paths, without changing unrelated ancestor permissions.

Discovery is pure. Creation walks directory descriptors with O_NOFOLLOW; existing
application directories and files must already be private and owned by this user.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

APPLICATION_NAME = "chat-dev-runner"


class UnsafePathError(ValueError):
    """A local storage path crosses a link or has unsafe ownership/permissions."""


def _absolute(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise UnsafePathError("Local storage paths must be absolute and contain no '..'")
    return path


def _check_private(info: os.stat_result, *, directory: bool, allow_unlinked: bool = False) -> None:
    expected = 0o700 if directory else 0o600
    kind_matches = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not kind_matches or info.st_uid != os.geteuid():
        raise UnsafePathError("Local storage must be an ordinary file/directory owned by this user")
    if stat.S_IMODE(info.st_mode) != expected:
        raise UnsafePathError(f"Local storage requires mode {expected:04o}")
    # SQLite may unlink a WAL/SHM/journal file after we open it but before
    # fstat. An already-unlinked descriptor has no pathname to follow.
    if not directory and info.st_nlink != 1 and not (allow_unlinked and info.st_nlink == 0):
        raise UnsafePathError("Local storage files must have one hard link")


def _open_directory(path: Path, *, create: bool = True) -> int:
    """Return an owned descriptor; never follow symlinks or chmod ancestors."""
    path = _absolute(path)
    if path == Path("/"):
        raise UnsafePathError("The filesystem root cannot be an application storage directory")
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open("/", flags)
    try:
        for position, part in enumerate(path.parts[1:]):
            created = False
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    created = True
                except FileExistsError:
                    pass  # Another creator won; still inspect without following links.
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            if created:
                # O_PATH also opens a directory created with an owner-denying
                # umask. The proc descriptor pins this newly created object;
                # fchmod itself does not support O_PATH on Linux.
                if os.fstat(descriptor).st_uid != os.geteuid():
                    raise UnsafePathError("New storage directory has unexpected ownership")
                os.chmod(f"/proc/self/fd/{descriptor}", 0o700)
            if position == len(path.parts) - 2:
                _check_private(os.fstat(descriptor), directory=True)
        return descriptor
    except OSError as error:
        os.close(descriptor)
        raise UnsafePathError(f"Cannot securely access local directory: {path}") from error
    except BaseException:
        os.close(descriptor)
        raise


def ensure_private_directory(path: Path) -> None:
    """Create a private directory, rejecting unsafe existing directories."""
    os.close(_open_directory(path))


def _open_file_at(directory_fd: int, name: str, *, create: bool, allow_unlinked: bool = False) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    created = False
    if create:
        try:
            descriptor = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
            created = True
        except FileExistsError:
            descriptor = os.open(name, flags, dir_fd=directory_fd)
    else:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        if created:
            os.fchmod(descriptor, 0o600)
        _check_private(os.fstat(descriptor), directory=False, allow_unlinked=allow_unlinked)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_private_file(path: Path, *, create: bool = False) -> int:
    """Return a read/write descriptor for an owner-only regular file.

    The caller closes the descriptor. Files are never truncated. Missing parent
    directories are created only when ``create=True``; links and hardlinks fail.
    """
    path = _absolute(path)
    descriptor = _open_directory(path.parent, create=create)
    try:
        return _open_file_at(descriptor, path.name, create=create)
    except OSError as error:
        raise UnsafePathError(f"Cannot securely access local file: {path}") from error
    finally:
        os.close(descriptor)


def _root(environment: Mapping[str, str], key: str, default: Path) -> Path:
    value = environment.get(key)
    return _absolute(Path(value)) if value else default


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    state_dir: Path
    cache_dir: Path
    runtime_dir: Path | None = None

    @classmethod
    def discover(cls, env: Mapping[str, str] | None = None) -> Paths:
        """Resolve paths without reading credentials or creating any files.

        The architecture stores durable state under XDG_DATA_HOME, not
        XDG_STATE_HOME. An absent XDG_RUNTIME_DIR has no invented /tmp fallback.
        """
        environment = os.environ if env is None else env
        home = _absolute(Path(environment.get("HOME") or Path.home()))
        runtime = environment.get("XDG_RUNTIME_DIR")
        return cls(
            config_dir=_root(environment, "XDG_CONFIG_HOME", home / ".config") / APPLICATION_NAME,
            state_dir=_root(environment, "XDG_DATA_HOME", home / ".local" / "share") / APPLICATION_NAME,
            cache_dir=_root(environment, "XDG_CACHE_HOME", home / ".cache") / APPLICATION_NAME,
            runtime_dir=_absolute(Path(runtime)) / APPLICATION_NAME if runtime else None,
        )

    @property
    def database_path(self) -> Path:
        return self.state_dir / "state.sqlite"

    @property
    def token_dir(self) -> Path:
        return self.config_dir / "tokens"

    @property
    def evidence_dir(self) -> Path:
        return self.state_dir / "outputs"

    def ensure_private(self) -> None:
        """Explicitly create local roots and token/evidence directories."""
        for path in (self.config_dir, self.state_dir, self.cache_dir, self.runtime_dir,
                     self.token_dir, self.evidence_dir):
            if path is not None:
                ensure_private_directory(path)
