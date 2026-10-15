"""V2 bridge integration with real source/freezing/preparation and fake OS boundary.

No test allocates a real mount, invokes sudo/Docker, or claims live qualification.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import pytest

import runner_app.executors.preparation_coordinator as coordinator_module
import runner_app.executors.rootless_v2 as bridge
import runner_app.executors.volume_provider as volume_module
from runner_app.executors.broker import (
    BrokerResult,
    ContainerIntent,
    ContainerReconciliation,
    SnapshotInput,
    _run_volume_budget,
    _sha256_argv,
)
from runner_app.executors.preparation_coordinator import preparation_run_id
from runner_app.executors.preparation_handoff import BuildManifest, _scan, _tree_digest
from runner_app.executors.preparation_plan import PreparationGrant, PreparationPlan
from runner_app.executors.profiles import ApprovedProfile
from runner_app.executors.rootless_v2 import RootlessError, RootlessTaskBackend
from runner_app.executors.volume_provider import HardQuotaVolumeProvider, VolumeReadiness
from runner_app.git.argv import run_git
from runner_app.git.source_v2 import LocalWorkspace, SourceProject, SourceResolver
from runner_app.git.templates import starter_files
from runner_app.outputs.capture import CaptureResult, StreamCapture

ROOT = Path(__file__).resolve().parents[1]


def private_file(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def owner_profiles(root: Path) -> Path:
    profiles = root / "owner-profiles"
    profiles.mkdir(mode=0o700)
    revisions: dict[str, str] = {}
    image = ""
    for name in ("python-quick", "python-prepare"):
        value = json.loads((ROOT / f"config/profile.{name}.json").read_text())
        value.update(installed=True, approved=True)
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        private_file(profiles / f"{name}.json", payload)
        revisions[name] = hashlib.sha256(payload).hexdigest()
        image = value["image"]
    grant = PreparationGrant("python-basic", "python-quick", revisions["python-quick"],
                             "python-prepare", revisions["python-prepare"], "requirements.lock")
    private_file(profiles / "python-quick-grant.json", json.dumps({
        "schema_version": "1.0", "installed": True, "profile_dir": str(profiles),
        "profile_revision_sha256": revisions, "preparation_grant": asdict(grant),
        "starter_lockfile_sha256": hashlib.sha256(
            starter_files("python-basic", "test-rootless-v2")["requirements.lock"]).hexdigest(),
        "image": image,
    }).encode())
    return profiles


class Fixture:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root, self.events = root, []
        self.request = str(uuid4())
        self.running = True
        self.profile_dir = owner_profiles(root)
        source = root / "original"
        source.mkdir(mode=0o700)
        run_git(source, ["init", "-b", "tested"])
        private_file(source / "requirements.lock", b"approved==1 --hash=sha256:abcd\n")
        private_file(source / "test_app.py", b"def test_app(): assert True\n")
        run_git(source, ["add", "--all"])
        run_git(source, ["-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
                         "commit", "-m", "Source"])
        repo = root / "controller.git"
        run_git(root, ["clone", "--bare", "--no-local", str(source), str(repo)])
        self.snapshot = SourceResolver({"demo": SourceProject(
            repo, root / "source-runs", {"editor": LocalWorkspace(source)})}).resolve_current(
                "demo", self.request, {"kind": "local_workspace", "branch": "tested",
                                       "workspace_id": "editor"})
        self.base = root / "fake-volumes"
        self.base.mkdir(mode=0o700)
        self.calls: list[tuple[str, ...]] = []
        self.started: list[str] = []
        self.mode = "pass"
        monkeypatch.setattr(bridge, "VOLUME_BASE", self.base)
        monkeypatch.setattr(volume_module, "VOLUME_BASE", self.base)
        monkeypatch.setattr(volume_module, "_verified_run_volume", lambda path, *_args, **_kw: path)
        monkeypatch.setattr(volume_module, "_verified_retired_volume", lambda path, *_args: path)
        monkeypatch.setattr(coordinator_module, "_verified_run_volume", lambda path, *_args, **_kw: path)
        monkeypatch.setattr(coordinator_module, "_separate_volume_devices", lambda *_args: True)
        monkeypatch.setattr(HardQuotaVolumeProvider, "readiness",
                            lambda _self: VolumeReadiness(True, "READY"))
        monkeypatch.setattr(HardQuotaVolumeProvider, "_helper", staticmethod(self.helper))
        monkeypatch.setattr(bridge, "RootlessDockerBroker", lambda *_args, **_kw: self)
        self.backend = RootlessTaskBackend(
            self.profile_dir, root / "backend-runs", require_running=self.require_running,
            record_event=self.record)

    def record(self, request_id: str, event: str, detail: dict[str, object]) -> None:
        assert request_id == self.request
        self.events.append((event, detail))
        if self.mode == "journal-failure" and event == "ROOTLESS_CONTAINER_INTENDED":
            raise RuntimeError("secret provider diagnostic must not escape")
        if self.mode == "admission-revoked" and event == "ROOTLESS_PREPARATION_HANDOFF":
            self.running = False

    def require_running(self, request_id: str) -> None:
        assert request_id == self.request
        if not self.running:
            raise RootlessError("OPERATION_NOT_RUNNING", cleanup_verified=True)

    def helper(self, action: str, *arguments: str) -> dict[str, Any]:
        self.calls.append((action, *arguments))
        run_id = arguments[0]
        if action == "allocate":
            assert self.running
            (self.base / run_id).mkdir(mode=0o700)
            return {"status": "ALLOCATED", "run_id": run_id, "size_mb": int(arguments[1]),
                    "path": str(self.base / run_id)}
        assert action == "release"
        assert any(event == "ROOTLESS_EVIDENCE_SEALED" for event, _ in self.events)
        assert (self.backend.evidence_root / self.request / "backend.json").exists()
        assert not any((self.base / run_id).iterdir())
        if self.mode == "release-failure":
            return {"status": "UNCERTAIN"}
        (self.base / run_id).rmdir()
        return {"status": "RELEASED", "run_id": run_id}

    def run(self, profile: ApprovedProfile, snapshot: SnapshotInput, run_root: Path, *,
            run_id: str, writable_volume: Path | None = None, cancel: Event | None = None,
            record_intent: Any, record_cleanup: Any,
            preparation_plan: PreparationPlan | None = None,
            staged_build: BuildManifest | None = None) -> BrokerResult:
        assert writable_volume is not None
        phase = "PREPARATION" if profile.profile_id == "python-prepare" else "TEST"
        actual = json.loads((self.profile_dir / f"{profile.profile_id}.json").read_bytes())
        assert profile.argv == tuple(actual["argv"])  # No v2 task substitution.
        record_intent(ContainerIntent(
            run_id, "fake-rootless-daemon", profile.profile_id, profile.revision_sha256,
            profile.image, "sha256:" + "a" * 64, _sha256_argv(profile.argv), "b" * 32,
            str(snapshot.path)))
        assert self.events[-1][0] == "ROOTLESS_CONTAINER_INTENDED"
        self.started.append(phase)
        run_root.mkdir(mode=0o700)
        capture_root = run_root / "capture"
        capture_root.mkdir(mode=0o700)
        for name in (("build", "out", "tmp") if phase == "PREPARATION" else ("out", "tmp")):
            (writable_volume / name).mkdir(mode=0o700)
        if phase == "PREPARATION":
            private_file(writable_volume / "build/module.py", b"VALUE = 7\n")
        else:
            assert preparation_plan is not None and staged_build is not None
            assert preparation_plan.snapshot_tree == self.snapshot.workload_tree
            assert staged_build.source_build == writable_volume / "build"
            entries, _ = _scan(staged_build.source_build)
            assert staged_build.tree_sha256 == _tree_digest(entries)
            if self.mode != "missing-output":
                private_file(writable_volume / "out/junit.xml", (
                    b'<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
                    b'<testcase classname="app" name="test_app"/>'
                    b'</testsuite></testsuites>'))
        outcome = "FAILED" if self.mode == "prep-failed" and phase == "PREPARATION" else "PASSED"
        if self.mode == "test-failed" and phase == "TEST":
            outcome = "FAILED"
        streams = []
        for name, payload in (("stdout", f"{phase} retained bytes\n".encode()), ("stderr", b"")):
            path = capture_root / f"{name}.bin"
            private_file(path, payload)
            streams.append(StreamCapture(
                name, path, None, len(payload), len(payload), hashlib.sha256(payload).hexdigest(),
                None, 0, not (self.mode == "prep-incomplete" and phase == "PREPARATION"), None))
        private_file(capture_root / "events.jsonl", b"")
        private_file(capture_root / "capture.json", b"{}")
        capture = CaptureResult(
            "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00", 1.0,
            0 if outcome == "PASSED" else 1, None, None, (streams[0], streams[1]),
            capture_root / "events.jsonl", hashlib.sha256(b"").hexdigest(),
            capture_root / "capture.json", hashlib.sha256(b"{}").hexdigest(), 0, 0)
        if not (self.mode == "missing-cleanup" and phase == "TEST"):
            record_cleanup(ContainerReconciliation(run_id, "fake-rootless-daemon",
                                                    "REMOVED", "c" * 64))
        containment = {
            "verified": True, "backend": "rootless_docker", "image_repo_digest": profile.image,
            "run_volume_hard_limit_bytes": _run_volume_budget(profile),
            "daemon_id": "fake-rootless-daemon", "daemon_rootless": True,
            "cleanup_verified": True, "network": "none", "read_only_source": True,
            "read_only_root": True, "no_new_privileges": True, "capabilities_dropped": "ALL",
            "mounts_verified_before_start": True,
        }
        if preparation_plan and staged_build:
            containment.update(preparation_plan_sha256=preparation_plan.identity_sha256,
                               preparation_run_id=staged_build.preparation_run_id,
                               prepared_build_tree_sha256=staged_build.tree_sha256)
        result = BrokerResult(
            run_id, snapshot.snapshot_id, snapshot.commit, snapshot.tree, snapshot.path,
            profile.profile_id, profile.revision_sha256, profile.image, _sha256_argv(profile.argv),
            ("<approved-profile>", profile.profile_id), capture.started_at, capture.ended_at, 1.0,
            outcome, capture.exit_code, writable_volume / "out", capture, containment)
        if self.mode == "bad-result" and phase == "TEST":
            result = replace(result, snapshot_tree="0" * 40)
        return result

    def execute(self) -> bridge.RootlessTaskResult:
        return self.backend.run(self.snapshot, request_id=self.request, profile_id="python-quick")


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    return Fixture(tmp_path, monkeypatch)


def test_two_phase_fixed_profile_freezes_evidence_before_exact_volume_release(fixture: Fixture) -> None:
    profile = fixture.backend.profile("python-quick")
    result = fixture.execute()
    assert result.outcome == "PASSED" and result.exit_code == 0 and result.cleanup_verified
    assert result.argv == profile.argv and result.image == profile.image
    assert (result.execution_os, result.execution_arch) == ("linux", "amd64")
    assert result.capture_complete and result.output_root.joinpath("junit.xml").exists()
    assert result.stdout_path and result.stdout_path.read_bytes() == b"TEST retained bytes\n"
    assert fixture.started == ["PREPARATION", "TEST"]
    prep_id = preparation_run_id(fixture.request)
    assert fixture.calls == [("allocate", fixture.request, "256"), ("allocate", prep_id, "64"),
                             ("release", prep_id), ("release", fixture.request)]
    assert not tuple(fixture.base.iterdir())
    assert fixture.snapshot.path.joinpath("requirements.lock").read_bytes().startswith(b"approved==")
    assert not fixture.snapshot.path.stat().st_mode & 0o222
    assert result.provenance["workload_tree"] == fixture.snapshot.workload_tree
    assert set(result.provenance["container_proofs"]) == {"TEST", "PREPARATION"}
    assert result.provenance["collection_complete"] is True
    with pytest.raises(RootlessError, match="ROOTLESS_REQUEST_REUSED"):
        fixture.execute()


def test_failed_test_is_execution_failure_with_retained_logs(fixture: Fixture) -> None:
    fixture.mode = "test-failed"
    result = fixture.execute()
    assert result.outcome == "FAILED" and result.exit_code == 1 and result.cleanup_verified
    assert result.stdout_path and result.stdout_path.exists()
    assert not tuple(fixture.base.iterdir())


def test_preparation_failure_preserves_test_not_run_and_frozen_preparation_logs(fixture: Fixture) -> None:
    fixture.mode = "prep-failed"
    result = fixture.execute()
    assert result.outcome == "NOT_RUN" and result.exit_code is None and result.cleanup_verified
    assert result.error_code == "PREPARATION_FAILED"
    assert result.provenance["preparation_outcome"] == "FAILED"
    assert result.stdout_path and result.stdout_path.read_bytes() == b"PREPARATION retained bytes\n"
    assert fixture.started == ["PREPARATION"]
    assert not tuple(fixture.base.iterdir())


@pytest.mark.parametrize("mode", ["missing-cleanup", "bad-result", "journal-failure", "release-failure"])
def test_uncertainty_preserves_mounts_and_refuses_replay(fixture: Fixture, mode: str) -> None:
    fixture.mode = mode
    with pytest.raises(RootlessError) as caught:
        fixture.execute()
    assert not caught.value.cleanup_verified
    assert "secret" not in str(caught.value)
    assert tuple(fixture.base.iterdir())
    with pytest.raises(RootlessError, match="ROOTLESS_REQUEST_REUSED"):
        fixture.execute()


@pytest.mark.parametrize("mode", ["missing-output", "prep-incomplete"])
def test_incomplete_evidence_keeps_lane_and_mounts(fixture: Fixture, mode: str) -> None:
    fixture.mode = mode
    result = fixture.execute()
    assert not result.cleanup_verified
    assert tuple(fixture.base.iterdir())
    assert not any(call[0] == "release" for call in fixture.calls)
    assert any(event == "ROOTLESS_VOLUMES_RETAINED" for event, _ in fixture.events)


def test_exact_running_admission_checked_again_before_test(fixture: Fixture) -> None:
    fixture.mode = "admission-revoked"
    with pytest.raises(RootlessError, match="OPERATION_NOT_RUNNING") as caught:
        fixture.execute()
    assert not caught.value.cleanup_verified
    assert fixture.started == ["PREPARATION"]
    assert not any(call[0] == "release" for call in fixture.calls)


def test_not_running_scope_profile_and_cancel_refused_before_allocation(fixture: Fixture) -> None:
    fixture.running = False
    with pytest.raises(RootlessError, match="OPERATION_NOT_RUNNING"):
        fixture.execute()
    fixture.running = True
    with pytest.raises(RootlessError, match="PROFILE_NOT_GRANTED"):
        fixture.backend.run(fixture.snapshot, request_id=fixture.request, profile_id="other")
    with pytest.raises(RootlessError, match="ROOTLESS_REQUEST_SCOPE_INVALID"):
        fixture.backend.run(fixture.snapshot, request_id=str(uuid4()), profile_id="python-quick")
    cancelled = Event()
    cancelled.set()
    with pytest.raises(RootlessError, match="CANCELLED_BEFORE_START"):
        fixture.backend.run(fixture.snapshot, request_id=fixture.request, profile_id="python-quick",
                            cancel=cancelled)
    assert not fixture.calls and not fixture.started


def test_missing_local_prerequisite_does_not_allocate(fixture: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(HardQuotaVolumeProvider, "readiness",
                        lambda _self: VolumeReadiness(False, "DISK_QUOTA_UNAVAILABLE"))
    with pytest.raises(RootlessError, match="DISK_QUOTA_UNAVAILABLE") as caught:
        fixture.execute()
    assert caught.value.cleanup_verified
    assert not fixture.calls and not fixture.started
