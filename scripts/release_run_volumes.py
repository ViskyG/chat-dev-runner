#!/usr/bin/env python3
"""Owner-local, proof-driven retirement of one finite run's quota mounts.

The command never contacts Drive or GitHub and never trusts a remote cleanup
request. It preserves any mount whose container or frozen evidence is uncertain.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from runner_app.executors.broker import RootlessDockerBroker
from runner_app.executors.preparation_coordinator import TrustedPreparationCoordinator
from runner_app.executors.volume_provider import (
    HardQuotaVolumeProvider,
    VolumeProviderError,
    load_python_quick_grant,
)
from runner_app.git.registry import SourceRegistry
from runner_app.storage import Database, Paths
from runner_app.storage.ledger import Ledger
from runner_app.storage.run_evidence import RunEvidenceStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--profile-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        paths = Paths.discover()
        if not paths.database_path.is_file():
            raise VolumeProviderError("OWNER_STATE_UNAVAILABLE")
        profiles, grant = load_python_quick_grant(args.profile_dir)
        ledger = Ledger(Database(paths.database_path))
        registry = SourceRegistry(ledger.database, paths.state_dir / "managed")
        evidence = RunEvidenceStore(ledger.database, registry, paths.evidence_dir)
        broker = RootlessDockerBroker(profiles)
        preparation = TrustedPreparationCoordinator(
            ledger, profiles, broker,
            evidence_root=paths.state_dir / "preparation-evidence",
        )
        volumes = HardQuotaVolumeProvider(
            ledger, profiles, preparation, grant,
            preparation_runs_root=paths.state_dir / "execution-runs",
        )
        released = volumes.release_evidenced(args.request_id, evidence)
    except VolumeProviderError as error:
        print(json.dumps({"status": "REFUSED", "code": error.code}, sort_keys=True))
        return 2
    print(json.dumps({"status": "RETIRED", "request_id": args.request_id,
                      "released_volumes": list(released)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
