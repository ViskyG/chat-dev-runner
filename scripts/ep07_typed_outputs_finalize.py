#!/usr/bin/env python3
"""Offline finalization of one already completed EP-07 typed-output rootless run.

Never starts Docker. Recollects the exact retained volume, compares the first
frozen copy byte-for-byte, and releases only that volume after cleanup proof.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

# Isolated Python still imports runner_app from the installed wheel. Only this
# repository-owned harness module is added for its validated fixture helpers.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ep07_typed_outputs import EVIDENCE, PROFILE_ID, _validate_outputs, _write

from runner_app.executors.profiles import ProfileRegistry
from runner_app.executors.run_service import _collection_metadata, _specs
from runner_app.executors.volume_provider import (
    VOLUME_BASE,
    HardQuotaVolumeProvider,
    _verified_retired_volume,
)
from runner_app.outputs.capture import CaptureResult, StreamCapture
from runner_app.outputs.collection import collect_outputs
from runner_app.outputs.inspection import inspect_registered_output
from runner_app.resources import load_schema


def _capture_from_frozen(evidence: Path) -> CaptureResult:
    """Rebuild typed descriptors from the broker's already frozen receipt."""
    directory = evidence / "capture"
    receipt = json.loads((directory / "capture.json").read_text(encoding="utf-8"))
    streams = []
    for name in ("stdout", "stderr"):
        item = receipt["streams"][name]
        raw = directory / f"{name}.bin"
        view = directory / f"{name}.txt"
        if (
            raw.stat().st_size != item["retained_bytes"]
            or hashlib.sha256(raw.read_bytes()).hexdigest() != item["sha256"]
            or item["complete"] is not True
            or item["truncated_reason"] is not None
        ):
            raise AssertionError("CAPTURE_RECEIPT_CHANGED:" + name)
        streams.append(
            StreamCapture(
                name,
                raw,
                view,
                item["observed_bytes"],
                item["retained_bytes"],
                item["sha256"],
                hashlib.sha256(view.read_bytes()).hexdigest(),
                view.stat().st_size,
                True,
                None,
            )
        )
    events = directory / "events.jsonl"
    metadata = directory / "capture.json"
    return CaptureResult(
        receipt["started_at"],
        receipt["ended_at"],
        receipt["duration_seconds"],
        receipt["exit_code"],
        receipt["termination_reason"],
        receipt["capture_fault"],
        (streams[0], streams[1]),
        events,
        hashlib.sha256(events.read_bytes()).hexdigest(),
        metadata,
        hashlib.sha256(metadata.read_bytes()).hexdigest(),
        receipt["events"]["observed"],
        receipt["events"]["retained"],
    )


