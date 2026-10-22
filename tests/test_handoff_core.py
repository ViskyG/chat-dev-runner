"""Focused production-port boundaries; the reference kit remains unchanged."""
from __future__ import annotations

import copy
import errno
import hashlib
import io
import os
import stat
import struct
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from runner_app.handoff import (
    HandoffError,
    Limits,
    TargetBinding,
    deliver,
    delivery,
    parse,
    reconcile,
)
from runner_app.handoff.contracts import canonical, digest


def zipped(entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return result.getvalue()


class FakeProvider:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.reads = 0
        self.downloads = 0
        self.override: dict[str, Any] = {}
        self.after: dict[str, Any] = {}

    def metadata(self, file_id: str) -> dict[str, Any]:
        self.reads += 1
        return {
            'id': file_id, 'version': '1', 'size': str(len(self.raw)),
            'parents': ['allowed'], 'trashed': False, 'mimeType': 'application/zip',
            'capabilities': {'canDownload': True}, **self.override,
            **(self.after if self.reads > 1 else {}),
        }

    def chunks(self, file_id: str) -> Any:
        self.downloads += 1
        for start in range(0, len(self.raw), 31):
            yield self.raw[start:start + 31]


def manifest(raw: bytes) -> dict[str, Any]:
    return {
        'protocol': 'cdr-handoff/1', 'handoff_id': str(uuid4()),
        'project_id': 'demo', 'target_id': 'editor', 'purpose': 'roadmap',
        'title': 'Roadmap handoff', 'base_commit': None,
        'entrypoint': {'artifact_id': 'plan', 'path': 'roadmap/START.md'},
        'artifacts': [{
            'artifact_id': 'plan', 'drive_file_id': 'drive-file', 'drive_version': '1',
            'filename': 'roadmap.zip', 'size_bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(), 'media_type': 'application/zip',
            'unpack': 'zip',
        }],
    }


@pytest.fixture
def sample(tmp_path: Path) -> tuple[dict[str, Any], FakeProvider, TargetBinding]:
    raw = zipped([('roadmap/START.md', b'# Next steps\n'), ('roadmap/data.bin', b'\x00\xff')])
    inbox = tmp_path / 'project' / '.runner' / 'handoffs'
    inbox.mkdir(parents=True, mode=0o700)
    return manifest(raw), FakeProvider(raw), TargetBinding(
        'demo', 'editor', inbox, frozenset({'allowed', 'also-allowed'}))


def run(sample: tuple[dict[str, Any], FakeProvider, TargetBinding], **kwargs: Any) -> dict[str, Any]:
    value, provider, binding = sample
    return deliver(value, provider, binding, request_id=str(uuid4()), **kwargs)


def install_blob(sample: tuple[dict[str, Any], FakeProvider, TargetBinding], raw: bytes) -> None:
    value, provider, _ = sample
    provider.raw = raw
    value['artifacts'][0].update(size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    value['entrypoint'] = None


def write_receipt(folder: Path, receipt: dict[str, Any]) -> None:
    unsigned = {key: value for key, value in receipt.items() if key != 'receipt_sha256'}
    receipt['receipt_sha256'] = digest(unsigned)
    (folder / 'delivery.json').write_bytes(canonical(receipt) + b'\n')


def test_transfer_is_private_inert_and_reconciles_without_provider(sample: Any) -> None:
    value, provider, binding = sample
    assert reconcile(value, binding) is None
    receipt = run(sample)
    folder = binding.inbox / value['handoff_id']
    assert (folder / 'files/plan/roadmap.zip').read_bytes() == provider.raw
    assert (folder / receipt['entrypoint']).read_bytes() == b'# Next steps\n'
    assert receipt['status'] == 'DELIVERED'
    assert receipt['code_executed'] is receipt['source_applied'] is receipt['git_published'] is False
    assert binding.relative_inbox in (folder / 'CODEX_PROMPT.txt').read_text()
    assert all(stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
               for path in folder.rglob('*'))
    assert not list(binding.inbox.glob('.pending-*'))
    before = provider.reads, provider.downloads
    provider.raw = b'now unavailable'
    assert reconcile(value, binding) == receipt
    assert run(sample) == receipt
    assert (provider.reads, provider.downloads) == before


def test_same_id_other_manifest_is_conflict(sample: Any) -> None:
    run(sample)
    sample[0]['title'] = 'A different instruction'
    with pytest.raises(HandoffError, match='HANDOFF_ID_CONFLICT'):
        run(sample)
    assert sample[1].downloads == 1


def test_cancelled_replay_returns_existing_commit(sample: Any) -> None:
    receipt = run(sample)
    assert run(sample, cancelled=lambda: True) == receipt
    assert sample[1].downloads == 1


@pytest.mark.parametrize('part', ['manifest', 'receipt', 'inventory', 'guide', 'content'])
def test_existing_proof_cannot_be_silently_repaired(sample: Any, part: str) -> None:
    receipt = run(sample)
    folder = sample[2].inbox / sample[0]['handoff_id']
    if part == 'manifest':
        changed = copy.deepcopy(sample[0])
        changed['title'] = 'Altered after delivery'
        (folder / 'manifest.json').write_bytes(canonical(changed) + b'\n')
    elif part in {'receipt', 'inventory'}:
        changed = copy.deepcopy(receipt)
        if part == 'receipt':
            changed['first_request_id'] = str(uuid4())
        else:
            changed['inventory'].pop()
        (folder / 'delivery.json').write_bytes(canonical(changed) + b'\n')
    else:
        name = 'START_HERE.md' if part == 'guide' else receipt['entrypoint']
        (folder / name).write_bytes(b'changed by owner')
    with pytest.raises(HandoffError, match='HANDOFF_CHANGED'):
        reconcile(sample[0], sample[2])
    assert sample[1].downloads == 1


def test_rehashed_modified_inventory_is_checked_against_original_archive(sample: Any) -> None:
    receipt = run(sample)
    folder = sample[2].inbox / sample[0]['handoff_id']
    changed = b'replacement content'
    (folder / receipt['entrypoint']).write_bytes(changed)
    for item in receipt['inventory']:
        if item['path'] == receipt['entrypoint']:
            item.update(sha256=hashlib.sha256(changed).hexdigest(), size_bytes=len(changed))
    write_receipt(folder, receipt)
    with pytest.raises(HandoffError, match='HANDOFF_CHANGED'):
        reconcile(sample[0], sample[2])


@pytest.mark.parametrize('extra', ['file', 'empty-directory', 'symlink', 'hardlink', 'fifo'])
def test_existing_inventory_rejects_added_or_special_objects(sample: Any, extra: str) -> None:
    run(sample)
    folder = sample[2].inbox / sample[0]['handoff_id']
    new = folder / 'unexpected'
    if extra == 'file':
        new.write_bytes(b'extra')
    elif extra == 'empty-directory':
        new.mkdir(mode=0o700)
    elif extra == 'symlink':
        new.symlink_to('manifest.json')
    elif extra == 'hardlink':
        os.link(folder / 'manifest.json', new)
    else:
        os.mkfifo(new, 0o600)
    with pytest.raises(HandoffError, match='HANDOFF_CHANGED'):
        reconcile(sample[0], sample[2])


@pytest.mark.parametrize('after', [{'version': '2'}, {'parents': ['also-allowed']}])
def test_download_rejects_version_or_even_allowed_parent_change(sample: Any,
                                                               after: dict[str, Any]) -> None:
    sample[1].after = after
    with pytest.raises(HandoffError, match='DRIVE_BINDING_MISMATCH'):
        run(sample)
    assert not (sample[2].inbox / sample[0]['handoff_id']).exists()
    assert not list(sample[2].inbox.glob('.pending-*'))


@pytest.mark.parametrize('patch', [
    {'mimeType': 'application/vnd.google-apps.document'}, {'parents': ['foreign']},
    {'shortcutDetails': {}}, {'size': True}, {'capabilities': {'canDownload': False}},
])
def test_metadata_refusal_has_no_download(sample: Any, patch: dict[str, Any]) -> None:
    sample[1].override = patch
    with pytest.raises(HandoffError):
        run(sample)
    assert sample[1].downloads == 0


def test_cancellation_before_and_immediately_after_metadata(sample: Any,
                                                          monkeypatch: Any) -> None:
    with pytest.raises(HandoffError, match='CANCELLED'):
        run(sample, cancelled=lambda: True)
    assert sample[1].reads == 0
    real_metadata = sample[1].metadata
    cancelled = [False]

    def metadata(file_id: str) -> dict[str, Any]:
        result = real_metadata(file_id)
        cancelled[0] = True
        return result

    monkeypatch.setattr(sample[1], 'metadata', metadata)
    with pytest.raises(HandoffError, match='CANCELLED'):
        run(sample, cancelled=lambda: cancelled[0])
    assert sample[1].downloads == 0
    assert not list(sample[2].inbox.glob('.pending-*'))


def test_inventory_hashing_remains_cancellable(sample: Any, monkeypatch: Any) -> None:
    real_inventory = delivery._inventory
    hashing = [False]
    calls = [0]

    def inventory(*args: Any) -> Any:
        hashing[0] = True
        return real_inventory(*args)

    def cancelled() -> bool:
        if hashing[0]:
            calls[0] += 1
        # The first calls walk the tree; subsequent calls hash its files.
        return calls[0] > 7

    monkeypatch.setattr(delivery, '_inventory', inventory)
    with pytest.raises(HandoffError, match='CANCELLED'):
        run(sample, cancelled=cancelled)
    assert not (sample[2].inbox / sample[0]['handoff_id']).exists()
    assert not list(sample[2].inbox.glob('.pending-*'))


def test_cancellation_after_atomic_commit_is_delivered(sample: Any, monkeypatch: Any) -> None:
    real_rename = delivery._rename_noreplace
    cancelled = [False]

    def rename(*args: Any) -> None:
        real_rename(*args)
        cancelled[0] = True

    monkeypatch.setattr(delivery, '_rename_noreplace', rename)
    receipt = run(sample, cancelled=lambda: cancelled[0])
    assert receipt['status'] == 'DELIVERED'
    assert reconcile(sample[0], sample[2]) == receipt


def test_post_commit_fsync_uncertainty_preserves_reconcilable_package(sample: Any,
                                                                   monkeypatch: Any) -> None:
    real_rename, real_fsync = delivery._rename_noreplace, delivery.os.fsync
    committed = [False]

    def rename(*args: Any) -> None:
        real_rename(*args)
        committed[0] = True

    def fsync(fd: int) -> None:
        if committed[0]:
            raise OSError(errno.EIO, 'injected durability failure')
        real_fsync(fd)

    monkeypatch.setattr(delivery, '_rename_noreplace', rename)
    monkeypatch.setattr(delivery.os, 'fsync', fsync)
    with pytest.raises(HandoffError, match='DELIVERY_UNCERTAIN'):
        run(sample)
    receipt = reconcile(sample[0], sample[2])
    assert receipt is not None and receipt['status'] == 'DELIVERED'
    assert sample[1].downloads == 1


def test_deadline_is_checked_after_blocking_metadata_returns(sample: Any, monkeypatch: Any) -> None:
    real_metadata = sample[1].metadata
    now = [0.0]
    monkeypatch.setattr(delivery.time, 'monotonic', lambda: now[0])

    def metadata(file_id: str) -> dict[str, Any]:
        result = real_metadata(file_id)
        now[0] = 20.0
        return result

    monkeypatch.setattr(sample[1], 'metadata', metadata)
    with pytest.raises(HandoffError, match='TIMED_OUT'):
        run(sample, timeout_seconds=10)
    assert sample[1].downloads == 0


def test_blocking_provider_timeout_is_terminal_and_cleans_stage(sample: Any,
                                                              monkeypatch: Any) -> None:
    def metadata(file_id: str) -> dict[str, Any]:
        raise TimeoutError('private provider details')

    monkeypatch.setattr(sample[1], 'metadata', metadata)
    with pytest.raises(HandoffError, match='TIMED_OUT'):
        run(sample)
    assert not list(sample[2].inbox.glob('.pending-*'))


@pytest.mark.parametrize('kind', ['directory-bytes', 'entries', 'lying-entry-count'])
def test_zip_resources_are_bounded_before_zipfile_allocates(sample: Any, monkeypatch: Any,
                                                          kind: str) -> None:
    raw = zipped([('one.txt', b'1'), ('two.txt', b'2')])
    limits = Limits()
    if kind == 'directory-bytes':
        limits = Limits(zip_central_directory_bytes=32)
        code = 'ZIP_DIRECTORY_LIMIT'
    elif kind == 'entries':
        limits = Limits(zip_entries=1)
        code = 'ZIP_ENTRY_LIMIT'
    else:
        mutable = bytearray(raw)
        end = mutable.rfind(b'PK\x05\x06')
        struct.pack_into('<2H', mutable, end + 8, 1, 1)
        raw = bytes(mutable)
        limits = Limits(zip_entries=1)
        code = 'ZIP_ENTRY_LIMIT'
    install_blob(sample, raw)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError('ZipFile allocated before the resource gate')

    monkeypatch.setattr(delivery.zipfile, 'ZipFile', forbidden)
    with pytest.raises(HandoffError, match=code):
        run(sample, limits=limits)
    assert not list(sample[2].inbox.glob('.pending-*'))


@pytest.mark.parametrize('entries,code', [
    ([('../outside', b'x')], 'UNSAFE_PATH'),
    ([('A.txt', b'a'), ('a.txt', b'b')], 'ZIP_DUPLICATE_PATH'),
    ([('a', b'a'), ('a/b', b'b')], 'ZIP_PATH_CONFLICT'),
    ([('A/x', b'a'), ('a/y', b'b')], 'ZIP_CASE_COLLISION'),
])
def test_archive_path_attacks(sample: Any, entries: Any, code: str) -> None:
    install_blob(sample, zipped(entries))
    with pytest.raises(HandoffError, match=code):
        run(sample)


@pytest.mark.parametrize('kind', [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK])
def test_archive_special_objects_rejected(sample: Any, kind: int) -> None:
    info = zipfile.ZipInfo('special')
    info.create_system = 3
    info.external_attr = (kind | 0o777) << 16
    install_blob(sample, zipped([(info, b'payload')]))
    with pytest.raises(HandoffError, match='ZIP_UNSUPPORTED_ENTRY'):
        run(sample)


def test_archive_link_extra_field_rejected(sample: Any) -> None:
    info = zipfile.ZipInfo('hardlink')
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.extra = struct.pack('<2H', 0x000d, 7) + b'target!'
    install_blob(sample, zipped([(info, b'payload')]))
    with pytest.raises(HandoffError, match='ZIP_UNSUPPORTED_ENTRY'):
        run(sample)


@pytest.mark.parametrize('limits', [Limits(expanded_bytes=1), Limits(zip_ratio=1)])
def test_expansion_limits_clean_stage(sample: Any, limits: Limits) -> None:
    install_blob(sample, zipped([('compressible.txt', b'x' * 1024)]))
    with pytest.raises(HandoffError, match='ZIP_EXPANSION_LIMIT'):
        run(sample, limits=limits)
    assert not list(sample[2].inbox.glob('.pending-*'))


def test_free_space_rechecked_during_expansion(sample: Any, monkeypatch: Any) -> None:
    real_statvfs = delivery.os.statvfs
    expanded_calls = [0]

    def statvfs(path: Any) -> Any:
        if '/content/plan' in str(path):
            expanded_calls[0] += 1
            if expanded_calls[0] > 1:
                return SimpleNamespace(f_bavail=0, f_frsize=1)
        return real_statvfs(path)

    monkeypatch.setattr(delivery.os, 'statvfs', statvfs)
    with pytest.raises(HandoffError, match='INSUFFICIENT_DISK'):
        run(sample)
    assert expanded_calls[0] == 2
    assert not (sample[2].inbox / sample[0]['handoff_id']).exists()
    assert not list(sample[2].inbox.glob('.pending-*'))


def test_nonprivate_or_symlink_inbox_has_no_provider_effect(sample: Any, tmp_path: Path) -> None:
    value, provider, binding = sample
    binding.inbox.chmod(0o755)
    with pytest.raises(HandoffError, match='TARGET_NOT_PRIVATE'):
        run(sample)
    binding.inbox.chmod(0o700)
    link = tmp_path / 'alias'
    link.symlink_to(binding.inbox, target_is_directory=True)
    alias = TargetBinding(binding.project_id, binding.target_id, link, binding.allowed_drive_parents)
    with pytest.raises(HandoffError, match='TARGET_UNSAFE'):
        deliver(value, provider, alias, request_id=str(uuid4()))
    assert provider.reads == 0


def test_no_replace_atomic_rename_preserves_raced_empty_destination(tmp_path: Path) -> None:
    (tmp_path / 'stage').mkdir()
    (tmp_path / 'final').mkdir()
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(HandoffError, match='HANDOFF_ALREADY_EXISTS'):
            delivery._rename_noreplace(descriptor, 'stage', 'final')
    finally:
        os.close(descriptor)
    assert (tmp_path / 'stage').is_dir() and (tmp_path / 'final').is_dir()


def test_duplicate_manifest_keys_and_limits_rejected() -> None:
    with pytest.raises(HandoffError, match='DUPLICATE_JSON_KEY'):
        parse(b'{"protocol":"first","protocol":"second"}')
    with pytest.raises(HandoffError, match='INVALID_LIMITS'):
        Limits(zip_entries=0)
