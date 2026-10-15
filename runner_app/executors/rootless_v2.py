"""V2 admission/journal bridge to the qualified, fixed Python rootless route.

Only controller-supplied owner profiles select argv, image, mounts and budgets.
The journal callback must commit each event durably before returning. A local
one-shot marker also prevents another backend instance from replaying a request.
No v1 operation or approval record is synthesized. Existing broker, preparation,
quota verification and installed helper policy remain the execution boundaries.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID

from runner_app.git.source_v2 import SourceSnapshot
from runner_app.outputs.collection import collect_outputs
from runner_app.storage.paths import UnsafePathError, ensure_private_directory

from .broker import (
    ContainerIntent,
    ContainerReconciliation,
    RootlessDockerBroker,
    SnapshotInput,
    _run_volume_budget,
    _sha256_argv,
    _snapshot_tree,
)
from .preparation_coordinator import (
    PreparationNotReady,
    TrustedPreparationCoordinator,
    _capture_complete,
    preparation_run_id,
)
from .preparation_plan import PreparationGrant, plan_preparation
from .profiles import ApprovedProfile, ProfileRegistry
from .run_service import RunService, _freeze_capture, _specs
from .volume_provider import VOLUME_BASE, HardQuotaVolumeProvider, load_python_quick_grant

JournalCallback = Callable[[str, str, dict[str, object]], None]
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")


class RootlessError(RuntimeError):
    """Safe blocker; uncertain cleanup requires retaining the execution lane."""

    def __init__(self, code: str, *, cleanup_verified: bool = False) -> None:
        self.code = code
        self.cleanup_verified = cleanup_verified
        super().__init__(code)


@dataclass(frozen=True)
class RootlessTaskResult:
    outcome: str
    exit_code: int | None
    cleanup_verified: bool
    output_root: Path
    stdout_path: Path | None
    stderr_path: Path | None
    capture_complete: bool
    duration_seconds: float
    profile_id: str
    argv: tuple[str, ...]
    image: str
    execution_os: str
    execution_arch: str
    provenance: Mapping[str, object]
    error_code: str | None = None


class _PhaseJournal:
    """The coordinator's PhaseLedger, implemented with explicit v2 callbacks."""

    def __init__(self, request_id: str, snapshot: SnapshotInput, profiles: ProfileRegistry,
                 grant: PreparationGrant, require_running: Callable[[str], None],
                 record_event: JournalCallback) -> None:
        self.request_id, self.snapshot = request_id, snapshot
        self.profiles, self.grant = profiles, grant
        self.require_running, self.record_event = require_running, record_event
        self.attempts: dict[str, str] = {}
        self.intents: dict[str, dict[str, str]] = {}
        self.proofs: dict[str, ContainerReconciliation] = {}
        self.preparation_result: dict[str, object] | None = None
        self.handoff: dict[str, object] | None = None

    def event(self, event: str, detail: dict[str, object]) -> None:
        self.record_event(self.request_id, event, detail)

    def _scope(self, request_id: str, phase: str) -> str:
        if request_id != self.request_id or phase not in {"TEST", "PREPARATION"}:
            raise RootlessError("ROOTLESS_PHASE_SCOPE_INVALID")
        return request_id if phase == "TEST" else preparation_run_id(request_id)

    def reserve_phase_attempt(self, request_id: str, phase: str, run_id: str,
                              snapshot_id: str) -> None:
        expected = self._scope(request_id, phase)
        self.require_running(request_id)
        if phase in self.attempts or run_id != expected or snapshot_id != self.snapshot.snapshot_id:
            raise RootlessError("ROOTLESS_PHASE_REUSED")
        self.event("ROOTLESS_PHASE_RESERVED", {
            "phase": phase, "run_id": run_id, "snapshot_id": snapshot_id})
        self.attempts[phase] = run_id

    def record_container_intent(self, request_id: str, detail: dict[str, str], *,
                                phase: str = "TEST") -> None:
        run_id = self._scope(request_id, phase)
        self.require_running(request_id)
        profile_id = (self.grant.execution_profile_id if phase == "TEST"
                      else self.grant.preparation_profile_id)
        profile = self.profiles.require(profile_id)
        if (self.attempts.get(phase) != run_id or phase in self.intents
                or detail.get("run_id") != run_id
                or detail.get("profile_id") != profile.profile_id
                or detail.get("profile_revision_sha256") != profile.revision_sha256
                or detail.get("image") != profile.image
                or detail.get("argv_sha256") != _sha256_argv(profile.argv)
                or detail.get("source_path") != str(self.snapshot.path)
                or not detail.get("daemon_id") or len(detail["daemon_id"]) > 256
                or _HEX64.fullmatch(detail.get("image_id", "").removeprefix("sha256:")) is None
                or not detail.get("image_id", "").startswith("sha256:")
                or _HEX32.fullmatch(detail.get("nonce", "")) is None):
            raise RootlessError("CONTAINER_INTENT_INVALID")
        if phase == "TEST" and self.handoff is None:
            raise RootlessError("PREPARATION_HANDOFF_REQUIRED")
        self.event("ROOTLESS_CONTAINER_INTENDED", {"phase": phase, **detail})
        self.intents[phase] = dict(detail)

    def _cleanup(self, request_id: str, phase: str, proof: ContainerReconciliation) -> None:
        run_id = self._scope(request_id, phase)
        intended = self.intents.get(phase)
        removed = (proof.state == "REMOVED" and isinstance(proof.container_id, str)
                   and _HEX64.fullmatch(proof.container_id) is not None)
        absent = proof.state == "ABSENT" and proof.container_id is None
        if (intended is None or phase in self.proofs or proof.run_id != run_id
                or proof.daemon_id != intended["daemon_id"] or not (removed or absent)):
            raise RootlessError("CONTAINER_CLEANUP_PROOF_INVALID")
        self.event("ROOTLESS_CONTAINER_CLEANUP", {"phase": phase, **asdict(proof)})
        self.proofs[phase] = proof

    def record_container_cleaned(self, request_id: str, phase: str,
                                 proof: ContainerReconciliation) -> None:
        self._cleanup(request_id, phase, proof)

    def record_container_absent(self, request_id: str, phase: str,
                                proof: ContainerReconciliation) -> None:
        self._cleanup(request_id, phase, proof)

    def record_preparation_result(self, request_id: str, **detail: Any) -> None:
        self._scope(request_id, "PREPARATION")
        proof = self.proofs.get("PREPARATION")
        if (self.preparation_result is not None or proof is None or proof.state != "REMOVED"
                or detail.get("run_id") != preparation_run_id(request_id)
                or detail.get("snapshot_id") != self.snapshot.snapshot_id):
            raise RootlessError("PREPARATION_RESULT_UNTRUSTED")
        self.event("ROOTLESS_PREPARATION_RESULT", dict(detail))
        self.preparation_result = dict(detail)

    def record_preparation_handoff(self, request_id: str, **detail: Any) -> None:
        self._scope(request_id, "PREPARATION")
        result = self.preparation_result
        if (self.handoff is not None or result is None or result.get("outcome") != "PASSED"
                or result.get("exit_code") != 0 or result.get("capture_complete") is not True
                or result.get("cleanup_verified") is not True
                or any(detail.get(key) != result.get(key) for key in (
                    "run_id", "snapshot_id", "plan_identity_sha256",
                    "preparation_profile_revision_sha256", "image", "evidence_sha256"))
                or detail.get("test_run_id") != request_id
                or detail.get("test_volume_path") != str(VOLUME_BASE / request_id)):
            raise RootlessError("PREPARATION_HANDOFF_INVALID")
        self.event("ROOTLESS_PREPARATION_HANDOFF", dict(detail))
        self.handoff = dict(detail)

    @property
    def cleanup_verified(self) -> bool:
        return set(self.intents) == set(self.proofs)


