"""Bounded blob delivery to an owner-created inbox; no code execution.

Its provider, binding and admitted request ID come from trusted local assembly.
The private inbox must remain quiescent to other owner processes during delivery.
Provider calls must implement their own blocking I/O timeouts; cooperative checks
here cover the transfer, validation, hashing and the last pre-publication boundary.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import struct
import time
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol, cast
from uuid import uuid4

from .contracts import (
    DEFAULT_LIMITS,
    HandoffError,
    Limits,
    canonical,
    digest,
    relative,
    slug,
    uuid,
    validate,
)


class BlobProvider(Protocol):
    def metadata(self, file_id: str) -> Mapping[str, Any]: ...
    def chunks(self, file_id: str) -> Iterable[bytes]: ...


@dataclass(frozen=True)
class TargetBinding:
    project_id: str
    target_id: str
    inbox: Path
    allowed_drive_parents: frozenset[str]
    # Runner resolves this from local enrollment, not from COMMAND.
    relative_inbox: str = '.runner/handoffs'


def _open_directory(path: Path) -> int:
    if not path.is_absolute() or '..' in path.parts:
        raise HandoffError('TARGET_UNSAFE')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd); fd = new
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise HandoffError('TARGET_NOT_PRIVATE')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _private_open(path: Path, writing: bool) -> int:
    flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    flags |= (os.O_WRONLY | os.O_CREAT | os.O_EXCL) if writing else os.O_RDONLY
    fd = os.open(path, flags, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
        os.close(fd)
        raise HandoffError('UNSAFE_LOCAL_FILE')
    if writing:
        os.fchmod(fd, 0o600)
    return fd


def _space(path: Path, needed: int, limits: Limits) -> None:
    available = os.statvfs(path)
    if available.f_bavail * available.f_frsize < needed + limits.minimum_free_bytes:
        raise HandoffError('INSUFFICIENT_DISK')


def _write(path: Path, raw: bytes, limits: Limits) -> None:
    _space(path.parent, len(raw), limits)
    fd = _private_open(path, True)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())


def _hash_file(path: Path, maximum: int,
               check: Callable[[], None] = lambda: None) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with os.fdopen(_private_open(path, False), 'rb') as stream:
        while chunk := stream.read(65536):
            check()
            n += len(chunk)
            if n > maximum:
                raise HandoffError('HANDOFF_CHANGED')
            h.update(chunk)
    return h.hexdigest(), n


def _check_metadata(meta: Mapping[str, Any], art: Mapping[str, Any], allowed: frozenset[str]) -> None:
    if (meta.get('id') != art['drive_file_id'] or meta.get('trashed') is not False
            or meta.get('version') != art['drive_version']
            or meta.get('mimeType') != art['media_type']
            or meta.get('shortcutDetails') is not None
            or not isinstance(meta.get('parents'), list) or len(meta['parents']) != 1
            or meta['parents'][0] not in allowed
            or not isinstance(meta.get('capabilities'), dict)
            or meta['capabilities'].get('canDownload') is not True):
        raise HandoffError('DRIVE_BINDING_MISMATCH')
    # Drive exposes int64 metadata as strings; do not accept bool or imprecise floats.
    size = meta.get('size')
    if type(size) not in (str, int) or str(size) != str(art['size_bytes']):
        raise HandoffError('DRIVE_SIZE_MISMATCH')
    known_hash = meta.get('sha256Checksum')
    if known_hash is not None and known_hash != art['sha256']:
        raise HandoffError('DRIVE_HASH_MISMATCH')


def _download(provider: BlobProvider, art: dict[str, Any], path: Path,
              binding: TargetBinding, check: Callable[[], None], limits: Limits) -> None:
    h, n = hashlib.sha256(), 0
    try:
        check()
        before = dict(provider.metadata(art['drive_file_id']))
        check()
        _check_metadata(before, art, binding.allowed_drive_parents)
        original_parents = tuple(before['parents'])
        with os.fdopen(_private_open(path, True), 'wb') as stream:
            for chunk in provider.chunks(art['drive_file_id']):
                check()
                if not isinstance(chunk, bytes) or len(chunk) > 1024 * 1024:
                    raise HandoffError('PROVIDER_CHUNK_INVALID')
                n += len(chunk)
                if n > art['size_bytes']:
                    raise HandoffError('DOWNLOAD_TOO_LARGE')
                _space(path.parent, len(chunk), limits)
                stream.write(chunk); h.update(chunk)
            stream.flush(); os.fsync(stream.fileno())
        check()
        after = dict(provider.metadata(art['drive_file_id']))
        check()
        _check_metadata(after, art, binding.allowed_drive_parents)
        if tuple(after['parents']) != original_parents:
            raise HandoffError('DRIVE_BINDING_MISMATCH')
    except HandoffError:
        raise
    except TimeoutError:
        raise HandoffError('TIMED_OUT') from None
    except OSError as exc:
        if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            raise HandoffError('INSUFFICIENT_DISK') from None
        raise HandoffError('PROVIDER_OR_DOWNLOAD_FAILED') from None
    except Exception:  # noqa: BLE001 -- provider errors must not expose credentials or response bodies.
        raise HandoffError('PROVIDER_OR_DOWNLOAD_FAILED') from None
    if n != art['size_bytes'] or h.hexdigest() != art['sha256']:
        raise HandoffError('CONTENT_MISMATCH')


def _zip_preflight(source: BinaryIO, limits: Limits, remaining_entries: int,
                   check: Callable[[], None]) -> int:
    """Bound central-directory work before ZipFile constructs member objects.

    ZIP64, multidisk, prepended payloads and trailing bytes are deliberately outside
    this small ordinary-ZIP interface. They are unnecessary for the blob limits.
    """
    source.seek(0, os.SEEK_END)
    size = source.tell()
    source.seek(max(0, size - 65557))
    tail = source.read(65557)
    offset = tail.rfind(b'PK\x05\x06')
    if offset < 0 or len(tail) - offset < 22:
        raise HandoffError('ZIP_INVALID')
    record = struct.unpack_from('<4s4H2LH', tail, offset)
    _, disk, central_disk, disk_entries, entries, central_size, central_offset, comment = record
    if (disk != 0 or central_disk != 0 or disk_entries != entries
            or entries == 65535 or central_size == 0xffffffff or central_offset == 0xffffffff
            or offset + 22 + comment != len(tail)
            or central_offset + central_size != size - len(tail) + offset):
        raise HandoffError('ZIP_UNSUPPORTED_ENTRY')
    if entries > remaining_entries:
        raise HandoffError('ZIP_ENTRY_LIMIT')
    if central_size > limits.zip_central_directory_bytes:
        raise HandoffError('ZIP_DIRECTORY_LIMIT')
    source.seek(central_offset)
    consumed, counted = 0, 0
    while consumed < central_size:
        check()
        header = source.read(46)
        if len(header) != 46 or header[:4] != b'PK\x01\x02':
            raise HandoffError('ZIP_INVALID')
        names, extras, comments = struct.unpack_from('<3H', header, 28)
        length = 46 + names + extras + comments
        consumed += length
        counted += 1
        if counted > remaining_entries:
            raise HandoffError('ZIP_ENTRY_LIMIT')
        if consumed > central_size:
            raise HandoffError('ZIP_INVALID')
        source.seek(length - 46, os.SEEK_CUR)
    if counted != entries:
        raise HandoffError('ZIP_INVALID')
    source.seek(0)
    if entries and source.read(4) != b'PK\x03\x04':
        raise HandoffError('ZIP_INVALID')
    source.seek(0)
    return int(entries)


def _zip_members(z: zipfile.ZipFile, limits: Limits, used: list[int],
                 check: Callable[[], None]) -> list[tuple[zipfile.ZipInfo, str]]:
    members = z.infolist()
    used[1] += len(members)
    if used[1] > limits.zip_entries:
        raise HandoffError('ZIP_ENTRY_LIMIT')
    explicit: set[str] = set()
    spelling: dict[str, str] = {}
    files: set[str] = set()
    planned = 0
    validated: list[tuple[zipfile.ZipInfo, str]] = []
    for item in members:
        check()
        if '\x00' in item.orig_filename or item.orig_filename != item.filename:
            raise HandoffError('UNSAFE_PATH')
        name = relative(item.filename[:-1] if item.is_dir() else item.filename, limits)
        folded = name.casefold()
        if folded in explicit:
            raise HandoffError('ZIP_DUPLICATE_PATH')
        explicit.add(folded)
        parts = name.split('/')
        for i in range(1, len(parts) + 1):
            part = '/'.join(parts[:i])
            key = part.casefold()
            if key in spelling and spelling[key] != part:
                raise HandoffError('ZIP_CASE_COLLISION')
            spelling[key] = part
        kind = stat.S_IFMT(item.external_attr >> 16)
        if (item.flag_bits & 65
                or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or kind not in {0, stat.S_IFREG, stat.S_IFDIR}
                or (kind == stat.S_IFDIR and not item.is_dir())
                or (kind == stat.S_IFREG and item.is_dir())):
            raise HandoffError('ZIP_UNSUPPORTED_ENTRY')
        # Unix and ASi extra fields may encode link targets. Never interpret them
        # or preserve archive permissions. ZIP64 is outside the bounded format.
        extra = item.extra
        while extra:
            if len(extra) < 4:
                raise HandoffError('ZIP_INVALID')
            tag, length = struct.unpack_from('<2H', extra)
            if len(extra) < 4 + length:
                raise HandoffError('ZIP_INVALID')
            if tag in {0x0001, 0x000d, 0x756e}:
                raise HandoffError('ZIP_UNSUPPORTED_ENTRY')
            extra = extra[4 + length:]
        if item.is_dir():
            if item.file_size != 0:
                raise HandoffError('ZIP_UNSUPPORTED_ENTRY')
        else:
            files.add(folded)
            planned += item.file_size
            if (item.file_size > limits.file_bytes
                    or item.file_size > max(1, item.compress_size) * limits.zip_ratio
                    or planned + used[0] > limits.expanded_bytes):
                raise HandoffError('ZIP_EXPANSION_LIMIT')
        validated.append((item, name))
    for _, name in validated:
        if any('/'.join(name.casefold().split('/')[:i]) in files
               for i in range(1, len(name.split('/')))):
            raise HandoffError('ZIP_PATH_CONFLICT')
    return validated


def _extract(archive: Path, out: Path, limits: Limits, check: Callable[[], None],
             used: list[int]) -> None:
    """Validate all names first; extract inert data with counted, disk-checked reads."""
    out.mkdir(mode=0o700)
    try:
        with os.fdopen(_private_open(archive, False), 'rb') as raw:
            count = _zip_preflight(raw, limits, limits.zip_entries - used[1], check)
            with zipfile.ZipFile(raw) as z:
                if len(z.infolist()) != count:
                    raise HandoffError('ZIP_INVALID')
                validated = _zip_members(z, limits, used, check)
                _space(out, sum(item.file_size for item, _ in validated), limits)
                for item, name in validated:
                    check()
                    path = out / name
                    # All parents are new within the owner-private staging tree.
                    for parent in reversed(path.parents):
                        if parent == out or out in parent.parents:
                            parent.mkdir(mode=0o700, exist_ok=True)
                    if item.is_dir():
                        path.mkdir(mode=0o700, exist_ok=True)
                        continue
                    count = 0
                    with z.open(item, 'r') as source, os.fdopen(
                            _private_open(path, True), 'wb') as dest:
                        while chunk := source.read(65536):
                            check()
                            count += len(chunk)
                            used[0] += len(chunk)
                            if count > item.file_size or used[0] > limits.expanded_bytes:
                                raise HandoffError('ZIP_EXPANSION_LIMIT')
                            _space(out, len(chunk), limits)
                            dest.write(chunk)
                        if count != item.file_size:
                            raise HandoffError('ZIP_SIZE_MISMATCH')
                        dest.flush()
                        os.fsync(dest.fileno())
    except HandoffError:
        raise
    except OSError as exc:
        if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            raise HandoffError('INSUFFICIENT_DISK') from None
        raise HandoffError('ZIP_INVALID') from None
    except (ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
        raise HandoffError('ZIP_INVALID') from None


def _rename_noreplace(root_fd: int, source: str, target: str) -> None:
    """Linux atomic publication, refusing even an empty existing destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    call = getattr(libc, 'renameat2', None)
    if call is None:
        raise HandoffError('ATOMIC_PUBLISH_UNAVAILABLE')
    call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    call.restype = ctypes.c_int
    if call(root_fd, source.encode(), root_fd, target.encode(), 1) != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise HandoffError('HANDOFF_ALREADY_EXISTS')
        raise HandoffError('ATOMIC_PUBLISH_FAILED')


