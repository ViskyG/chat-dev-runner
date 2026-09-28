"""One admitted VERIFY command -> one clean source snapshot and contained run.

This is an offline-composable Engine handler, not a registered live capability.
The caller must inject an already provisioned, private run volume. The broker
remains responsible for proving that it is a dedicated hard-quota mount before
any project code can execute; this handler never falls back to host execution.
"""
from __future__ import annotations

import errno
import os
import re
import stat
from pathlib import Path
from typing import Protocol

from runner_app.domain.commands import FrozenCommand
from runner_app.engine.service import Context, OperationResult
from runner_app.git.argv import GitError
from runner_app.git.changes import ChangeError
from runner_app.git.registry import SourceError
from runner_app.git.service import SourceService

from .broker import BrokerError, _trusted_volume_parent
from .preparation_coordinator import PreparationNotReady
from .preparation_plan import PreparationError
from .profiles import ProfileError
from .run_service import RunCompletion, RunService, RunServiceError, TrustedPreparation

_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_SOURCE_REFUSALS = frozenset({
    "DIRTY_WORKSPACE", "PROJECT_NOT_REGISTERED", "SOURCE_CHANGED",
    "STALE_BASE", "WORKSPACE_ARCHIVED", "WORKSPACE_CHANGED",
    "WORKSPACE_NOT_REGISTERED",
})
_BROKER_PRESTART = frozenset({
    "CGROUP_CONTROLLER_MISSING", "CGROUP_UNAVAILABLE", "DISK_QUOTA_UNAVAILABLE",
    "DISK_RESERVE_LOW", "IMAGE_NOT_APPROVED", "PROFILE_CHANGED",
    "PROFILE_INVALID", "ROOTFUL_DAEMON_REFUSED", "ROOTLESS_RUNTIME_UNAVAILABLE",
    "ROOTLESS_SOCKET_MISSING", "RUNTIME_UNAVAILABLE", "UNSAFE_ROOTLESS_SOCKET",
})
_PREPARATION_BLOCKED = frozenset({
    "LOCKFILE_CHANGED", "LOCKFILE_REQUIRED", "LOCKFILE_UNSAFE",
    "PREPARATION_ARTIFACT_CHANGED", "PREPARATION_ARTIFACT_UNSAFE",
    "PREPARATION_GRANT_CHANGED", "PREPARATION_GRANT_INVALID",
    "PREPARATION_NOT_GRANTED", "PREPARATION_PROFILE_UNAVAILABLE",
})


class PreprovisionedVolumeProvider(Protocol):
    """Trusted local capability: return the existing slot named by run UUID."""

    def __call__(self, run_id: str) -> Path | None: ...


class TrustedPreparationProvider(Protocol):
    """Owner-local mapping from an approved test profile to a preparation grant."""

    def __call__(self, request_id: str, profile_id: str) -> TrustedPreparation | None: ...


