"""Offline assembled delivery, recovery and populated migration boundaries."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import sqlite3
import stat
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from test_intake_v2 import Port, accept, setup
from test_v2_runtime import configuration
from test_v2_source import Fixture as SourceFixture

import runner_app.storage.database as database_module
from runner_app.config_v2 import parse_v2_config
from runner_app.engine.handoff_v2 import HandoffRunner, V2TaskDispatcher
from runner_app.engine.intake_v2 import IntakeCoordinator
from runner_app.engine.queue_v2 import QueueWorker
from runner_app.git.argv import run_git, run_git_bytes
from runner_app.git.changes import ChangeError, safe_source_path
from runner_app.git.source_v2 import LocalWorkspace
from runner_app.handoff.contracts import digest
from runner_app.handoff.delivery import TargetBinding, deliver
from runner_app.storage.database import Database
from runner_app.storage.intake_v2 import IntakeStore
from runner_app.storage.paths import Paths, ensure_private_directory


class NoNativeExecution:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.publications: list[str] = []

    def __call__(self, operation: dict[str, Any], cancel: threading.Event) -> Any:
        self.calls.append(operation['request_id'])
        raise AssertionError('handoff reached native task execution')

    def publish_state(self, request_id: str) -> None:
        self.publications.append(request_id)
        raise AssertionError('handoff reached native task publication')


class FakeHandoffProvider:
    def __init__(self, blobs: dict[str, tuple[bytes, str]]) -> None:
        self.blobs = blobs
        self.reads: list[str] = []
        self.downloads: list[str] = []

    def metadata(self, file_id: str) -> dict[str, Any]:
        self.reads.append(file_id)
        raw, media_type = self.blobs[file_id]
        return {'id': file_id, 'version': '1', 'size': str(len(raw)),
                'sha256Checksum': hashlib.sha256(raw).hexdigest(),
                'parents': ['allowed-folder'], 'trashed': False, 'mimeType': media_type,
                'capabilities': {'canDownload': True}}

    def chunks(self, file_id: str) -> Any:
        self.downloads.append(file_id)
        raw, _ = self.blobs[file_id]
        for offset in range(0, len(raw), 29):
            yield raw[offset:offset + 29]


class FakeHandoffFactory:
    def __init__(self, blobs: dict[str, tuple[bytes, str]]) -> None:
        self.provider = FakeHandoffProvider(blobs)
        self.calls: list[Any] = []
        self.publisher_calls: list[Any] = []
        self.states: list[dict[str, Any]] = []
        self.publication = {'status': 'CONFIRMED', 'code': 'DELIVERY_RESULT_CONFIRMED'}

    def __call__(self, *args: Any, **kwargs: Any) -> FakeHandoffProvider:
        self.calls.append((args, kwargs))
        return self.provider

    def publisher(self, *args: Any, **kwargs: Any) -> FakeHandoffFactory:
        self.publisher_calls.append((args, kwargs))
        return self

    def publish(self, state: dict[str, Any]) -> dict[str, Any]:
        self.states.append(copy.deepcopy(state))
        return dict(self.publication)


def source_fingerprint(workspace: Path) -> dict[str, Any]:
    names = run_git_bytes(workspace, ['ls-files', '-z']).decode().split('\0')
    return {
        'head': run_git_bytes(workspace, ['rev-parse', 'HEAD']),
        'index': (workspace / '.git/index').read_bytes(),
        'tracked': {name: (workspace / name).read_bytes() for name in names if name},
        'exclude': (workspace / '.git/info/exclude').read_bytes(),
        'config': (workspace / '.git/config').read_bytes(),
    }


def assembly(tmp_path: Path, *, enrolled: bool = True,
             workspace: Path | None = None) -> SimpleNamespace:
    if workspace is None:
        workspace = tmp_path / 'workspace'
        workspace.mkdir()
        run_git(workspace, ['init', '-q'])
        run_git(workspace, ['config', 'user.name', 'Handoff integration'])
        run_git(workspace, ['config', 'user.email', 'handoff@example.invalid'])
        source = workspace / 'source.txt'
        source.write_bytes(b'committed source\n')
        (workspace / '.gitignore').write_bytes(b'# owner policy\n')
        run_git(workspace, ['add', 'source.txt', '.gitignore'])
        run_git(workspace, ['commit', '-qm', 'baseline'])
        source.write_bytes(b'staged owner edit\n')
        run_git(workspace, ['add', 'source.txt'])
        source.write_bytes(b'unstaged owner edit\n')
        (workspace / 'owner-untracked.txt').write_bytes(b'untracked sentinel\n')
    paths = Paths(tmp_path / 'config', tmp_path / 'state', tmp_path / 'cache')
    paths.ensure_private()
    config_value = configuration()
    config_value['projects'][0]['workspaces'] = {'editor': {'path': str(workspace)}}
    if enrolled:
        config_value['projects'][0]['handoff_targets'] = {
            'inbox': {'workspace_id': 'editor', 'allowed_drive_parents': ['allowed-folder'],
                      'result_document_id': 'handoff-result-document', 'result_tab_id': 't.result'}}
    else:
        # This is a valid GitHub-only project with no enrolled local handoff target.
        config_value['projects'][0].pop('workspaces')
    config = parse_v2_config(json.dumps(config_value).encode())
    marker = tmp_path / 'MUST_NOT_BE_CREATED'
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('roadmap/START.md', b'# Approved next steps\n')
        script = zipfile.ZipInfo('scripts/install.sh')
        script.create_system = 3
        script.external_attr = (stat.S_IFREG | 0o755) << 16
        archive.writestr(script, f'#!/bin/sh\ntouch {marker}\n'.encode())
    blobs = {'zip-file': (archive_bytes.getvalue(), 'application/zip'),
             'notes-file': (b'Extra design notes\n', 'text/plain')}
    value: dict[str, Any] = {
        'protocol': 'cdr-handoff/1', 'handoff_id': str(uuid4()),
        'project_id': 'project-1', 'target_id': 'inbox', 'purpose': 'roadmap',
        'title': 'An inert project handoff', 'base_commit': None,
        'entrypoint': {'artifact_id': 'roadmap', 'path': 'roadmap/START.md'},
        'artifacts': [
            {'artifact_id': artifact_id, 'drive_file_id': file_id, 'drive_version': '1',
             'filename': name, 'size_bytes': len(blobs[file_id][0]),
             'sha256': hashlib.sha256(blobs[file_id][0]).hexdigest(),
             'media_type': blobs[file_id][1], 'unpack': unpack}
            for artifact_id, file_id, name, unpack in [
                ('roadmap', 'zip-file', 'roadmap.zip', 'zip'),
                ('notes', 'notes-file', 'notes.txt', 'none')]],
    }
    store, session, _, _ = setup(tmp_path)
    binding = 'docs-handoff-slot'
    coordinates = {'document_id': 'existing-document', 'tab_id': 't.existing'}
    store.bind(binding, runner_id='runner-1', project_id='project-1', provider='drive',
               coordinates=coordinates, session_id=session)
    port = Port()
    port.value['request'] = {'request_id': None, 'action': 'DELIVER_HANDOFF', 'handoff': value}
    factory, native = FakeHandoffFactory(blobs), NoNativeExecution()
    handoff = HandoffRunner(store, config, paths, provider_factory=factory,
                            protected_document_ids=frozenset({'existing-document'}))
    dispatcher = V2TaskDispatcher(native, handoff)
    coordinator = IntakeCoordinator(store, session, terminal_observer=dispatcher.publish_state)
    worker = QueueWorker(store, session, dispatcher, terminal_observer=dispatcher.publish_state)
    return SimpleNamespace(store=store, session=session, config=config, paths=paths,
                           workspace=workspace, marker=marker, blobs=blobs, manifest=value,
                           port=port, binding=binding, factory=factory, native=native,
                           handoff=handoff, dispatcher=dispatcher, coordinator=coordinator,
                           worker=worker)


def baseline(a: SimpleNamespace) -> None:
    assert a.coordinator.poll_once(a.binding, a.port).kind == 'BASELINED'


def submit(a: SimpleNamespace, request: dict[str, Any] | None = None) -> str:
    if request is not None:
        a.port.value['request'] = request
    a.port.ready()
    observed = a.coordinator.poll_once(a.binding, a.port)
    assert observed.kind == 'ACCEPTED', observed
    assert observed.request_id is not None
    return observed.request_id


def control(a: SimpleNamespace, action: str, target: str) -> str:
    return submit(a, {'request_id': None, 'action': action, 'target_request_id': target})


def journal(store: IntakeStore, request_id: str) -> list[str]:
    with store._connection() as connection:
        return [row[0] for row in connection.execute(
            'SELECT event FROM v2_journal WHERE request_id=? ORDER BY event_id', (request_id,))]


@pytest.mark.parametrize('recover', [False, True])
def test_legacy_delivery_keeps_receipt_and_prompt_without_download(
        tmp_path: Path, recover: bool) -> None:
    a = assembly(tmp_path)
    baseline(a)
    request_id = submit(a)
    legacy = a.workspace / '.runner/handoffs'
    ensure_private_directory(legacy)
    binding = TargetBinding('project-1', 'inbox', legacy, frozenset({'allowed-folder'}),
                            relative_inbox='.runner/handoffs')
    receipt = deliver(a.manifest, a.factory.provider, binding, request_id=request_id)
    folder = legacy / a.manifest['handoff_id']
    original = {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    downloads = list(a.factory.provider.downloads)
    if recover:
        a.store.claim_next(a.session)
        a.store.finish(request_id, status='FAILED', code='INTERRUPTED')
        operation = a.handoff._recover(a.store.get(request_id))
    else:
        assert a.worker.run_once()
        operation = a.store.get(request_id)
    assert operation['status'] == 'COMPLETED'
    assert operation['result']['handoff']['safe_relative_handoff_path'] == receipt['relative_path']
    assert '.runner/handoffs/' in operation['result']['codex_prompt']
    assert a.factory.calls == [] and a.factory.provider.downloads == downloads
    assert not (a.workspace / 'handoffs').exists()
    assert original == {str(p.relative_to(folder)): p.read_bytes()
                        for p in folder.rglob('*') if p.is_file()}


@pytest.mark.parametrize('unsafe', ['tracked', 'symlink', 'duplicate', 'legacy-symlink'])
def test_visible_inbox_does_not_bypass_existing_safety(tmp_path: Path, unsafe: str) -> None:
    a = assembly(tmp_path)
    visible = a.workspace / 'handoffs'
    legacy = a.workspace / '.runner/handoffs'
    if unsafe == 'tracked':
        visible.mkdir()
        (visible / 'owner.txt').write_text('owner file')
        run_git(a.workspace, ['add', 'handoffs/owner.txt'])
    elif unsafe == 'symlink':
        visible.symlink_to(tmp_path, target_is_directory=True)
    elif unsafe == 'legacy-symlink':
        ensure_private_directory(legacy)
        (legacy / a.manifest['handoff_id']).symlink_to(tmp_path, target_is_directory=True)
    else:
        for root in (visible, legacy):
            (root / a.manifest['handoff_id']).mkdir(parents=True)
    before = source_fingerprint(a.workspace)
    baseline(a)
    request_id = submit(a)
    assert a.worker.run_once()
    operation = a.store.get(request_id)
    assert operation['status'] == 'FAILED'
    assert operation['code'] == {'tracked': 'HANDOFF_INBOX_TRACKED',
                                 'symlink': 'TARGET_UNSAFE',
                                 'duplicate': 'HANDOFF_ID_CONFLICT',
                                 'legacy-symlink': 'HANDOFF_CHANGED'}[unsafe]
    assert a.factory.provider.downloads == []
    assert source_fingerprint(a.workspace) == before


@pytest.mark.parametrize('path', ['handoffs', 'handoffs/file.zip', 'HANDOFFS/file.zip',
                                '.runner/handoffs/file.zip'])
def test_both_inboxes_are_protected_from_source_changes(path: str) -> None:
    with pytest.raises(ChangeError, match='separate from project source'):
        safe_source_path(path)


@pytest.mark.parametrize('versions', ['explicit', 'omitted', 'mixed'])
def test_draft_ready_ack_worker_receipt_is_delivery_only_and_preserves_git(
        tmp_path: Path, versions: str) -> None:
    a = assembly(tmp_path)
    if versions != 'explicit':
        for artifact in a.manifest['artifacts'][:1 if versions == 'mixed' else None]:
            artifact.pop('drive_version')
    before = source_fingerprint(a.workspace)
    baseline(a)
    assert a.store.list_requests() == []
    assert a.factory.calls == a.factory.publisher_calls == []
    consumed_token = a.port.value['slot_token']
    request_id = submit(a)
    assert a.port.value['dispatch'] == 'ACCEPTED'
    assert a.port.value['receipt']['request_id'] == request_id
    assert a.port.value['receipt']['accepted_slot_token'] == consumed_token
    assert a.port.value['slot_token'] != consumed_token
    assert a.store.get(request_id)['status'] == 'QUEUED'
    assert a.factory.calls == a.factory.states == []
    ack = copy.deepcopy(a.port.value)
    assert a.worker.run_once()
    operation = a.store.get(request_id)
    result = operation['result']
    assert operation['status'] == 'COMPLETED'
    assert result['handoff']['local_delivery_status'] == 'DELIVERED'
    assert result['handoff']['safe_relative_handoff_path'] == 'handoffs/' + a.manifest['handoff_id']
    assert result['handoff']['files_count'] == 2
    assert result['handoff']['manifest_sha256'] == digest(a.manifest)
    assert result['handoff']['inbox_visible_in_git'] is True
    assert result['receipt_publication']['status'] == 'CONFIRMED'
    assert a.port.value == ack, 'terminal receipt must not overwrite the command ACK'
    folder = a.workspace / result['handoff']['safe_relative_handoff_path']
    assert (folder / 'files/roadmap/roadmap.zip').read_bytes() == a.blobs['zip-file'][0]
    assert (folder / 'files/notes/notes.txt').read_bytes() == a.blobs['notes-file'][0]
    assert (folder / 'content/roadmap/roadmap/START.md').read_bytes() == b'# Approved next steps\n'
    assert not ((folder / 'content/roadmap/scripts/install.sh').stat().st_mode & 0o111)
    assert not a.marker.exists()
    assert (folder / 'CODEX_PROMPT.txt').is_file() and (folder / 'START_HERE.md').is_file()
    receipt = json.loads((folder / 'delivery.json').read_bytes())
    assert receipt['first_request_id'] == request_id
    assert receipt['code_executed'] is receipt['source_applied'] is receipt['git_published'] is False
    assert source_fingerprint(a.workspace) == before
    assert (a.workspace / 'owner-untracked.txt').read_bytes() == b'untracked sentinel\n'
    assert a.native.calls == a.native.publications == []
    assert a.factory.states[-1]['request_id'] == request_id
    assert a.factory.states[-1]['status'] == 'DELIVERED'
    assert a.factory.states[-1]['handoff'] == result['handoff']
    assert result['handoff']['codex_entrypoint'] in a.factory.states[-1]['codex_prompt']
    remote = json.dumps(a.factory.states)
    assert str(a.workspace) not in remote and str(tmp_path) not in remote
    assert not {'tested_commit', 'run_id', 'quality_gate', 'test_count', 'github_pr'} & result.keys()
    options = a.factory.publisher_calls[-1][1]
    assert options['document_id'] == 'handoff-result-document' and options['tab_id'] == 't.result'
    assert options['protected_document_ids'] == frozenset({'existing-document'})
    events = journal(a.store, request_id)
    assert events.index('HANDOFF_INTENT') < events.index('HANDOFF_LOCAL_COMMITTED')
    assert events.index('HANDOFF_LOCAL_COMMITTED') < events.index('COMPLETED')
    assert events.index('COMPLETED') < events.index('HANDOFF_RECEIPT_INTENT')
    with a.store._connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM execution_lane').fetchone()[0] == 0


@pytest.mark.parametrize('mode', ['unknown-target', 'github-only'])
def test_unregistered_target_has_no_provider_effects(tmp_path: Path, mode: str) -> None:
    a = assembly(tmp_path, enrolled=mode != 'github-only')
    if mode == 'unknown-target':
        a.manifest['target_id'] = 'unregistered'
    baseline(a)
    request_id = submit(a)
    assert a.worker.run_once()
    operation = a.store.get(request_id)
    assert operation['status'] == 'FAILED' and operation['code'] == 'TARGET_NOT_ENROLLED'
    assert a.factory.calls == a.factory.provider.reads == a.factory.publisher_calls == []
    assert not (a.workspace / 'handoffs').exists()
    assert a.native.calls == a.native.publications == []


@pytest.mark.parametrize('mode,code', [
    ('project-mismatch', 'HANDOFF_PROJECT_MISMATCH'), ('absolute-filename', 'UNSAFE_PATH'),
    ('absolute-entrypoint', 'UNSAFE_PATH'), ('remote-inbox-field', 'INVALID_SCHEMA'),
])
def test_remote_scope_or_absolute_path_is_refused_before_admission(tmp_path: Path, mode: str,
                                                                  code: str) -> None:
    a = assembly(tmp_path)
    baseline(a)
    if mode == 'project-mismatch':
        a.manifest['project_id'] = 'other-project'
    elif mode == 'absolute-filename':
        a.manifest['artifacts'][0]['filename'] = '/tmp/outside.zip'
    elif mode == 'absolute-entrypoint':
        a.manifest['entrypoint']['path'] = '/tmp/outside.md'
    else:
        a.manifest['inbox'] = str(tmp_path / 'outside')
    # Baseline ACK serialization detaches the Port value from the original dict.
    a.port.value['request']['handoff'] = copy.deepcopy(a.manifest)
    a.port.ready()
    observed = a.coordinator.poll_once(a.binding, a.port)
    assert observed.kind == 'REJECTED' and observed.code == code
    assert a.store.list_requests() == []
    assert a.factory.calls == a.factory.publisher_calls == a.factory.provider.reads == []
    assert not (a.workspace / '.runner').exists()


def test_new_request_for_same_handoff_reads_existing_commit_without_download(tmp_path: Path) -> None:
    a = assembly(tmp_path)
    baseline(a)
    first = submit(a)
    assert a.worker.run_once()
    first_result = a.store.get(first)['result']['handoff']
    calls = len(a.factory.calls), list(a.factory.provider.reads), list(a.factory.provider.downloads)
    second = submit(a)
    assert second != first and a.worker.run_once()
    assert a.store.get(second)['status'] == 'COMPLETED'
    assert a.store.get(second)['result']['handoff'] == first_result
    assert (len(a.factory.calls), a.factory.provider.reads, a.factory.provider.downloads) == calls
    folder = a.workspace / first_result['safe_relative_handoff_path']
    assert json.loads((folder / 'delivery.json').read_bytes())['first_request_id'] == first


def test_modified_local_delivery_is_terminal_conflict_without_repair(tmp_path: Path) -> None:
    a = assembly(tmp_path)
    baseline(a)
    first = submit(a)
    assert a.worker.run_once()
    result = a.store.get(first)['result']['handoff']
    changed = a.workspace / result['safe_relative_handoff_path'] / 'content/roadmap/roadmap/START.md'
    changed.write_bytes(b'owner has edited this file\n')
    before = list(a.factory.provider.downloads)
    second = submit(a)
    assert a.worker.run_once()
    operation = a.store.get(second)
    assert operation['status'] == 'FAILED' and operation['code'] == 'HANDOFF_CHANGED'
    assert changed.read_bytes() == b'owner has edited this file\n'
    assert a.factory.provider.downloads == before
    assert a.store.get(first)['status'] == 'COMPLETED'


def test_queued_cancel_bypasses_worker_without_provider_access(tmp_path: Path) -> None:
    a = assembly(tmp_path)
    baseline(a)
    target = submit(a)
    actor = control(a, 'CANCEL', target)
    assert a.store.get(actor)['status'] == 'COMPLETED'
    assert a.store.get(target)['status'] == 'CANCELLED'
    assert not a.worker.run_once()
    assert a.factory.calls == a.factory.provider.reads == []
    assert not (a.workspace / 'handoffs').exists()
    assert a.native.calls == a.native.publications == []
    target_states = [state for state in a.factory.states if state['request_id'] == target]
    assert target_states[-1]['handoff']['local_delivery_status'] == 'NOT_DELIVERED'


def test_unconfirmed_remote_receipt_preserves_local_delivery_and_can_redeliver(tmp_path: Path) -> None:
    a = assembly(tmp_path)
    a.factory.publication = {'status': 'UNCONFIRMED', 'code': 'DELIVERY_RESULT_UNCONFIRMED'}
    baseline(a)
    target = submit(a)
    assert a.worker.run_once()
    result = a.store.get(target)['result']
    assert a.store.get(target)['status'] == 'COMPLETED'
    assert result['handoff']['local_delivery_status'] == 'DELIVERED'
    assert result['receipt_publication']['code'] == 'DELIVERY_RESULT_UNCONFIRMED'
    folder = a.workspace / result['handoff']['safe_relative_handoff_path']
    original = (folder / 'files/roadmap/roadmap.zip').read_bytes()
    downloads = list(a.factory.provider.downloads)
    a.factory.publication = {'status': 'CONFIRMED', 'code': 'DELIVERY_RESULT_CONFIRMED'}
    actor = control(a, 'REDELIVER', target)
    assert a.worker.run_once()
    assert a.store.get(actor)['status'] == 'COMPLETED'
    assert a.store.get(target)['result']['receipt_publication']['status'] == 'CONFIRMED'
    assert (folder / 'files/roadmap/roadmap.zip').read_bytes() == original
    assert a.factory.provider.downloads == downloads
    assert a.native.calls == a.native.publications == []


def test_status_and_redeliver_use_handoff_dispatch_without_native_execution(tmp_path: Path) -> None:
    a = assembly(tmp_path)
    baseline(a)
    target = submit(a)
    assert a.worker.run_once()
    before = len(a.factory.calls), list(a.factory.provider.downloads)
    status = control(a, 'STATUS', target)
    assert a.store.get(status)['status'] == 'COMPLETED'
    assert not a.worker.run_once(), 'STATUS is an inline control action'
    redelivery = control(a, 'REDELIVER', target)
    assert a.worker.run_once() and a.store.get(redelivery)['status'] == 'COMPLETED'
    assert a.native.calls == a.native.publications == []
    assert (len(a.factory.calls), a.factory.provider.downloads) == before
    status_view = next(state for state in a.factory.states if state['request_id'] == status)
    assert status_view['target']['request_id'] == target
    assert status_view['target']['handoff']['local_delivery_status'] == 'DELIVERED'


@pytest.mark.parametrize('phase', ['QUEUED', 'RUNNING'])
def test_status_does_not_freeze_nonterminal_handoff_receipt(tmp_path: Path, phase: str) -> None:
    a = assembly(tmp_path)
    baseline(a)
    target = submit(a)
    if phase == 'RUNNING':
        claimed = a.store.claim_next(a.session)
        assert claimed['request_id'] == target
        a.store.mark_running(target, {'fixture': 'held-nonterminal-handoff'})
    actor = control(a, 'STATUS', target)
    assert a.store.get(actor)['status'] == 'COMPLETED'
    assert a.store.get(target)['status'] == phase
    assert not any(state['request_id'] == target for state in a.factory.states)
    view = next(state for state in a.factory.states if state['request_id'] == actor)
    assert view['target']['status'] == phase
    assert view['target']['handoff']['local_delivery_status'] == 'NOT_DELIVERED'
    assert a.factory.calls == a.factory.provider.downloads == []
    if phase == 'QUEUED':
        assert a.worker.run_once()
    else:
        outcome = a.dispatcher(a.store.get(target), threading.Event())
        a.store.finish(target, status=outcome.status, code=outcome.code, result=outcome.result)
        a.dispatcher.publish_state(target)
    terminal = [state for state in a.factory.states if state['request_id'] == target]
    assert len(terminal) == 1 and terminal[0]['status'] == 'DELIVERED'
    assert a.native.calls == a.native.publications == []


@pytest.mark.parametrize('stage', ['factory-setup', 'publisher-write'])
def test_receipt_exception_is_unconfirmed_and_preserves_committed_bytes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    a = assembly(tmp_path)

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise OSError('private-provider-response-must-not-escape')

    monkeypatch.setattr(a.factory, 'publisher' if stage == 'factory-setup' else 'publish', unavailable)
    baseline(a)
    target = submit(a)
    assert a.worker.run_once()
    operation = a.store.get(target)
    result = operation['result']
    assert operation['status'] == 'COMPLETED'
    assert result['handoff']['local_delivery_status'] == 'DELIVERED'
    assert result['receipt_publication']['status'] == 'UNCONFIRMED'
    assert result['receipt_publication']['code'] == 'DELIVERY_RESULT_UNCONFIRMED'
    assert 'private-provider-response' not in json.dumps(operation)
    folder = a.workspace / result['handoff']['safe_relative_handoff_path']
    assert (folder / 'files/roadmap/roadmap.zip').read_bytes() == a.blobs['zip-file'][0]
    assert (folder / 'delivery.json').is_file()
    assert 'HANDOFF_RECEIPT_DELIVERY' in journal(a.store, target)
    assert a.native.calls == a.native.publications == []


def test_delivered_inbox_is_excluded_from_dirty_untracked_source_snapshot(tmp_path: Path) -> None:
    source_root, application_root = tmp_path / 'source', tmp_path / 'application'
    source_root.mkdir()
    application_root.mkdir(mode=0o700)
    fixture = SourceFixture(source_root)
    project_bytes = b'{"project_id":"demo","owner_edit":true}\n'
    (fixture.workspace / '.runner/project.json').write_bytes(project_bytes)
    (fixture.workspace / '.runner/owner-notes.txt').write_bytes(b'approved untracked notes\n')
    a = assembly(application_root, workspace=fixture.workspace)
    legacy = fixture.workspace / '.runner/handoffs/legacy/sentinel.txt'
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b'legacy receipt material\n')
    before = source_fingerprint(fixture.workspace)
    baseline(a)
    target = submit(a)
    assert a.worker.run_once() and a.store.get(target)['status'] == 'COMPLETED'
    snapshot = fixture.capture(local=LocalWorkspace(
        fixture.workspace, include_dirty=True, include_untracked=('.runner/', 'handoffs/')))
    included = {item.path for item in snapshot.files}
    assert '.runner/project.json' in included and '.runner/owner-notes.txt' in included
    assert not any(name == 'handoffs' or name.startswith('handoffs/')
                   for name in included)
    assert any(item.reason == 'controller_owned' and (
        item.path == 'handoffs' or item.path.startswith('handoffs/'))
               for item in snapshot.excluded_paths)
    assert (snapshot.path / '.runner/project.json').read_bytes() == project_bytes
    assert (snapshot.path / '.runner/owner-notes.txt').read_bytes() == b'approved untracked notes\n'
    assert not (snapshot.path / 'handoffs').exists()
    assert not (snapshot.path / '.runner/handoffs').exists()
    folder = a.workspace / a.store.get(target)['result']['handoff']['safe_relative_handoff_path']
    assert (folder / 'files/roadmap/roadmap.zip').read_bytes() == a.blobs['zip-file'][0]
    assert source_fingerprint(fixture.workspace) == before


@pytest.mark.parametrize('cut', ['before-local-event', 'before-finish'])
def test_rename_before_database_finish_recovers_in_fresh_session_via_status(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cut: str) -> None:
    a = assembly(tmp_path)
    baseline(a)
    target = submit(a)
    before = source_fingerprint(a.workspace)

    def lost_finish(*args: Any, **kwargs: Any) -> None:
        raise OSError('simulated crash after local commit, before DB finish')

    class SimulatedPowerLoss(BaseException):
        pass

    record_event = a.store.record_event

    def lost_event(request_id: str, event: str, detail: Any) -> None:
        if event == 'HANDOFF_LOCAL_COMMITTED':
            raise SimulatedPowerLoss()
        record_event(request_id, event, detail)

    with monkeypatch.context() as patch:
        if cut == 'before-local-event':
            patch.setattr(a.store, 'record_event', lost_event)
        else:
            patch.setattr(a.store, 'finish', lost_finish)
        with pytest.raises((OSError, SimulatedPowerLoss)):
            a.worker.run_once()
    assert a.store.get(target)['status'] == 'STARTING'
    assert (a.workspace / 'handoffs' / a.manifest['handoff_id'] / 'delivery.json').exists()
    assert not a.factory.states
    assert ('HANDOFF_LOCAL_COMMITTED' in journal(a.store, target)) == (cut == 'before-finish')
    before_downloads = list(a.factory.provider.downloads)
    recovered_store = IntakeStore(Database(a.store.database.path))
    recovered_session = recovered_store.start_session('runner-1')
    assert recovered_store.get(target)['status'] == 'INTERRUPTED'
    recovered_store.bind(a.binding, runner_id='runner-1', project_id='project-1', provider='drive',
                         coordinates={'document_id': 'existing-document', 'tab_id': 't.existing'},
                         session_id=recovered_session)
    recovered = HandoffRunner(recovered_store, a.config, a.paths, provider_factory=a.factory,
                              protected_document_ids=frozenset({'existing-document'}))
    dispatcher = V2TaskDispatcher(a.native, recovered)
    coordinator = IntakeCoordinator(recovered_store, recovered_session,
                                     terminal_observer=dispatcher.publish_state)
    a.port.value['dispatch'] = 'DRAFT'
    assert coordinator.poll_once(a.binding, a.port).kind == 'BASELINED'
    a.port.ready(request={'request_id': None, 'action': 'STATUS', 'target_request_id': target})
    status = coordinator.poll_once(a.binding, a.port)
    assert status.kind == 'ACCEPTED'
    operation = recovered_store.get(target)
    assert operation['status'] == 'COMPLETED'
    assert operation['result']['handoff']['local_delivery_status'] == 'DELIVERED'
    assert a.factory.provider.downloads == before_downloads and len(a.factory.calls) == 1
    assert 'HANDOFF_RECOVERED' in journal(recovered_store, target)
    assert source_fingerprint(a.workspace) == before
    assert a.native.calls == a.native.publications == []
    with recovered_store._connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM execution_lane').fetchone()[0] == 0


def historical_rows(store: IntakeStore) -> dict[str, list[tuple[Any, ...]]]:
    with store._connection() as connection:
        return {table: [tuple(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY 1')]
                for table in ('v2_requests', 'v2_candidates', 'v2_queue', 'v2_journal')}


def test_populated_schema10_upgrade_preserves_history_and_immutable_guards(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert database_module.SCHEMA_VERSION == 11
    with monkeypatch.context() as patch:
        patch.setattr(database_module, 'SCHEMA_VERSION', 10)
        store, session, coordinator, port = setup(tmp_path)
        assert coordinator.poll_once('slot', port).kind == 'BASELINED'
        queued = accept(coordinator, port)
        store.record_event(queued, 'PRESERVED_MIGRATION_SENTINEL', {'unchanged': True})
        port.ready()
        port.lost = True
        unknown = coordinator.poll_once('slot', port)
        assert unknown.kind == 'ACK_UNKNOWN'
        with store._connection() as connection:
            assert connection.execute('PRAGMA user_version').fetchone()[0] == 10
        installation = store.installation_id
        before = historical_rows(store)
    assert store.database.migrate() == 11
    assert historical_rows(store) == before
    assert store.installation_id == installation
    with store._connection() as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 11
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='v2_requests'").fetchone()[0]
        assert "'DELIVER_HANDOFF'" in sql
        guards = (
            ("UPDATE v2_requests SET digest='changed' WHERE request_id=?", queued),
            ('DELETE FROM v2_requests WHERE request_id=?', queued),
            ("UPDATE v2_candidates SET ack_sha256='changed' WHERE request_id=?", queued),
            ('DELETE FROM v2_candidates WHERE request_id=?', queued),
            ('UPDATE v2_queue SET sequence=999 WHERE request_id=?', queued),
            ('DELETE FROM v2_queue WHERE request_id=?', queued),
            ("UPDATE v2_journal SET event='changed' WHERE request_id=?", queued),
            ('DELETE FROM v2_journal WHERE request_id=?', queued),
        )
        for statement, request_id in guards:
            with pytest.raises(sqlite3.IntegrityError, match='immutable|append-only'):
                connection.execute(statement, (request_id,))
    assert historical_rows(store) == before
    assert coordinator.poll_once('slot', port).kind == 'ACCEPTED', 'old ACK proof must still reconcile'
    assert store.get(queued)['status'] == 'QUEUED'
    assert store.claim_next(session)['request_id'] == queued