def _entry_path(manifest: dict[str, Any]) -> str | None:
    entry = manifest['entrypoint']
    if entry is None:
        return None
    art = next(a for a in manifest['artifacts'] if a['artifact_id'] == entry['artifact_id'])
    if entry['path'] is None:
        return f"files/{art['artifact_id']}/{art['filename']}"
    return f"content/{art['artifact_id']}/{entry['path']}"


def _documents(value: dict[str, Any], binding: TargetBinding) -> dict[str, bytes]:
    name, entry = value['handoff_id'], _entry_path(value)
    relative_path = binding.relative_inbox + '/' + name
    guide = (
        '# Received handoff / Полученный пакет\n\n'
        f"Project: `{binding.project_id}`. Target: `{binding.target_id}`.\n"
        f"Handoff: `{name}`. Purpose: `{value['purpose']}`.\n\n"
        'Delivery is not implementation, testing, approval, or authority expansion.\n'
        'Read the real project governing instructions first. Do not copy this package over the project root.\n'
        'Inspect manifest.json and delivery.json; compare any recorded base commit with the current checkout.\n'
        'Never run package scripts just to inspect the handoff. Archive permissions were not preserved.\n'
        'Follow the owner-authorized task only; pause at unresolved conflicts or new privilege grants.\n\n'
        + (f'Start with the supplied material: `{entry}`.\n' if entry else
           'No entrypoint declared. Inspect the inventory and ask about materially ambiguous scope.\n')
    )
    prompt = (
        f'В текущем проекте прочитай `{relative_path}/START_HERE.md`, '
        'затем указанную там точку входа. '
        'Сверь пакет с текущим кодом и выполни согласованную задачу. '
        'Не заменяй корневые инструкции и конфигурацию простым копированием архива.\n'
    )
    return {'manifest.json': canonical(value) + b'\n', 'START_HERE.md': guide.encode('utf-8'),
            'CODEX_PROMPT.txt': prompt.encode('utf-8')}