class _V2Volumes(HardQuotaVolumeProvider):
    """Keep allocation/helper/quota policy; replace only the v1 admission lookup."""

    def __init__(self, request_id: str, profiles: ProfileRegistry,
                 preparation: TrustedPreparationCoordinator,
                 grant: PreparationGrant, preparation_runs_root: Path,
                 require_running: Callable[[str], None], record_event: JournalCallback) -> None:
        self.profiles, self.preparation, self.grant = profiles, preparation, grant
        self.request_id = request_id
        self.preparation_runs_root = preparation_runs_root
        self.require_running, self.record_event = require_running, record_event

    def _admitted(self, request_id: str, profile_id: str) -> None:
        self._uuid(request_id)
        if profile_id != self.grant.execution_profile_id:
            raise RootlessError("PROFILE_NOT_GRANTED", cleanup_verified=True)
        self.require_running(request_id)

    def _allocate(self, run_id: str, profile_id: str) -> Path:
        request_id = self.request_id
        self.require_running(request_id)
        self.record_event(request_id, "ROOTLESS_VOLUME_INTENDED", {
            "run_id": run_id, "profile_id": profile_id,
            "budget_bytes": _run_volume_budget(self.profiles.require(profile_id))})
        volume = super()._allocate(run_id, profile_id)
        self.record_event(request_id, "ROOTLESS_VOLUME_ALLOCATED", {
            "run_id": run_id, "path": str(volume)})
        return volume


