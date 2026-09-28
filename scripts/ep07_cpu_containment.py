#!/usr/bin/env python3
"""One finite EP-07 CPU-throttling proof through the real EP-03 rootless broker.

Only generated toy code runs, inside the existing rootless Docker boundary. The
locally approved profile and hard-quota volume exist only for this proof. A
failed or uncertain run retains its volume and receipts for manual review.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from runner_app.executors.broker import (
    ContainerIntent,
    ContainerReconciliation,
    RootlessDockerBroker,
    SnapshotInput,
    _run_volume_budget,
    _verified_run_volume,
)
from runner_app.executors.profiles import ProfileRegistry
from runner_app.executors.run_service import _freeze_capture
from runner_app.executors.volume_provider import (
    HELPER,
    VOLUME_BASE,
    HardQuotaVolumeProvider,
)
from runner_app.outputs.collection import OutputSpec, collect_outputs
from runner_app.storage.paths import ensure_private_directory
from scripts.ep03_live_toy import _git, _write_json

EVIDENCE_ROOT = PROJECT_ROOT / "verification/implementation/EP-07/cpu-containment"
IMAGE = ("docker.io/library/python@sha256:"
         "4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111")
PROFILE_ID = "ep07-cpu-proof"
CPU_LIMIT = 0.25
SOURCE = dedent("""\
    from pathlib import Path
    import json
    import sys
    import time

    def cpu_stat():
        result = {}
        for line in Path('/sys/fs/cgroup/cpu.stat').read_text(encoding='ascii').splitlines():
            key, value = line.split()
            result[key] = int(value)
        return result

    cpu_max = Path('/sys/fs/cgroup/cpu.max').read_text(encoding='ascii').strip()
    before = cpu_stat()
    start = time.monotonic()
    accumulator = 1
    iterations = 0
    while time.monotonic() - start < 3.0:
        for _ in range(20000):
            accumulator = (accumulator * 1664525 + 1013904223) & 0xffffffff
        iterations += 20000
    elapsed = time.monotonic() - start
    after = cpu_stat()
    parts = cpu_max.split()
    quota = int(parts[0]) if len(parts) == 2 and parts[0] != 'max' else 0
    period = int(parts[1]) if len(parts) == 2 else 0
    throttled = after.get('nr_throttled', -1) - before.get('nr_throttled', 0)
    throttled_us = after.get('throttled_usec', -1) - before.get('throttled_usec', 0)
    enforced = (quota > 0 and period > 0 and quota * 4 == period
                and throttled > 0 and throttled_us > 0
                and iterations > 0 and 2.5 <= elapsed < 9.0)
    proof = {
        'cpu_max': cpu_max, 'expected_cpu_fraction': 0.25,
        'nr_periods_before': before.get('nr_periods'),
        'nr_periods_after': after.get('nr_periods'),
        'nr_throttled_before': before.get('nr_throttled'),
        'nr_throttled_after': after.get('nr_throttled'),
        'throttled_usec_before': before.get('throttled_usec'),
        'throttled_usec_after': after.get('throttled_usec'),
        'usage_usec_before': before.get('usage_usec'),
        'usage_usec_after': after.get('usage_usec'),
        'elapsed_seconds': round(elapsed, 6), 'iterations': iterations,
        'limit_enforced': enforced,
    }
    Path('/out/cpu.json').write_text(json.dumps(proof, sort_keys=True), encoding='utf-8')
    sys.exit(0 if enforced else 1)