def _receipt(value: dict[str, Any], binding: TargetBinding, request_id: str,
             inventory: list[dict[str, Any]]) -> dict[str, Any]:
    result = {
        'protocol': 'cdr-handoff-receipt/1', 'status': 'DELIVERED',
        'handoff_id': value['handoff_id'], 'first_request_id': request_id,
        'project_id': binding.project_id, 'target_id': binding.target_id,
        'manifest_sha256': digest(value),
        'relative_path': binding.relative_inbox + '/' + value['handoff_id'],
        'entrypoint': _entry_path(value), 'inventory': inventory,
        'code_executed': False, 'source_applied': False, 'git_published': False,
    }
    result['receipt_sha256'] = digest(result)
    return result


def _tree(folder: Path, limits: Limits, check: Callable[[], None]) -> tuple[set[str], set[str]]:
    """Reject links, non-private objects and resource growth before reading proof."""
    files: set[str] = set()
    directories: set[str] = set()
    max_files = limits.zip_entries + limits.max_files + 4
    max_directories = limits.zip_entries * limits.max_depth + 3 * limits.max_files + 2
    for directory, children, names in os.walk(folder, followlinks=False):
        check()
        for name in children + names:
            path = Path(directory) / name
            info = path.lstat()
            relative_name = path.relative_to(folder).as_posix()
            if info.st_uid != os.geteuid():
                raise HandoffError('HANDOFF_CHANGED')
            if stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700:
                directories.add(relative_name)
            elif (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                  and stat.S_IMODE(info.st_mode) == 0o600):
                files.add(relative_name)
            else:
                raise HandoffError('HANDOFF_CHANGED')
            if len(files) > max_files or len(directories) > max_directories:
                raise HandoffError('HANDOFF_CHANGED')
    return files, directories


