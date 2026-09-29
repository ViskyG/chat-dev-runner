#!/usr/bin/env python3
"""Finish one retained EP-07 artifact probe after its stale binary-refusal assertion.

This rechecks the original bounded files and exact receipts without a second
container execution, then releases only the matching retired quota mount.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner_app.executors.broker import _run_volume_budget
from runner_app.executors.profiles import _validate
from runner_app.executors.volume_provider import (
    VOLUME_BASE,
    HardQuotaVolumeProvider,
    _verified_retired_volume,
)
from runner_app.outputs.collection import OutputSpec, collect_outputs
from runner_app.storage.paths import ensure_private_directory
from scripts.ep03_live_toy import _write_json
from scripts.ep07_artifact_boundary import (
    EVIDENCE_ROOT,
    PROFILE_ID,
    SOURCE,
    _assertions,
    _profile_bytes,
)


def run(run_id: str) -> int:
    if str(UUID(run_id)) != run_id:
        raise ValueError("noncanonical UUID")
    evidence = EVIDENCE_ROOT / run_id
    if evidence.is_symlink() or not evidence.is_dir() or (evidence / "retirement.json").exists():
        raise ValueError("exact retained evidence is absent or already retired")
    setup = json.loads((evidence / "setup.json").read_text(encoding="utf-8"))
    prior = json.loads((evidence / "result.json").read_text(encoding="utf-8"))
    intent = json.loads((evidence / "intent.json").read_text(encoding="utf-8"))
    cleanup = json.loads((evidence / "cleanup.json").read_text(encoding="utf-8"))
    if (not all(item.get("run_id") == run_id for item in (setup, prior, intent, cleanup))
            or setup.get("volume") != str(VOLUME_BASE / run_id)
            or setup.get("toy_source_sha256") != hashlib.sha256(SOURCE).hexdigest()
            or setup.get("profile_revision_sha256") != hashlib.sha256(_profile_bytes()).hexdigest()
            or prior.get("outcome") != "PASSED" or prior.get("exit_code") != 0
            or prior.get("capture_fault") is not None
            or prior.get("intent_recorded") is not True
            or prior.get("cleanup_recorded") is not True
            or prior.get("containment", {}).get("verified") is not True
            or prior.get("containment", {}).get("cleanup_verified") is not True
            or cleanup.get("state") != "REMOVED"
            or cleanup.get("daemon_id") != setup.get("daemon_id")):
        raise ValueError("retained run identity is not exact")
    old = prior["observed"]["checks"]
    if (old.get("large_export_refused") is not False
            or any(value is not True for key, value in old.items()
                   if key != "large_export_refused")
            or prior["observed"]["export_refusals"].get("large")
            != "EXPORT_FORMAT_UNSUPPORTED"):
        raise ValueError("original failure is not solely the stale assertion")
    profile = _validate(_profile_bytes(), requested_id=PROFILE_ID,
                        revision_sha256=hashlib.sha256(_profile_bytes()).hexdigest())
    budget = _run_volume_budget(profile)
    if setup.get("quota_bytes") != budget:
        raise ValueError("retained volume quota differs")
    volume = VOLUME_BASE / run_id
    _verified_retired_volume(volume, run_id, budget)
    recheck = evidence / "recheck"
    ensure_private_directory(recheck)
    collection = collect_outputs(
        volume / "out",
        tuple(OutputSpec(item.id, item.kind, item.path, item.format, item.required)
              for item in profile.collect),
        recheck / "collected", max_file_bytes=128 * 1024,
        max_total_bytes=512 * 1024,
    )
    observed = _assertions(collection, volume / "out", recheck)
    _write_json(recheck / "result.json", {
        "schema_version": "1.0", "run_id": run_id, "passed": observed["all_passed"],
        "no_second_container": True,
        "corrected_expectation": "binary format refusal precedes incomplete-file refusal",
        "observed": observed, "checked_at": datetime.now(timezone.utc).isoformat(),
    })
    if not observed["all_passed"]:
        return 1
    HardQuotaVolumeProvider._clear_verified_mount(volume, run_id, budget)
    released = HardQuotaVolumeProvider._helper("release", run_id) == {
        "status": "RELEASED", "run_id": run_id}
    if not released:
        return 1
    roots = list(Path("/tmp").glob(f"cdr-ep07-artifacts-{run_id}-*"))
    if len(roots) != 1 or roots[0].is_symlink() or not roots[0].is_dir():
        raise ValueError("retired temporary root is not exact")
    source = roots[0] / "sources" / run_id
    source.chmod(0o700)
    shutil.rmtree(roots[0])
    _write_json(evidence / "retirement.json", {
        "schema_version": "1.0", "run_id": run_id, "released": True,
        "rechecked_without_rerun": True, "private_temp_root_removed": True,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    })
    print(f"Artifact boundary rechecked and retired: {evidence}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: ep07_artifact_recheck.py RUN_ID")
    raise SystemExit(run(sys.argv[1]))