""").encode("utf-8")


def _profile_bytes() -> bytes:
    profile: dict[str, Any] = {
        "schema_version": "1.0", "profile_id": PROFILE_ID,
        "description": "Disposable EP-07 CPU enforcement proof; generated toy only",
        "execution_backend": "rootless_container", "image": IMAGE,
        "argv": ["python", "-B", "/src/cpu_toy.py"], "cwd": "/src", "user": "0:0",
        "timeout_seconds": 10, "network": "none", "memory_mb": 128,
        "cpu_limit": CPU_LIMIT, "pids_limit": 32, "output_budget_mb": 8,
        "capabilities": "drop_all", "no_new_privileges": True,
        "read_only_root": True, "secrets": [], "platform": "linux/amd64",
        "collect": [{"id": "cpu-proof", "kind": "report", "path": "cpu.json",
                     "format": "json", "required": True, "upload_policy": "local_only"}],
        "approval": "Owner-authorized local EP-07 host acceptance fixture only",
        "installed": True, "approved": True,
    }
    return (json.dumps(profile, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _snapshot(root: Path, run_id: str) -> SnapshotInput:
    repo = root / "toy-git"
    repo.mkdir(mode=0o700)
    _git(repo, "init", "-q", "--template=/dev/null")
    (repo / "cpu_toy.py").write_bytes(SOURCE)
    _git(repo, "add", "--", "cpu_toy.py")
    _git(repo, "-c", "commit.gpgSign=false", "-c", "user.name=EP07 CPU Toy",
         "-c", "user.email=toy@local.invalid", "commit", "-q", "-m", "EP07 CPU toy")
    commit = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    source = root / "sources" / run_id
    source.mkdir(parents=True, mode=0o700)
    (source / "cpu_toy.py").write_bytes(SOURCE)
    (source / "cpu_toy.py").chmod(0o444)
    source.chmod(0o555)
    return SnapshotInput(str(uuid4()), commit, tree, source)


def _proof_ok(proof: Any) -> bool:
    if not isinstance(proof, dict):
        return False
    try:
        quota, period = (int(part) for part in proof["cpu_max"].split())
    except (KeyError, AttributeError, ValueError, TypeError):
        return False
    integer_keys = ("nr_periods_before", "nr_periods_after", "nr_throttled_before",
                    "nr_throttled_after", "throttled_usec_before", "throttled_usec_after",
                    "usage_usec_before", "usage_usec_after", "iterations")
    return (quota > 0 and period > 0 and quota * 4 == period
            and proof.get("expected_cpu_fraction") == CPU_LIMIT
            and proof.get("limit_enforced") is True
            and all(type(proof.get(key)) is int for key in integer_keys)
            and proof["nr_periods_after"] > proof["nr_periods_before"]
            and proof["nr_throttled_after"] > proof["nr_throttled_before"]
            and proof["throttled_usec_after"] > proof["throttled_usec_before"]
            and proof["usage_usec_after"] > proof["usage_usec_before"]
            and proof["iterations"] > 0)


def run() -> int:
    run_id = str(uuid4())
    ensure_private_directory(EVIDENCE_ROOT)
    evidence_dir = EVIDENCE_ROOT / run_id
    ensure_private_directory(evidence_dir)
    root = Path(tempfile.mkdtemp(prefix=f"cdr-ep07-cpu-{run_id}-", dir="/tmp"))
    root.chmod(0o700)
    volume = VOLUME_BASE / run_id
    volume_allocated = False
    intent: ContainerIntent | None = None
    cleanup: ContainerReconciliation | None = None
    released = False
    try:
        if not HardQuotaVolumeProvider._installed_helper():
            raise RuntimeError("VOLUME_HELPER_UNAVAILABLE")
        profile_dir = root / "profiles"
        profile_dir.mkdir(mode=0o700)
        profile_path = profile_dir / f"{PROFILE_ID}.json"
        descriptor = os.open(profile_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(_profile_bytes())
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        profile_sha = hashlib.sha256(_profile_bytes()).hexdigest()
        registry = ProfileRegistry(profile_dir, {PROFILE_ID: profile_sha})
        profile = registry.require(PROFILE_ID)
        broker = RootlessDockerBroker(registry, runtime_dir=Path(f"/run/user/{os.getuid()}"))
        probe = broker.probe()
        if (not probe.available or probe.cgroup_version != "2"
                or probe.cgroup_driver != "systemd"
                or "cpu" not in probe.delegated_controllers):
            raise RuntimeError("ROOTLESS_CPU_CONTROLLER_UNAVAILABLE")
        helper_probe = HardQuotaVolumeProvider._helper("probe")
        if helper_probe != {"status": "READY", "base": str(VOLUME_BASE), "max_slots": 8}:
            raise RuntimeError("VOLUME_HELPER_UNAVAILABLE")
        snapshot = _snapshot(root, run_id)
        _write_json(evidence_dir / "setup.json", {
            "schema_version": "1.0", "run_id": run_id,
            "source_checkout_commit": _git(PROJECT_ROOT, "rev-parse", "HEAD"),
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "toy_source_sha256": hashlib.sha256(SOURCE).hexdigest(),
            "snapshot_id": snapshot.snapshot_id, "snapshot_commit": snapshot.commit,
            "snapshot_tree": snapshot.tree, "profile_id": PROFILE_ID,
            "profile_revision_sha256": profile_sha, "cpu_limit": CPU_LIMIT,
            "image": IMAGE, "volume": str(volume), "quota_bytes": _run_volume_budget(profile),
            "daemon_id": probe.daemon_id, "server_version": probe.server_version,
            "cgroup_version": probe.cgroup_version, "cgroup_driver": probe.cgroup_driver,
            "delegated_controllers": list(probe.delegated_controllers),
            "helper_sha256": hashlib.sha256(HELPER.read_bytes()).hexdigest(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        })
        budget = _run_volume_budget(profile)
        size_mb = budget // (1024 * 1024)
        allocated = HardQuotaVolumeProvider._helper("allocate", run_id, str(size_mb))
        if allocated != {"status": "ALLOCATED", "run_id": run_id,
                         "size_mb": size_mb, "path": str(volume)}:
            raise RuntimeError("VOLUME_HELPER_RESPONSE_INVALID")
        volume_allocated = True
        _verified_run_volume(volume, run_id, budget)
        run_root = root / "runs" / run_id
        run_root.parent.mkdir(mode=0o700)

        def record_intent(value: ContainerIntent) -> None:
            nonlocal intent
            if (intent is not None or value.run_id != run_id or value.daemon_id != probe.daemon_id
                    or value.profile_revision_sha256 != profile_sha
                    or value.source_path != str(snapshot.path)):
                raise RuntimeError("BROKER_INTENT_CHANGED")
            _write_json(evidence_dir / "intent.json", asdict(value))
            intent = value

        def record_cleanup(value: ContainerReconciliation) -> None:
            nonlocal cleanup
            if (intent is None or cleanup is not None or value.run_id != run_id
                    or value.daemon_id != probe.daemon_id or value.state != "REMOVED"
                    or value.container_id is None):
                raise RuntimeError("BROKER_CLEANUP_CHANGED")
            _write_json(evidence_dir / "cleanup.json", asdict(value))
            cleanup = value

        result = broker.run(profile, snapshot, run_root, run_id=run_id,
                            writable_volume=volume, record_intent=record_intent,
                            record_cleanup=record_cleanup)
        capture_outputs, _ = _freeze_capture(result.capture, run_root, evidence_dir)
        collected = collect_outputs(
            result.output_root,
            (OutputSpec("cpu-proof", "report", "cpu.json", "json", required=True),),
            evidence_dir / "collected", max_total_bytes=4096,
        )
        frozen = collected.files.get("cpu-proof")
        proof = json.loads(frozen.read_text(encoding="utf-8")) if frozen is not None else None
        containment = result.containment
        passed = (intent is not None and cleanup is not None
                  and result.outcome == "PASSED" and result.exit_code == 0
                  and result.capture.capture_fault is None and collected.complete
                  and _proof_ok(proof) and containment.get("verified") is True
                  and containment.get("daemon_rootless") is True
                  and containment.get("network") == "none"
                  and containment.get("read_only_source") is True
                  and containment.get("read_only_root") is True
                  and containment.get("no_new_privileges") is True
                  and containment.get("capabilities_dropped") == "ALL"
                  and containment.get("cpu_nano") == 250_000_000
                  and containment.get("memory_bytes") == 128 * 1024 * 1024
                  and containment.get("pids_limit") == 32
                  and containment.get("cleanup_verified") is True)
        _write_json(evidence_dir / "result.json", {
            "schema_version": "1.0", "run_id": run_id, "passed": passed,
            "outcome": result.outcome, "exit_code": result.exit_code,
            "duration_seconds": result.duration_seconds,
            "capture_fault": result.capture.capture_fault,
            "capture_outputs": list(capture_outputs),
            "collection_outputs": list(collected.outputs),
            "collection_complete": collected.complete,
            "cpu_proof": proof, "containment": dict(containment),
            "intent_recorded": intent is not None, "cleanup_recorded": cleanup is not None,
            "ended_at": datetime.now(timezone.utc).isoformat(),
        })
        if not passed:
            print(f"CPU proof failed; evidence: {evidence_dir}", file=sys.stderr)
            return 1
        # Release only this exact disposable volume after the broker's durable
        # cleanup receipt and copied, bounded output have been independently read.
        HardQuotaVolumeProvider._clear_verified_mount(volume, run_id, budget)
        response = HardQuotaVolumeProvider._helper("release", run_id)
        released = response == {"status": "RELEASED", "run_id": run_id}
        _write_json(evidence_dir / "retirement.json", {
            "schema_version": "1.0", "run_id": run_id, "released": released,
            "ended_at": datetime.now(timezone.utc).isoformat(),
        })
        if not released:
            return 1
        print(f"CPU cgroup throttling verified; evidence: {evidence_dir}")
        return 0
    except Exception as error:  # noqa: BLE001 -- stable local diagnostic, retain artifacts
        code = getattr(error, "code", type(error).__name__)
        _write_json(evidence_dir / "failure.json", {
            "schema_version": "1.0", "run_id": run_id, "code": str(code),
            "intent_recorded": intent is not None, "cleanup_recorded": cleanup is not None,
            "volume_allocated": volume_allocated, "volume_released": released,
            "retained_temp_root": str(root),
            "ended_at": datetime.now(timezone.utc).isoformat(),
        })
        print(f"CPU proof error {code}; evidence: {evidence_dir}", file=sys.stderr)
        return 1
    finally:
        if released:
            source = root / "sources" / run_id
            if source.exists():
                source.chmod(0o700)
            shutil.rmtree(root)


if __name__ == "__main__":
    raise SystemExit(run())