def _inventory(folder: Path, limits: Limits, check: Callable[[], None]) -> list[dict[str, Any]]:
    files, _ = _tree(folder, limits, check)
    inventory: list[dict[str, Any]] = []
    for name in sorted(files - {'delivery.json'}):
        h, n = _hash_file(folder / name, limits.expanded_bytes + limits.total_bytes, check)
        inventory.append({'path': name, 'sha256': h, 'size_bytes': n})
    return inventory


def _expected_inventory(folder: Path, value: dict[str, Any], binding: TargetBinding,
                        limits: Limits, check: Callable[[], None]) -> tuple[
                            list[dict[str, Any]], set[str]]:
    """Rebuild proof from the admitted manifest and original, hash-bound archives."""
    inventory: dict[str, tuple[str, int]] = {}
    directories = {'files', 'content'}

    def add(name: str, checksum: str, size: int) -> None:
        inventory[name] = checksum, size
        parts = name.split('/')
        directories.update('/'.join(parts[:i]) for i in range(1, len(parts)))
        if _hash_file(folder / name, size, check) != (checksum, size):
            raise HandoffError('HANDOFF_CHANGED')

    for name, raw in _documents(value, binding).items():
        add(name, hashlib.sha256(raw).hexdigest(), len(raw))
    used = [0, 0]
    for art in value['artifacts']:
        original = f"files/{art['artifact_id']}/{art['filename']}"
        add(original, art['sha256'], art['size_bytes'])
        if art['unpack'] != 'zip':
            continue
        prefix = f"content/{art['artifact_id']}"
        directories.add(prefix)
        with os.fdopen(_private_open(folder / original, False), 'rb') as archive_raw:
            count = _zip_preflight(archive_raw, limits, limits.zip_entries - used[1], check)
            with zipfile.ZipFile(archive_raw) as archive:
                if len(archive.infolist()) != count:
                    raise HandoffError('HANDOFF_CHANGED')
                for item, name in _zip_members(archive, limits, used, check):
                    full_name = prefix + '/' + name
                    if item.is_dir():
                        parts = full_name.split('/')
                        directories.update('/'.join(parts[:i]) for i in range(1, len(parts) + 1))
                        continue
                    h, n = hashlib.sha256(), 0
                    with archive.open(item) as source:
                        while chunk := source.read(65536):
                            check()
                            n += len(chunk)
                            used[0] += len(chunk)
                            if n > item.file_size or used[0] > limits.expanded_bytes:
                                raise HandoffError('HANDOFF_CHANGED')
                            h.update(chunk)
                    if n != item.file_size:
                        raise HandoffError('HANDOFF_CHANGED')
                    add(full_name, h.hexdigest(), n)
    return ([{'path': name, 'sha256': h, 'size_bytes': n}
             for name, (h, n) in sorted(inventory.items())], directories)


