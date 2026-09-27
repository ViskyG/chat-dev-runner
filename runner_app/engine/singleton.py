"""Nonblocking process ownership of a local runner state directory."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

from runner_app.storage.paths import open_private_file


class AlreadyRunning(ValueError):
    pass


class Singleton:
    def __init__(self, path: Path) -> None:
        self._fd = open_private_file(path, create=True)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(self._fd)
            self._fd = -1
            raise AlreadyRunning("Another runner owns this state directory") from error

    def close(self) -> None:
        if self._fd >= 0:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = -1
