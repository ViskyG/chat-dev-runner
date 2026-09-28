#!/usr/bin/python3 -I
"""Root-owned narrow tmpfs allocator for one runner-owned finite operation.

Install this file root:root 0755 at /usr/local/libexec/chat-dev-runner-volume-helper.
It accepts only canonical run UUIDs below a fixed root-controlled mount base.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import stat
import subprocess
import sys
from pathlib import Path
from uuid import UUID

BASE = Path('/run/chat-dev-runner/volumes')
SOURCE = 'cdr-run-quota'
UID = GID = 1000
ALLOWED_MB = frozenset({64, 256})
MAX_SLOTS = 8


def _uuid(value: str) -> str:
    try:
        if str(UUID(value)) == value and re.fullmatch(r'[0-9a-f-]{36}', value):
            return value
    except ValueError:
        pass
    raise ValueError('canonical run UUID required')


def _check_root_directory(path: Path, mode: int) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != mode):
        raise ValueError('root-controlled volume path changed')


def _base(*, create: bool) -> None:
    owner = pwd.getpwnam('visky')
    if owner.pw_uid != UID or owner.pw_gid != GID:
        raise ValueError('approved owner identity changed')
    _check_root_directory(Path('/run'), 0o755)
    app = BASE.parent
    if create:
        app.mkdir(mode=0o711, exist_ok=True)
        BASE.mkdir(mode=0o711, exist_ok=True)
    if app.exists():
        _check_root_directory(app, 0o711)
    if BASE.exists():
        _check_root_directory(BASE, 0o711)


def _mount_record(path: Path) -> tuple[str, str] | None:
    # UUID-only paths have no mountinfo escapes, spaces, or special characters.
    found = []
    for line in Path('/proc/self/mountinfo').read_text(encoding='ascii').splitlines():
        parts = line.split()
        if len(parts) > 9 and parts[4] == str(path) and '-' in parts:
            sep = parts.index('-')
            if sep + 2 < len(parts):
                found.append((parts[sep + 1], parts[sep + 2]))
    if len(found) > 1:
        raise ValueError('stacked run-volume mounts are forbidden')
    return found[0] if found else None


def _mounted(path: Path, expected_mb: int | None = None) -> bool:
    record = _mount_record(path)
    if record is None:
        return False
    if record != ('tmpfs', SOURCE):
        raise ValueError('run-volume mount identity differs')
    info = path.stat()
    fs = os.statvfs(path)
    capacity = fs.f_blocks * fs.f_frsize
    if (info.st_uid != UID or info.st_gid != GID or stat.S_IMODE(info.st_mode) != 0o700
            or fs.f_files != 16384
            or capacity not in {size * 1024 * 1024 for size in ALLOWED_MB}
            or expected_mb is not None and capacity != expected_mb * 1024 * 1024):
        raise ValueError('run-volume quota differs')
    return True


def allocate(run_id: str, size_mb: int) -> dict[str, object]:
    run_id = _uuid(run_id)
    if size_mb not in ALLOWED_MB:
        raise ValueError('run-volume size outside approved profiles')
    _base(create=True)
    path = BASE / run_id
    if path.exists() or path.is_symlink():
        raise ValueError('run-volume ID already exists; reconcile before retry')
    active = sum(_mounted(item) for item in BASE.iterdir() if item.is_dir())
    if active >= MAX_SLOTS:
        raise ValueError('run-volume capacity reached')
    path.mkdir(mode=0o700)
    try:
        _check_root_directory(path, 0o700)
        subprocess.run([
            '/usr/bin/mount', '-t', 'tmpfs', '-o',
            f'size={size_mb}m,nr_inodes=16384,uid={UID},gid={GID},mode=0700,nosuid,nodev',
            SOURCE, str(path),
        ], check=True, timeout=8, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _mounted(path, size_mb) or any(path.iterdir()):
            raise ValueError('mounted run-volume verification failed')
    except BaseException:
        if not _mount_record(path):
            path.rmdir()
        raise
    return {'status': 'ALLOCATED', 'run_id': run_id, 'size_mb': size_mb, 'path': str(path)}


def release(run_id: str) -> dict[str, object]:
    run_id = _uuid(run_id)
    _base(create=False)
    path = BASE / run_id
    if not _mounted(path):
        raise ValueError('exact run-volume mount is absent')
    if any(path.iterdir()):
        raise ValueError('run-volume is not empty; preserve evidence and reconcile first')
    subprocess.run(['/usr/bin/umount', str(path)], check=True, timeout=8,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _check_root_directory(path, 0o700)
    path.rmdir()
    return {'status': 'RELEASED', 'run_id': run_id}


def main() -> int:
    try:
        if os.geteuid() != 0:
            raise ValueError('root-owned helper requires administrative execution')
        if len(sys.argv) == 2 and sys.argv[1] == 'probe':
            _base(create=False)
            value = {'status': 'READY', 'base': str(BASE), 'max_slots': MAX_SLOTS}
        elif len(sys.argv) == 4 and sys.argv[1] == 'allocate':
            value = allocate(sys.argv[2], int(sys.argv[3], 10))
        elif len(sys.argv) == 3 and sys.argv[1] == 'release':
            value = release(sys.argv[2])
        else:
            raise ValueError('unsupported helper action')
        print(json.dumps(value, sort_keys=True, separators=(',', ':')))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Never relay OS paths or subprocess diagnostics into runner error surfaces.
        print(json.dumps({'status': 'REFUSED', 'reason': type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