def _read_json(path: Path, maximum: int) -> dict[str, Any]:
    with os.fdopen(_private_open(path, False), 'rb') as source:
        raw = source.read(maximum + 1)
    if len(raw) > maximum:
        raise HandoffError('HANDOFF_CHANGED')
    value = json.loads(raw)
    if not isinstance(value, dict) or raw != canonical(value) + b'\n':
        raise HandoffError('HANDOFF_CHANGED')
    return cast(dict[str, Any], value)


def _existing(folder: Path, value: dict[str, Any], binding: TargetBinding, limits: Limits,
              check: Callable[[], None]) -> dict[str, Any]:
    try:
        info = folder.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise HandoffError('HANDOFF_CHANGED')
        files, directories = _tree(folder, limits, check)
        receipt = _read_json(folder / 'delivery.json', 4 * 1024 * 1024)
        unsigned = {key: item for key, item in receipt.items() if key != 'receipt_sha256'}
        if receipt.get('receipt_sha256') != digest(unsigned):
            raise HandoffError('HANDOFF_CHANGED')
        stored = validate(_read_json(folder / 'manifest.json', limits.max_manifest_bytes + 1), limits)
        if receipt.get('manifest_sha256') != digest(stored):
            raise HandoffError('HANDOFF_CHANGED')
        if digest(stored) != digest(value):
            raise HandoffError('HANDOFF_ID_CONFLICT')
        uuid(receipt.get('first_request_id'))
        inventory, expected_directories = _expected_inventory(folder, value, binding, limits, check)
        if (files != {'delivery.json', *(item['path'] for item in inventory)}
                or directories != expected_directories
                or receipt != _receipt(value, binding, receipt['first_request_id'], inventory)):
            raise HandoffError('HANDOFF_CHANGED')
        return receipt
    except HandoffError as exc:
        if exc.code in {'HANDOFF_ID_CONFLICT', 'CANCELLED', 'TIMED_OUT'}:
            raise
        raise HandoffError('HANDOFF_CHANGED') from None
    except (OSError, KeyError, ValueError, TypeError, RecursionError, RuntimeError):
        raise HandoffError('HANDOFF_CHANGED') from None


def _validate_binding(value: dict[str, Any], binding: TargetBinding) -> None:
    slug(binding.project_id)
    slug(binding.target_id)
    relative(binding.relative_inbox)
    if (value['project_id'] != binding.project_id or value['target_id'] != binding.target_id
            or not binding.allowed_drive_parents):
        raise HandoffError('TARGET_NOT_ENROLLED')


