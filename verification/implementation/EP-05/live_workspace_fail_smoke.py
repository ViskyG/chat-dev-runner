#!/usr/bin/env python3
"""One-shot failing-test Drive acceptance in the existing EP-05 smoke project.

Default invocation only reads the exact registered mailbox and local project.
--apply stages one changeset and DRAFT command, starts a dedicated unarmed
Runtime on 127.0.0.1:18766, arms through its local API, promotes that command
to READY with the new epoch, then polls and disarms. Provider writes are each
preceded by an exclusive intent and classified by exact revision readback.
There is no replay after a partial or uncertain write.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from runner_app.adapters.drive_docs import (
    MAX_COMMAND_DOC_BYTES,
    DocumentSnapshot,
    extract_document,
    require_registered_doc,
)
from runner_app.adapters.drive_writer import _plain_body_span
from runner_app.config import Settings
from runner_app.domain.commands import validate_shape
from runner_app.engine.integrations import build_runtime_integrations, load_owner_runtime_config
from runner_app.engine.runtime import Runtime
from runner_app.git.argv import run_git, run_git_bytes
from runner_app.git.changes import MAX_CHANGESET_BYTES, validate_changeset
from runner_app.git.imports import ImportGrants
from runner_app.git.registry import SourceRegistry
from runner_app.git.templates import _python_quick_assets
from runner_app.storage import Database, Paths, open_private_file
from runner_app.storage.ledger import TERMINAL, Ledger

EVIDENCE = ROOT / 'verification/implementation/EP-05'
PREFIX = 'live-workspace-fail-fresh'
STATE = Path('/home/visky/Work/GPTRunner/.ep05-smoke/cdr-ep05-fail-20260928')
PROJECT = 'cdr-ep05-fail-20260928'
WORKSPACE = 'primary'
HEAD = '82fe8133230dad2da937cff8b819dc98a55a6fd0'
REPOSITORY_ID = 1392232161
COMMAND_ID = '1gxVPb976bZyNkequvublHoRtRuCZ3-ZN0LgydJ7bgik'
CHANGESET_ID = '1qwlXxfemMkOLjWwIQZknpy8dESYTQsSqbIxGygSHD6s'
PORT = 18766
BASE_URL = f'http://127.0.0.1:{PORT}'


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n'


def _evidence(name: str, value: dict[str, Any]) -> None:
    path = EVIDENCE / f'{PREFIX}-{name}.json'
    raw = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read(provider: Any, doc_id: str, maximum: int) -> tuple[DocumentSnapshot, int, int]:
    document = provider.get_document(doc_id, include_tabs_content=True)
    snapshot = extract_document(document, doc_id, max_bytes=maximum)
    start, end = _plain_body_span(document, snapshot)
    return snapshot, start, end


def _replace_once(provider: Any, doc_id: str, expected: DocumentSnapshot,
                  desired: str, maximum: int) -> tuple[DocumentSnapshot, str]:
    before, start, end = _read(provider, doc_id, maximum)
    if (before.revision_id != expected.revision_id or before.sha256 != expected.sha256
            or before.tab_id is None or end - 1 < start or not desired.endswith('\n')):
        raise ValueError('DOCUMENT_PRECONDITION_CHANGED')
    requests: list[dict[str, Any]] = []
    if end - 1 > start:
        requests.append({'deleteContentRange': {'range': {
            'startIndex': start, 'endIndex': end - 1, 'tabId': before.tab_id,
        }}})
    requests.append({'insertText': {'location': {'index': start, 'tabId': before.tab_id},
                                    'text': desired[:-1]}})
    write_error: Exception | None = None
    try:
        provider.batch_update_document(
            doc_id, requests=requests, required_revision_id=before.revision_id,
        )
    except Exception as error:  # noqa: BLE001 -- exact readback classifies lost acknowledgements
        write_error = error
    after, _, _ = _read(provider, doc_id, maximum)
    if (after.text != desired or after.sha256 != _sha(desired)
            or after.revision_id == before.revision_id):
        raise ValueError('DOCUMENT_WRITE_UNCONFIRMED') from None
    return after, 'ACK_LOST_EXACT_READBACK' if write_error else 'CONFIRMED'


@dataclass(frozen=True)
class Preflight:
    paths: Paths
    ledger: Ledger
    provider: Any
    registration: Any
    command: DocumentSnapshot
    changeset: DocumentSnapshot
    runner_id: str
    epoch: str


def _preflight() -> Preflight:
    discovered = Paths.discover()
    paths = Paths(config_dir=discovered.config_dir, state_dir=STATE,
                  cache_dir=STATE / 'cache')
    if not paths.database_path.is_file():
        raise ValueError('SMOKE_STATE_MISSING')
    config = load_owner_runtime_config(paths)
    if config is None or config.execution_profile_dir is None:
        raise ValueError('OWNER_RUNTIME_UNCONFIGURED')
    ledger = Ledger(Database(paths.database_path))
    state = ledger.state()
    if state['armed'] or state['reserved_capacity']:
        raise ValueError('SMOKE_RUNNER_NOT_DISARMED')
    registry = SourceRegistry(ledger.database, STATE / 'managed')
    project = registry.get_project(PROJECT)
    workspace = registry.get_workspace(PROJECT, WORKSPACE)
    profile = registry.get_workspace_profile(PROJECT, WORKSPACE)
    remote = registry.get_project_remote(PROJECT)
    if (project is None or project.get('state') != 'READY'
            or workspace is None or workspace.get('state') != 'ACTIVE'
            or workspace.get('archived') or workspace.get('head') != HEAD
            or profile != {'template_id': 'python-basic', 'profile_id': 'python-quick'}
            or remote is None or remote.get('provider_id') != REPOSITORY_ID
            or remote.get('owner') != 'ViskyG' or remote.get('name') != PROJECT
            or remote.get('private') is not True):
        raise ValueError('SMOKE_PROJECT_CHANGED')
    worktree = Path(workspace['worktree_path'])
    if (run_git(worktree, ['rev-parse', 'HEAD']).strip() != HEAD
            or run_git(worktree, ['status', '--porcelain=v1']) != ''
            or run_git(worktree, ['branch', '--show-current']).strip() != f'work/{PROJECT}'
            or run_git(worktree, ['remote', 'get-url', 'origin']).strip()
            != f'https://github.com/ViskyG/{PROJECT}.git'
            or run_git(worktree, ['ls-tree', '--name-only', HEAD, '--',
                                  'src/feature.py', 'tests/test_feature.py']).strip()):
        raise ValueError('SMOKE_WORKTREE_CHANGED')
    # This project's current starter must carry the exact approved offline
    # lockfile and all five binary wheels before a failing-test run is staged.
    for name, expected in _python_quick_assets().items():
        if run_git_bytes(worktree, ['show', f'{HEAD}:{name}']) != expected:
            raise ValueError('SMOKE_STARTER_ASSET_CHANGED')
    imports = ImportGrants(STATE / 'import-handles', roots=config.import_roots)
    integrations = build_runtime_integrations(paths, ledger, registry, imports, config)
    registration = integrations.drive_registry.get(PROJECT, WORKSPACE)
    if (registration.command_doc_id != COMMAND_ID
            or registration.changeset_doc_id != CHANGESET_ID):
        raise ValueError('SMOKE_MAILBOX_CHANGED')
    provider = integrations.provider
    require_registered_doc(provider, COMMAND_ID, registration.workspace_folder_id)
    require_registered_doc(provider, CHANGESET_ID, registration.workspace_folder_id)
    command, _, _ = _read(provider, COMMAND_ID, MAX_COMMAND_DOC_BYTES)
    changeset, _, _ = _read(provider, CHANGESET_ID, MAX_CHANGESET_BYTES)
    if command.text.strip('\n') or changeset.text.strip('\n'):
        raise ValueError('SMOKE_INPUT_DOC_NOT_EMPTY')
    return Preflight(paths, ledger, provider, registration, command, changeset,
                     state['runner_id'], state['epoch'])


def _changeset(change_id: str) -> dict[str, Any]:
    change = {
        'schema_version': '1.0', 'change_id': change_id,
        'project_id': PROJECT, 'workspace_id': WORKSPACE, 'base_head': HEAD,
        'files': [
            {'op': 'upsert', 'path': 'src/feature.py', 'expected_sha256': None,
             'content_utf8': 'def double(value: int) -> int:\n    return value * 2\n'},
            {'op': 'upsert', 'path': 'tests/test_feature.py', 'expected_sha256': None,
             'content_utf8': ('from src.feature import double\n\n\n'
                              'def test_double_failure() -> None:\n'
                              '    assert double(21) == 43\n')},
        ],
    }
    validate_changeset(change, project_id=PROJECT, workspace_id=WORKSPACE,
                       expected_head=HEAD)
    return change


def _command(request_id: str, *, dispatch: str, epoch: str, runner_id: str,
             changeset_revision: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    command = {
        'schema_version': '1.0', 'request_id': request_id, 'dispatch': dispatch,
        'runner_id': runner_id, 'epoch': epoch,
        'issued_at': now.isoformat(),
        'expires_at': (now + timedelta(minutes=10)).isoformat(),
        'action': 'DELIVER_VERIFY_PUBLISH', 'project_id': PROJECT,
        'workspace_id': WORKSPACE, 'expected_head': HEAD,
        'params': {'changeset_doc_id': CHANGESET_ID,
                   'changeset_revision': changeset_revision,
                   'profile_id': 'python-quick', 'publication': 'draft_always'},
        'intent': 'Run the disposable EP-05 failing test and publish a reviewable draft',
    }
    validate_shape(command)
    return command


def _port_free() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(('127.0.0.1', PORT))
        except OSError:
            return False
    return True


def _api_session(paths: Paths) -> httpx.Client:
    descriptor = open_private_file(paths.token_dir / 'session-bootstrap.json')
    try:
        value = json.loads(os.read(descriptor, 4096))
    finally:
        os.close(descriptor)
    if set(value) != {'bootstrap', 'expires_in_seconds'}:
        raise ValueError('LOCAL_BOOTSTRAP_INVALID')
    client = httpx.Client(base_url=BASE_URL, headers={'Origin': BASE_URL}, timeout=15.0)
    response = client.post('/api/v1/local/session', json={'bootstrap': value['bootstrap']})
    response.raise_for_status()
    body = response.json()
    csrf = body.get('csrf_token')
    if not isinstance(csrf, str) or not csrf or 'cdr_session' not in client.cookies:
        client.close()
        raise ValueError('LOCAL_SESSION_INVALID')
    client.headers['X-CSRF-Token'] = csrf
    return client


def _get(client: httpx.Client, path: str) -> dict[str, Any]:
    response = client.get(path)
    response.raise_for_status()
    return cast(dict[str, Any], response.json())


def _post(client: httpx.Client, path: str) -> dict[str, Any]:
    response = client.post(path, json={})
    response.raise_for_status()
    return cast(dict[str, Any], response.json())


def _start_runtime(paths: Paths) -> tuple[Runtime, uvicorn.Server, threading.Thread]:
    runtime = Runtime(Settings(listen_port=PORT), paths)
    try:
        manager = runtime.workspace_mailboxes
        if manager is None:
            raise ValueError('WORKSPACE_MAILBOX_UNAVAILABLE')
        refresh = manager.refresh()
        mailbox = manager.mailboxes().get((PROJECT, WORKSPACE))
        if (refresh.activated != 1 or mailbox is None
                or mailbox.command_id != COMMAND_ID
                or 'DELIVER_VERIFY_PUBLISH' not in runtime.engine.capabilities()):
            raise ValueError('SMOKE_ROUTE_NOT_READY')
        manager.start()
        server = uvicorn.Server(uvicorn.Config(
            runtime.app, host='127.0.0.1', port=PORT, workers=1,
            proxy_headers=False, access_log=False, server_header=False,
            log_level='error',
        ))
        thread = threading.Thread(target=server.run, name='cdr-ep05-fail-smoke', daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not server.started:
            raise ValueError('SMOKE_RUNTIME_START_FAILED')
        return runtime, server, thread
    except BaseException:
        runtime.close()
        raise


def _poll(client: httpx.Client, request_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 900
    last_status: str | None = None
    while time.monotonic() < deadline:
        response = client.get(f'/api/v1/operations/{request_id}')
        if response.status_code == 404:
            status, operation = 'NOT_ADMITTED', None
        else:
            response.raise_for_status()
            operation = response.json()
            status = operation['status']
        if status != last_status:
            print(json.dumps({'event': 'STATUS', 'request_id': request_id,
                              'status': status,
                              'code': operation.get('code') if operation else None}), flush=True)
            last_status = status
        if status in TERMINAL and operation is not None:
            result = operation.get('result') or {}
            summary = {
                'schema_version': '1.0', 'request_id': request_id,
                'status': status, 'code': operation.get('code'),
                'effects': len(operation['effects']),
                'confirmed_effects': sum(effect['state'] == 'CONFIRMED'
                                         for effect in operation['effects']),
                'result': result,
            }
            _evidence('result', summary)
            return summary
        time.sleep(2)
    raise TimeoutError('SMOKE_OPERATION_NOT_TERMINAL')


def _apply(preflight: Preflight) -> int:
    if not _port_free():
        raise ValueError('SMOKE_PORT_IN_USE')
    if os.path.lexists(preflight.paths.token_dir / 'session-bootstrap.json'):
        raise ValueError('OTHER_RUNTIME_MAY_BE_RUNNING')
    request_id, change_id = str(uuid4()), str(uuid4())
    change = _changeset(change_id)
    change_text = _json_text(change)
    intent = {
        'schema_version': '1.0', 'request_id': request_id, 'change_id': change_id,
        'project_id': PROJECT, 'workspace_id': WORKSPACE, 'expected_head': HEAD,
        'repository_id': REPOSITORY_ID, 'state_dir': str(STATE),
        'command_doc_id': COMMAND_ID, 'command_revision': preflight.command.revision_id,
        'command_sha256': preflight.command.sha256,
        'changeset_doc_id': CHANGESET_ID,
        'changeset_revision': preflight.changeset.revision_id,
        'changeset_sha256_before': preflight.changeset.sha256,
        'changeset_sha256_desired': _sha(change_text),
        'planned_files': [{'path': item['path'], 'sha256': _sha(item['content_utf8'])}
                          for item in change['files']],
        'expected_execution': 'FAILED', 'publication_policy': 'draft_always',
        'created_at': datetime.now(timezone.utc).isoformat(),
    }
    runtime: Runtime | None = None
    server: uvicorn.Server | None = None
    thread: threading.Thread | None = None
    client: httpx.Client | None = None
    armed = False
    try:
        runtime, server, thread = _start_runtime(preflight.paths)
        client = _api_session(preflight.paths)
        system = _get(client, '/api/v1/system')
        if (system['armed'] or 'DELIVER_VERIFY_PUBLISH' not in system['capabilities']
                or system['runner_id'] != preflight.runner_id):
            raise ValueError('SMOKE_RUNTIME_NOT_READY')
        _evidence('intent', intent)
        _evidence('runtime', {'schema_version': '1.0', 'request_id': request_id,
                              'port': PORT, 'runner_id': system['runner_id'],
                              'epoch_before_arm': system['epoch'],
                              'capability': 'DELIVER_VERIFY_PUBLISH'})

        _evidence('changeset-intent', {'request_id': request_id,
                                       'document_id': CHANGESET_ID,
                                       'required_revision': preflight.changeset.revision_id,
                                       'desired_sha256': _sha(change_text)})
        change_after, change_ack = _replace_once(
            preflight.provider, CHANGESET_ID, preflight.changeset,
            change_text, MAX_CHANGESET_BYTES,
        )
        _evidence('changeset-readback', {'request_id': request_id,
                                         'document_id': CHANGESET_ID,
                                         'revision': change_after.revision_id,
                                         'sha256': change_after.sha256,
                                         'write_state': change_ack})

        draft = _command(request_id, dispatch='DRAFT', epoch=system['epoch'],
                         runner_id=system['runner_id'],
                         changeset_revision=change_after.revision_id)
        draft_text = _json_text(draft)
        _evidence('draft-intent', {'request_id': request_id, 'document_id': COMMAND_ID,
                                   'required_revision': preflight.command.revision_id,
                                   'desired_sha256': _sha(draft_text),
                                   'changeset_revision': change_after.revision_id})
        draft_after, draft_ack = _replace_once(
            preflight.provider, COMMAND_ID, preflight.command,
            draft_text, MAX_COMMAND_DOC_BYTES,
        )
        _evidence('draft-readback', {'request_id': request_id,
                                     'document_id': COMMAND_ID,
                                     'revision': draft_after.revision_id,
                                     'sha256': draft_after.sha256,
                                     'write_state': draft_ack})

        _evidence('arm-intent', {'request_id': request_id, 'port': PORT,
                                 'epoch_before': system['epoch']})
        armed_response = _post(client, '/api/v1/local/arm')
        armed = True
        system = _get(client, '/api/v1/system')
        if (not system['armed'] or system['epoch'] == draft['epoch']
                or system['epoch'] != armed_response['epoch']
                or system['runner_id'] != draft['runner_id']):
            raise ValueError('SMOKE_ARM_STATE_MISMATCH')
        _evidence('armed-readback', {'request_id': request_id, 'port': PORT,
                                     'runner_id': system['runner_id'],
                                     'epoch': system['epoch'], 'armed': True})

        ready = _command(request_id, dispatch='READY', epoch=system['epoch'],
                         runner_id=system['runner_id'],
                         changeset_revision=change_after.revision_id)
        ready_text = _json_text(ready)
        _evidence('ready-intent', {'request_id': request_id, 'document_id': COMMAND_ID,
                                   'required_revision': draft_after.revision_id,
                                   'draft_sha256': draft_after.sha256,
                                   'desired_sha256': _sha(ready_text),
                                   'epoch': system['epoch'],
                                   'changeset_revision': change_after.revision_id})
        ready_after, ready_ack = _replace_once(
            preflight.provider, COMMAND_ID, draft_after,
            ready_text, MAX_COMMAND_DOC_BYTES,
        )
        _evidence('ready-readback', {'request_id': request_id,
                                     'document_id': COMMAND_ID,
                                     'revision': ready_after.revision_id,
                                     'sha256': ready_after.sha256,
                                     'write_state': ready_ack})
        print(json.dumps({'event': 'READY', 'request_id': request_id,
                          'epoch': system['epoch']}), flush=True)
        result = _poll(client, request_id)
        axes = result.get('result') or {}
        expected = (result['status'] == 'COMPLETED'
                    and axes.get('execution', {}).get('outcome') == 'FAILED'
                    and axes.get('publication', {}).get('status') == 'DRAFT_PUBLISHED'
                    and axes.get('delivery', {}).get('status') == 'DELIVERED'
                    and axes.get('report_link', {}).get('status') == 'ATTACHED')
        print(json.dumps({'event': 'TERMINAL', 'request_id': request_id,
                          'status': result['status'], 'code': result['code'],
                          'execution': axes.get('execution'),
                          'publication': axes.get('publication'),
                          'delivery': axes.get('delivery'),
                          'report_link': axes.get('report_link'),
                          'acceptance_met': expected}), flush=True)
        return 0 if expected else 2
    finally:
        if client is not None:
            if armed:
                try:
                    disarmed = _post(client, '/api/v1/local/disarm')
                    _evidence('disarmed-readback', {'request_id': request_id,
                                                    'port': PORT,
                                                    'armed': disarmed.get('armed'),
                                                    'epoch': disarmed.get('epoch')})
                    print(json.dumps({'event': 'DISARMED', 'armed': disarmed.get('armed')}),
                          flush=True)
                except Exception:  # noqa: BLE001 -- report uncertainty without secret details
                    print(json.dumps({'event': 'DISARM_UNCONFIRMED',
                                      'request_id': request_id}), flush=True)
            client.close()
        if server is not None and thread is not None:
            server.should_exit = True
            thread.join(timeout=30)
        if runtime is not None:
            runtime.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    preflight = _preflight()
    change = _changeset(str(uuid4()))
    plan = {
        'mode': 'READ_ONLY' if not args.apply else 'APPLY',
        'project_id': PROJECT, 'workspace_id': WORKSPACE,
        'state_dir': str(STATE), 'head': HEAD,
        'repository_id': REPOSITORY_ID,
        'command_doc_id': COMMAND_ID,
        'command_revision': preflight.command.revision_id,
        'changeset_doc_id': CHANGESET_ID,
        'changeset_revision': preflight.changeset.revision_id,
        'planned_files': [{'path': item['path'], 'sha256': _sha(item['content_utf8'])}
                          for item in change['files']],
        'expected_execution': 'FAILED', 'publication_policy': 'draft_always',
        'port': PORT, 'cloud_writes': args.apply,
    }
    print(json.dumps(plan, sort_keys=True), flush=True)
    if not args.apply:
        return 0
    return _apply(preflight)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001 -- never expose third-party credential-bearing text
        code = getattr(error, 'code', None)
        safe = code if isinstance(code, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', code) else 'SMOKE_FAILED'
        print(json.dumps({'status': 'FAULT', 'code': safe}), flush=True)
        raise SystemExit(2) from None
