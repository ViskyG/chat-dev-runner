"""Real Git/SQLite/Engine, fake-contained two-phase VERIFY without project execution."""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import pytest
from test_preparation_coordinator import FakePreparationBroker
from test_preparation_plan import _profiles
from test_verify_handler import FakeBroker as FakeTestBroker

import runner_app.executors.preparation_coordinator as coordinator_module
from runner_app.domain.commands import LocalPolicy, Provenance
from runner_app.engine.service import Engine
from runner_app.executors.broker import (
    BrokerResult,
    ContainerIntent,
    ContainerReconciliation,
    SnapshotInput,
)
from runner_app.executors.preparation_coordinator import (
    TrustedPreparationCoordinator,
    preparation_run_id,
)
from runner_app.executors.run_service import RunService, TrustedPreparation
from runner_app.executors.verify_handler import VerifyHandler
from runner_app.git.argv import run_git
from runner_app.git.registry import SourceRegistry
from runner_app.git.service import SourceService
from runner_app.git.worktrees import create_repository, create_workspace
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger
from runner_app.storage.run_evidence import RunEvidenceStore

_LOCAL = Provenance(kind="local", source_id="local-owner")


class TwoPhaseFakeBroker:
    """Records phase calls and delegates to finite in-memory fake containers."""

    def __init__(self, *, preparation_outcome: str = "PASSED") -> None:
        self.preparation = FakePreparationBroker(outcome=preparation_outcome)
        self.test = FakeTestBroker(outcome="PASSED", include_junit=False)
        self.phases: list[str] = []
        self.bad_attestation = False

    def run(self, profile: Any, snapshot: SnapshotInput, run_root: Path, *,
            run_id: str, writable_volume: Path | None = None,
            cancel: Event | None = None,
            record_intent: Callable[[ContainerIntent], None],
            record_cleanup: Callable[[ContainerReconciliation], None],
            preparation_plan: Any = None, staged_build: Any = None) -> BrokerResult:
        if profile.profile_id == "python-prepare":
            assert preparation_plan is None and staged_build is None
            self.phases.append("PREPARATION")
            return self.preparation.run(
                profile, snapshot, run_root, run_id=run_id,
                writable_volume=writable_volume, cancel=cancel,
                record_intent=record_intent, record_cleanup=record_cleanup,
            )
        assert profile.profile_id == "python-quick"
        assert preparation_plan is not None and staged_build is not None
        assert writable_volume is not None
        assert staged_build.source_build == writable_volume / "build"
        assert (writable_volume / "build" / "module.py").read_bytes() == b"VALUE = 7\n"
        assert staged_build.preparation_run_id != run_id
        self.phases.append("TEST")
        result = self.test.run(
            profile, snapshot, run_root, run_id=run_id,
            writable_volume=writable_volume, cancel=cancel,
            record_intent=record_intent, record_cleanup=record_cleanup,
            preparation_plan=preparation_plan, staged_build=staged_build,
        )
        return replace(result, containment={
            **result.containment,
            "preparation_plan_sha256": ("0" * 64 if self.bad_attestation
                                        else preparation_plan.identity_sha256),
            "preparation_run_id": staged_build.preparation_run_id,
            "prepared_build_tree_sha256": staged_build.tree_sha256,
        })


