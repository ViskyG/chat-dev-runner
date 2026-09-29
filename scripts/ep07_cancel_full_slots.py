#!/usr/bin/env python3
"""One finite real-host cancellation test while both Engine slots are occupied.

Runs generated toy code only inside the installed rootless Docker boundary.
Uses a disposable owner-local SQLite ledger and exact quota volumes. No service,
Drive, GitHub, owner project or shared EP-03 volume pool is touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator

from runner_app.domain.commands import LocalPolicy, Provenance
from runner_app.engine.service import Engine, OperationResult
from runner_app.executors.broker import (
    ContainerIntent,
    ContainerReconciliation,
    RootlessDockerBroker,
    SnapshotInput,
    _run_volume_budget,
    _verified_run_volume,
)
from runner_app.executors.profiles import ProfileRegistry
from runner_app.executors.run_service import _freeze_capture, _specs
from runner_app.executors.volume_provider import (
    HELPER,
    VOLUME_BASE,
    HardQuotaVolumeProvider,
)
from runner_app.outputs.collection import collect_outputs
from runner_app.resources import load_schema
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger
from runner_app.storage.paths import ensure_private_directory

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "verification/implementation/EP-07/cancel-full-slots"
IMAGE = (
    "docker.io/library/python@sha256:"
    "4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111"
)
PROFILE_ID = "ep07-cancel-slots-proof"
LOCAL = Provenance(kind="local", source_id="ep07-local-proof")
PROJECT = "ep07-cancel-proof"
TOY = (
    b"from pathlib import Path\n"
    b"import time\n"
    b"Path('/out/started.txt').write_text('started\\n', encoding='utf-8')\n"
    b"print('rootless toy started', flush=True)\n"
    b"time.sleep(60)\n"
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, value: Any) -> None:
    raw = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(root), "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return completed.stdout.decode("ascii").strip()


def _snapshot(root: Path) -> SnapshotInput:
    repo = root / "toy-git"
    repo.mkdir(mode=0o700)
    _git(repo, "init", "-q", "--template=/dev/null")
    (repo / "long_toy.py").write_bytes(TOY)
    _git(repo, "add", "--", "long_toy.py")
    _git(
        repo,
        "-c",
        "commit.gpgSign=false",
        "-c",
        "user.name=EP07 Cancel Toy",
        "-c",
        "user.email=toy@local.invalid",
        "commit",
        "-q",
        "-m",
        "EP07 cancel toy",
    )
    commit = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    source = root / "source"
    source.mkdir(mode=0o700)
    (source / "long_toy.py").write_bytes(TOY)
    (source / "long_toy.py").chmod(0o444)
    source.chmod(0o555)
    return SnapshotInput(str(uuid4()), commit, tree, source)


def _profile_bytes() -> bytes:
    profile = {
        "schema_version": "1.0",
        "profile_id": PROFILE_ID,
        "description": "Disposable EP-07 full-slots cancellation proof",
        "execution_backend": "rootless_container",
        "image": IMAGE,
        "argv": ["python", "-B", "/src/long_toy.py"],
        "cwd": "/src",
        "user": "0:0",
        "timeout_seconds": 90,
        "network": "none",
        "memory_mb": 128,
        "cpu_limit": 1,
        "pids_limit": 32,
        "output_budget_mb": 8,
        "capabilities": "drop_all",
        "no_new_privileges": True,
        "read_only_root": True,
        "secrets": [],
        "platform": "linux/amd64",
        "collect": [
            {
                "id": "started",
                "kind": "artifact",
                "path": "started.txt",
                "format": "text",
                "required": True,
                "upload_policy": "local_only",
            }
        ],
        "approval": "Owner-authorized local EP-07 host acceptance fixture only",
        "installed": True,
        "approved": True,
    }
    return (json.dumps(profile, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _command(
    ledger: Ledger, workspace: str, action: str = "VERIFY", target: str | None = None
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "schema_version": "1.0",
        "request_id": str(uuid4()),
        "dispatch": "READY",
        "runner_id": ledger.state()["runner_id"],
        "epoch": ledger.state()["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": action,
        "project_id": PROJECT,
        "workspace_id": workspace,
        "expected_head": "a" * 40,
        "params": ({"target_request_id": target} if target else {"profile_id": PROFILE_ID}),
        "intent": "Run or cancel this finite, disposable local host proof.",
    }


def _submit(engine: Engine, command: dict[str, Any]):
    raw = json.dumps(command, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return engine.submit(raw, LOCAL)


def _wait_terminal(ledger: Ledger, request_id: str, deadline: float) -> dict[str, Any]:
    while time.monotonic() < deadline:
        operation = ledger.get(request_id)
        if operation is not None and operation["status"] != "RUNNING":
            return operation
        time.sleep(0.02)
    raise AssertionError("TERMINAL_TIMEOUT:" + request_id)


def run() -> int:
    probe_id = str(uuid4())
    ensure_private_directory(EVIDENCE)
    evidence = EVIDENCE / probe_id
    ensure_private_directory(evidence)
    root = Path(tempfile.mkdtemp(prefix=f"cdr-ep07-cancel-{probe_id}-", dir="/tmp"))
    root.chmod(0o700)
    engine: Engine | None = None
    volume_ids: list[str] = []
    released: set[str] = set()
    handler_calls: list[str] = []
    handler_lock = threading.Lock()
    try:
        if not HardQuotaVolumeProvider._installed_helper():
            raise RuntimeError("VOLUME_HELPER_UNAVAILABLE")
        profile_dir = root / "profiles"
        profile_dir.mkdir(mode=0o700)
        profile_bytes = _profile_bytes()
        profile_path = profile_dir / f"{PROFILE_ID}.json"
        profile_path.write_bytes(profile_bytes)
        profile_path.chmod(0o600)
        profile_sha = hashlib.sha256(profile_bytes).hexdigest()
        registry = ProfileRegistry(profile_dir, {PROFILE_ID: profile_sha})
        profile = registry.require(PROFILE_ID)
        probe_broker = RootlessDockerBroker(registry, runtime_dir=Path(f"/run/user/{os.getuid()}"))
        probe = probe_broker.probe()
        if not probe.available or not probe.daemon_id:
            raise RuntimeError("ROOTLESS_DOCKER_UNAVAILABLE:" + probe.code)
        if HardQuotaVolumeProvider._helper("probe") != {
            "status": "READY",
            "base": str(VOLUME_BASE),
            "max_slots": 8,
        }:
            raise RuntimeError("VOLUME_HELPER_UNAVAILABLE")
        snapshot = _snapshot(root)
        budget = _run_volume_budget(profile)
        (root / "runs").mkdir(mode=0o700)
        ledger = Ledger(Database(root / "state.sqlite"), max_parallel=2)
        ledger.startup()
        ledger.arm()
        policy = LocalPolicy(
            project_ids=frozenset({PROJECT}),
            workspace_pairs=frozenset({(PROJECT, name) for name in "abc"}),
            approved_profiles=frozenset({PROFILE_ID}),
        )

        def handler(frozen, context):
            command = frozen.command
            run_id = command["request_id"]
            run_evidence = evidence / run_id
            ensure_private_directory(run_evidence)
            volume = VOLUME_BASE / run_id
            intent: ContainerIntent | None = None
            cleanup: ContainerReconciliation | None = None
            with handler_lock:
                handler_calls.append(run_id)
            try:
                size_mb = budget // (1024 * 1024)
                allocation = HardQuotaVolumeProvider._helper("allocate", run_id, str(size_mb))
                if allocation != {
                    "status": "ALLOCATED",
                    "run_id": run_id,
                    "size_mb": size_mb,
                    "path": str(volume),
                }:
                    raise AssertionError("ALLOCATION_CHANGED")
                with handler_lock:
                    volume_ids.append(run_id)
                _verified_run_volume(volume, run_id, budget)
                run_root = root / "runs" / run_id

                def record_intent(value: ContainerIntent) -> None:
                    nonlocal intent
                    if (
                        intent is not None
                        or value.run_id != run_id
                        or value.daemon_id != probe.daemon_id
                        or value.profile_revision_sha256 != profile_sha
                    ):
                        raise AssertionError("INTENT_CHANGED")
                    _write(run_evidence / "intent.json", asdict(value))
                    intent = value

                def record_cleanup(value: ContainerReconciliation) -> None:
                    nonlocal cleanup
                    if (
                        intent is None
                        or cleanup is not None
                        or value.run_id != run_id
                        or value.daemon_id != probe.daemon_id
                        or value.state != "REMOVED"
                    ):
                        raise AssertionError("CLEANUP_CHANGED")
                    _write(run_evidence / "cleanup.json", asdict(value))
                    cleanup = value

                broker = RootlessDockerBroker(
                    registry, runtime_dir=Path(f"/run/user/{os.getuid()}")
                )
                result = broker.run(
                    profile,
                    snapshot,
                    run_root,
                    run_id=run_id,
                    writable_volume=volume,
                    cancel=context.cancel,
                    record_intent=record_intent,
                    record_cleanup=record_cleanup,
                )
                capture, capture_files = _freeze_capture(result.capture, run_root, run_evidence)
                collected = collect_outputs(
                    result.output_root,
                    _specs(profile),
                    run_evidence / "collected",
                    max_total_bytes=profile.output_budget_mb * 1024 * 1024,
                )
                descriptors = [*capture, *collected.outputs]
                files = {**capture_files, **collected.files}
                validator = Draft202012Validator(load_schema("output"))
                for item in descriptors:
                    validator.validate(item)
                    file = files[item["output_id"]]
                    if (
                        hashlib.sha256(file.read_bytes()).hexdigest() != item["sha256"]
                        or file.stat().st_size != item["size_bytes"]
                        or item["complete"] is not True
                    ):
                        raise AssertionError("FROZEN_OUTPUT_CHANGED")
                if (
                    result.outcome != "CANCELLED"
                    or cleanup is None
                    or intent is None
                    or not collected.complete
                    or collected.findings
                    or (run_evidence / "collected/started.txt").read_text() != "started\n"
                    or not (run_evidence / "capture/stdout.txt")
                    .read_text()
                    .startswith("rootless toy started")
                ):
                    raise AssertionError("CANCELLATION_CAPTURE_CHANGED")
                absent = broker.reconcile_absent_container(intent)
                if absent.state != "ABSENT":
                    raise AssertionError("CONTAINER_STILL_PRESENT")
                _write(
                    run_evidence / "result.json",
                    {
                        "run_id": run_id,
                        "workspace_id": command["workspace_id"],
                        "outcome": result.outcome,
                        "exit_code": result.exit_code,
                        "duration_seconds": result.duration_seconds,
                        "containment": dict(result.containment),
                        "capture": list(capture),
                        "collection": list(collected.outputs),
                        "collection_complete": collected.complete,
                        "findings": list(collected.findings),
                        "container_absence": asdict(absent),
                        "ended_at": _utc(),
                    },
                )
                HardQuotaVolumeProvider._clear_verified_mount(volume, run_id, budget)
                retirement = HardQuotaVolumeProvider._helper("release", run_id)
                if retirement != {"status": "RELEASED", "run_id": run_id}:
                    raise AssertionError("VOLUME_RELEASE_FAILED")
                with handler_lock:
                    released.add(run_id)
                _write(
                    run_evidence / "retirement.json",
                    {
                        "run_id": run_id,
                        "released": True,
                        "ended_at": _utc(),
                    },
                )
                return OperationResult(
                    "CANCELLED",
                    result={
                        "execution": {"outcome": result.outcome, "exit_code": result.exit_code},
                        "delivery": {"status": "NOT_ATTEMPTED"},
                    },
                )
            except Exception as error:
                _write(
                    run_evidence / "failure.json",
                    {
                        "run_id": run_id,
                        "code": str(getattr(error, "code", type(error).__name__)),
                        "intent_recorded": intent is not None,
                        "cleanup_recorded": cleanup is not None,
                        "volume_allocated": run_id in volume_ids,
                        "volume_released": run_id in released,
                        "ended_at": _utc(),
                    },
                )
                raise

        engine = Engine(ledger, policy=policy, handlers={"VERIFY": handler})
        first = _command(ledger, "a")
        second = _command(ledger, "b")
        third = _command(ledger, "c")
        _write(
            evidence / "setup.json",
            {
                "probe_id": probe_id,
                "source_commit": _git(ROOT, "rev-parse", "HEAD"),
                "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "toy_sha256": hashlib.sha256(TOY).hexdigest(),
                "snapshot_commit": snapshot.commit,
                "snapshot_tree": snapshot.tree,
                "profile_revision_sha256": profile_sha,
                "image": IMAGE,
                "helper_sha256": hashlib.sha256(HELPER.read_bytes()).hexdigest(),
                "installed_package": str(Path(sys.modules["runner_app"].__file__).resolve()),
                "daemon_id": probe.daemon_id,
                "server_version": probe.server_version,
                "quota_bytes_per_run": budget,
                "max_parallel": 2,
                "request_ids": {
                    "a": first["request_id"],
                    "b": second["request_id"],
                    "c": third["request_id"],
                },
                "started_at": _utc(),
            },
        )
        admissions = [_submit(engine, first), _submit(engine, second)]
        if not all(value.execute and value.receipt["status"] == "RUNNING" for value in admissions):
            raise AssertionError("TWO_ADMISSIONS_FAILED")
        started_deadline = time.monotonic() + 20
        while time.monotonic() < started_deadline:
            paths = [
                VOLUME_BASE / command["request_id"] / "out/started.txt"
                for command in (first, second)
            ]
            if all(path.is_file() and path.read_text() == "started\n" for path in paths):
                break
            time.sleep(0.03)
        else:
            raise AssertionError("TWO_CONTAINERS_DID_NOT_START")
        with ledger.database.connect() as connection:
            held = connection.execute(
                "SELECT count(*) FROM operations WHERE capacity_held=1",
            ).fetchone()[0]
            locks = sorted(row[0] for row in connection.execute("SELECT scope FROM locks"))
        if (
            held != 2
            or locks != [f"workspace:{PROJECT}/a", f"workspace:{PROJECT}/b"]
            or any(
                ledger.get(item["request_id"])["status"] != "RUNNING" for item in (first, second)
            )
        ):
            raise AssertionError("SLOTS_NOT_FULL")
        started_at = time.monotonic()
        busy = _submit(engine, third)
        busy_seconds = round(time.monotonic() - started_at, 6)
        if (
            busy.execute
            or busy.receipt["status"] != "REJECTED"
            or busy.receipt["code"] != "BUSY"
            or busy_seconds > 2
        ):
            raise AssertionError("THIRD_NOT_IMMEDIATE_BUSY")
        cancel_first = _command(ledger, "a", "CANCEL_OPERATION", first["request_id"])
        started_at = time.monotonic()
        cancelled = _submit(engine, cancel_first)
        cancel_seconds = round(time.monotonic() - started_at, 6)
        if (
            not cancelled.execute
            or cancelled.receipt["status"] != "COMPLETED"
            or cancel_seconds > 2
        ):
            raise AssertionError("CANCEL_NOT_ACCEPTED_WITH_FULL_SLOTS")
        first_terminal = _wait_terminal(ledger, first["request_id"], time.monotonic() + 15)
        if first_terminal["status"] != "CANCELLED":
            raise AssertionError("FIRST_NOT_CANCELLED")
        duplicate = _submit(engine, third)
        if not duplicate.duplicate or duplicate.receipt != busy.receipt:
            raise AssertionError("BUSY_REPLAY_CHANGED")
        cancel_second = _command(ledger, "b", "CANCEL_OPERATION", second["request_id"])
        cancelled_second = _submit(engine, cancel_second)
        second_terminal = _wait_terminal(ledger, second["request_id"], time.monotonic() + 15)
        if (
            not cancelled_second.execute
            or cancelled_second.receipt["status"] != "COMPLETED"
            or second_terminal["status"] != "CANCELLED"
        ):
            raise AssertionError("SECOND_NOT_CANCELLED")
        with ledger.database.connect() as connection:
            final_held = connection.execute(
                "SELECT count(*) FROM operations WHERE capacity_held=1",
            ).fetchone()[0]
            final_locks = connection.execute("SELECT count(*) FROM locks").fetchone()[0]
            third_dispatch = connection.execute(
                "SELECT count(*) FROM journal WHERE request_id=? AND event_type='STARTED'",
                (third["request_id"],),
            ).fetchone()[0]
        if (
            final_held != 0
            or final_locks != 0
            or third_dispatch != 0
            or sorted(handler_calls) != sorted([first["request_id"], second["request_id"]])
            or released != {first["request_id"], second["request_id"]}
            or any((VOLUME_BASE / request_id).exists() for request_id in released)
        ):
            raise AssertionError("CAPACITY_OR_CLEANUP_CHANGED")
        _write(
            evidence / "result.json",
            {
                "probe_id": probe_id,
                "passed": True,
                "two_real_rootless_runs": True,
                "slots_held_at_busy": held,
                "locks_at_busy": locks,
                "third_receipt": busy.receipt,
                "third_busy_latency_seconds": busy_seconds,
                "cancel_first_receipt": cancelled.receipt,
                "cancel_first_latency_seconds": cancel_seconds,
                "first_terminal": first_terminal["receipt"],
                "second_terminal": second_terminal["receipt"],
                "third_duplicate_same_receipt": True,
                "third_dispatch_count": third_dispatch,
                "handler_calls": handler_calls,
                "capacity_held_final": final_held,
                "locks_final": final_locks,
                "volumes_released": sorted(released),
                "service_touched": False,
                "drive_or_github_touched": False,
                "ended_at": _utc(),
            },
        )
        print(f"Full-slots cancellation proof passed: {evidence}")
        return 0
    except Exception as error:  # noqa: BLE001 -- stable checkpoint, never conceal uncertainty
        _write(
            evidence / "failure.json",
            {
                "probe_id": probe_id,
                "code": str(getattr(error, "code", type(error).__name__)),
                "handler_calls": handler_calls,
                "volume_ids": volume_ids,
                "volumes_released": sorted(released),
                "retained_temp_root": str(root),
                "ended_at": _utc(),
            },
        )
        print(
            f"Full-slots cancellation probe failed: {type(error).__name__}: {error}; "
            f"evidence: {evidence}",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.shutdown(timeout=5)
        if sorted(volume_ids) == sorted(released):
            source = root / "source"
            if source.exists():
                source.chmod(0o700)
            shutil.rmtree(root)


if __name__ == "__main__":
    raise SystemExit(run())
