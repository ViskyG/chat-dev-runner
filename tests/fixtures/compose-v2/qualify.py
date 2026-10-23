"""Explicitly invoked, finite real host qualification; never imported by pytest.

Uses scratch local-file admission and actual QueueWorker/NativeTaskRunner,
rootless Docker/Compose and the already installed quota helper. No provider,
installed service, SDK, image pull or project outside the scratch tree is changed.
On uncertainty it stops immediately and preserves exact request evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Timer
from typing import Any
from uuid import uuid4

from runner_app.engine.intake_v2 import AckResult, IntakeCoordinator, Observation
from runner_app.engine.queue_v2 import QueueWorker
from runner_app.engine.task_v2 import NativeTaskRunner, TaskProject
from runner_app.executors.compose_v2 import ComposeTaskBackend
from runner_app.executors.rootless_v2 import RootlessError
from runner_app.executors.volume_provider import VOLUME_BASE
from runner_app.git.argv import run_git
from runner_app.git.source_v2 import LocalWorkspace, SourceProject, SourceResolver
from runner_app.storage.database import Database
from runner_app.storage.intake_v2 import IntakeStore

IMAGE = "docker.io/library/python@sha256:4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111"


def write(path: Path, value: Any) -> bytes:
    raw = json.dumps(value, sort_keys=True, indent=2, default=str).encode() + b"\n"
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return raw


class LocalFileMailbox:
    def __init__(self, path: Path, value: dict[str, Any]):
        self.path = path
        write(path, value)

    def read_current(self) -> Observation:
        raw = self.path.read_bytes()
        return Observation(raw, hashlib.sha256(raw).hexdigest())

    def replace(self, raw: bytes) -> None:
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def ack_if_current(self, observed: Observation, payload: bytes) -> AckResult:
        if self.path.read_bytes() != observed.payload:
            return AckResult("CONFLICT")
        self.replace(payload)
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        return AckResult("APPLIED", digest, digest)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--mode", choices=["pass", "fail", "unhealthy", "timeout", "cancel"], required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    root = Path(tempfile.mkdtemp(prefix="cdr-compose-qualification-"))
    write(args.out / "intent.json", {"mode": args.mode, "scratch": root,
                                    "started": datetime.now(timezone.utc).isoformat()})
    source = root / "source"
    source.mkdir(mode=0o700)
    for name in ("api.py", "migrate.py", "client.py"):
        shutil.copyfile(Path(__file__).parent / name, source / name)
    argv = ["python", "/src/client.py", args.mode]
    health = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/', timeout=1).read()"]
    if args.mode == "unhealthy":
        health = ["CMD", "python", "-c", "raise SystemExit(1)"]
    compose = {"services": {
        "api": {"image": IMAGE, "command": ["python", "/src/api.py"], "healthcheck": {
            "test": health, "interval": "1s", "timeout": "1s", "retries": 2}},
        "migrate": {"image": IMAGE, "command": ["python", "/src/migrate.py"]},
        "test": {"image": IMAGE, "command": argv}}}
    raw_compose = write(source / "compose.json", compose)
    profile = {
        "schema_version": "cdr-compose-profile/1", "profile_id": "compose-python", "image": IMAGE,
        "platform": "linux/amd64", "compose_file": "compose.json",
        "compose_sha256": hashlib.sha256(raw_compose).hexdigest(),
        "test_service": "test", "ready_services": ["api"], "migration_services": ["migrate"],
        "argv": argv, "timeout_seconds": 3 if args.mode == "timeout" else 15,
        "readiness_seconds": 15, "migration_seconds": 10, "cleanup_seconds": 2,
        "volume_mb": 64, "memory_mb": 96, "cpu_limit": 0.5, "pids_limit": 32,
        "max_log_bytes": 65536, "mount_policy": "frozen-source-and-run-quota-v1",
        "collect": [{"id": "junit", "kind": "test_result", "path": "junit.xml",
                     "format": "junit-xml", "required": True, "upload_policy": "local_only"},
                    {"id": "partial", "kind": "report", "path": "partial.json",
                     "format": "json", "required": False, "upload_policy": "local_only"},
                    {"id": "answer", "kind": "report", "path": "result.json",
                     "format": "json", "required": False, "upload_policy": "local_only"}]}
    profiles = root / "profiles"
    profiles.mkdir(mode=0o700)
    raw = write(profiles / "compose-python.json", profile)
    write(profiles / "compose-grant.json", {"schema_version": "cdr-compose-grant/1",
          "profile_revision_sha256": {"compose-python": hashlib.sha256(raw).hexdigest()}})
    (source / ".runner").mkdir(mode=0o700)
    write(source / ".runner/project.json", {
        "protocol": "cdr-project/2", "project_id": "compose-canary", "tasks": {"test": {
            "language": "python", "profile_id": "compose-python", "steps": [{
                "id": "compose-test", "phase": "test", "argv": argv, "cwd": ".",
                "timeout_seconds": profile["timeout_seconds"]}],
            "outputs": [{key: item[key] for key in ("id", "kind", "path", "format", "required")}
                        | {"source_root": "out", "publish": "local_only"} for item in profile["collect"]],
            "quality_gate": {"test_required": True, "minimum_executed": 2, "required_outputs": ["junit"]}}}})
    run_git(source, ["init", "-b", "compose-qualification"])
    run_git(source, ["add", "--all"])
    run_git(source, ["-c", "user.name=Compose fixture", "-c", "user.email=compose@localhost",
                     "commit", "-m", "Disposable Compose fixture"])
    repository = root / "source.git"
    run_git(root, ["clone", "--bare", "--no-local", str(source), str(repository)])
    store = IntakeStore(Database(root / "state.sqlite"))
    session = store.start_session("compose-canary")
    request_id = str(uuid4())
    timers: list[Timer] = []

    def require_running(identifier: str) -> None:
        row = store.get(identifier)
        if row is None or row["status"] != "RUNNING":
            raise RootlessError("OPERATION_NOT_RUNNING")

    def record(identifier: str, name: str, detail: dict[str, object]) -> None:
        store.record_event(identifier, name, detail)
        if args.mode == "cancel" and name == "COMPOSE_PHASE_START_INTENDED" and detail["phase"] == "test":
            timer = Timer(1, lambda: store.cancel(identifier))
            timers.append(timer)
            timer.start()

    backend = ComposeTaskBackend(profiles, root / "backend-runs", require_running=require_running,
                                  record_event=record)
    request = {"request_id": request_id, "action": "RUN_TASK", "task": "test",
               "source": {"kind": "local_workspace", "workspace_id": "fixture", "branch": "compose-qualification"},
               "execution": {"mode": "container", "backend_id": "rootless-compose", "profile_id": "compose-python",
                             "expected_os": "linux", "timeout_seconds": backend.timeout_seconds("compose-python")}}
    mailbox = LocalFileMailbox(root / "mailbox.json", {
        "protocol": "cdr-mailbox/2", "runner_id": "compose-canary", "project_id": "compose-canary",
        "slot_token": str(uuid4()), "dispatch": "DRAFT", "receipt": None, "request": request})
    store.bind("fixture", runner_id="compose-canary", project_id="compose-canary", provider="github",
               coordinates={"qualification_transport": "private-local-file", "path": str(mailbox.path)}, session_id=session)
    coordinator = IntakeCoordinator(store, session)
    assert coordinator.poll_once("fixture", mailbox).kind == "BASELINED"
    ready = json.loads(mailbox.read_current().payload)
    ready["dispatch"] = "READY"
    mailbox.replace(json.dumps(ready).encode())
    accepted = coordinator.poll_once("fixture", mailbox)
    assert accepted.kind == "ACCEPTED" and accepted.request_id == request_id
    write(args.out / "request.json", {"request_id": request_id, "request": request, "scratch": root})
    resolver = SourceResolver({"compose-canary": SourceProject(repository, root / "runs",
                                                               {"fixture": LocalWorkspace(source)})})
    handler = NativeTaskRunner(store, resolver, {"compose-canary": TaskProject(
        repository, root / "results", (), None, ("compose-python",))}, {},
        container_backends={"rootless-compose": backend})
    print('@@HUD ' + json.dumps({"phase": "Compose " + args.mode, "current": 0, "total": 1, "unit": "cases"}), flush=True)
    try:
        assert QueueWorker(store, session, handler).run_once()
    finally:
        for timer in timers:
            timer.join(2)
        operation = store.get(request_id)
        with store._connection() as connection:
            journal = [dict(row) for row in connection.execute("SELECT * FROM v2_journal ORDER BY event_id")]
            lane_count = connection.execute("SELECT COUNT(*) FROM execution_lane").fetchone()[0]
        write(args.out / "operation.json", operation)
        write(args.out / "journal.json", journal)
        if backend.root.exists():
            shutil.copytree(backend.root, args.out / "backend")
        if (root / "results").exists():
            shutil.copytree(root / "results", args.out / "results")
        shutil.copytree(source, args.out / "source", ignore=shutil.ignore_patterns(".git"))
        summary = {"mode": args.mode, "request_id": request_id, "scratch": root,
                   "status": operation["status"], "code": operation["code"], "lane_count": lane_count,
                   "quota_mount_absent": not os.path.ismount(VOLUME_BASE / request_id),
                   "source_clean": not bool(run_git(source, ["status", "--porcelain"])),
                   "scope": "actual rootless Compose, helper, SourceResolver, NativeTaskRunner, QueueWorker; local-file fixture admission, no cloud provider",
                   "backend_sha256": hashlib.sha256(Path(__import__("runner_app.executors.compose_v2", fromlist=["x"]).__file__).read_bytes()).hexdigest(),
                   "finished": datetime.now(timezone.utc).isoformat()}
        write(args.out / "summary.json", summary)
        print(json.dumps(summary, default=str), flush=True)
    assert lane_count == 0 and summary["quota_mount_absent"] and summary["source_clean"], summary
    manifest = operation["result"]["manifest"]
    expected = {"pass": "PASSED", "fail": "FAILED", "unhealthy": "NOT_RUN",
                "timeout": "TIMED_OUT", "cancel": "CANCELLED"}[args.mode]
    assert manifest["execution"]["outcome"] == expected, manifest["execution"]
    assert manifest["execution"]["cleanup"] == "CONFIRMED", manifest["execution"]
    print('@@HUD ' + json.dumps({"phase": "Verified " + args.mode, "current": 1, "total": 1, "unit": "cases"}), flush=True)


if __name__ == "__main__":
    main()