@dataclass
class Fixture:
    engine: Engine
    ledger: Ledger
    store: RunEvidenceStore
    broker: TwoPhaseFakeBroker
    workspace: Path
    head: str
    volumes: Path
    prep_evidence: Path

    def command(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "schema_version": "1.0", "request_id": str(uuid4()),
            "dispatch": "READY", "runner_id": self.ledger.state()["runner_id"],
            "epoch": self.ledger.state()["epoch"],
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
            "action": "VERIFY", "project_id": "fixture", "workspace_id": "area",
            "expected_head": self.head, "params": {"profile_id": "python-quick"},
            "intent": "Run one prepared, contained check",
        }

    def prepare_test_volume(self, request_id: str) -> Path:
        volume = self.volumes / request_id
        volume.mkdir(mode=0o700)
        return volume

    def submit_and_wait(self, command: dict[str, Any]) -> dict[str, Any]:
        admitted = self.engine.submit(json.dumps(command).encode(), _LOCAL)
        assert admitted.execute, admitted.receipt
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            operation = self.ledger.get(command["request_id"])
            if operation is not None and operation["status"] != "RUNNING":
                return operation
            time.sleep(0.01)
        pytest.fail("Prepared VERIFY did not become terminal")


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
          request: pytest.FixtureRequest) -> Fixture:
    managed = tmp_path / "managed"
    repo = create_repository(managed, "fixture")
    initial = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()
    workspace = create_workspace(managed, "fixture", repo, "area", "feature/area", initial)
    (workspace / "README.txt").write_bytes(b"pinned source\n")
    (workspace / "requirements.lock").write_bytes(b"sample==1.0 --hash=sha256:abc\n")
    run_git(workspace, ["add", "--", "README.txt", "requirements.lock"])
    run_git(workspace, ["-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
                        "commit", "-m", "Prepared source"])
    head = run_git(workspace, ["rev-parse", "HEAD"]).strip()
    state = tmp_path / "state"
    database = Database(state / "state.sqlite")
    registry = SourceRegistry(database, managed)
    registry.register_project("fixture", "Fixture", repo, origin_kind="new")
    registry.register_workspace("fixture", "area", "Area", workspace,
                                branch="feature/area", head=head)
    ledger = Ledger(database)
    ledger.startup()
    ledger.arm()
    profiles, grant, _ = _profiles(tmp_path)
    store = RunEvidenceStore(database, registry, state / "evidence")
    broker = TwoPhaseFakeBroker(preparation_outcome=request.param)
    sources_root = state / "sealed-sources"
    run_roots = state / "run-slots"
    runs = RunService(ledger, registry, profiles, broker, store,
                      sources_root=sources_root, runs_root=run_roots)
    prep_evidence = state / "preparation-evidence"
    coordinator = TrustedPreparationCoordinator(
        ledger, profiles, broker, evidence_root=prep_evidence,
    )
    volumes = state / "quota-volumes"
    volumes.mkdir(mode=0o700)
    # Only the quota/mount provider is fake here. Source, ledger, Engine and
    # capture evidence still use their real implementations.
    monkeypatch.setattr(coordinator_module, "_verified_run_volume",
                        lambda volume, _run_id, _budget, **_kwargs: volume)
    monkeypatch.setattr(coordinator_module, "_separate_volume_devices",
                        lambda _preparation, _test: True)

    def preparation_for(request_id: str, profile_id: str) -> TrustedPreparation:
        assert profile_id == "python-quick"
        prep_id = preparation_run_id(request_id)
        prep_volume = volumes / prep_id
        prep_volume.mkdir(mode=0o700)
        return TrustedPreparation(
            coordinator, grant, prep_volume, run_roots / prep_id,
        )

    handler = VerifyHandler(
        SourceService(ledger, registry), runs,
        volume_for_run=lambda run_id: volumes / run_id,
        preparation_for_run=preparation_for,
    )
    policy = LocalPolicy(
        project_ids=frozenset({"fixture"}),
        workspace_pairs=frozenset({("fixture", "area")}),
        approved_profiles=frozenset({"python-quick"}),
    )
    engine = Engine(ledger, policy=policy, handlers={"VERIFY": handler})
    yield Fixture(engine, ledger, store, broker, workspace, head, volumes,
                  prep_evidence)
    engine.shutdown()