def _write_json(path: Path, value: object) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("ascii") + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    return hashlib.sha256(payload).hexdigest()


class RootlessTaskBackend:
    """One contained Python preparation/test pair, using owner-approved fixed argv."""

    def __init__(self, profile_dir: Path, runs_root: Path, *,
                 require_running: Callable[[str], None], record_event: JournalCallback) -> None:
        self.profiles, self.grant = load_python_quick_grant(profile_dir)
        self.require_running, self.record_event = require_running, record_event
        self.root = runs_root / "rootless-v2"
        self.execution_root, self.evidence_root = self.root / "execution", self.root / "evidence"
        self.preparation_evidence = self.root / "preparation-evidence"
        self.requests_root = self.root / "requests"
        for path in (self.root, self.execution_root, self.evidence_root,
                     self.preparation_evidence, self.requests_root):
            try:
                ensure_private_directory(path)
            except (OSError, UnsafePathError):
                raise RootlessError("UNSAFE_ROOTLESS_ROOT", cleanup_verified=True) from None

    def profile(self, profile_id: str) -> ApprovedProfile:
        """Read exact approved argv/image before comparing a project task declaration."""
        if profile_id != self.grant.execution_profile_id or profile_id != "python-quick":
            raise RootlessError("PROFILE_NOT_GRANTED", cleanup_verified=True)
        return self.profiles.require(profile_id)

    def run(self, snapshot: SourceSnapshot, *, request_id: str, profile_id: str,
            cancel: Event | None = None) -> RootlessTaskResult:
        try:
            if str(UUID(request_id)) != request_id or snapshot.request_id != request_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise RootlessError("ROOTLESS_REQUEST_SCOPE_INVALID", cleanup_verified=True) from None
        self.require_running(request_id)
        profile = self.profile(profile_id)
        materialized = SnapshotInput(request_id, snapshot.source_commit,
                                     snapshot.workload_tree, snapshot.path)
        _snapshot_tree(materialized.path, materialized.tree)
        if cancel is not None and cancel.is_set():
            raise RootlessError("CANCELLED_BEFORE_START", cleanup_verified=True)
        # Source and execution/evidence roots are distinct; never write beneath the sealed input.
        if self.root.is_relative_to(snapshot.path) or snapshot.path.is_relative_to(self.root):
            raise RootlessError("ROOTLESS_SOURCE_ROOT_OVERLAP", cleanup_verified=True)
        marker = self.requests_root / request_id
        try:
            marker.mkdir(mode=0o700)
        except FileExistsError:
            raise RootlessError("ROOTLESS_REQUEST_REUSED", cleanup_verified=False) from None
        _write_json(marker / "request.json", {
            "request_id": request_id, "project_id": snapshot.project_id,
            "source_commit": snapshot.source_commit, "workload_tree": snapshot.workload_tree,
            "workload_sha256": snapshot.workload_snapshot_sha256,
            "profile_id": profile.profile_id, "profile_revision_sha256": profile.revision_sha256})
        journal = _PhaseJournal(request_id, materialized, self.profiles, self.grant,
                                self.require_running, self.record_event)
        broker = RootlessDockerBroker(self.profiles, runtime_dir=Path(f"/run/user/{os.getuid()}"))
        coordinator = TrustedPreparationCoordinator(
            journal, self.profiles, broker, evidence_root=self.preparation_evidence)
        volumes = _V2Volumes(request_id, self.profiles, coordinator, self.grant, self.execution_root,
                             self.require_running, self.record_event)
        readiness = volumes.readiness()
        if not readiness.available:
            journal.event("ROOTLESS_UNAVAILABLE", {"code": readiness.code})
            raise RootlessError(readiness.code, cleanup_verified=True)
        try:
            return self._execute(snapshot, materialized, request_id, profile, cancel,
                                 journal, broker, coordinator, volumes)
        except Exception as error:  # noqa: BLE001 -- callback diagnostics are never exposed.
            if isinstance(error, RootlessError):
                # Execution has entered allocation/preparation: retain its slots
                # until the failure and any prepared build are reconciled.
                raise RootlessError(error.code, cleanup_verified=False) from None
            code = str(getattr(error, "code", "ROOTLESS_EXECUTION_FAULT"))
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code) is None:
                code = "ROOTLESS_EXECUTION_FAULT"
            # A failed evidence/journal write must retain mounts and the lane.
            raise RootlessError(code, cleanup_verified=False) from None

    def _execute(self, source: SourceSnapshot, snapshot: SnapshotInput, request_id: str,
                 profile: ApprovedProfile, cancel: Event | None, journal: _PhaseJournal,
                 broker: RootlessDockerBroker, coordinator: TrustedPreparationCoordinator,
                 volumes: _V2Volumes) -> RootlessTaskResult:
        execution_os, _, execution_arch = profile.platform.partition("/")
        plan = plan_preparation(snapshot, template_id=self.grant.template_id,
                                execution_profile_id=profile.profile_id,
                                grant=self.grant, profiles=self.profiles)
        journal.reserve_phase_attempt(request_id, "TEST", request_id, snapshot.snapshot_id)
        test_volume = volumes.volume_for_run(request_id)
        preparation = volumes.preparation_for_run(request_id, profile.profile_id)
        output_dir = self.evidence_root / request_id
        ensure_private_directory(output_dir)
        try:
            handoff = coordinator.prepare_and_stage(
                request_id, snapshot, plan, self.grant,
                preparation_volume=preparation.preparation_volume, test_volume=test_volume,
                preparation_run_root=preparation.preparation_run_root, cancel=cancel)
        except PreparationNotReady as error:
            prep_evidence = self.preparation_evidence / preparation_run_id(request_id)
            output_root = output_dir / "outputs"
            output_root.mkdir(mode=0o700)
            prep_complete = (journal.preparation_result is not None
                             and journal.preparation_result.get("capture_complete") is True)
            provenance = self._provenance(source, profile, journal, {
                "preparation_outcome": error.outcome, "preparation_evidence_sha256": error.evidence_sha256,
                "test_started": False, "capture_complete": prep_complete})
            # Evidence has been frozen by the coordinator; test mount is still unstarted/empty.
            if prep_complete:
                self._seal_and_release(output_dir, provenance, journal, volumes, profile,
                                       preparation.preparation_volume, test_volume)
            else:
                _write_json(output_dir / "backend.json", provenance)
                journal.event("ROOTLESS_VOLUMES_RETAINED", {"reason": "EVIDENCE_INCOMPLETE"})
            return RootlessTaskResult(
                "NOT_RUN", None, prep_complete, output_root,
                prep_evidence / "capture/stdout.bin", prep_evidence / "capture/stderr.bin",
                prep_complete, 0.0, profile.profile_id, profile.argv, profile.image,
                execution_os, execution_arch, provenance, error.code)
        self.require_running(request_id)

        def intended(intent: ContainerIntent) -> None:
            journal.record_container_intent(request_id, asdict(intent), phase="TEST")

        def cleaned(proof: ContainerReconciliation) -> None:
            journal.record_container_cleaned(request_id, "TEST", proof)

        run_root = self.execution_root / request_id
        result = broker.run(
            profile, snapshot, run_root, run_id=request_id, writable_volume=test_volume,
            cancel=cancel, record_intent=intended, record_cleanup=cleaned,
            preparation_plan=handoff.plan, staged_build=handoff.staged_build)
        if not journal.cleanup_verified or set(journal.proofs) != {"TEST", "PREPARATION"}:
            raise RootlessError("CONTAINER_CLEANUP_UNVERIFIED")
        RunService._verify_result(result, profile, snapshot, run_root, test_volume, handoff)
        if result.containment.get("daemon_id") != journal.intents["TEST"]["daemon_id"]:
            raise RootlessError("BROKER_IDENTITY_MISMATCH")
        capture_descriptors, capture_files = _freeze_capture(result.capture, run_root, output_dir)
        collection = collect_outputs(
            result.output_root, _specs(profile), output_dir / "outputs",
            max_total_bytes=profile.output_budget_mb * 1024 * 1024)
        complete = _capture_complete(result) and collection.complete
        provenance = self._provenance(source, profile, journal, {
            "preparation_outcome": "PASSED", "preparation_evidence_sha256": handoff.evidence_sha256,
            "preparation_plan_sha256": plan.identity_sha256,
            "prepared_build_tree_sha256": handoff.staged_build.tree_sha256,
            "test_started": True, "capture_complete": _capture_complete(result),
            "collection_complete": collection.complete, "findings": list(collection.findings),
            "duration_seconds": result.duration_seconds, "outcome": result.outcome,
            "exit_code": result.exit_code, "containment": dict(result.containment),
            "outputs": [*capture_descriptors, *collection.outputs]})
        if complete:
            self._seal_and_release(output_dir, provenance, journal, volumes, profile,
                                   preparation.preparation_volume, test_volume)
        else:
            _write_json(output_dir / "backend.json", provenance)
            journal.event("ROOTLESS_VOLUMES_RETAINED", {"reason": "EVIDENCE_INCOMPLETE"})
        return RootlessTaskResult(
            result.outcome, result.exit_code, complete, output_dir / "outputs",
            capture_files["stdout-raw"], capture_files["stderr-raw"], _capture_complete(result),
            result.duration_seconds, profile.profile_id, profile.argv, profile.image,
            execution_os, execution_arch, provenance,
            None if complete else "ROOTLESS_EVIDENCE_INCOMPLETE")

    @staticmethod
    def _provenance(source: SourceSnapshot, profile: ApprovedProfile, journal: _PhaseJournal,
                    detail: dict[str, object]) -> dict[str, object]:
        return {"schema_version": "cdr-rootless-backend/2", "backend": "rootless_docker",
                "request_id": source.request_id, "project_id": source.project_id,
                "snapshot_id": journal.snapshot.snapshot_id, "profile_id": profile.profile_id,
                "profile_revision_sha256": profile.revision_sha256,
                "argv": list(profile.argv), "argv_sha256": _sha256_argv(profile.argv),
                "image": profile.image, "execution_platform": profile.platform,
                "source_commit": source.source_commit, "source_tree": source.source_tree,
                "workload_tree": source.workload_tree,
                "workload_sha256": source.workload_snapshot_sha256,
                "container_proofs": {phase: asdict(proof) for phase, proof in journal.proofs.items()},
                **detail}

    def _seal_and_release(self, output_dir: Path, provenance: dict[str, object],
                          journal: _PhaseJournal, volumes: _V2Volumes, profile: ApprovedProfile,
                          preparation_volume: Path, test_volume: Path) -> None:
        if (not journal.cleanup_verified or not journal.intents
                or any(proof.state != "REMOVED" for proof in journal.proofs.values())):
            raise RootlessError("CONTAINER_CLEANUP_UNVERIFIED")
        digest = _write_json(output_dir / "backend.json", provenance)
        journal.event("ROOTLESS_EVIDENCE_SEALED", {"sha256": digest})
        preparation_profile = self.profiles.require(self.grant.preparation_profile_id)
        for phase, path, selected_profile in (
            ("PREPARATION", preparation_volume, preparation_profile),
            ("TEST", test_volume, profile),
        ):
            run_id = journal._scope(journal.request_id, phase)
            if path != VOLUME_BASE / run_id:
                raise RootlessError("ROOTLESS_VOLUME_SCOPE_INVALID")
            journal.event("ROOTLESS_VOLUME_RETIRE_INTENDED", {"phase": phase, "run_id": run_id})
            budget = _run_volume_budget(selected_profile)
            if phase in journal.intents:
                volumes._clear_verified_mount(path, run_id, budget)
            else:
                volumes._clear_unstarted_scaffold(path, run_id, budget)
            response = volumes._helper("release", run_id)
            if response != {"status": "RELEASED", "run_id": run_id}:
                raise RootlessError("VOLUME_HELPER_RESPONSE_INVALID")
            journal.event("ROOTLESS_VOLUME_RELEASED", {"phase": phase, "run_id": run_id})