def finalize(run_id: str) -> int:
    if str(UUID(run_id)) != run_id:
        raise ValueError("INVALID_RUN_ID")
    evidence = EVIDENCE / run_id
    setup = json.loads((evidence / "setup.json").read_text(encoding="utf-8"))
    intent = json.loads((evidence / "intent.json").read_text(encoding="utf-8"))
    cleanup = json.loads((evidence / "cleanup.json").read_text(encoding="utf-8"))
    failure = json.loads((evidence / "failure.json").read_text(encoding="utf-8"))
    root = Path(failure["retained_temp_root"])
    volume = VOLUME_BASE / run_id
    budget = setup["quota_bytes"]
    if (
        setup["run_id"] != run_id
        or setup["volume"] != str(volume)
        or intent["run_id"] != run_id
        or cleanup["run_id"] != run_id
        or cleanup["state"] != "REMOVED"
        or cleanup["daemon_id"] != setup["daemon_id"]
        or not str(root).startswith(f"/tmp/cdr-ep07-typed-{run_id}-")
        or root.is_symlink()
        or failure["volume_released"] is not False
    ):
        raise AssertionError("EXISTING_RUN_IDENTITY_CHANGED")
    _verified_retired_volume(volume, run_id, budget)
    profile_dir = root / "profiles"
    registry = ProfileRegistry(profile_dir, {PROFILE_ID: setup["profile_revision_sha256"]})
    profile = registry.require(PROFILE_ID)
    capture = _capture_from_frozen(evidence)
    if (
        capture.outcome != "PASSED"
        or capture.exit_code != 0
        or capture.capture_fault is not None
        or capture.observed_events != capture.retained_events
    ):
        raise AssertionError("EXISTING_CAPTURE_INCOMPLETE")
    collected = collect_outputs(
        volume / "out",
        _specs(profile),
        evidence / "collected-recheck",
        max_total_bytes=profile.output_budget_mb * 1024 * 1024,
    )
    if not collected.complete or collected.findings != ("report:FORMAT_NOT_REGISTERED",):
        raise AssertionError("EXISTING_COLLECTION_INCOMPLETE")
    first = evidence / "collected"
    for output_id, path in collected.files.items():
        if path.read_bytes() != (first / path.name).read_bytes():
            raise AssertionError("FIRST_FREEZE_CHANGED:" + output_id)
    metadata = _collection_metadata(
        collected,
        capture,
        evidence / "collection-recheck.json",
        complete=collected.complete,
        findings=collected.findings,
    )
    outputs = [*capture.outputs(relative_prefix="capture"), *collected.outputs, metadata]
    files = {
        **capture.files(),
        **collected.files,
        "runner-collection": evidence / "collection-recheck.json",
    }
    facts = _validate_outputs(outputs, files, collected.provenance)
    parse = {
        key: {"status": value.status, "summary": value.summary, "findings": list(value.findings)}
        for key, value in collected.parses.items()
    }
    if (
        set(parse) != {"app-log", "junit", "coverage", "report"}
        or any(
            parse[key]["status"] != "PARSED" or parse[key]["findings"]
            for key in ("app-log", "junit", "coverage")
        )
        or parse["report"]["status"] != "UNSUPPORTED"
        or parse["report"]["findings"] != ["FORMAT_NOT_REGISTERED"]
        or parse["app-log"]["summary"]["valid_events"] != 2
        or parse["junit"]["summary"]["passed"] != 2
        or parse["coverage"]["summary"]["line_rate"] != 0.75
    ):
        raise AssertionError("EXISTING_PARSE_CHANGED")
    inspections = {
        key: inspect_registered_output(
            next(item for item in outputs if item["output_id"] == key), files[key].read_bytes()
        )
        for key in ("app-log", "junit", "coverage", "report", "image", "binary")
    }
    if (
        inspections["app-log"]["status"] != "PARSED"
        or len(inspections["app-log"]["error_events"]) != 1
        or inspections["junit"]["status"] != "PARSED"
        or inspections["image"]["safe_preview"] is not True
        or any(
            inspections[key]["status"] != "UNSUPPORTED" for key in ("coverage", "report", "binary")
        )
    ):
        raise AssertionError("EXISTING_INSPECTION_CHANGED")
    manifest = {
        "schema_version": "1.0",
        "run_id": run_id,
        "request_id": run_id,
        "project_id": "ep07-typed-proof",
        "workspace_id": "fixture",
        "snapshot": {
            "commit": setup["snapshot_commit"],
            "tree": setup["snapshot_tree"],
            "source_dirty_at_capture": False,
        },
        "operation_status": "COMPLETED",
        "execution": {
            "outcome": "PASSED",
            "exit_code": 0,
            "duration_seconds": capture.duration_seconds,
        },
        "delivery": {"status": "NOT_ATTEMPTED", "reason": None},
        "outputs": outputs,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    Draft202012Validator(load_schema("run-manifest"), format_checker=FormatChecker()).validate(
        manifest
    )
    # This is an independently assembled schema proof, not a sealed RunService
    # record or a registered workspace execution.
    _write(evidence / "schema-shaped-host-proof.json", manifest)
    _write(
        evidence / "result.json",
        {
            "schema_version": "1.0",
            "run_id": run_id,
            "passed": True,
            "one_real_rootless_run": True,
            "finalized_offline": True,
            "execution_outcome": "PASSED",
            "collection_complete": True,
            "capture_complete": True,
            "first_freeze_matches_recheck": True,
            "output_schema_valid": True,
            "run_manifest_schema_valid": True,
            "outputs": facts,
            "parses": parse,
            "inspections": inspections,
            "provenance": collected.provenance,
            "intent_recorded": True,
            "cleanup_recorded": True,
            "ended_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    HardQuotaVolumeProvider._clear_verified_mount(volume, run_id, budget)
    released = HardQuotaVolumeProvider._helper("release", run_id) == {
        "status": "RELEASED",
        "run_id": run_id,
    }
    _write(
        evidence / "retirement.json",
        {
            "run_id": run_id,
            "released": released,
            "ended_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    if not released:
        return 1
    source = root / "sources" / run_id
    if source.exists():
        source.chmod(0o700)
    shutil.rmtree(root)
    print(f"Typed-output one-run gate finalized offline: {evidence}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: ep07_typed_outputs_finalize.py RUN_UUID")
    raise SystemExit(finalize(sys.argv[1]))
