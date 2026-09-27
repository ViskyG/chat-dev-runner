"""A finite Engine VERIFY with real Git/SQLite and no project-code execution."""
from __future__ import annotations

import errno
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from runner_app.domain.commands import LocalPolicy, Provenance
from runner_app.engine.service import Engine
from runner_app.executors.broker import (
    BrokerError,
    BrokerResult,
    ContainerIntent,
    ContainerReconciliation,
    SnapshotInput,
    _sha256_argv,
)
from runner_app.executors.profiles import ProfileRegistry
from runner_app.executors.reconciliation import ReconciliationError, reconcile_run_attempt
from runner_app.executors.run_service import RunService
from runner_app.executors.verify_handler import VerifyHandler
from runner_app.git.argv import run_git
from runner_app.git.registry import SourceRegistry
from runner_app.git.service import SourceService
from runner_app.git.worktrees import create_repository, create_workspace
from runner_app.outputs.capture import CaptureResult, StreamCapture
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger, LedgerError
from runner_app.storage.run_evidence import RunEvidenceStore

_LOCAL = Provenance(kind="local", source_id="local-owner")


def _profile(tmp_path: Path) -> ProfileRegistry:
    directory = tmp_path / "owner-profiles"
    directory.mkdir(mode=0o700)
    value = {
        "schema_version": "1.0", "profile_id": "fixture-run",
        "execution_backend": "rootless_container",
        "image": "example.invalid/fixture/python@sha256:" + "a" * 64,
        "argv": ["python", "-m", "pytest", "--junitxml=/out/junit.xml"],
        "timeout_seconds": 30, "network": "none", "memory_mb": 64,
        "cpu_limit": 1, "pids_limit": 32, "capabilities": "drop_all",
        "no_new_privileges": True, "read_only_root": True, "secrets": [],
        "output_budget_mb": 8, "platform": "linux/amd64", "user": "0:0",
        "installed": True, "approved": True,
        "collect": [{"id": "junit", "kind": "test_result", "path": "junit.xml",
                     "format": "junit-xml", "required": True,
                     "upload_policy": "local_only"}],
    }
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    path = directory / "fixture-run.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    return ProfileRegistry(directory, {"fixture-run": hashlib.sha256(raw).hexdigest()})


