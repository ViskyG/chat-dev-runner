"""Authorized finite HOST pass/fail boundary check; installed service remains stopped.

The private local-file mailbox exercises real durable v2 admission, not GitHub or
Docs. All project source/state is fresh beneath one owner-private /tmp directory.
Unknown cleanup retains that tree and its v2 lane for explicit reconciliation.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

from prerequisites import live_counts, profile_dir, service_state

import runner_app.executors.rootless_v2 as backend_module
from runner_app.engine.intake_v2 import AckResult, IntakeCoordinator, Observation
from runner_app.executors.preparation_coordinator import preparation_run_id
from runner_app.executors.rootless_v2 import RootlessError, RootlessTaskBackend
from runner_app.executors.volume_provider import VOLUME_BASE
from runner_app.git.argv import run_git
from runner_app.git.source_v2 import LocalWorkspace, SourceProject, SourceResolver
from runner_app.git.templates import starter_files
from runner_app.outputs.quality_v2 import evaluate_quality, parse_test_report
from runner_app.storage.database import Database
from runner_app.storage.intake_v2 import IntakeStore

REPO = Path(__file__).resolve().parents[4]


def write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, sort_keys=True, indent=2, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


class LocalFileMailbox:
    """One private local file with exact-byte CAS and persisted ACK readback."""

    def __init__(self, root: Path, value: dict[str, Any]) -> None:
        self.root, self.path = root, root / "mailbox.json"
        root.mkdir(mode=0o700)
        self.replace(value)

    @contextmanager
    def locked(self):
        with (self.root / "mailbox.lock").open("a+b") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _replace(self, raw: bytes) -> None:
        temporary = self.root / f"mailbox-{uuid4()}.tmp"
        with temporary.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def replace(self, value: dict[str, Any]) -> None:
        with self.locked():
            self._replace(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())

    def read_current(self) -> Observation:
        with self.locked():
            raw = self.path.read_bytes()
        return Observation(raw, hashlib.sha256(raw).hexdigest())

    def ack_if_current(self, observed: Observation, payload: bytes) -> AckResult:
        with self.locked():
            if self.path.read_bytes() != observed.payload:
                return AckResult("CONFLICT")
            self._replace(payload)
            readback = self.path.read_bytes()
            if readback != payload:
                return AckResult("UNKNOWN")
            digest = hashlib.sha256(readback).hexdigest()
            return AckResult("APPLIED", digest, digest)


def stopped_and_idle() -> dict[str, Any]:
    service = service_state()
    if not service.get("available") or service.get("ActiveState") != "inactive":
        raise RuntimeError("INSTALLED_SERVICE_NOT_CONFIRMED_INACTIVE")
    selected = service.get("XDG_DATA_HOME")
    if not selected:
        raise RuntimeError("INSTALLED_STATE_PATH_UNKNOWN")
    counts = live_counts(Path(selected) / "chat-dev-runner/state.sqlite")
    if not counts.get("idle_without_pending_intent"):
        raise RuntimeError("INSTALLED_LEDGER_NOT_IDLE")
    return {"service": service, "ledger": counts,
            "observed_at": datetime.now(timezone.utc).isoformat()}


def absent(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def export_state(store: IntakeStore, destination: Path) -> dict[str, Any]:
    with store._connection() as connection:
        journal = [dict(row) for row in connection.execute("SELECT * FROM v2_journal ORDER BY event_id")]
        lane_count = connection.execute("SELECT COUNT(*) FROM execution_lane").fetchone()[0]
        v1_count = connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        target = sqlite3.connect(destination / "admission.sqlite")
        try:
            connection.backup(target)
        finally:
            target.close()
    (destination / "admission.sqlite").chmod(0o600)
    write_json(destination / "journal.json", journal)
    write_json(destination / "requests.json", store.list_requests())
    return {"lane_count": lane_count, "v1_operation_count": v1_count, "integrity_check": integrity}


def run_case(batch: Path, out: Path, mode: str, store: IntakeStore, session: str,
             selected_profile_dir: Path) -> dict[str, Any]:
    before = stopped_and_idle()
    root = batch / mode
    root.mkdir(mode=0o700)
    destination = out / mode
    destination.mkdir(mode=0o700)
    project_id = f"gp03-rootless-{mode}"
    source = root / "source"
    source.mkdir(mode=0o700)
    for name, payload in starter_files("python-basic", project_id).items():
        path = source / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(0o600)
    if mode == "fail":
        (source / "tests/test_expected_failure.py").write_text(
            'def test_deliberate_canary_failure():\n    assert False, "GP-03 deliberate failure"\n')
    run_git(source, ["init", "-b", "gp03-canary"])
    run_git(source, ["add", "--all"])
    run_git(source, ["-c", "user.name=Runner canary", "-c", "user.email=canary@localhost",
                     "commit", "-m", f"Fresh {mode} starter canary"])
    repository = root / "controller.git"
    run_git(root, ["clone", "--bare", "--no-local", str(source), str(repository)])
    binding_id = f"qualification-local-file-{mode}"
    mailbox = LocalFileMailbox(root / "mailbox", {
        "protocol": "cdr-mailbox/2", "runner_id": "gp03-host-canary", "project_id": project_id,
        "slot_token": str(uuid4()), "dispatch": "DRAFT", "receipt": None,
        "request": {"request_id": None, "action": "RUN_TASK", "task": "test",
                    "source": {"kind": "local_workspace", "branch": "gp03-canary", "workspace_id": "starter"},
                    "execution": {"mode": "container", "backend_id": "rootless-python",
                                  "profile_id": "python-quick", "expected_os": "linux", "timeout_seconds": 600}}})
    store.bind(binding_id, runner_id="gp03-host-canary", project_id=project_id,
               provider="github", coordinates={"path": str(mailbox.path),
                   "qualification_transport": "private-local-file", "network": "none"}, session_id=session)
    coordinator = IntakeCoordinator(store, session)
    assert coordinator.poll_once(binding_id, mailbox).kind == "BASELINED"
    ready = json.loads(mailbox.read_current().payload)
    ready["dispatch"] = "READY"
    mailbox.replace(ready)
    accepted = coordinator.poll_once(binding_id, mailbox)
    assert accepted.kind == "ACCEPTED" and accepted.request_id
    request_id = accepted.request_id
    write_json(destination / "ack.json", json.loads(mailbox.read_current().payload))
    claim = store.claim_next(session)
    assert claim and claim["request_id"] == request_id and claim["status"] == "STARTING"
    snapshot = SourceResolver({project_id: SourceProject(repository, root / "source-runs",
        {"starter": LocalWorkspace(source)})}).resolve_current(project_id, request_id, ready["request"]["source"])
    store.mark_running(request_id, {"source_commit": snapshot.source_commit,
        "workload_sha256": snapshot.workload_sha256, "qualification": "host-rootless-boundary"})

    def require_running(selected: str) -> None:
        record = store.get(selected)
        with store._connection() as connection:
            lane = connection.execute("SELECT protocol,request_id FROM execution_lane").fetchone()
        if (selected != request_id or record is None or record["status"] != "RUNNING"
                or lane is None or tuple(lane) != ("v2", request_id)):
            raise RootlessError("OPERATION_NOT_RUNNING", cleanup_verified=True)

    backend = RootlessTaskBackend(selected_profile_dir, root / "backend-runs",
                                  require_running=require_running, record_event=store.record_event)
    profile = backend.profile("python-quick")
    try:
        result = backend.run(snapshot, request_id=request_id, profile_id="python-quick", cancel=Event())
    except RootlessError as error:
        store.finish(request_id, status="FAULT", code=error.code, cleanup_verified=error.cleanup_verified)
        if backend.root.exists():
            shutil.copytree(backend.root, destination / "backend-retained", symlinks=True)
        raise
    assert result.argv == profile.argv and result.image == profile.image
    raw = (result.output_root / "junit.xml").read_bytes()
    parsed = parse_test_report(raw, "junit-xml")
    quality = evaluate_quality({"test_required": True, "minimum_executed": 1, "required_outputs": ["junit"]},
        {"junit": parsed}, available_outputs=["junit"], execution_outcomes=[result.outcome])
    mounts_absent = all(absent(VOLUME_BASE / value) for value in (request_id, preparation_run_id(request_id)))
    expected = "PASSED" if mode == "pass" else "FAILED"
    valid = (result.outcome == expected and result.exit_code == (0 if mode == "pass" else 1)
             and result.cleanup_verified and result.capture_complete and mounts_absent
             and quality.quality_gate == expected and not quality.runner_fault)
    report = {"mode": mode, "request_id": request_id, "sequence": accepted.sequence,
              "expected_outcome": expected, "result": asdict(result), "quality_gate": quality.quality_gate,
              "test_counts": dict(quality.counts), "runner_fault": quality.runner_fault,
              "source_commit": snapshot.source_commit, "workload_tree": snapshot.workload_tree,
              "workload_sha256": snapshot.workload_sha256, "source_clean_after": not run_git(source, ["status", "--porcelain"]),
              "exact_preparation_and_test_mounts_absent": mounts_absent, "pre_workload_guard": before,
              "check_passed": valid}
    store.finish(request_id, status="COMPLETED", code=result.error_code,
                 result={"outcome": result.outcome, "quality_gate": quality.quality_gate,
                         "cleanup_verified": result.cleanup_verified}, cleanup_verified=result.cleanup_verified)
    shutil.copytree(backend.evidence_root, destination / "backend-evidence")
    shutil.copytree(backend.preparation_evidence, destination / "preparation-evidence")
    shutil.copyfile(snapshot.manifest_path, destination / "source.json")
    write_json(destination / "result.json", report)
    assert valid, "ROOTLESS_CANARY_OBSERVATION_MISMATCH"
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", required=True,
                        help="Requires owner stage approval plus root go after stopping installed service")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    guard = stopped_and_idle()
    selected = profile_dir()
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    batch = Path(tempfile.mkdtemp(prefix="cdr-gp03-rootless-live-", dir="/tmp"))
    store = IntakeStore(Database(batch / "admission.sqlite"))
    session = store.start_session("gp03-host-canary")
    summary: dict[str, Any] = {"scope": "actual HOST rootless backend, SourceResolver and durable v2 journal",
        "started_at": datetime.now(timezone.utc).isoformat(), "uid": os.getuid(), "euid": os.geteuid(),
        "source_commit": run_git(REPO, ["rev-parse", "HEAD"]).strip(),
        "backend_source_sha256": hashlib.sha256(Path(backend_module.__file__).read_bytes()).hexdigest(),
        "canary_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scratch": str(batch), "persistent_evidence": str(args.out), "initial_guard": guard,
        "transport": "private local-file conditional ACK under scratch schema github tag; GitHub/Docs NOT_RUN",
        "cases": []}
    try:
        for mode in ("pass", "fail"):
            case = run_case(batch, args.out, mode, store, session, selected)
            summary["cases"].append({key: case[key] for key in ("mode", "request_id", "sequence",
                "expected_outcome", "test_counts", "quality_gate", "source_commit", "workload_sha256",
                "exact_preparation_and_test_mounts_absent", "check_passed")})
        store.end_session(session)
        summary["status"] = "PASSED"
    except Exception as error:  # noqa: BLE001 -- keep safe diagnosis and retained lane evidence.
        summary["status"] = "FAILED"
        summary["error_code"] = getattr(error, "code", type(error).__name__)
    finally:
        summary["journal_state"] = export_state(store, args.out)
        summary["final_guard"] = stopped_and_idle()
        summary["ended_at"] = datetime.now(timezone.utc).isoformat()
        write_json(args.out / "summary.json", summary)
        print(json.dumps(summary, sort_keys=True))
    if summary["status"] != "PASSED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
