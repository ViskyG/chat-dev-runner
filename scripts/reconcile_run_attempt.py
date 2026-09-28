#!/usr/bin/env python3
"""Owner-local, proof-driven cleanup reconciliation for one terminal run.

`--allow-absent` explicitly authorizes an aged same-daemon absence proof after
an ambiguous create. It never reruns project code or deletes a Drive/Git file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from runner_app.executors.broker import BrokerError, RootlessDockerBroker
from runner_app.executors.reconciliation import ReconciliationError, reconcile_run_attempt
from runner_app.executors.volume_provider import VolumeProviderError, load_python_quick_grant
from runner_app.storage import Database, Paths
from runner_app.storage.ledger import Ledger, LedgerError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--profile-dir", required=True, type=Path)
    parser.add_argument("--allow-absent", action="store_true")
    args = parser.parse_args()
    try:
        paths = Paths.discover()
        if not paths.database_path.is_file():
            raise VolumeProviderError("OWNER_STATE_UNAVAILABLE")
        profiles, _ = load_python_quick_grant(args.profile_dir)
        ledger = Ledger(Database(paths.database_path))
        broker = RootlessDockerBroker(profiles, runtime_dir=Path(f"/run/user/{os.getuid()}"))
        proof = reconcile_run_attempt(
            ledger, broker, args.request_id, allow_absence=args.allow_absent,
        )
    except (BrokerError, ReconciliationError, VolumeProviderError, LedgerError) as error:
        print(json.dumps({"status": "REFUSED", "code": error.code}, sort_keys=True))
        return 2
    print(json.dumps({"status": "RECONCILED", "request_id": args.request_id,
                      "container_state": proof.state}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
