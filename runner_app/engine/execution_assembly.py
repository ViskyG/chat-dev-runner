"""Owner-local, read-only readiness assembly for the bounded Python route.

Profile grant bytes are loaded only from an explicit owner path. No command or
project checkout can select a profile directory, helper, broker or volume.
Missing execution prerequisites leave project mailboxes inert.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from runner_app.executors.broker import RootlessDockerBroker
from runner_app.executors.preparation_coordinator import TrustedPreparationCoordinator
from runner_app.executors.run_service import RunService
from runner_app.executors.volume_provider import (
    HardQuotaVolumeProvider,
    VolumeProviderError,
    load_python_quick_grant,
)
from runner_app.git.registry import SourceRegistry
from runner_app.storage.ledger import Ledger
from runner_app.storage.paths import Paths
from runner_app.storage.run_evidence import RunEvidenceStore

from .full_route import ExecutionPorts


@dataclass(frozen=True)
class OwnerExecutionAssembly:
    ports: ExecutionPorts | None
    code: str


def build_owner_execution_ports(
    paths: Paths, ledger: Ledger, registry: SourceRegistry,
    evidence: RunEvidenceStore, profile_dir: Path,
) -> OwnerExecutionAssembly:
    """Prove installed local execution before exposing a workspace route.

    Readiness performs no mount allocation or cloud operation. The provider
    allocates a volume only inside a later admitted RUNNING operation.
    """
    try:
        profiles, grant = load_python_quick_grant(profile_dir)
    except VolumeProviderError as error:
        return OwnerExecutionAssembly(None, error.code)
    if not HardQuotaVolumeProvider._installed_helper():
        return OwnerExecutionAssembly(None, "VOLUME_HELPER_UNAVAILABLE")
    broker = RootlessDockerBroker(profiles, runtime_dir=Path(f"/run/user/{os.getuid()}"))
    preparation = TrustedPreparationCoordinator(
        ledger, profiles, broker,
        evidence_root=paths.state_dir / "preparation-evidence",
    )
    runs_root = paths.state_dir / "execution-runs"
    volumes = HardQuotaVolumeProvider(
        ledger, profiles, preparation, grant,
        preparation_runs_root=runs_root,
    )
    readiness = volumes.readiness()
    if not readiness.available:
        return OwnerExecutionAssembly(None, readiness.code)
    runs = RunService(
        ledger, registry, profiles, broker, evidence,
        sources_root=paths.state_dir / "execution-sources", runs_root=runs_root,
    )
    def volume_for_run(run_id: str) -> Path | None:
        return volumes.volume_for_run(run_id)

    return OwnerExecutionAssembly(
        ExecutionPorts(
            runs, volume_for_run, volumes.preparation_for_run,
            lambda profile_id: (
                profile_id == grant.execution_profile_id
                and volumes.readiness().available
            ),
        ),
        "READY",
    )
