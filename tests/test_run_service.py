"""Offline EP-03 orchestration checks: real Git/SQLite, synthetic broker, no project execution."""
from __future__ import annotations

import errno
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import pytest

from runner_app.domain.commands import freeze
from runner_app.executors.broker import (
    BrokerError,
    BrokerResult,
    ContainerIntent,
    SnapshotInput,
    _sha256_argv,
)
from runner_app.executors.profiles import ProfileError, ProfileRegistry
from runner_app.executors.run_service import RunService, RunServiceError
from runner_app.git.argv import run_git
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.git.snapshots import capture_clean_verify
from runner_app.git.worktrees import create_repository, create_workspace
from runner_app.outputs.capture import CaptureResult, StreamCapture
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger
from runner_app.storage.run_evidence import RunEvidenceStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _profile(root: Path, *, required: bool = True) -> ProfileRegistry:
    """Synthetic local grant for a fake only; its image digest makes no install claim."""
    directory = root / "owner-profiles"
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
                     "format": "junit-xml", "required": required,
                     "upload_policy": "local_only"}],
    }
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    path = directory / "fixture-run.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    return ProfileRegistry(directory, {"fixture-run": _sha(raw)})


class FakeBroker:
    def __init__(self, *, outcome: str = "FAILED", junit: bool = True,
                 truncate: bool = False, mutation: str | None = None,
                 fail_before_slot: bool = False, capture_fault: str | None = None,
                 event_drop: bool = False) -> None:
        self.outcome = outcome
        self.junit = junit
        self.truncate = truncate
        self.mutation = mutation
        self.fail_before_slot = fail_before_slot
        self.capture_fault = capture_fault
        self.event_drop = event_drop
        self.calls: list[tuple[SnapshotInput, Path, Path | None, Event | None]] = []

    def run(self, profile: Any, snapshot: SnapshotInput, run_root: Path,
            *, run_id: str, writable_volume: Path | None = None,
            cancel: Event | None = None,
            record_intent: Callable[[ContainerIntent], None]) -> BrokerResult:
        self.calls.append((snapshot, run_root, writable_volume, cancel))
        if self.fail_before_slot:
            raise BrokerError("ROOTLESS_RUNTIME_UNAVAILABLE", "Synthetic runtime refusal")
        record_intent(ContainerIntent(
            run_id, "fake-rootless-daemon", profile.profile_id, profile.revision_sha256,
            profile.image, "sha256:" + "c" * 64, _sha256_argv(profile.argv), "f" * 32,
        ))
        assert snapshot.path.is_dir()
        assert (snapshot.path / "README.txt").read_bytes() == b"pinned source\n"
        run_root.mkdir(mode=0o700)
        capture_dir = run_root / "capture"
        capture_dir.mkdir(mode=0o700)
        output_root = (writable_volume if writable_volume is not None else run_root) / "out"
        output_root.mkdir(mode=0o700)
        if self.junit:
            (output_root / "junit.xml").write_bytes(
                b'<testsuite><testcase name="fails"><failure>failure</failure></testcase>'
                b'<testcase name="passes"/></testsuite>'
            )
        streams = []
        for name, original in (("stdout", b"project failed\n"), ("stderr", b"")):
            storage_fault = (self.capture_fault == "STORAGE_WRITE_FAILED:OSError"
                             and name == "stdout")
            content = original[:5] if storage_fault else original
            path = capture_dir / f"{name}.bin"
            path.write_bytes(content)
            path.chmod(0o600)
            streams.append(StreamCapture(
                name=name, raw_path=path, view_path=None,
                observed_bytes=len(original) + (100 if self.truncate and name == "stdout" else 0),
                retained_bytes=len(content), raw_sha256=_sha(content), view_sha256=None,
                view_bytes=0, complete=not (storage_fault or self.truncate and name == "stdout"),
                truncated_reason=("storage_error" if storage_fault else
                                  "stream_limit" if self.truncate and name == "stdout" else None),
            ))
        exit_code = (1 if self.outcome == "FAILED" else 0 if self.outcome == "PASSED"
                     else 137 if self.outcome == "UNKNOWN" else None)
        now = _now()
        metadata_path = None
        metadata_digest = None
        if self.capture_fault is not None and not self.capture_fault.startswith("METADATA_WRITE_FAILED"):
            metadata_path = capture_dir / "capture.json"
            metadata = (json.dumps({"capture_fault": self.capture_fault}) + "\n").encode()
            metadata_path.write_bytes(metadata)
            metadata_path.chmod(0o600)
            metadata_digest = _sha(metadata)
        events_path = None
        events_digest = None
        if self.event_drop:
            events_path = capture_dir / "events.jsonl"
            events = b'{"sequence":1}\n'
            events_path.write_bytes(events)
            events_path.chmod(0o600)
            events_digest = _sha(events)
        capture = CaptureResult(
            started_at=now, ended_at=now, duration_seconds=0.05,
            exit_code=exit_code, termination_reason=("CANCELLED" if self.outcome == "CANCELLED"
                                                     else "TIMED_OUT" if self.outcome == "TIMED_OUT"
                                                     else "RESOURCE_LIMIT" if self.outcome == "UNKNOWN"
                                                     else None),
            capture_fault=self.capture_fault, streams=(streams[0], streams[1]),
            events_path=events_path, events_sha256=events_digest,
            metadata_path=metadata_path, metadata_sha256=metadata_digest,
            observed_events=2 if self.event_drop else 0,
            retained_events=1 if self.event_drop else 0,
        )
        result = BrokerResult(
            run_id=run_id, snapshot_id=snapshot.snapshot_id,
            snapshot_commit=snapshot.commit, snapshot_tree=snapshot.tree,
            snapshot_path=snapshot.path, profile_id=profile.profile_id,
            profile_revision_sha256=profile.revision_sha256, image=profile.image,
            argv_sha256=_sha256_argv(profile.argv), sanitized_argv=("<approved-profile>",),
            started_at=now, ended_at=now, duration_seconds=0.05,
            outcome=self.outcome, exit_code=exit_code, output_root=output_root,
            capture=capture,
            containment={
                "verified": True, "backend": "rootless_docker", "daemon_rootless": True,
                "cleanup_verified": True, "image_repo_digest": profile.image,
                "network": "none", "read_only_source": True,
            },
        )
        if self.mutation == "tree":
            return replace(result, snapshot_tree="0" * len(snapshot.tree))
        if self.mutation == "commit":
            return replace(result, snapshot_commit="0" * len(snapshot.commit))
        if self.mutation == "source-path":
            return replace(result, snapshot_path=run_root)
        if self.mutation == "profile-revision":
            return replace(result, profile_revision_sha256="0" * 64)
        if self.mutation == "argv":
            return replace(result, argv_sha256="0" * 64)
        if self.mutation == "capture-path":
            bad_stream = replace(capture.streams[0], raw_path=run_root / "outside.bin")
            return replace(result, capture=replace(capture, streams=(bad_stream, capture.streams[1])))
        return result


