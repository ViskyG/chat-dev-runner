#!/usr/bin/env python3
"""One-shot owner-side Drive input for EP-05's disposable workspace gate.

Default is read-only. --apply records a fresh immutable intent before writing the
registered CHANGESET, then DRAFT COMMAND, once each with a required Docs revision.
No provider write is retried; exact readback classifies an uncertain ack.
The owner must arm locally before promoting DRAFT to READY with the new epoch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from runner_app.adapters.drive_docs import (
    MAX_CHANGESET_BYTES,
    MAX_COMMAND_DOC_BYTES,
    extract_document,
    require_registered_doc,
)
from runner_app.adapters.drive_writer import _plain_body_span
from runner_app.domain.commands import validate_shape
from runner_app.engine.integrations import (
    build_runtime_integrations,
    load_owner_runtime_config,
)
from runner_app.git.argv import run_git
from runner_app.git.changes import validate_changeset
from runner_app.git.imports import ImportGrants
from runner_app.git.registry import SourceRegistry
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger
from runner_app.storage.paths import Paths

EVIDENCE = ROOT / 'verification/implementation/EP-05'
EXPECTED_PROJECT = 'cdr-ep05-drive-20260928'
EXPECTED_REPOSITORY_ID = 1391917494
EXPECTED_COMMAND_ID = '1XTA26PC-9DIbGDka3ec5tkNxttYYouwT39aEe2XxEdY'
EXPECTED_CHANGESET_ID = '1FSwggdAmoyoVrxlm1fi5vkIjs7PWK_F4hZPj3nDEzSY'


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n'


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _evidence_exclusive(path: Path, value: dict[str, Any]) -> None:
    raw = (json.dumps(value, indent=2, sort_keys=True) + '\n').encode('utf-8')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read(provider: Any, document_id: str, maximum: int) -> tuple[dict[str, Any], Any, int, int]:
    document = provider.get_document(document_id, include_tabs_content=True)
    snapshot = extract_document(document, document_id, max_bytes=maximum)
    start, end = _plain_body_span(document, snapshot)
    return document, snapshot, start, end


def _replace_once(provider: Any, document_id: str, desired: str, maximum: int) -> tuple[str, str]:
    _, before, start, end = _read(provider, document_id, maximum)
    if before.text.strip('\n'):
        raise ValueError('INPUT_DOC_NOT_EMPTY')
    if before.tab_id is None or not desired.endswith('\n') or _sha(before.text) == _sha(desired):
        raise ValueError('INPUT_DOC_INVALID')
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
            document_id, requests=requests, required_revision_id=before.revision_id,
        )
    except Exception as error:  # noqa: BLE001 - exact readback decides lost acknowledgements
        write_error = error
    _, after, _, _ = _read(provider, document_id, maximum)
    if (after.text != desired or after.sha256 != _sha(desired)
            or after.revision_id == before.revision_id):
        raise ValueError('INPUT_WRITE_UNCONFIRMED') from None
    return after.revision_id, 'ACK_LOST_EXACT_READBACK' if write_error else 'CONFIRMED'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--phase', choices=('pass', 'fail'), required=True)
    args = parser.parse_args()
    if args.project_id != EXPECTED_PROJECT or args.phase != 'pass':
        raise ValueError('Only the existing first-pass acceptance project is enabled')
    paths = Paths.discover()
    config = load_owner_runtime_config(paths)
    if config is None or config.execution_profile_dir is None:
        raise ValueError('OWNER_RUNTIME_UNCONFIGURED')
    ledger = Ledger(Database(paths.database_path))
    state = ledger.state()
    if state['armed'] or state['reserved_capacity']:
        raise ValueError('RUNNER_MUST_BE_DISARMED')
    registry = SourceRegistry(ledger.database, paths.state_dir / 'managed')
    imports = ImportGrants(paths.state_dir / 'import-handles', roots=config.import_roots)
    integrations = build_runtime_integrations(paths, ledger, registry, imports, config)
    registration = integrations.drive_registry.get(args.project_id, 'primary')
    assert registration.command_doc_id == EXPECTED_COMMAND_ID
    assert registration.changeset_doc_id == EXPECTED_CHANGESET_ID
    project = registry.get_project(args.project_id)
    workspace = registry.get_workspace(args.project_id, 'primary')
    remote = registry.get_project_remote(args.project_id)
    if (project is None or project['state'] != 'READY' or workspace is None
            or workspace['archived'] or workspace['state'] != 'ACTIVE'
            or remote is None or remote['provider_id'] != EXPECTED_REPOSITORY_ID
            or remote['private'] is not True):
        raise ValueError('PROJECT_NOT_READY')
    worktree = Path(workspace['worktree_path'])
    head = workspace['head']
    if (not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}', head)
            or run_git(worktree, ['rev-parse', 'HEAD']).strip() != head
            or run_git(worktree, ['status', '--porcelain=v1']) != ''):
        raise ValueError('WORKSPACE_CHANGED')
    provider = integrations.provider
    require_registered_doc(provider, registration.command_doc_id, registration.workspace_folder_id)
    require_registered_doc(provider, registration.changeset_doc_id, registration.workspace_folder_id)
    _, existing_command, _, _ = _read(provider, registration.command_doc_id, MAX_COMMAND_DOC_BYTES)
    _, existing_change, _, _ = _read(provider, registration.changeset_doc_id, MAX_CHANGESET_BYTES)
    if existing_command.text.strip('\n') or existing_change.text.strip('\n'):
        raise ValueError('INPUT_DOC_NOT_EMPTY')

    feature = 'def double(value: int) -> int:\n    return value * 2\n'
    test = ('from src.feature import double\n\n\ndef test_double() -> None:\n'
            '    assert double(21) == 42\n')
    change = {
        'schema_version': '1.0', 'change_id': str(uuid4()),
        'project_id': args.project_id, 'workspace_id': 'primary', 'base_head': head,
        'files': [
            {'op': 'upsert', 'path': 'src/feature.py', 'expected_sha256': None,
             'content_utf8': feature},
            {'op': 'upsert', 'path': 'tests/test_feature.py', 'expected_sha256': None,
             'content_utf8': test},
        ],
    }
    validate_changeset(change, project_id=args.project_id, workspace_id='primary',
                       expected_head=head)
    change_text = _json_text(change)
    now = datetime.now(timezone.utc)
    request_id = str(uuid4())
    intent = {
        'schema_version': '1.0', 'request_id': request_id, 'phase': args.phase,
        'project_id': args.project_id, 'workspace_id': 'primary',
        'expected_head': head, 'command_doc_id': registration.command_doc_id,
        'changeset_doc_id': registration.changeset_doc_id,
        'changeset_sha256': _sha(change_text), 'change_id': change['change_id'],
        'files': [{'path': item['path'], 'content_sha256': _sha(item['content_utf8'])}
                  for item in change['files']],
        'created_at': now.isoformat(),
    }
    if not args.apply:
        print(json.dumps({'mode': 'READ_ONLY', **intent}, sort_keys=True))
        return 0
    _evidence_exclusive(EVIDENCE / 'live-workspace-pass-intent.json', intent)
    change_revision, change_write = _replace_once(
        provider, registration.changeset_doc_id, change_text, MAX_CHANGESET_BYTES,
    )
    issued = datetime.now(timezone.utc)
    command = {
        'schema_version': '1.0', 'request_id': request_id, 'dispatch': 'DRAFT',
        'runner_id': state['runner_id'], 'epoch': state['epoch'],
        'issued_at': issued.isoformat(),
        'expires_at': (issued + timedelta(minutes=10)).isoformat(),
        'action': 'DELIVER_VERIFY_PUBLISH', 'project_id': args.project_id,
        'workspace_id': 'primary', 'expected_head': head,
        'params': {'changeset_doc_id': registration.changeset_doc_id,
                   'changeset_revision': change_revision,
                   'profile_id': 'python-quick', 'publication': 'draft_always'},
        'intent': 'Run the disposable EP-05 passing code-and-test change and publish its draft result',
    }
    validate_shape(command)
    command_text = _json_text(command)
    command_revision, command_write = _replace_once(
        provider, registration.command_doc_id, command_text, MAX_COMMAND_DOC_BYTES,
    )
    result = {**intent, 'mode': 'INPUTS_STAGED',
              'changeset_revision': change_revision, 'changeset_write': change_write,
              'command_revision': command_revision, 'command_write': command_write,
              'command_sha256': _sha(command_text), 'issued_at': command['issued_at'],
              'expires_at': command['expires_at']}
    _evidence_exclusive(EVIDENCE / 'live-workspace-pass-inputs.json', result)
    print(json.dumps({'mode': 'INPUTS_STAGED', 'request_id': request_id,
                      'changeset_revision': change_revision,
                      'command_revision': command_revision}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