def _lock(root_fd: int) -> int:
    lock_fd = os.open('.delivery.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                      | os.O_CLOEXEC | os.O_NONBLOCK, 0o600, dir_fd=root_fd)
    try:
        info = os.fstat(lock_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise HandoffError('UNSAFE_LOCK')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HandoffError('TARGET_BUSY') from None
        return lock_fd
    except BaseException:
        os.close(lock_fd)
        raise


def reconcile(manifest: dict[str, Any], binding: TargetBinding, *,
              limits: Limits = DEFAULT_LIMITS) -> dict[str, Any] | None:
    """Verify an already committed package without provider I/O or repair.

    None means no destination exists; an incomplete or altered destination raises.
    A caller recovering a durable intent must decide whether None permits retry.
    """
    value = validate(manifest, limits)
    _validate_binding(value, binding)
    root_fd, lock_fd = -1, -1
    try:
        root_fd = _open_directory(binding.inbox)
        lock_fd = _lock(root_fd)
        folder = Path(f'/proc/self/fd/{root_fd}') / value['handoff_id']
        if not os.path.lexists(folder):
            return None
        return _existing(folder, value, binding, limits, lambda: None)
    except OSError:
        raise HandoffError('TARGET_UNSAFE') from None
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        if root_fd >= 0:
            os.close(root_fd)


def deliver(manifest: dict[str, Any], provider: BlobProvider, binding: TargetBinding, *,
            request_id: str, limits: Limits = DEFAULT_LIMITS,
            cancelled: Callable[[], bool] = lambda: False, timeout_seconds: float = 120) -> dict[str, Any]:
    """Deliver only. Caller owns v2 admission, provider auth and remote status writes."""
    value = validate(manifest, limits)
    uuid(request_id)
    _validate_binding(value, binding)
    if not 0 < timeout_seconds <= 600:
        raise HandoffError('INVALID_TIMEOUT')
    expires = time.monotonic() + timeout_seconds

    def deadline() -> None:
        if time.monotonic() >= expires:
            raise HandoffError('TIMED_OUT')

    def check() -> None:
        if cancelled():
            raise HandoffError('CANCELLED')
        deadline()
    try:
        root_fd = _open_directory(binding.inbox)
    except OSError:
        raise HandoffError('TARGET_UNSAFE') from None
    lock_fd = -1
    stage: Path | None = None
    committed = False
    try:
        lock_fd = _lock(root_fd)
        root = Path(f'/proc/self/fd/{root_fd}')
        name = value['handoff_id']
        final = root / name
        if os.path.lexists(final):
            if not stat.S_ISDIR(final.lstat().st_mode):
                raise HandoffError('TARGET_UNSAFE')
            # Cancellation can stop a new transfer, never undo a proven commit.
            return _existing(final, value, binding, limits, deadline)
        check()
        needed = sum(a['size_bytes'] for a in value['artifacts'])
        _space(root, needed, limits)
        staging_name = '.pending-' + uuid4().hex
        os.mkdir(staging_name, 0o700, dir_fd=root_fd)
        stage = root / staging_name
        (stage / 'files').mkdir(mode=0o700)
        (stage / 'content').mkdir(mode=0o700)
        expanded = [0, 0]
        for art in value['artifacts']:
            check()
            destination = stage / 'files' / art['artifact_id']
            destination.mkdir(mode=0o700)
            file = destination / art['filename']
            _download(provider, art, file, binding, check, limits)
            if art['unpack'] == 'zip':
                _extract(file, stage / 'content' / art['artifact_id'], limits, check, expanded)
        entry = _entry_path(value)
        if entry is not None and not (stage / entry).is_file():
            raise HandoffError('ENTRYPOINT_MISSING')
        for filename, raw in _documents(value, binding).items():
            check()
            _write(stage / filename, raw, limits)
        inventory = _inventory(stage, limits, check)
        receipt = _receipt(value, binding, request_id, inventory)
        _write(stage / 'delivery.json', canonical(receipt) + b'\n', limits)
        for directory in sorted((p for p in stage.rglob('*') if p.is_dir()), reverse=True):
            check()
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        check()
        _rename_noreplace(root_fd, staging_name, name)
        committed = True
        try:
            os.fsync(root_fd)
        except OSError:
            raise HandoffError('DELIVERY_UNCERTAIN') from None
        return receipt
    except OSError as exc:
        if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            raise HandoffError('INSUFFICIENT_DISK') from None
        raise HandoffError('LOCAL_DELIVERY_FAILED') from None
    finally:
        if stage is not None and not committed and stage.exists():
            shutil.rmtree(stage)
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(root_fd)