@dataclass
class Fixture:
    service: RunService
    broker: FakeBroker
    ledger: Ledger
    registry: SourceRegistry
    store: RunEvidenceStore
    request_id: str
    snapshot_id: str
    snapshot_commit: str
    snapshot_tree: str
    volume: Path


def _fixture(tmp_path: Path, *, broker: FakeBroker | None = None,
             approved: bool = True, effect: bool = True,
             required: bool = True) -> Fixture:
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
    snapshot = capture_clean_verify(repo, workspace, managed / "fixture" / "snapshots",
                                    project_id="fixture", workspace_id="area",
                                    expected_head=head, source_generation=0)
    registry.register_snapshot_record(snapshot)
    ledger = Ledger(database)
    ledger.startup()
    state = ledger.arm()
    request_id = str(uuid4())
    issued = datetime.now(timezone.utc)
    command = {
        "schema_version": "1.0", "request_id": request_id, "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(minutes=5)).isoformat(),
        "action": "VERIFY", "project_id": "fixture", "workspace_id": "area",
        "expected_head": head, "params": {"profile_id": "fixture-run"},
        "intent": "Synthetic broker verification fixture",
    }
    assert ledger.admit(command, freeze(command).digest, "local-owner",
                        ("workspace:fixture/area",)).execute
    if effect:
        intent = {"snapshot_id": snapshot.snapshot_id, "project_id": "fixture",
                  "workspace_id": "area", "kind": snapshot.provenance,
                  "expected_head": head}
        effect_id = ledger.begin_effect(request_id, "source-snapshot", "git_ref", intent)
        ledger.confirm_effect(effect_id, snapshot.commit)
    profiles = _profile(tmp_path, required=required) if approved else ProfileRegistry(tmp_path)
    store = RunEvidenceStore(database, registry, tmp_path / "state" / "evidence")
    fake = broker or FakeBroker()
    service = RunService(ledger, registry, profiles, fake, store,
                         sources_root=tmp_path / "state" / "sealed-sources",
                         runs_root=tmp_path / "state" / "run-slots")
    volume = tmp_path / "state" / "quota-volumes" / str(uuid4())
    volume.mkdir(mode=0o700, parents=True)
    return Fixture(service, fake, ledger, registry, store, request_id,
                   snapshot.snapshot_id, snapshot.commit, snapshot.tree, volume)


