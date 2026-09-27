"""Trusted, offline-composable preparation phase before a contained test run.

This component is deliberately not registered as an Engine handler. It accepts
only a locally granted plan and invokes the same rootless broker boundary as a
test run. A durable phase ledger and separate hard-quota volumes are required;
neither a remote command nor a repository can supply the preparation profile.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Protocol
from uuid import UUID, uuid5

from runner_app.storage.paths import UnsafePathError, _absolute, ensure_private_directory

from .broker import (
    BrokerResult,
    ContainerIntent,
    ContainerReconciliation,
    SnapshotInput,
    _run_volume_budget,
    _sha256_argv,
    _verified_run_volume,
)
from .preparation_handoff import BuildManifest, OfflinePreparationCoordinator
from .preparation_plan import (
    PreparationError,
    PreparationGrant,
    PreparationPlan,
    plan_preparation,
)
from .profiles import ApprovedProfile, ProfileRegistry

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
_MAX_RECEIPT_BYTES = 1_048_576


class PhaseLedger(Protocol):
    """Private transaction boundary implemented by the durable local ledger."""

    def reserve_phase_attempt(self, request_id: str, phase: str, run_id: str,
                              snapshot_id: str) -> None: ...

    def record_container_intent(self, request_id: str, detail: dict[str, str], *,
                                phase: str = "TEST") -> None: ...

    def record_container_cleaned(self, request_id: str, phase: str,
                                 proof: ContainerReconciliation) -> None: ...

    def record_preparation_result(self, request_id: str, *, run_id: str,
                                  snapshot_id: str, plan_identity_sha256: str,
                                  preparation_profile_revision_sha256: str, image: str,
                                  outcome: str, exit_code: int | None,
                                  evidence_sha256: str, capture_complete: bool,
                                  cleanup_verified: bool) -> None: ...

    def record_preparation_handoff(self, request_id: str, *, run_id: str,
                                   snapshot_id: str, test_run_id: str,
                                   plan_identity_sha256: str,
                                   preparation_profile_revision_sha256: str,
                                   image: str, build_tree_sha256: str,
                                   evidence_sha256: str, test_volume_path: str) -> None: ...


class PreparationBroker(Protocol):
    """Only the trusted rootless broker is allowed outside fake-backed tests."""

    def run(self, profile: ApprovedProfile, snapshot: SnapshotInput, run_root: Path,
            *, run_id: str, writable_volume: Path | None = None,
            cancel: Event | None = None,
            record_intent: Callable[[ContainerIntent], None],
            record_cleanup: Callable[[ContainerReconciliation], None]) -> BrokerResult: ...


class PreparationNotReady(PreparationError):
    """A recorded preparation outcome that cannot authorize a test phase."""

    def __init__(self, code: str, outcome: str, run_id: str,
                 evidence_sha256: str) -> None:
        self.outcome = outcome
        self.run_id = run_id
        self.evidence_sha256 = evidence_sha256
        super().__init__(code, "Contained preparation did not produce a usable build")


@dataclass(frozen=True)
class PreparedHandoff:
    plan: PreparationPlan
    preparation_run_id: str
    test_run_id: str
    test_volume: Path
    staged_build: BuildManifest
    evidence_sha256: str


def preparation_run_id(request_id: str) -> str:
    """Use a stable distinct UUID, so a restart cannot invent another attempt."""
    try:
        if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
            raise ValueError
        return str(uuid5(UUID(request_id), "preparation"))
    except (ValueError, TypeError, AttributeError):
        raise PreparationError("INVALID_RUN_ID", "A canonical request UUID is required") from None


def _write_receipt(evidence_dir: Path, value: dict[str, object]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > _MAX_RECEIPT_BYTES:
        raise PreparationError("PREPARATION_EVIDENCE_TOO_LARGE",
                               "Preparation receipt exceeds its local limit")
    path = evidence_dir / "preparation.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW |
                         os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("Preparation receipt write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(evidence_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                        os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return hashlib.sha256(raw).hexdigest()


def _separate_volume_devices(preparation_volume: Path, test_volume: Path) -> bool:
    return preparation_volume.stat().st_dev != test_volume.stat().st_dev


def _capture_complete(result: BrokerResult) -> bool:
    capture = result.capture
    return (
        capture.capture_fault is None
        and capture.events_path is not None and capture.events_sha256 is not None
        and capture.metadata_path is not None and capture.metadata_sha256 is not None
        and capture.observed_events == capture.retained_events
        and len(capture.streams) == 2
        and {stream.name for stream in capture.streams} == {"stdout", "stderr"}
        and all(stream.complete and stream.observed_bytes == stream.retained_bytes
                for stream in capture.streams)
    )


class TrustedPreparationCoordinator:
    """Run one contained preparation, retain evidence, then stage one build."""

    def __init__(self, ledger: PhaseLedger, profiles: ProfileRegistry,
                 broker: PreparationBroker, *, evidence_root: Path) -> None:
        try:
            self.evidence_root = _absolute(evidence_root)
            ensure_private_directory(self.evidence_root)
        except (OSError, UnsafePathError):
            raise PreparationError("PREPARATION_EVIDENCE_UNSAFE",
                                   "Preparation evidence root is not private") from None
        self.ledger = ledger
        self.profiles = profiles
        self.broker = broker
        self.handoff = OfflinePreparationCoordinator()

    def prepare_and_stage(self, request_id: str, snapshot: SnapshotInput,
                          plan: PreparationPlan, grant: PreparationGrant, *,
                          preparation_volume: Path, test_volume: Path,
                          preparation_run_root: Path,
                          cancel: Event | None = None) -> PreparedHandoff:
        """Return an admitted handoff only after successful, complete preparation.

        A failed project preparation retains bounded local logs and a durable
        phase result, then raises ``PreparationNotReady``. A broker fault or
        uncertain cleanup never authorizes the test container and is not retried.
        """
        test_run_id = request_id
        prep_run_id = preparation_run_id(request_id)
        if not isinstance(plan, PreparationPlan) or not isinstance(grant, PreparationGrant):
            raise PreparationError("PREPARATION_GRANT_INVALID",
                                   "Local preparation plan or grant is invalid")
        current = plan_preparation(
            snapshot, template_id=grant.template_id,
            execution_profile_id=grant.execution_profile_id,
            grant=grant, profiles=self.profiles,
        )
        if current != plan:
            raise PreparationError("PREPARATION_GRANT_CHANGED",
                                   "Preparation plan changed after local approval")
        preparation_profile = self.profiles.require(plan.preparation_profile_id)
        test_profile = self.profiles.require(plan.execution_profile_id)
        if (preparation_profile.revision_sha256 != plan.preparation_revision_sha256
                or test_profile.revision_sha256 != plan.execution_revision_sha256):
            raise PreparationError("PREPARATION_GRANT_CHANGED",
                                   "Approved profile revision changed")
        if (not isinstance(preparation_volume, Path) or not isinstance(test_volume, Path)
                or preparation_volume == test_volume
                or preparation_volume.name != prep_run_id
                or test_volume.name != test_run_id):
            raise PreparationError("PREPARATION_VOLUME_INVALID",
                                   "Preparation and test need separate run volumes")
        _verified_run_volume(preparation_volume, prep_run_id,
                             _run_volume_budget(preparation_profile))
        _verified_run_volume(test_volume, test_run_id, _run_volume_budget(test_profile))
        if not _separate_volume_devices(preparation_volume, test_volume):
            raise PreparationError("PREPARATION_VOLUME_INVALID",
                                   "Preparation and test volumes share one filesystem")
        if (not isinstance(preparation_run_root, Path)
                or preparation_run_root.name != prep_run_id
                or os.path.lexists(preparation_run_root)):
            raise PreparationError("PREPARATION_RUN_ROOT_INVALID",
                                   "Preparation run slot must be fresh")
        evidence_dir = self.evidence_root / prep_run_id
        if os.path.lexists(evidence_dir):
            raise PreparationError("PREPARATION_EVIDENCE_EXISTS",
                                   "Preparation evidence slot already exists")
        if cancel is not None and cancel.is_set():
            raise PreparationError("CANCELLED_BEFORE_START",
                                   "Preparation was cancelled before its attempt")

        self.ledger.reserve_phase_attempt(request_id, "PREPARATION", prep_run_id,
                                          snapshot.snapshot_id)
        recorded_intent: ContainerIntent | None = None
        cleaned = False

        def record_intent(intent: ContainerIntent) -> None:
            nonlocal recorded_intent
            if (recorded_intent is not None or intent.run_id != prep_run_id
                    or intent.profile_id != preparation_profile.profile_id
                    or intent.profile_revision_sha256 != preparation_profile.revision_sha256
                    or intent.image != preparation_profile.image
                    or intent.argv_sha256 != _sha256_argv(preparation_profile.argv)
                    or intent.source_path != str(snapshot.path)
                    or not intent.daemon_id or len(intent.daemon_id) > 256
                    or not intent.image_id.startswith("sha256:")
                    or not _HEX64.fullmatch(intent.image_id.removeprefix("sha256:"))
                    or not _HEX32.fullmatch(intent.nonce)):
                raise PreparationError("CONTAINER_INTENT_INVALID",
                                       "Preparation container intent does not match the grant")
            self.ledger.record_container_intent(request_id, {
                "run_id": intent.run_id, "daemon_id": intent.daemon_id,
                "profile_id": intent.profile_id,
                "profile_revision_sha256": intent.profile_revision_sha256,
                "image": intent.image, "image_id": intent.image_id,
                "argv_sha256": intent.argv_sha256, "nonce": intent.nonce,
                "source_path": str(snapshot.path),
            }, phase="PREPARATION")
            recorded_intent = intent

        def record_cleanup(proof: ContainerReconciliation) -> None:
            nonlocal cleaned
            if (recorded_intent is None or cleaned or proof.run_id != prep_run_id
                    or proof.daemon_id != recorded_intent.daemon_id
                    or proof.state != "REMOVED"
                    or not isinstance(proof.container_id, str)
                    or not _CONTAINER_ID.fullmatch(proof.container_id)):
                raise PreparationError("CONTAINER_CLEANUP_UNVERIFIED",
                                       "Preparation container cleanup was not verified")
            self.ledger.record_container_cleaned(request_id, "PREPARATION", proof)
            cleaned = True

        result = self.broker.run(
            preparation_profile, snapshot, preparation_run_root,
            run_id=prep_run_id, writable_volume=preparation_volume, cancel=cancel,
            record_intent=record_intent, record_cleanup=record_cleanup,
        )
        if (not cleaned or recorded_intent is None
                or result.run_id != prep_run_id or result.snapshot_id != snapshot.snapshot_id
                or result.snapshot_commit != snapshot.commit
                or result.snapshot_tree != snapshot.tree or result.snapshot_path != snapshot.path
                or result.profile_id != preparation_profile.profile_id
                or result.profile_revision_sha256 != preparation_profile.revision_sha256
                or result.image != preparation_profile.image
                or result.argv_sha256 != _sha256_argv(preparation_profile.argv)
                or result.output_root != preparation_volume / "out"
                or result.outcome not in {"PASSED", "FAILED", "TIMED_OUT",
                                          "CANCELLED", "UNKNOWN"}
                or (result.outcome == "UNKNOWN"
                    and result.capture.capture_fault is None)
                or (result.outcome != "UNKNOWN"
                    and result.capture.outcome_hint != result.outcome)
                or (result.outcome in {"PASSED", "FAILED"}
                    and result.capture.exit_code != result.exit_code)
                or isinstance(result.duration_seconds, bool)
                or not isinstance(result.duration_seconds, (int, float))
                or not math.isfinite(result.duration_seconds)
                or result.duration_seconds < 0
                or result.containment.get("verified") is not True
                or result.containment.get("backend") != "rootless_docker"
                or result.containment.get("daemon_rootless") is not True
                or result.containment.get("cleanup_verified") is not True
                or result.containment.get("daemon_id") != recorded_intent.daemon_id
                or result.containment.get("image_repo_digest") != preparation_profile.image
                or result.containment.get("network") != "none"
                or result.containment.get("read_only_source") is not True):
            raise PreparationError("PREPARATION_RESULT_UNTRUSTED",
                                   "Preparation broker returned an untrusted result")
        # Reuse the run service's bounded, nofollow byte freezer. It has no
        # database side effect and only accepts files in this exact run slot.
        from .run_service import RunServiceError, _freeze_capture

        ensure_private_directory(evidence_dir)
        try:
            outputs, _ = _freeze_capture(result.capture, preparation_run_root, evidence_dir)
        except RunServiceError:
            raise PreparationError("PREPARATION_EVIDENCE_FAILED",
                                   "Preparation capture could not be frozen") from None
        complete = _capture_complete(result)
        receipt: dict[str, object] = {
            "schema_version": "1.0", "phase": "PREPARATION",
            "request_id": request_id, "run_id": prep_run_id,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_commit": snapshot.commit, "snapshot_tree": snapshot.tree,
            "plan_identity_sha256": plan.identity_sha256,
            "profile_id": preparation_profile.profile_id,
            "profile_revision_sha256": preparation_profile.revision_sha256,
            "image": preparation_profile.image, "argv_sha256": result.argv_sha256,
            "outcome": result.outcome, "exit_code": result.exit_code,
            "capture_complete": complete, "cleanup_verified": True,
            "outputs": outputs,
        }
        evidence_sha256 = _write_receipt(evidence_dir, receipt)
        self.ledger.record_preparation_result(
            request_id, run_id=prep_run_id, snapshot_id=snapshot.snapshot_id,
            plan_identity_sha256=plan.identity_sha256,
            preparation_profile_revision_sha256=preparation_profile.revision_sha256,
            image=preparation_profile.image, outcome=result.outcome,
            exit_code=result.exit_code, evidence_sha256=evidence_sha256,
            capture_complete=complete, cleanup_verified=True,
        )
        if cancel is not None and cancel.is_set():
            raise PreparationNotReady("CANCELLED_AFTER_PREPARATION", result.outcome,
                                      prep_run_id, evidence_sha256)
        if result.outcome != "PASSED" or result.exit_code != 0 or not complete:
            code = ("PREPARATION_CAPTURE_INCOMPLETE" if not complete
                    else "PREPARATION_" + result.outcome)
            raise PreparationNotReady(code, result.outcome, prep_run_id, evidence_sha256)
        # Preparation may take minutes; the target mount must still be the
        # original empty hard-quota slot immediately before copying bytes.
        _verified_run_volume(test_volume, test_run_id, _run_volume_budget(test_profile))
        target_build = test_volume / "build"
        target_build.mkdir(mode=0o700)
        staged = self.handoff.attest_and_stage(
            plan, result, preparation_volume / "build", target_build,
        )
        _verified_run_volume(test_volume, test_run_id, _run_volume_budget(test_profile),
                             preloaded_build=True)
        self.ledger.record_preparation_handoff(
            request_id, run_id=prep_run_id, snapshot_id=snapshot.snapshot_id,
            test_run_id=test_run_id, plan_identity_sha256=plan.identity_sha256,
            preparation_profile_revision_sha256=preparation_profile.revision_sha256,
            image=preparation_profile.image, build_tree_sha256=staged.tree_sha256,
            evidence_sha256=evidence_sha256, test_volume_path=str(test_volume),
        )
        return PreparedHandoff(plan, prep_run_id, test_run_id, test_volume, staged,
                               evidence_sha256)
