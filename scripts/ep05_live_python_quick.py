#!/usr/bin/env python3
"""Disposable real broker PREPARATION→TEST acceptance for the python-basic starter.

Uses one already mounted EP-03 hard-quota pair, owner-approved local profiles,
real Git and SQLite. Never arms the production runner or touches cloud providers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid5
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner_app.domain.commands import LocalPolicy, Provenance
from runner_app.engine.service import Engine
from runner_app.executors.broker import (
    RootlessDockerBroker,
    _run_volume_budget,
    _verified_run_volume,
)
from runner_app.executors.preparation_coordinator import TrustedPreparationCoordinator
from runner_app.executors.preparation_plan import PreparationGrant
from runner_app.executors.profiles import ProfileRegistry
from runner_app.executors.run_service import RunService, TrustedPreparation
from runner_app.executors.verify_handler import VerifyHandler
from runner_app.git.argv import run_git
from runner_app.git.registry import SourceRegistry
from runner_app.git.service import SourceService
from runner_app.git.templates import starter_files
from runner_app.git.worktrees import create_repository, create_workspace
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger
from runner_app.storage.paths import ensure_private_directory
from runner_app.storage.run_evidence import RunEvidenceStore

POOL = ROOT / 'verification/implementation/EP-03/toy-volume-pool.tsv'
EVIDENCE = ROOT / 'verification/implementation/EP-05/live-python-quick'
VOLUME_BASE = Path('/home/visky/.local/share/chat-dev-runner/volumes')
OWNER_PROFILES = Path('/home/visky/Work/GPTRunner/owner-profiles')
PROJECT = 'python-quick-acceptance'
WORKSPACE = 'primary'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request-id', required=True)
    args = parser.parse_args()
    request_id = args.request_id
    if str(UUID(request_id)) != request_id:
        raise ValueError('canonical request UUID required')
    pool = {}
    for line in POOL.read_text(encoding='ascii').splitlines():
        if line and not line.startswith('#'):
            request, preparation_id = line.split('\t')
            if str(uuid5(UUID(request), 'preparation')) != preparation_id:
                raise ValueError('EP-03 pool identity changed')
            pool[request] = preparation_id
    if request_id not in pool:
        raise ValueError('request ID must come from the preprovisioned acceptance pool')
    prep_id = pool[request_id]
    test_volume = VOLUME_BASE / request_id
    prep_volume = VOLUME_BASE / prep_id
    grant_data = json.loads((OWNER_PROFILES / 'python-quick-grant.json').read_text())
    hashes = grant_data['profile_revision_sha256']
    profiles = ProfileRegistry(OWNER_PROFILES, hashes)
    test_profile = profiles.require('python-quick')
    prep_profile = profiles.require('python-prepare')
    if test_profile.image != prep_profile.image:
        raise ValueError('owner profiles use different images')
    _verified_run_volume(test_volume, request_id, _run_volume_budget(test_profile))
    _verified_run_volume(prep_volume, prep_id, _run_volume_budget(prep_profile))
    if test_volume.stat().st_dev == prep_volume.stat().st_dev:
        raise ValueError('phase volumes share a device')
    ensure_private_directory(EVIDENCE)
    evidence_dir = EVIDENCE / request_id
    if evidence_dir.exists():
        raise ValueError('request already has acceptance evidence')
    ensure_private_directory(evidence_dir)
    temp_root = Path(tempfile.mkdtemp(prefix='cdr-python-quick-accept-', dir='/tmp'))
    temp_root.chmod(0o700)
    managed = temp_root / 'managed'
    repo = create_repository(managed, PROJECT, starter_files=starter_files('python-basic', PROJECT))
    initial = run_git(repo, ['rev-parse', 'refs/heads/main']).strip()
    workspace = create_workspace(managed, PROJECT, repo, WORKSPACE,
                                 'work/python-quick-acceptance', initial)
    head = run_git(workspace, ['rev-parse', 'HEAD']).strip()
    ensure_private_directory(evidence_dir / 'state')
    database = Database(evidence_dir / 'state' / 'runner.sqlite')
    registry = SourceRegistry(database, managed)
    registry.register_project(PROJECT, 'Python quick acceptance', repo, origin_kind='new')
    registry.register_workspace(PROJECT, WORKSPACE, 'Primary', workspace,
                                branch='work/python-quick-acceptance', head=head)
    ledger = Ledger(database)
    ledger.startup()
    ledger.arm()
    broker = RootlessDockerBroker(profiles, runtime_dir=Path(f'/run/user/{os.getuid()}'))
    probe = broker.probe()
    if not probe.available:
        raise ValueError(f'rootless broker unavailable: {probe.code}')
    run_evidence = RunEvidenceStore(database, registry, evidence_dir / 'test-evidence')
    service = RunService(
        ledger, registry, profiles, broker, run_evidence,
        sources_root=temp_root / 'sealed-sources', runs_root=temp_root / 'run-slots',
    )
    coordinator = TrustedPreparationCoordinator(
        ledger, profiles, broker, evidence_root=evidence_dir / 'preparation-evidence',
    )
    grant = PreparationGrant(**grant_data['preparation_grant'])
    handler = VerifyHandler(
        SourceService(ledger, registry), service,
        volume_for_run=lambda run_id: test_volume if run_id == request_id else None,
        preparation_for_run=lambda run_id, profile_id: TrustedPreparation(
            coordinator, grant, prep_volume, service.runs_root / prep_id,
        ) if run_id == request_id and profile_id == 'python-quick' else None,
    )
    policy = LocalPolicy(
        project_ids=frozenset({PROJECT}),
        workspace_pairs=frozenset({(PROJECT, WORKSPACE)}),
        approved_profiles=frozenset({'python-quick'}),
    )
    engine = Engine(ledger, policy=policy, handlers={'VERIFY': handler})
    now = datetime.now(timezone.utc)
    command = {
        'schema_version': '1.0', 'request_id': request_id, 'dispatch': 'READY',
        'runner_id': ledger.state()['runner_id'], 'epoch': ledger.state()['epoch'],
        'issued_at': now.isoformat(), 'expires_at': (now + timedelta(minutes=5)).isoformat(),
        'action': 'VERIFY', 'project_id': PROJECT, 'workspace_id': WORKSPACE,
        'expected_head': head, 'params': {'profile_id': 'python-quick'},
        'intent': 'Verify the hash-locked python-basic starter in the real rootless broker',
    }
    accepted = engine.submit(json.dumps(command).encode(), Provenance('local', 'owner-acceptance'))
    if not accepted.execute:
        raise ValueError('disposable VERIFY was not admitted')
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        operation = ledger.get(request_id)
        if operation is not None and operation['status'] != 'RUNNING':
            break
        time.sleep(0.05)
    else:
        raise ValueError('disposable VERIFY did not finish before watchdog')
    assert operation is not None
    preparation = ledger.get_preparation_result(request_id)
    manifest = ledger.get_run(request_id)
    junit = test_volume / 'out' / 'junit.xml'
    parsed = None
    if junit.is_file():
        root = ElementTree.parse(junit).getroot()
        suite = root.find('testsuite') if root.tag == 'testsuites' else root
        if suite is not None:
            parsed = {name: suite.attrib.get(name) for name in ('tests', 'failures', 'errors')}
    events = [event for event in ledger.events() if event['request_id'] == request_id]
    phases = [event['detail']['phase'] for event in events
              if event['type'] == 'CONTAINER_INTENDED']
    success = (operation['status'] == 'COMPLETED' and operation['code'] is None
               and preparation is not None and preparation['outcome'] == 'PASSED'
               and manifest is not None and manifest['execution']['outcome'] == 'PASSED'
               and parsed == {'tests': '1', 'failures': '0', 'errors': '0'}
               and phases == ['PREPARATION', 'TEST'])
    summary = {
        'schema_version': '1.0', 'request_id': request_id,
        'operation_status': operation['status'], 'operation_code': operation['code'],
        'preparation_outcome': preparation['outcome'] if preparation else None,
        'execution_outcome': manifest['execution']['outcome'] if manifest else None,
        'junit': parsed,
        'junit_sha256': hashlib.sha256(junit.read_bytes()).hexdigest() if junit.is_file() else None,
        'container_intent_phases': phases,
        'rootless_daemon_id': probe.daemon_id,
        'image': test_profile.image,
        'preparation_profile_sha256': prep_profile.revision_sha256,
        'test_profile_sha256': test_profile.revision_sha256,
        'temporary_acceptance_pair_only': True,
        'production_runner_armed': False,
        'success': success,
        'retained_temp_root': str(temp_root),
        'evidence_dir': str(evidence_dir),
    }
    (evidence_dir / 'summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True)+'\n')
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())