class FakeBroker:
    """Fabricates contained evidence; it never invokes the approved argv."""

    def __init__(self, *, outcome: str = "FAILED", refuse: str | None = None,
                 wait_for_cancel: bool = False, bad_identity: bool = False,
                 include_junit: bool = True) -> None:
        self.outcome = outcome
        self.refuse = refuse
        self.wait_for_cancel = wait_for_cancel
        self.bad_identity = bad_identity
        self.include_junit = include_junit
        self.started = Event()
        self.calls: list[tuple[SnapshotInput, Path, Path | None, Event | None]] = []

    def run(self, profile: Any, snapshot: SnapshotInput, run_root: Path,
            *, run_id: str, writable_volume: Path | None = None,
            cancel: Event | None = None,
            record_intent: Callable[[ContainerIntent], None]) -> BrokerResult:
        self.calls.append((snapshot, run_root, writable_volume, cancel))
        if self.refuse is not None and self.refuse not in {
            "CONTAINER_CLEANUP_FAILED", "CONTAINER_CLEANUP_UNCERTAIN",
        }:
            raise BrokerError(self.refuse, "Synthetic runtime refusal with untrusted details")
        record_intent(ContainerIntent(
            run_id, "fake-rootless-daemon", profile.profile_id, profile.revision_sha256,
            profile.image, "sha256:" + "c" * 64, _sha256_argv(profile.argv), "f" * 32,
        ))
        if self.refuse is not None:
            raise BrokerError(self.refuse, "Synthetic container cleanup refusal")
        assert snapshot.path.is_dir()
        assert (snapshot.path / "README.txt").read_bytes() == b"pinned source\n"
        self.started.set()
        if self.wait_for_cancel:
            assert cancel is not None
            cancel.wait(5)
        outcome = "CANCELLED" if cancel is not None and cancel.is_set() else self.outcome
        run_root.mkdir(mode=0o700)
        capture_dir = run_root / "capture"
        capture_dir.mkdir(mode=0o700)
        assert writable_volume is not None
        output_root = writable_volume / "out"
        output_root.mkdir(mode=0o700)
        if self.include_junit:
            (output_root / "junit.xml").write_bytes(
                b'<testsuite><testcase name="fails"><failure>failure</failure></testcase>'
                b'<testcase name="passes"/></testsuite>'
            )
        streams = []
        for name, content in (("stdout", b"project failed\n"), ("stderr", b"")):
            path = capture_dir / f"{name}.bin"
            path.write_bytes(content)
            path.chmod(0o600)
            streams.append(StreamCapture(
                name=name, raw_path=path, view_path=None,
                observed_bytes=len(content), retained_bytes=len(content),
                raw_sha256=hashlib.sha256(content).hexdigest(), view_sha256=None,
                view_bytes=0, complete=True, truncated_reason=None,
            ))
        now = datetime.now(timezone.utc).isoformat()
        exit_code = 1 if outcome == "FAILED" else 0 if outcome == "PASSED" else None
        capture = CaptureResult(
            started_at=now, ended_at=now, duration_seconds=0.05,
            exit_code=exit_code,
            termination_reason="CANCELLED" if outcome == "CANCELLED" else None,
            capture_fault=None, streams=(streams[0], streams[1]),
            events_path=None, events_sha256=None, metadata_path=None,
            metadata_sha256=None, observed_events=0, retained_events=0,
        )
        result = BrokerResult(
            run_id=run_id, snapshot_id=snapshot.snapshot_id,
            snapshot_commit=snapshot.commit, snapshot_tree=snapshot.tree,
            snapshot_path=snapshot.path, profile_id=profile.profile_id,
            profile_revision_sha256=profile.revision_sha256, image=profile.image,
            argv_sha256=_sha256_argv(profile.argv), sanitized_argv=("<approved-profile>",),
            started_at=now, ended_at=now, duration_seconds=0.05,
            outcome=outcome, exit_code=exit_code, output_root=output_root,
            capture=capture,
            containment={
                "verified": True, "backend": "rootless_docker", "daemon_rootless": True,
                "cleanup_verified": True, "image_repo_digest": profile.image,
                "network": "none", "read_only_source": True,
            },
        )
        return replace(result, snapshot_tree="0" * len(snapshot.tree)) if self.bad_identity else result