def _expect_unbound(value: Fixture, run_id: str) -> None:
    with pytest.raises(SourceError) as error:
        value.registry.publication_target(run_id, project_id="fixture", workspace_id="area")
    assert error.value.code == "RUN_NOT_TESTED"


def test_failed_project_tests_finish_operation_and_bind_exact_source(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    run_id = str(uuid4())
    result = fixture.service.execute(fixture.request_id, fixture.snapshot_id,
                                     run_id=run_id, writable_volume=fixture.volume)
    assert result.operation_status == "COMPLETED"
    assert result.operation_code is None
    assert result.manifest["operation_status"] == "COMPLETED"
    assert result.manifest["execution"] == {
        "outcome": "FAILED", "exit_code": 1, "duration_seconds": 0.05,
    }
    assert result.manifest["delivery"] == {"status": "NOT_ATTEMPTED", "reason": None}
    assert result.parse_summaries["junit"]["tests"] == 2
    assert result.parse_summaries["junit"]["failed"] == 1
    assert result.manifest["snapshot"]["commit"] == fixture.snapshot_commit
    assert result.manifest["snapshot"]["tree"] == fixture.snapshot_tree
    assert fixture.broker.calls[0][2] == fixture.volume
    assert fixture.broker.calls[0][0].path == fixture.service.sources_root / run_id
    assert fixture.ledger.get_run(run_id) == result.manifest
    assert fixture.registry.publication_target(run_id, project_id="fixture",
                                               workspace_id="area")["commit"] == fixture.snapshot_commit
    assert {item["output_id"] for item in result.manifest["outputs"]} == {
        "stdout-raw", "stderr-raw", "junit", "runner-collection",
    }
    collection = json.loads((fixture.store.evidence_root / run_id / "collection.json").read_text())
    assert collection["collection_complete"] is True
    assert collection["parses"]["junit"]["status"] == "PARSED"
    assert (fixture.store.evidence_root / run_id / "capture" / "stdout.bin").read_bytes() == b"project failed\n"


def test_missing_required_output_is_fault_with_known_execution_and_no_binding(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, broker=FakeBroker(outcome="FAILED", junit=False))
    result = fixture.service.execute(fixture.request_id, fixture.snapshot_id)
    assert result.operation_status == "FAULT"
    assert result.operation_code == "OUTPUT_COLLECTION_INCOMPLETE"
    assert result.manifest["execution"]["outcome"] == "FAILED"
    assert "junit:REQUIRED_OUTPUT_MISSING" in result.findings
    assert result.parse_summaries == {}
    assert fixture.ledger.get_run(result.run_id) == result.manifest
    _expect_unbound(fixture, result.run_id)
    metadata = json.loads((fixture.store.evidence_root / result.run_id / "collection.json").read_text())
    assert metadata["collection_complete"] is False


def test_cancelled_execution_is_durable_without_publication_authority(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, broker=FakeBroker(outcome="CANCELLED"))
    cancel = Event()
    result = fixture.service.execute(fixture.request_id, fixture.snapshot_id, cancel=cancel)
    assert fixture.broker.calls[0][3] is cancel
    assert result.operation_status == "CANCELLED"
    assert result.manifest["execution"]["outcome"] == "CANCELLED"
    assert fixture.ledger.get_run(result.run_id) == result.manifest
    _expect_unbound(fixture, result.run_id)


def test_truncated_capture_retains_execution_but_prevents_binding(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, broker=FakeBroker(truncate=True))
    result = fixture.service.execute(fixture.request_id, fixture.snapshot_id)
    assert result.operation_status == "FAULT"
    assert result.operation_code == "CAPTURE_INCOMPLETE"
    assert result.manifest["execution"]["outcome"] == "FAILED"
    assert "stdout:CAPTURE_TRUNCATED" in result.findings
    stdout = next(item for item in result.manifest["outputs"] if item["output_id"] == "stdout-raw")
    assert stdout["complete"] is False
    assert stdout["truncated_reason"] == "stream_limit"
    metadata = json.loads((fixture.store.evidence_root / result.run_id / "collection.json").read_text())
    assert metadata["collection_complete"] is False
    assert metadata["capture"]["stdout"]["complete"] is False
    _expect_unbound(fixture, result.run_id)


def test_partial_storage_fault_preserves_raw_evidence_with_known_failed_exit(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, broker=FakeBroker(capture_fault="STORAGE_WRITE_FAILED:OSError"))
    result = fixture.service.execute(fixture.request_id, fixture.snapshot_id)
    assert result.operation_status == "FAULT" and result.operation_code == "CAPTURE_FAULT"
    assert result.manifest["execution"]["outcome"] == "FAILED"
    assert result.manifest["execution"]["exit_code"] == 1
    assert "CAPTURE_FAULT:STORAGE_WRITE_FAILED:OSError" in result.findings
    assert fixture.ledger.get_run(result.run_id) == result.manifest
    stdout = next(item for item in result.manifest["outputs"] if item["output_id"] == "stdout-raw")
    assert (stdout["size_bytes"], stdout["complete"], stdout["truncated_reason"]) == (
        5, False, "storage_error",
    )
    assert (fixture.store.evidence_root / result.run_id / "capture" / "stdout.bin").read_bytes() == b"proje"
    assert (fixture.store.evidence_root / result.run_id / "capture" / "capture.json").is_file()
    metadata = json.loads((fixture.store.evidence_root / result.run_id / "collection.json").read_text())
    assert metadata["capture_fault"] == "STORAGE_WRITE_FAILED:OSError"
    assert metadata["collection_complete"] is False
    _expect_unbound(fixture, result.run_id)


def test_unknown_resource_fault_retains_exit_code_without_test_authority(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, broker=FakeBroker(
        outcome="UNKNOWN", capture_fault="RESOURCE_LIMIT:RUN_VOLUME_FULL",
    ))
    result = fixture.service.execute(fixture.request_id, fixture.snapshot_id)
    assert result.operation_status == "FAULT" and result.operation_code == "CAPTURE_FAULT"
    assert result.manifest["execution"] == {
        "outcome": "UNKNOWN", "exit_code": 137, "duration_seconds": 0.05,
    }
    assert fixture.ledger.get_run(result.run_id) == result.manifest
    _expect_unbound(fixture, result.run_id)


def test_missing_capture_metadata_keeps_raw_streams_and_fault_code(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, broker=FakeBroker(capture_fault="METADATA_WRITE_FAILED:OSError"))
    result = fixture.service.execute(fixture.request_id, fixture.snapshot_id)
    assert result.operation_status == "FAULT"
    assert {item["output_id"] for item in result.manifest["outputs"]} >= {
        "stdout-raw", "stderr-raw", "runner-collection",
    }
    assert "capture-metadata" not in {item["output_id"] for item in result.manifest["outputs"]}
    metadata = json.loads((fixture.store.evidence_root / result.run_id / "collection.json").read_text())
    assert metadata["capture_fault"] == "METADATA_WRITE_FAILED:OSError"
    _expect_unbound(fixture, result.run_id)


def test_evidence_volume_failure_retains_private_raw_slot_but_cannot_register_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    run_id = str(uuid4())

    def fail_freeze(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "synthetic evidence volume full")

    monkeypatch.setattr("runner_app.executors.run_service._freeze_one", fail_freeze)
    with pytest.raises(OSError) as error:
        fixture.service.execute(fixture.request_id, fixture.snapshot_id, run_id=run_id)
    assert error.value.errno == errno.ENOSPC
    assert (fixture.service.runs_root / run_id / "capture" / "stdout.bin").read_bytes() == (
        b"project failed\n"
    )
    assert fixture.ledger.get_run(run_id) is None
    with pytest.raises(RunServiceError) as retry:
        fixture.service.execute(fixture.request_id, fixture.snapshot_id, run_id=str(uuid4()))
    assert retry.value.code == "RUN_ALREADY_ATTEMPTED"
    _expect_unbound(fixture, run_id)


def test_event_limit_faults_operation_without_discarding_streams(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, broker=FakeBroker(event_drop=True))
    result = fixture.service.execute(fixture.request_id, fixture.snapshot_id)
    assert result.operation_status == "FAULT" and result.operation_code == "CAPTURE_INCOMPLETE"
    assert "capture-events:CAPTURE_TRUNCATED" in result.findings
    events = next(item for item in result.manifest["outputs"] if item["output_id"] == "capture-events")
    assert events["complete"] is False and events["truncated_reason"] == "event_limit"
    _expect_unbound(fixture, result.run_id)


@pytest.mark.parametrize("mutation", ["tree", "commit", "source-path", "profile-revision",
                                      "argv", "capture-path"])
def test_mismatched_broker_evidence_cannot_be_persisted(tmp_path: Path, mutation: str) -> None:
    fixture = _fixture(tmp_path, broker=FakeBroker(mutation=mutation))
    run_id = str(uuid4())
    with pytest.raises(RunServiceError) as error:
        fixture.service.execute(fixture.request_id, fixture.snapshot_id, run_id=run_id)
    assert error.value.code == ("UNSAFE_CAPTURE" if mutation == "capture-path"
                                else "BROKER_IDENTITY_MISMATCH")
    assert fixture.ledger.get_run(run_id) is None
    _expect_unbound(fixture, run_id)


def test_missing_snapshot_effect_refuses_before_materialization_and_broker(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, effect=False)
    run_id = str(uuid4())
    with pytest.raises(RunServiceError) as error:
        fixture.service.execute(fixture.request_id, fixture.snapshot_id, run_id=run_id)
    assert error.value.code == "SNAPSHOT_NOT_ADMITTED"
    assert fixture.broker.calls == []
    assert not (fixture.service.sources_root / run_id).exists()
    assert fixture.ledger.get_run(run_id) is None


def test_missing_source_lock_refuses_before_materialization_and_broker(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    connection = fixture.registry.database.connect()
    try:
        connection.execute("DELETE FROM locks WHERE request_id=?", (fixture.request_id,))
    finally:
        connection.close()
    run_id = str(uuid4())
    with pytest.raises(RunServiceError) as error:
        fixture.service.execute(fixture.request_id, fixture.snapshot_id, run_id=run_id)
    assert error.value.code == "SOURCE_LOCK_MISSING"
    assert fixture.broker.calls == []
    assert not (fixture.service.sources_root / run_id).exists()


def test_profile_without_local_grant_refuses_before_materialization_and_broker(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, approved=False)
    run_id = str(uuid4())
    with pytest.raises(ProfileError) as error:
        fixture.service.execute(fixture.request_id, fixture.snapshot_id, run_id=run_id)
    assert error.value.code == "PROFILE_UNAVAILABLE"
    assert fixture.broker.calls == []
    assert not (fixture.service.sources_root / run_id).exists()


def test_runtime_gate_failure_does_not_create_execution_or_run_slot(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, broker=FakeBroker(fail_before_slot=True))
    run_id = str(uuid4())
    with pytest.raises(BrokerError) as error:
        fixture.service.execute(fixture.request_id, fixture.snapshot_id, run_id=run_id)
    assert error.value.code == "ROOTLESS_RUNTIME_UNAVAILABLE"
    assert not (fixture.service.runs_root / run_id).exists()
    assert fixture.ledger.get_run(run_id) is None
    _expect_unbound(fixture, run_id)


def test_failed_broker_attempt_cannot_start_second_run_for_same_request(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, broker=FakeBroker(fail_before_slot=True))
    first_id, second_id = str(uuid4()), str(uuid4())
    with pytest.raises(BrokerError) as first:
        fixture.service.execute(fixture.request_id, fixture.snapshot_id, run_id=first_id)
    assert first.value.code == "ROOTLESS_RUNTIME_UNAVAILABLE"
    connection = fixture.registry.database.connect()
    try:
        assert connection.execute(
            "SELECT run_id,snapshot_id FROM run_attempts WHERE request_id=?",
            (fixture.request_id,),
        ).fetchone() == (first_id, fixture.snapshot_id)
    finally:
        connection.close()

    with pytest.raises(RunServiceError) as second:
        fixture.service.execute(fixture.request_id, fixture.snapshot_id, run_id=second_id)
    assert second.value.code == "RUN_ALREADY_ATTEMPTED"
    assert len(fixture.broker.calls) == 1
    assert not (fixture.service.sources_root / second_id).exists()
    assert fixture.ledger.get_run(second_id) is None


@pytest.mark.parametrize("lost_authority", ["startup_release", "lock_lost", "disarmed"])
def test_precreate_intent_rechecks_admission_after_broker_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lost_authority: str,
) -> None:
    """An old worker cannot create after recovery releases its reservation."""
    fixture = _fixture(tmp_path)
    run_id = str(uuid4())
    create_reached = False

    def broker_after_slow_preparation(
        profile: Any, _snapshot: SnapshotInput, _run_root: Path, *,
        run_id: str, writable_volume: Path | None = None,
        cancel: Event | None = None,
        record_intent: Callable[[ContainerIntent], None],
    ) -> BrokerResult:
        nonlocal create_reached
        if lost_authority == "startup_release":
            fixture.ledger.startup()
            fixture.ledger.confirm_stopped(fixture.request_id)
        elif lost_authority == "lock_lost":
            connection = fixture.registry.database.connect()
            try:
                connection.execute("DELETE FROM locks WHERE request_id=?", (fixture.request_id,))
            finally:
                connection.close()
        else:
            fixture.ledger.disarm()
        record_intent(ContainerIntent(
            run_id, "fake-rootless-daemon", profile.profile_id, profile.revision_sha256,
            profile.image, "sha256:" + "c" * 64, _sha256_argv(profile.argv), "f" * 32,
        ))
        create_reached = True
        raise AssertionError("Docker create must not be reached after authority is lost")

    monkeypatch.setattr(fixture.broker, "run", broker_after_slow_preparation)
    with pytest.raises(RunServiceError) as error:
        fixture.service.execute(fixture.request_id, fixture.snapshot_id, run_id=run_id,
                                writable_volume=fixture.volume)
    assert error.value.code == "CONTAINER_INTENT_NOT_ADMITTED"
    assert create_reached is False
    assert fixture.ledger.get_run(run_id) is None
    assert all(event["type"] != "CONTAINER_INTENDED" for event in fixture.ledger.events())


def test_overlapping_private_roots_are_refused(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(RunServiceError) as error:
        RunService(fixture.ledger, fixture.registry, fixture.service.profiles,
                   fixture.broker, fixture.store,
                   sources_root=fixture.service.runs_root / "nested",
                   runs_root=fixture.service.runs_root)
    assert error.value.code == "UNSAFE_RUN_ROOT"
