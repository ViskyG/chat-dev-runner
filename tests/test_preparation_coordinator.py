"""Fake-backed two-phase preparation; neither Docker nor project code is run."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import pytest
from test_preparation_plan import _profiles, _snapshot

import runner_app.executors.preparation_coordinator as coordinator_module
from runner_app.executors.broker import (
    BrokerResult,
    ContainerIntent,
    ContainerReconciliation,
    SnapshotInput,
    _sha256_argv,
)
from runner_app.executors.preparation_coordinator import (
    PreparationNotReady,
    TrustedPreparationCoordinator,
    preparation_run_id,
)
from runner_app.executors.preparation_plan import PreparationError, plan_preparation
from runner_app.outputs.capture import CaptureResult, StreamCapture


@dataclass
class FakePhaseLedger:
    calls: list[tuple[str, object]] = field(default_factory=list)

    def reserve_phase_attempt(self, request_id: str, phase: str, run_id: str,
                              snapshot_id: str) -> None:
        self.calls.append(("reserve", (request_id, phase, run_id, snapshot_id)))

    def record_container_intent(self, request_id: str, detail: dict[str, str], *,
                                phase: str = "TEST") -> None:
        self.calls.append(("intent", (request_id, phase, detail)))

    def record_container_cleaned(self, request_id: str, phase: str,
                                 proof: ContainerReconciliation) -> None:
        self.calls.append(("cleaned", (request_id, phase, proof)))

    def record_preparation_result(self, request_id: str, **detail: Any) -> None:
        self.calls.append(("result", (request_id, detail)))

    def record_preparation_handoff(self, request_id: str, **detail: Any) -> None:
        self.calls.append(("handoff", (request_id, detail)))


class FakePreparationBroker:
    def __init__(self, *, outcome: str = "PASSED", bad_capture: bool = False,
                 skip_cleanup: bool = False) -> None:
        self.outcome = outcome
        self.bad_capture = bad_capture
        self.skip_cleanup = skip_cleanup
        self.calls = 0

    def run(self, profile: Any, snapshot: SnapshotInput, run_root: Path, *,
            run_id: str, writable_volume: Path | None = None,
            cancel: Event | None = None, record_intent: Any,
            record_cleanup: Any) -> BrokerResult:
        self.calls += 1
        assert writable_volume is not None
        record_intent(ContainerIntent(
            run_id, "fake-rootless-daemon", profile.profile_id, profile.revision_sha256,
            profile.image, "sha256:" + "a" * 64, _sha256_argv(profile.argv), "b" * 32,
            str(snapshot.path),
        ))
        run_root.mkdir(mode=0o700)
        capture_root = run_root / "capture"
        capture_root.mkdir(mode=0o700)
        build = writable_volume / "build"
        build.mkdir(mode=0o700)
        (build / "module.py").write_bytes(b"VALUE = 7\n")
        (build / "module.py").chmod(0o600)
        output = writable_volume / "out"
        output.mkdir(mode=0o700)
        streams = []
        for name, data in (("stdout", b"dependency preparation\n"), ("stderr", b"")):
            path = capture_root / f"{name}.bin"
            path.write_bytes(data)
            path.chmod(0o600)
            digest = hashlib.sha256(data).hexdigest()
            if name == "stdout" and self.bad_capture:
                digest = "0" * 64
            streams.append(StreamCapture(name, path, None, len(data), len(data), digest,
                                         None, 0, True, None))
        events = capture_root / "events.jsonl"
        events.write_bytes(b"")
        events.chmod(0o600)
        metadata = capture_root / "capture.json"
        metadata.write_bytes(b"{}")
        metadata.chmod(0o600)
        exit_code = 0 if self.outcome == "PASSED" else 1 if self.outcome == "FAILED" else None
        termination = self.outcome if self.outcome in {"CANCELLED", "TIMED_OUT"} else None
        capture = CaptureResult(
            "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00", 1.0,
            exit_code, termination, None, (streams[0], streams[1]),
            events, hashlib.sha256(b"").hexdigest(), metadata,
            hashlib.sha256(b"{}").hexdigest(), 0, 0,
        )
        if not self.skip_cleanup:
            record_cleanup(ContainerReconciliation(run_id, "fake-rootless-daemon",
                                                    "REMOVED", "c" * 64))
        return BrokerResult(
            run_id, snapshot.snapshot_id, snapshot.commit, snapshot.tree,
            snapshot.path, profile.profile_id, profile.revision_sha256,
            profile.image, _sha256_argv(profile.argv),
            ("<approved-profile>", profile.profile_id),
            "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00", 1.0,
            self.outcome, exit_code, output, capture,
            {
                "verified": True, "backend": "rootless_docker",
                "image_repo_digest": profile.image,
                "run_volume_hard_limit_bytes": 64 * 1024 * 1024,
                "daemon_id": "fake-rootless-daemon", "daemon_rootless": True,
                "cleanup_verified": True, "network": "none",
                "read_only_source": True, "read_only_root": True,
                "no_new_privileges": True, "capabilities_dropped": "ALL",
                "mounts_verified_before_start": True,
            },
        )


@dataclass
class Case:
    request_id: str
    snapshot: SnapshotInput
    plan: Any
    grant: Any
    prep_volume: Path
    test_volume: Path
    run_root: Path
    coordinator: TrustedPreparationCoordinator
    ledger: FakePhaseLedger
    broker: FakePreparationBroker
    evidence_root: Path


def _case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
          outcome: str = "PASSED", bad_capture: bool = False,
          skip_cleanup: bool = False) -> Case:
    registry, grant, _ = _profiles(tmp_path)
    snapshot = _snapshot(tmp_path)
    plan = plan_preparation(
        snapshot, template_id=grant.template_id,
        execution_profile_id=grant.execution_profile_id,
        grant=grant, profiles=registry,
    )
    request_id = str(uuid4())
    prep_id = preparation_run_id(request_id)
    volumes = tmp_path / "volumes"
    volumes.mkdir(mode=0o700)
    prep_volume = volumes / prep_id
    test_volume = volumes / request_id
    prep_volume.mkdir(mode=0o700)
    test_volume.mkdir(mode=0o700)
    run_roots = tmp_path / "run-roots"
    run_roots.mkdir(mode=0o700)
    evidence_root = tmp_path / "preparation-evidence"
    monkeypatch.setattr(coordinator_module, "_verified_run_volume",
                        lambda volume, _run_id, _budget, **_kwargs: volume)
    monkeypatch.setattr(coordinator_module, "_separate_volume_devices",
                        lambda _preparation, _test: True)
    ledger = FakePhaseLedger()
    broker = FakePreparationBroker(outcome=outcome, bad_capture=bad_capture,
                                   skip_cleanup=skip_cleanup)
    coordinator = TrustedPreparationCoordinator(
        ledger, registry, broker, evidence_root=evidence_root,
    )
    return Case(request_id, snapshot, plan, grant, prep_volume, test_volume,
                run_roots / prep_id, coordinator, ledger, broker, evidence_root)


def test_success_requires_durable_cleanup_result_before_staged_handoff(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case = _case(tmp_path, monkeypatch)
    handoff = case.coordinator.prepare_and_stage(
        case.request_id, case.snapshot, case.plan, case.grant,
        preparation_volume=case.prep_volume, test_volume=case.test_volume,
        preparation_run_root=case.run_root,
    )
    assert handoff.preparation_run_id != case.request_id
    assert handoff.test_run_id == case.request_id
    assert handoff.test_volume == case.test_volume
    assert handoff.staged_build.source_build == case.test_volume / "build"
    assert (case.test_volume / "build" / "module.py").read_bytes() == b"VALUE = 7\n"
    assert case.broker.calls == 1
    assert [name for name, _ in case.ledger.calls] == [
        "reserve", "intent", "cleaned", "result", "handoff",
    ]
    receipt_path = case.evidence_root / handoff.preparation_run_id / "preparation.json"
    raw = receipt_path.read_bytes()
    receipt = json.loads(raw)
    assert hashlib.sha256(raw).hexdigest() == handoff.evidence_sha256
    assert receipt["outcome"] == "PASSED" and receipt["capture_complete"] is True
    assert receipt["outputs"][0]["publication"] == "local_only"
    assert (receipt_path.stat().st_mode & 0o777) == 0o600
    assert (case.evidence_root / handoff.preparation_run_id / "capture" / "stdout.bin").read_bytes() == b"dependency preparation\n"


def test_failed_preparation_keeps_logs_and_never_stages_test(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case = _case(tmp_path, monkeypatch, outcome="FAILED")
    with pytest.raises(PreparationNotReady) as raised:
        case.coordinator.prepare_and_stage(
            case.request_id, case.snapshot, case.plan, case.grant,
            preparation_volume=case.prep_volume, test_volume=case.test_volume,
            preparation_run_root=case.run_root,
        )
    assert raised.value.code == "PREPARATION_FAILED"
    assert raised.value.outcome == "FAILED"
    assert [name for name, _ in case.ledger.calls] == [
        "reserve", "intent", "cleaned", "result",
    ]
    assert list(case.test_volume.iterdir()) == []
    receipt = case.evidence_root / raised.value.run_id / "preparation.json"
    assert hashlib.sha256(receipt.read_bytes()).hexdigest() == raised.value.evidence_sha256


def test_cancel_before_attempt_never_invokes_broker(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case = _case(tmp_path, monkeypatch)
    cancel = Event()
    cancel.set()
    with pytest.raises(PreparationError) as raised:
        case.coordinator.prepare_and_stage(
            case.request_id, case.snapshot, case.plan, case.grant,
            preparation_volume=case.prep_volume, test_volume=case.test_volume,
            preparation_run_root=case.run_root, cancel=cancel,
        )
    assert raised.value.code == "CANCELLED_BEFORE_START"
    assert case.ledger.calls == [] and case.broker.calls == 0


def test_untrusted_plan_and_shared_volume_refuse_before_attempt(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case = _case(tmp_path, monkeypatch)
    with pytest.raises(PreparationError) as raised:
        case.coordinator.prepare_and_stage(
            case.request_id, case.snapshot,
            replace(case.plan, identity_sha256="0" * 64), case.grant,
            preparation_volume=case.prep_volume, test_volume=case.test_volume,
            preparation_run_root=case.run_root,
        )
    assert raised.value.code == "PREPARATION_GRANT_CHANGED"
    monkeypatch.setattr(coordinator_module, "_separate_volume_devices",
                        lambda _preparation, _test: False)
    with pytest.raises(PreparationError) as raised:
        case.coordinator.prepare_and_stage(
            case.request_id, case.snapshot, case.plan, case.grant,
            preparation_volume=case.prep_volume, test_volume=case.test_volume,
            preparation_run_root=case.run_root,
        )
    assert raised.value.code == "PREPARATION_VOLUME_INVALID"
    assert case.ledger.calls == [] and case.broker.calls == 0


def test_changed_capture_cannot_produce_durable_result_or_handoff(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case = _case(tmp_path, monkeypatch, bad_capture=True)
    with pytest.raises(PreparationError) as raised:
        case.coordinator.prepare_and_stage(
            case.request_id, case.snapshot, case.plan, case.grant,
            preparation_volume=case.prep_volume, test_volume=case.test_volume,
            preparation_run_root=case.run_root,
        )
    assert raised.value.code == "PREPARATION_EVIDENCE_FAILED"
    assert [name for name, _ in case.ledger.calls] == ["reserve", "intent", "cleaned"]
    assert list(case.test_volume.iterdir()) == []


def test_missing_cleanup_proof_cannot_attest_or_stage(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case = _case(tmp_path, monkeypatch, skip_cleanup=True)
    with pytest.raises(PreparationError) as raised:
        case.coordinator.prepare_and_stage(
            case.request_id, case.snapshot, case.plan, case.grant,
            preparation_volume=case.prep_volume, test_volume=case.test_volume,
            preparation_run_root=case.run_root,
        )
    assert raised.value.code == "PREPARATION_RESULT_UNTRUSTED"
    assert [name for name, _ in case.ledger.calls] == ["reserve", "intent"]
    assert list(case.test_volume.iterdir()) == []