def _safe_code(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and _CODE.fullmatch(value) else fallback


def _local_volume(value: object, run_id: str) -> Path | None:
    """Refuse absent/unsafe slots early; the broker still proves the hard quota."""
    if (not isinstance(value, Path) or not value.is_absolute()
            or ".." in value.parts or value.name != run_id):
        return None
    try:
        current = Path(value.anchor)
        for part in value.parts[1:]:
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                return None
        volume = value.stat()
        if (not _trusted_volume_parent(value.parent)
                or volume.st_uid != os.getuid() or volume.st_mode & 0o077
                or any(value.iterdir())):
            return None
    except OSError:
        return None
    return value


def _axes(outcome: str, *, snapshot_id: str | None = None,
          run_id: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "execution": {"outcome": outcome, "exit_code": None},
        "publication": {"status": "NOT_REQUESTED"},
        "delivery": {"status": "NOT_ATTEMPTED"},
    }
    if snapshot_id is not None:
        result["snapshot_id"] = snapshot_id
    if run_id is not None:
        result["run_id"] = run_id
    return result


def _completed(completion: RunCompletion, snapshot_id: str) -> OperationResult:
    manifest = completion.manifest
    result = _axes(
        manifest["execution"]["outcome"],
        snapshot_id=snapshot_id, run_id=completion.run_id,
    )
    result["execution"] = manifest["execution"]
    result["delivery"] = manifest["delivery"]
    if completion.preparation is not None:
        result["preparation"] = completion.preparation
    return OperationResult(completion.operation_status, completion.operation_code, result)


class VerifyHandler:
    """Finite clean-HEAD VERIFY handler for explicit Engine registration.

    The request UUID doubles as the sole run UUID, making a pre-provisioned
    volume's name deterministic. A failed Engine admission never reaches here,
    and this handler never retries a failed or uncertain execution attempt.
    """

    def __init__(self, sources: SourceService, runs: RunService, *,
                 volume_for_run: PreprovisionedVolumeProvider,
                 preparation_for_run: TrustedPreparationProvider | None = None) -> None:
        if sources.ledger.database.path != runs.ledger.database.path:
            raise ValueError("VERIFY services must share one ledger database")
        self.sources = sources
        self.runs = runs
        self.volume_for_run = volume_for_run
        self.preparation_for_run = preparation_for_run

    def __call__(self, frozen: FrozenCommand, context: Context) -> OperationResult:
        if context.ledger.database.path != self.runs.ledger.database.path:
            return OperationResult("FAULT", "VERIFY_LEDGER_MISMATCH", _axes("NOT_RUN"))
        command = frozen.command
        if command["action"] != "VERIFY":
            return OperationResult("BLOCKED", "WRONG_VERIFY_ACTION", _axes("NOT_RUN"))
        request_id = command["request_id"]
        if context.cancel.is_set():
            return OperationResult("CANCELLED", result=_axes("NOT_RUN"))
        try:
            offered = self.volume_for_run(request_id)
        except Exception:  # noqa: BLE001 -- provider text may contain private paths
            return OperationResult("FAULT", "VOLUME_PROVIDER_FAILED", _axes("NOT_RUN"))
        volume = _local_volume(offered, request_id)
        if volume is None:
            return OperationResult("BLOCKED", "DISK_QUOTA_UNAVAILABLE", _axes("NOT_RUN"))
        if context.cancel.is_set():
            return OperationResult("CANCELLED", result=_axes("NOT_RUN"))
        preparation: TrustedPreparation | None = None
        if self.preparation_for_run is not None:
            try:
                preparation = self.preparation_for_run(
                    request_id, command["params"]["profile_id"],
                )
            except Exception:  # noqa: BLE001 -- local provider text may contain private paths
                return OperationResult("FAULT", "PREPARATION_PROVIDER_FAILED", _axes("NOT_RUN"))
            if preparation is not None and not isinstance(preparation, TrustedPreparation):
                return OperationResult("FAULT", "PREPARATION_POLICY_INVALID", _axes("NOT_RUN"))

        def attach_preparation(result: dict[str, object]) -> dict[str, object]:
            # A prepared phase can finish durably before staging or TEST fails.
            # Its receipt remains reviewable even when no test-run manifest exists.
            try:
                durable = context.ledger.get_preparation_result(request_id)
            except Exception:  # noqa: BLE001 -- preserve the primary terminal fault
                return result
            if durable is not None:
                result["preparation"] = durable
            return result

        def failure_axes(outcome: str, *, snapshot_id: str | None = None
                         ) -> dict[str, object]:
            return attach_preparation(_axes(outcome, snapshot_id=snapshot_id))

        snapshot_id: str | None = None
        try:
            snapshot = self.sources.capture_clean_verify_source(request_id)
            snapshot_id = snapshot.snapshot_id
            if context.cancel.is_set():
                return OperationResult("CANCELLED", result=failure_axes(
                    "NOT_RUN", snapshot_id=snapshot_id,
                ))
            completion = self.runs.execute(
                request_id, snapshot_id, run_id=request_id,
                writable_volume=volume, cancel=context.cancel,
                preparation=preparation,
            )
            completed = _completed(completion, snapshot_id)
            if completed.status != "COMPLETED":
                return OperationResult(completed.status, completed.code,
                                       attach_preparation(completed.result))
            return completed
        except SourceError as error:
            code = _safe_code(error.code, "SOURCE_STEP_FAILED")
            status = "BLOCKED" if code in _SOURCE_REFUSALS else "FAULT"
            return OperationResult(status, code, failure_axes("NOT_RUN", snapshot_id=snapshot_id))
        except (ChangeError, GitError) as error:
            code = _safe_code(error.code, "GIT_STEP_FAILED")
            status = "BLOCKED" if code in _SOURCE_REFUSALS else "FAULT"
            return OperationResult(status, code, failure_axes("NOT_RUN", snapshot_id=snapshot_id))
        except ProfileError as error:
            code = _safe_code(error.code, "PROFILE_STEP_FAILED")
            return OperationResult("BLOCKED", code, failure_axes("NOT_RUN", snapshot_id=snapshot_id))
        except PreparationNotReady as error:
            code = _safe_code(error.code, "PREPARATION_NOT_READY")
            status = ("CANCELLED" if error.outcome == "CANCELLED" or code.startswith("CANCELLED_")
                      else "BLOCKED" if error.outcome in {"FAILED", "TIMED_OUT"}
                      else "FAULT")
            result = failure_axes("NOT_RUN", snapshot_id=snapshot_id)
            if "preparation" not in result:
                result["preparation"] = {
                    "run_id": error.run_id, "outcome": error.outcome,
                    "evidence_sha256": error.evidence_sha256,
                }
            return OperationResult(status, code, result)
        except PreparationError as error:
            code = _safe_code(error.code, "PREPARATION_FAILED")
            status = ("CANCELLED" if code == "CANCELLED_BEFORE_START"
                      else "BLOCKED" if code in _PREPARATION_BLOCKED else "FAULT")
            return OperationResult(status, code, failure_axes("NOT_RUN", snapshot_id=snapshot_id))
        except BrokerError as error:
            code = _safe_code(error.code, "BROKER_STEP_FAILED")
            if code == "CANCELLED_BEFORE_START":
                return OperationResult("CANCELLED", result=failure_axes(
                    "NOT_RUN", snapshot_id=snapshot_id,
                ))
            # A missing/broken runtime is an infrastructure fault, even if no
            # project process started. Keep that distinct from a project failure.
            outcome = "NOT_RUN" if code in _BROKER_PRESTART else "UNKNOWN"
            return OperationResult("FAULT", code, failure_axes(outcome, snapshot_id=snapshot_id))
        except RunServiceError as error:
            code = _safe_code(error.code, "RUN_STEP_FAILED")
            return OperationResult("FAULT", code, failure_axes("UNKNOWN", snapshot_id=snapshot_id))
        except OSError as error:
            # Evidence storage can fill after the container exits but before an
            # immutable run is registered. The attempt prevents silent replay.
            code = ("EVIDENCE_STORAGE_FULL" if error.errno in {errno.ENOSPC, errno.EDQUOT}
                    else "VERIFY_HANDLER_FAILED")
            outcome = "UNKNOWN" if snapshot_id is not None else "NOT_RUN"
            return OperationResult("FAULT", code, failure_axes(outcome, snapshot_id=snapshot_id))
        except Exception:  # noqa: BLE001 -- Engine receives only a stable, sanitized fault
            outcome = "UNKNOWN" if snapshot_id is not None else "NOT_RUN"
            return OperationResult("FAULT", "VERIFY_HANDLER_FAILED", failure_axes(
                outcome, snapshot_id=snapshot_id,
            ))
