#!/usr/bin/python3
"""Restore the finite EP-03 acceptance tmpfs pool after reboot; never provision live runs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import uuid
from pathlib import Path

POOL = Path('/home/visky/Work/GPTRunner/chat-dev-runner/verification/implementation/EP-03/toy-volume-pool.tsv')
BASE = Path('/home/visky/.local/share/chat-dev-runner/volumes')
POOL_SHA256 = 'e2d87f533ca95d9c361e0e950a9475014aaba93fc2b22baa4aa17149e47f21ce'
OWNER = 'visky'
UID = GID = 1000


def slots() -> list[Path]:
    raw = POOL.read_bytes()
    if hashlib.sha256(raw).hexdigest() != POOL_SHA256:
        raise ValueError('EP-03 toy pool plan changed')
    result = []
    for line in raw.decode('ascii').splitlines():
        if not line or line.startswith('#'):
            continue
        fields = line.split('\t')
        if len(fields) != 2 or any(re.fullmatch(r'[0-9a-f-]{36}', part) is None for part in fields):
            raise ValueError('Invalid toy pool row')
        request, preparation = fields
        if (str(uuid.UUID(request)) != request or str(uuid.UUID(preparation)) != preparation
                or str(uuid.uuid5(uuid.UUID(request), 'preparation')) != preparation):
            raise ValueError('Invalid toy pool identity')
        result.extend((BASE / preparation, BASE / request))
    if len(result) != 24 or len(set(result)) != 24:
        raise ValueError('Toy pool count changed')
    return result


def preflight() -> list[Path]:
    owner = pwd.getpwnam(OWNER)
    if owner.pw_uid != UID or owner.pw_gid != GID:
        raise ValueError('EP-03 owner identity changed')
    parent = BASE.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != UID or parent.st_gid != GID
            or stat.S_IMODE(parent.st_mode) != 0o700):
        raise ValueError('Toy volume parent changed')
    paths = slots()
    for path in paths:
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != UID or info.st_gid != GID
                or stat.S_IMODE(info.st_mode) != 0o700 or info.st_dev != parent.st_dev
                or any(path.iterdir())):
            raise ValueError(f'Toy volume slot is not an empty unmounted owner directory: {path.name}')
    return paths


def verify(paths: list[Path]) -> dict[str, object]:
    devices = set()
    for path in paths:
        info = path.stat()
        fs = os.statvfs(path)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != UID or info.st_gid != GID
                or stat.S_IMODE(info.st_mode) != 0o700 or info.st_dev == BASE.stat().st_dev
                or info.st_dev in devices or fs.f_blocks * fs.f_frsize != 64 * 1024 * 1024
                or fs.f_files != 16384 or any(path.iterdir())):
            raise ValueError(f'Restored toy volume did not verify: {path.name}')
        devices.add(info.st_dev)
    return {'slot_count': len(paths), 'distinct_devices': len(devices),
            'capacity_bytes_each': 64 * 1024 * 1024, 'inode_capacity_each': 16384}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    paths = preflight()
    if not args.apply:
        print(json.dumps({'status': 'READY_TO_REMOUNT', 'slot_count': len(paths),
                          'plan_sha256': POOL_SHA256}, sort_keys=True))
        return
    if os.geteuid() != 0:
        raise SystemExit('Administrative root is required to mount tmpfs')
    for path in paths:
        subprocess.run([
            '/usr/bin/mount', '-t', 'tmpfs', '-o',
            'size=64m,nr_inodes=16384,uid=1000,gid=1000,mode=0700,nosuid,nodev',
            'cdr-ep03-toy', str(path),
        ], check=True)
    print(json.dumps({'status': 'RESTORED', **verify(paths)}, sort_keys=True))


if __name__ == '__main__':
    main()
