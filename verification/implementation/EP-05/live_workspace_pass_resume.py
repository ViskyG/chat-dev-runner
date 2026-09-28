#!/usr/bin/env python3
"""Continue the disposable EP-05 pass gate after a terminal prior command.

This one-shot owner harness stages DRAFT while disarmed, arms locally, then
promotes the same fresh request to READY with the new epoch. Every Drive write
has a prior durable intent, one revision-guarded request, and exact readback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from live_workspace_gate import _evidence_exclusive, _json_text

from runner_app.adapters.drive_docs import (
    MAX_COMMAND_DOC_BYTES,
    extract_document,
    require_registered_doc,
)
from runner_app.adapters.drive_writer import _plain_body_span
from runner_app.domain.commands import validate_shape
from runner_app.engine.integrations import build_runtime_integrations, load_owner_runtime_config
from runner_app.git.imports import ImportGrants
from runner_app.git.registry import SourceRegistry
from runner_app.storage import Database
from runner_app.storage.ledger import TERMINAL, Ledger
from runner_app.storage.paths import Paths

EVIDENCE = ROOT / 'verification/implementation/EP-05'
CURRENT = EVIDENCE / 'live-workspace-pass-current.json'
INPUTS = EVIDENCE / 'live-workspace-pass-inputs.json'
SESSION = Path('/tmp/cdr-ep05-local-session.json')
PROJECT_ID = 'cdr-ep05-drive-20260928'
COMMAND_ID = '1XTA26PC-9DIbGDka3ec5tkNxttYYouwT39aEe2XxEdY'
CHANGESET_ID = '1FSwggdAmoyoVrxlm1fi5vkIjs7PWK_F4hZPj3nDEzSY'
PREVIOUS_REQUEST = 'c9c8bb32-94e5-4b6a-94d0-24924ae68158'


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _read(provider: Any) -> tuple[Any, int, int]:
    document = provider.get_document(COMMAND_ID, include_tabs_content=True)
    snapshot = extract_document(document, COMMAND_ID, max_bytes=MAX_COMMAND_DOC_BYTES)
    start, end = _plain_body_span(document, snapshot)
    return snapshot, start, end


def _replace(provider: Any, old_sha: str, desired: str) -> tuple[str, str]:
    before, start, end = _read(provider)
    if (before.sha256 != old_sha or before.tab_id is None or not desired.endswith('\n')
            or end - 1 <= start):
        raise ValueError('COMMAND_PRECONDITION_CHANGED')
    requests = [
        {'deleteContentRange': {'range': {'startIndex': start, 'endIndex': end - 1,
                                          'tabId': before.tab_id}}},
        {'insertText': {'location': {'index': start, 'tabId': before.tab_id},
                        'text': desired[:-1]}},
    ]
    write_error: Exception | None = None
    try:
        provider.batch_update_document(
            COMMAND_ID, requests=requests, required_revision_id=before.revision_id,
        )
    except Exception as error:  # noqa: BLE001 - readback classifies lost acknowledgements
        write_error = error
    after, _, _ = _read(provider)
    if (after.text != desired or after.sha256 != _sha(desired)
            or after.revision_id == before.revision_id):
        raise ValueError('COMMAND_WRITE_UNCONFIRMED') from None
    return after.revision_id, 'ACK_LOST_EXACT_READBACK' if write_error else 'CONFIRMED'


def _session() -> dict[str, str]:
    descriptor = os.open(SESSION, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
            raise ValueError('LOCAL_SESSION_UNSAFE')
        value = json.loads(os.read(descriptor, 4096))
    finally:
        os.close(descriptor)
    if set(value) != {'base_url', 'cookie_name', 'cookie_value', 'csrf_token'}:
        raise ValueError('LOCAL_SESSION_INVALID')
    return value


def _client(value: dict[str, str]) -> httpx.Client:
    base = value['base_url']
    if base != 'http://127.0.0.1:18765':
        raise ValueError('LOCAL_SERVER_UNEXPECTED')
    client = httpx.Client(
        base_url=base, headers={'Origin': base, 'X-CSRF-Token': value['csrf_token']},
        timeout=15.0,
    )
    client.cookies.set(value['cookie_name'], value['cookie_value'], path='/')
    return client


def _get(client: httpx.Client, path: str) -> dict[str, Any]:
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def _post(client: httpx.Client, path: str) -> dict[str, Any]:
    response = client.post(path, json={})
    response.raise_for_status()
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--previous-request', default=PREVIOUS_REQUEST)
    parser.add_argument('--previous-evidence', default=CURRENT.name)
    parser.add_argument('--expected-status', default='REJECTED')
    parser.add_argument('--expected-code', default='WRONG_RUNNER_EPOCH')
    parser.add_argument('--expected-effects', type=int, default=0)
    parser.add_argument('--evidence-prefix', default='live-workspace-pass')
    args = parser.parse_args()
    if (not args.previous_evidence.startswith('live-workspace-pass-')
            or '/' in args.previous_evidence or '\u0000' in args.previous_evidence
            or not args.previous_evidence.endswith('.json')
            or not args.evidence_prefix.startswith('live-workspace-pass')
            or not args.evidence_prefix.replace('-', '').isalnum()):
        raise ValueError('EVIDENCE_NAME_INVALID')
    paths = Paths.discover()
    config = load_owner_runtime_config(paths)
    if config is None:
        raise ValueError('OWNER_RUNTIME_UNCONFIGURED')
    ledger = Ledger(Database(paths.database_path))
    state = ledger.state()
    if state['armed'] or state['reserved_capacity']:
        raise ValueError('RUNNER_MUST_BE_DISARMED')
    previous = ledger.get(args.previous_request)
    if (previous is None or previous['status'] != args.expected_status
            or previous['code'] != args.expected_code
            or len(previous['effects']) != args.expected_effects
            or previous['reconcile_required']):
        raise ValueError('PREVIOUS_REQUEST_NOT_SETTLED')
    old = json.loads((EVIDENCE / args.previous_evidence).read_text())
    inputs = json.loads(INPUTS.read_text())
    if (old['request_id'] != args.previous_request or old['command_doc_id'] != COMMAND_ID
            or old.get('changeset_doc_id', CHANGESET_ID) != CHANGESET_ID
            or old['changeset_revision'] != inputs['changeset_revision']):
        raise ValueError('PREVIOUS_EVIDENCE_CHANGED')
    old_revision = old.get('command_revision') or old.get('ready_revision')
    old_sha256 = old.get('command_sha256') or old.get('ready_sha256')
    registry = SourceRegistry(ledger.database, paths.state_dir / 'managed')
    imports = ImportGrants(paths.state_dir / 'import-handles', roots=config.import_roots)
    integrations = build_runtime_integrations(paths, ledger, registry, imports, config)
    registration = integrations.drive_registry.get(PROJECT_ID, 'primary')
    if (registration.command_doc_id != COMMAND_ID
            or registration.changeset_doc_id != CHANGESET_ID):
        raise ValueError('REGISTERED_MAILBOX_CHANGED')
    provider = integrations.provider
    require_registered_doc(provider, COMMAND_ID, registration.workspace_folder_id)
    require_registered_doc(provider, CHANGESET_ID, registration.workspace_folder_id)
    observed, _, _ = _read(provider)
    if (observed.revision_id != old_revision
            or observed.sha256 != old_sha256
            or json.loads(observed.text)['request_id'] != args.previous_request):
        raise ValueError('OLD_COMMAND_CHANGED')
    request_id = str(uuid4())
    now = datetime.now(timezone.utc)
    command = {
        'schema_version': '1.0', 'request_id': request_id, 'dispatch': 'DRAFT',
        'runner_id': state['runner_id'], 'epoch': state['epoch'],
        'issued_at': now.isoformat(), 'expires_at': (now + timedelta(minutes=10)).isoformat(),
        'action': 'DELIVER_VERIFY_PUBLISH', 'project_id': PROJECT_ID,
        'workspace_id': 'primary', 'expected_head': inputs['expected_head'],
        'params': {'changeset_doc_id': CHANGESET_ID,
                   'changeset_revision': inputs['changeset_revision'],
                   'profile_id': 'python-quick', 'publication': 'draft_always'},
        'intent': 'Run the disposable EP-05 passing code-and-test change and publish its draft result',
    }
    validate_shape(command)
    draft_text = _json_text(command)
    if not args.apply:
        print(json.dumps({'mode': 'READ_ONLY', 'request_id': request_id,
                          'previous_status': previous['status'], 'command_revision': observed.revision_id,
                          'changeset_revision': inputs['changeset_revision']}))
        return 0
    draft_intent = {'schema_version': '1.0', 'request_id': request_id,
                    'prior_request_id': args.previous_request, 'prior_status': previous['status'],
                    'command_doc_id': COMMAND_ID, 'prior_revision': observed.revision_id,
                    'prior_sha256': observed.sha256, 'draft_sha256': _sha(draft_text),
                    'changeset_doc_id': CHANGESET_ID,
                    'changeset_revision': inputs['changeset_revision'],
                    'created_at': now.isoformat()}
    _evidence_exclusive(EVIDENCE / f'{args.evidence_prefix}-draft-intent.json', draft_intent)
    draft_revision, draft_write = _replace(provider, observed.sha256, draft_text)
    _evidence_exclusive(EVIDENCE / f'{args.evidence_prefix}-draft.json', {
        **draft_intent, 'draft_revision': draft_revision, 'write_state': draft_write,
    })
    print(json.dumps({'event': 'DRAFT_CONFIRMED', 'request_id': request_id}), flush=True)

    client = _client(_session())
    armed = False
    try:
        system = _get(client, '/api/v1/system')
        if system['armed'] or 'DELIVER_VERIFY_PUBLISH' not in system['capabilities']:
            raise ValueError('SERVER_NOT_READY')
        armed_response = _post(client, '/api/v1/local/arm')
        armed = True
        system = _get(client, '/api/v1/system')
        if (not system['armed'] or system['epoch'] != armed_response['epoch']
                or system['runner_id'] != command['runner_id']):
            raise ValueError('ARM_STATE_MISMATCH')
        print(json.dumps({'event': 'ARMED', 'request_id': request_id,
                          'epoch': system['epoch']}), flush=True)
        issued = datetime.now(timezone.utc)
        command.update(dispatch='READY', epoch=system['epoch'],
                       issued_at=issued.isoformat(),
                       expires_at=(issued + timedelta(minutes=10)).isoformat())
        validate_shape(command)
        ready_text = _json_text(command)
        ready_intent = {'schema_version': '1.0', 'request_id': request_id,
                        'command_doc_id': COMMAND_ID, 'draft_revision': draft_revision,
                        'draft_sha256': _sha(draft_text), 'ready_sha256': _sha(ready_text),
                        'runner_id': system['runner_id'], 'epoch': system['epoch'],
                        'issued_at': command['issued_at'], 'expires_at': command['expires_at'],
                        'changeset_revision': inputs['changeset_revision']}
        _evidence_exclusive(EVIDENCE / f'{args.evidence_prefix}-ready-intent.json', ready_intent)
        ready_revision, ready_write = _replace(provider, _sha(draft_text), ready_text)
        _evidence_exclusive(EVIDENCE / f'{args.evidence_prefix}-ready.json', {
            **ready_intent, 'ready_revision': ready_revision, 'write_state': ready_write,
        })
        print(json.dumps({'event': 'READY_CONFIRMED', 'request_id': request_id}), flush=True)
        deadline = time.monotonic() + 900
        last_status: str | None = None
        heartbeat = time.monotonic()
        while time.monotonic() < deadline:
            response = client.get(f'/api/v1/operations/{request_id}')
            if response.status_code == 404:
                status, result = 'NOT_ADMITTED', None
            else:
                response.raise_for_status()
                result = response.json()
                status = result['status']
            if status != last_status or time.monotonic() - heartbeat >= 30:
                print(json.dumps({'event': 'STATUS', 'request_id': request_id, 'status': status,
                                  'code': result.get('code') if result else None,
                                  'effect_count': len(result['effects']) if result else 0}), flush=True)
                last_status, heartbeat = status, time.monotonic()
            if status in TERMINAL and result is not None:
                axes = result.get('result') or {}
                summary = {'schema_version': '1.0', 'request_id': request_id,
                           'status': status, 'code': result.get('code'),
                           'effects': len(result['effects']),
                           'confirmed_effects': sum(effect['state'] == 'CONFIRMED'
                                                    for effect in result['effects']),
                           'result': axes}
                _evidence_exclusive(EVIDENCE / f'{args.evidence_prefix}-result.json', summary)
                print(json.dumps({'event': 'TERMINAL', 'request_id': request_id,
                                  'status': status, 'code': result.get('code'),
                                  'execution': axes.get('execution'),
                                  'publication': axes.get('publication'),
                                  'delivery': axes.get('delivery'),
                                  'report_link': axes.get('report_link')}), flush=True)
                return 0 if status == 'COMPLETED' else 2
            time.sleep(2)
        raise TimeoutError('WORKSPACE_OPERATION_NOT_TERMINAL')
    finally:
        if armed:
            try:
                disarmed = _post(client, '/api/v1/local/disarm')
                print(json.dumps({'event': 'DISARMED', 'armed': disarmed.get('armed')}), flush=True)
            except Exception:  # noqa: BLE001 - preserve original outcome
                print(json.dumps({'event': 'DISARM_UNCONFIRMED'}), flush=True)
        client.close()


if __name__ == '__main__':
    raise SystemExit(main())