@pytest.mark.parametrize("setup", ["PASSED"], indirect=True)
def test_prepared_verify_persists_both_phases_then_completes_one_test(
        setup: Fixture) -> None:
    command = setup.command()
    request_id = command["request_id"]
    test_volume = setup.prepare_test_volume(request_id)
    operation = setup.submit_and_wait(command)
    prep_id = preparation_run_id(request_id)
    assert operation["status"] == "COMPLETED" and operation["code"] is None
    assert setup.broker.phases == ["PREPARATION", "TEST"]
    assert operation["result"]["execution"]["outcome"] == "PASSED"
    assert operation["result"]["preparation"]["run_id"] == prep_id
    assert operation["result"]["preparation"]["outcome"] == "PASSED"
    assert operation["result"]["run_id"] == request_id
    run = setup.ledger.get_run(request_id)
    assert run is not None and run["execution"]["outcome"] == "PASSED"
    assert run["snapshot"]["commit"] == setup.head
    assert (test_volume / "build" / "module.py").read_bytes() == b"VALUE = 7\n"
    assert (setup.store.evidence_root / request_id / "capture" / "stdout.bin").exists()
    receipt = setup.prep_evidence / prep_id / "preparation.json"
    raw = receipt.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == operation["result"]["preparation"]["evidence_sha256"]
    events = [event for event in setup.ledger.events() if event["request_id"] == request_id]
    intents = [event for event in events if event["type"] == "CONTAINER_INTENDED"]
    assert [event["detail"]["phase"] for event in intents] == ["PREPARATION", "TEST"]
    assert all("nonce" not in event["detail"] and "source_path" not in event["detail"]
               and "test_volume_path" not in event["detail"] for event in intents)
    connection = setup.ledger.database.connect()
    try:
        assert connection.execute(
            "SELECT phase,run_id FROM run_attempts WHERE request_id=? ORDER BY phase",
            (request_id,),
        ).fetchall() == [("PREPARATION", prep_id), ("TEST", request_id)]
        handoff = connection.execute(
            "SELECT preparation_run_id,test_run_id,build_tree_sha256 "
            "FROM preparation_handoffs WHERE request_id=?", (request_id,),
        ).fetchone()
        assert handoff is not None and handoff[0] == prep_id and handoff[1] == request_id
        assert len(handoff[2]) == 64
    finally:
        connection.close()


@pytest.mark.parametrize("setup", ["FAILED"], indirect=True)
def test_failed_preparation_returns_reviewable_evidence_and_never_starts_test(
        setup: Fixture) -> None:
    command = setup.command()
    request_id = command["request_id"]
    test_volume = setup.prepare_test_volume(request_id)
    operation = setup.submit_and_wait(command)
    prep_id = preparation_run_id(request_id)
    assert operation["status"] == "BLOCKED" and operation["code"] == "PREPARATION_FAILED"
    assert operation["result"]["execution"]["outcome"] == "NOT_RUN"
    assert operation["result"]["preparation"]["run_id"] == prep_id
    assert operation["result"]["preparation"]["outcome"] == "FAILED"
    assert setup.broker.phases == ["PREPARATION"]
    assert setup.ledger.get_run(request_id) is None
    assert list(test_volume.iterdir()) == []
    receipt = setup.prep_evidence / prep_id / "preparation.json"
    raw = receipt.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == operation["result"]["preparation"]["evidence_sha256"]
    events = [event for event in setup.ledger.events() if event["request_id"] == request_id]
    assert [event["detail"]["phase"] for event in events
            if event["type"] == "CONTAINER_INTENDED"] == ["PREPARATION"]
    connection = setup.ledger.database.connect()
    try:
        result = connection.execute(
            "SELECT outcome,evidence_sha256 FROM preparation_results WHERE request_id=?",
            (request_id,),
        ).fetchone()
        assert result == ("FAILED", hashlib.sha256(raw).hexdigest())
        assert connection.execute(
            "SELECT 1 FROM preparation_handoffs WHERE request_id=?", (request_id,),
        ).fetchone() is None
    finally:
        connection.close()
    assert operation["reconcile_required"] == 0


@pytest.mark.parametrize("setup", ["PASSED"], indirect=True)
def test_mismatched_test_broker_attestation_cannot_register_complete_run(
        setup: Fixture) -> None:
    setup.broker.bad_attestation = True
    command = setup.command()
    request_id = command["request_id"]
    setup.prepare_test_volume(request_id)
    operation = setup.submit_and_wait(command)
    assert operation["status"] == "FAULT"
    assert operation["code"] == "PREPARATION_ATTESTATION_MISMATCH"
    assert setup.broker.phases == ["PREPARATION", "TEST"]
    assert setup.ledger.get_run(request_id) is None
    assert operation["reconcile_required"] == 0
    connection = setup.ledger.database.connect()
    try:
        assert connection.execute(
            "SELECT 1 FROM preparation_handoffs WHERE request_id=?", (request_id,),
        ).fetchone() is not None
    finally:
        connection.close()