@dataclass
class Fixture:
    engine: Engine
    ledger: Ledger
    registry: SourceRegistry
    store: RunEvidenceStore
    broker: FakeBroker
    workspace: Path
    head: str
    volumes: Path

    def command(self, action: str = "VERIFY", *, target: str | None = None) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
            "runner_id": self.ledger.state()["runner_id"], "epoch": self.ledger.state()["epoch"],
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
            "action": action, "project_id": "fixture", "workspace_id": "area",
            "expected_head": self.head,
            "params": ({"target_request_id": target} if target is not None
                       else {"profile_id": "fixture-run"}),
            "intent": "Run one explicitly requested check",
        }

    def prepare_volume(self, request_id: str) -> Path:
        path = self.volumes / request_id
        path.mkdir(mode=0o700)
        return path

    def submit(self, command: dict[str, Any]) -> None:
        result = self.engine.submit(json.dumps(command).encode(), _LOCAL)
        assert result.execute, result.receipt

    def terminal(self, request_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            value = self.ledger.get(request_id)
            if value is not None and value["status"] != "RUNNING":
                return value
            time.sleep(0.01)
        pytest.fail("VERIFY operation never became terminal")


@pytest.fixture
def setup(tmp_path: Path) -> Fixture:
    managed = tmp_path / "managed"
    repo = create_repository(managed, "fixture")
    initial = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()
    workspace = create_workspace(managed, "fixture", repo, "area", "feature/area", initial)
    (workspace / "README.txt").write_bytes(b"pinned source\n")
    run_git(workspace, ["add", "--", "README.txt"])
    run_git(workspace, ["-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
                        "commit", "-m", "Fixture source"])
    head = run_git(workspace, ["rev-parse", "HEAD"]).strip()
    database = Database(tmp_path / "state" / "state.sqlite")
    registry = SourceRegistry(database, managed)
    registry.register_project("fixture", "Fixture", repo, origin_kind="new")
    registry.register_workspace("fixture", "area", "Area", workspace,
                                branch="feature/area", head=head)
    ledger = Ledger(database)
    ledger.startup()
    ledger.arm()
    store = RunEvidenceStore(database, registry, tmp_path / "state" / "evidence")
    broker = FakeBroker()
    sources = SourceService(ledger, registry)
    runs = RunService(ledger, registry, _profile(tmp_path), broker, store,
                      sources_root=tmp_path / "state" / "sealed-sources",
                      runs_root=tmp_path / "state" / "run-slots")
    volumes = tmp_path / "state" / "quota-volumes"
    volumes.mkdir(mode=0o700)
    handler = VerifyHandler(sources, runs, volume_for_run=lambda run_id: volumes / run_id)
    policy = LocalPolicy(project_ids=frozenset({"fixture"}),
                         workspace_pairs=frozenset({("fixture", "area")}),
                         approved_profiles=frozenset({"fixture-run"}))
    engine = Engine(ledger, policy=policy, handlers={"VERIFY": handler})
    fixture = Fixture(engine, ledger, registry, store, broker, workspace, head, volumes)
    yield fixture
    engine.shutdown()


def test_failed_project_check_is_completed_with_pinned_real_git_source(setup: Fixture) -> None:
    command = setup.command()
    volume = setup.prepare_volume(command["request_id"])
    setup.submit(command)
    terminal = setup.terminal(command["request_id"])
    assert terminal["status"] == "COMPLETED" and terminal["code"] is None
    assert terminal["result"]["run_id"] == command["request_id"]
    assert terminal["result"]["execution"]["outcome"] == "FAILED"
    assert terminal["result"]["publication"]["status"] == "NOT_REQUESTED"
    assert terminal["result"]["delivery"]["status"] == "NOT_ATTEMPTED"
    run = setup.ledger.get_run(command["request_id"])
    assert run["snapshot"]["commit"] == setup.head
    assert run["execution"]["outcome"] == "FAILED"
    assert setup.registry.publication_target(command["request_id"], project_id="fixture",
                                              workspace_id="area")["commit"] == setup.head
    assert setup.ledger.get(command["request_id"])["effects"][0]["state"] == "CONFIRMED"
    assert setup.broker.calls[0][2] == volume
    assert setup.broker.calls[0][0].path != setup.workspace
    assert run_git(setup.workspace, ["rev-parse", "HEAD"]).strip() == setup.head
    assert (setup.store.evidence_root / command["request_id"] / "capture" / "stdout.bin").read_bytes() == b"project failed\n"
    visible_intents = [event for event in setup.ledger.events()
                       if event["type"] == "CONTAINER_INTENDED"]
    assert len(visible_intents) == 1
    assert "nonce" not in visible_intents[0]["detail"]
    connection = setup.registry.database.connect()
    try:
        stored = connection.execute(
            "SELECT detail_json FROM journal WHERE request_id=? "
            "AND event_type='CONTAINER_INTENDED'", (command["request_id"],)
        ).fetchone()
        assert len(json.loads(stored[0])["nonce"]) == 32
    finally:
        connection.close()


def test_missing_preprovisioned_volume_blocks_before_source_capture(setup: Fixture) -> None:
    command = setup.command()
    setup.submit(command)
    terminal = setup.terminal(command["request_id"])
    assert terminal["status"] == "BLOCKED"
    assert terminal["code"] == "DISK_QUOTA_UNAVAILABLE"
    assert terminal["result"]["execution"]["outcome"] == "NOT_RUN"
    assert "run_id" not in terminal["result"]
    assert terminal["effects"] == []
    assert setup.broker.calls == []
    assert setup.ledger.get_run(command["request_id"]) is None


def test_dirty_workspace_blocks_without_running_project_code(setup: Fixture) -> None:
    command = setup.command()
    setup.prepare_volume(command["request_id"])
    (setup.workspace / "README.txt").write_text("owner edit\n")
    setup.submit(command)
    terminal = setup.terminal(command["request_id"])
    assert terminal["status"] == "BLOCKED" and terminal["code"] == "DIRTY_WORKSPACE"
    assert terminal["result"]["execution"]["outcome"] == "NOT_RUN"
    assert terminal["effects"][0]["state"] == "CONFIRMED"
    assert setup.broker.calls == []
    assert (setup.workspace / "README.txt").read_text() == "owner edit\n"


def test_runtime_refusal_is_infrastructure_fault_with_no_fabricated_run(setup: Fixture) -> None:
    setup.broker.refuse = "RUNTIME_UNAVAILABLE"
    command = setup.command()
    setup.prepare_volume(command["request_id"])
    setup.submit(command)
    terminal = setup.terminal(command["request_id"])
    assert terminal["status"] == "FAULT" and terminal["code"] == "RUNTIME_UNAVAILABLE"
    assert terminal["result"]["execution"]["outcome"] == "NOT_RUN"
    assert terminal["effects"][0]["state"] == "CONFIRMED"
    assert setup.ledger.get_run(command["request_id"]) is None
    assert len(setup.broker.calls) == 1


def test_uncertain_container_cleanup_keeps_workspace_reserved(setup: Fixture) -> None:
    setup.broker.refuse = "CONTAINER_CLEANUP_UNCERTAIN"
    command = setup.command()
    setup.prepare_volume(command["request_id"])
    setup.submit(command)
    terminal = setup.terminal(command["request_id"])
    assert terminal["status"] == "FAULT"
    assert terminal["code"] == "CONTAINER_CLEANUP_UNCERTAIN"
    assert terminal["reconcile_required"] == 1
    assert terminal["capacity_held"] == 1
    assert terminal["result"]["execution"]["outcome"] == "UNKNOWN"
    assert setup.ledger.get_run(command["request_id"]) is None
    connection = setup.registry.database.connect()
    try:
        assert connection.execute(
            "SELECT scope FROM locks WHERE request_id=?", (command["request_id"],)
        ).fetchone() == ("workspace:fixture/area",)
    finally:
        connection.close()


def test_trusted_recovery_releases_lock_only_after_exact_container_proof(
    setup: Fixture,
) -> None:
    setup.broker.refuse = "CONTAINER_CLEANUP_UNCERTAIN"
    command = setup.command()
    setup.prepare_volume(command["request_id"])
    setup.submit(command)
    assert setup.terminal(command["request_id"])["reconcile_required"] == 1
    setup.ledger.disarm()
    with pytest.raises(LedgerError) as still_held:
        setup.ledger.arm()
    assert still_held.value.code == "NEEDS_RECONCILIATION"
    with pytest.raises(LedgerError) as no_container_proof:
        setup.ledger.confirm_stopped(command["request_id"])
    assert no_container_proof.value.code == "CONTAINER_PROOF_REQUIRED"

    def prove_removed(intent: ContainerIntent) -> ContainerReconciliation:
        assert intent.run_id == command["request_id"]
        assert intent.daemon_id == "fake-rootless-daemon"
        return ContainerReconciliation(intent.run_id, intent.daemon_id,
                                       "REMOVED", "d" * 64)

    proof = reconcile_run_attempt(
        setup.ledger, SimpleNamespace(reconcile_container=prove_removed), command["request_id"],
    )
    assert proof.state == "REMOVED"
    assert setup.ledger.get(command["request_id"])["reconcile_required"] == 0
    assert setup.ledger.arm()["armed"] is True
    assert any(event["type"] == "CONTAINER_RECONCILED" for event in setup.ledger.events())


def test_invalid_recovery_proof_keeps_uncertain_workspace_reserved(
    setup: Fixture,
) -> None:
    setup.broker.refuse = "CONTAINER_CLEANUP_UNCERTAIN"
    command = setup.command()
    setup.prepare_volume(command["request_id"])
    setup.submit(command)
    assert setup.terminal(command["request_id"])["reconcile_required"] == 1

    def wrong_proof(intent: ContainerIntent) -> ContainerReconciliation:
        return ContainerReconciliation(str(uuid4()), intent.daemon_id, "REMOVED", "d" * 64)

    with pytest.raises(ReconciliationError) as error:
        reconcile_run_attempt(
            setup.ledger, SimpleNamespace(reconcile_container=wrong_proof),
            command["request_id"],
        )
    assert error.value.code == "CONTAINER_PROOF_INVALID"
    retained = setup.ledger.get(command["request_id"])
    assert retained["reconcile_required"] == 1
    assert retained["capacity_held"] == 1


def test_cancellation_reaches_broker_and_retains_cancelled_evidence(setup: Fixture) -> None:
    setup.broker.wait_for_cancel = True
    command = setup.command()
    setup.prepare_volume(command["request_id"])
    setup.submit(command)
    assert setup.broker.started.wait(8)
    cancel = setup.command("CANCEL_OPERATION", target=command["request_id"])
    setup.submit(cancel)
    assert setup.terminal(cancel["request_id"])["status"] == "COMPLETED"
    terminal = setup.terminal(command["request_id"])
    assert terminal["status"] == "CANCELLED"
    assert terminal["result"]["run_id"] == command["request_id"]
    assert terminal["result"]["execution"]["outcome"] == "CANCELLED"
    assert setup.ledger.get_run(command["request_id"])["execution"]["outcome"] == "CANCELLED"
    assert setup.broker.calls[0][3] is not None
    assert setup.broker.calls[0][3].is_set()


def test_hard_evidence_enospc_is_a_nonreplayed_storage_fault(
    setup: Fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = setup.command()
    setup.prepare_volume(command["request_id"])

    def full_store(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "synthetic private filesystem detail")

    monkeypatch.setattr(setup.store, "record_contained_run", full_store)
    setup.submit(command)
    terminal = setup.terminal(command["request_id"])
    assert terminal["status"] == "FAULT"
    assert terminal["code"] == "EVIDENCE_STORAGE_FULL"
    assert terminal["result"]["execution"]["outcome"] == "UNKNOWN"
    assert setup.ledger.get_run(command["request_id"]) is None
    assert len(setup.broker.calls) == 1
    connection = setup.registry.database.connect()
    try:
        assert connection.execute(
            "SELECT run_id FROM run_attempts WHERE request_id=?", (command["request_id"],)
        ).fetchone() == (command["request_id"],)
    finally:
        connection.close()
    duplicate = setup.engine.submit(json.dumps(command).encode(), _LOCAL)
    assert duplicate.duplicate and not duplicate.execute
    assert len(setup.broker.calls) == 1
    assert "synthetic private filesystem detail" not in json.dumps(terminal)


def test_invalid_broker_identity_is_fault_without_claimed_test_result(setup: Fixture) -> None:
    setup.broker.bad_identity = True
    command = setup.command()
    setup.prepare_volume(command["request_id"])
    setup.submit(command)
    terminal = setup.terminal(command["request_id"])
    assert terminal["status"] == "FAULT" and terminal["code"] == "BROKER_IDENTITY_MISMATCH"
    assert terminal["result"]["execution"]["outcome"] == "UNKNOWN"
    assert setup.ledger.get_run(command["request_id"]) is None


def test_unknown_broker_code_cannot_enter_receipt_or_result(setup: Fixture) -> None:
    setup.broker.refuse = "private path /not/for/receipts"
    command = setup.command()
    setup.prepare_volume(command["request_id"])
    setup.submit(command)
    terminal = setup.terminal(command["request_id"])
    assert terminal["status"] == "FAULT" and terminal["code"] == "BROKER_STEP_FAILED"
    assert "/not/for/receipts" not in json.dumps(terminal)


def test_missing_required_output_keeps_known_project_failure_separate(setup: Fixture) -> None:
    setup.broker.include_junit = False
    command = setup.command()
    setup.prepare_volume(command["request_id"])
    setup.submit(command)
    terminal = setup.terminal(command["request_id"])
    assert terminal["status"] == "FAULT"
    assert terminal["code"] == "OUTPUT_COLLECTION_INCOMPLETE"
    assert terminal["result"]["run_id"] == command["request_id"]
    assert terminal["result"]["execution"]["outcome"] == "FAILED"
    run = setup.ledger.get_run(command["request_id"])
    assert run["operation_status"] == "FAULT"
    assert run["execution"]["outcome"] == "FAILED"
