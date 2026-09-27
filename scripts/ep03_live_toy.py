#!/usr/bin/env python3
"""One-shot, disposable EP-03 rootless smoke run on generated toy source only.

This intentionally exercises the broker directly. It is not the production
admission/RunService path, and a successful toy run alone cannot arm VERIFY.
Each invocation consumes one fresh request-ID mount from the checked-in pool.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from threading import Event, Thread
from typing import Any
from uuid import UUID, uuid4, uuid5

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from runner_app.executors.broker import (
    BrokerError,
    BrokerResult,
    ContainerIntent,
    ContainerReconciliation,
    RootlessDockerBroker,
    SnapshotInput,
    _run_volume_budget,
    _verified_run_volume,
)
from runner_app.executors.profiles import ApprovedProfile, ProfileError, ProfileRegistry
from runner_app.executors.run_service import _freeze_capture
from runner_app.outputs.collection import CollectionResult, OutputSpec, collect_outputs
from runner_app.storage.paths import ensure_private_directory

POOL = PROJECT_ROOT / "verification/implementation/EP-03/toy-volume-pool.tsv"
EVIDENCE_ROOT = PROJECT_ROOT / "verification/implementation/EP-03/live-toy"
CASES = ("pass", "fail", "timeout", "cancel", "hostile", "fork",
         "memory", "log-volume", "disk-full")
EXPECTED = {"pass": "PASSED", "fail": "FAILED", "timeout": "TIMED_OUT",
            "cancel": "CANCELLED", "hostile": "PASSED", "fork": "PASSED",
            "memory": "PASSED", "log-volume": "PASSED", "disk-full": "UNKNOWN"}
TOY_ARGV = ("python", "-B", "/src/toy.py")


def _canonical_uuid(value: str) -> str:
    try:
        parsed = str(UUID(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("request ID must be a canonical UUID") from error
    if parsed != value:
        raise ValueError("request ID must be a canonical UUID")
    return value


def _pool_ids(path: Path) -> set[str]:
    if path != POOL or path.is_symlink() or not path.is_file():
        raise ValueError("toy volume pool must be the checked-in EP-03 plan")
    requests: set[str] = set()
    all_ids: set[str] = set()
    for line in path.read_text(encoding="ascii").splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 2:
            raise ValueError("invalid toy volume plan row")
        request_id, preparation_id = (_canonical_uuid(item) for item in fields)
        if str(uuid5(UUID(request_id), "preparation")) != preparation_id:
            raise ValueError("toy preparation ID does not match its request")
        if request_id in all_ids or preparation_id in all_ids:
            raise ValueError("duplicate toy volume ID")
        requests.add(request_id)
        all_ids.update((request_id, preparation_id))
    if len(requests) != 12:
        raise ValueError("toy volume plan count changed")
    return requests


def _write_json(path: Path, value: dict[str, Any]) -> None:
    raw = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False,
                      allow_nan=False) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _git(worktree: Path, *args: str) -> str:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(worktree.parent),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "LANG": "C.UTF-8"}
    completed = subprocess.run(
        ["git", "-C", str(worktree), *args], stdin=subprocess.DEVNULL,
        capture_output=True, env=env, timeout=15,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError("generated toy Git operation failed")
    return completed.stdout.decode("ascii", "strict").strip()


def _toy_source(case: str, *, pids_limit: int = 32, memory_mb: int = 128,
                output_budget_mb: int = 8, sibling_path: str = "") -> bytes:
    """Return generated, bounded Python with no host dependencies or secrets."""
    header = (
        "from pathlib import Path\n"
        "import sys\n"
        "import time\n"
        f"CASE = {case!r}\n"
        "print('EP-03 disposable toy ' + CASE, flush=True)\n"
    )
    if case in ("pass", "fail", "timeout", "cancel"):
        body = (
            "if CASE in ('timeout', 'cancel'):\n"
            "    time.sleep(60)\n"
            "elif CASE == 'fail':\n"
            "    Path('/out/junit.xml').write_text("
            "'<testsuite tests=\"1\" failures=\"1\">'"
            "'<testcase name=\"toy\"><failure/></testcase></testsuite>', "
            "encoding='utf-8')\n"
            "    sys.exit(1)\n"
            "else:\n"
            "    Path('/out/junit.xml').write_text("
            "'<testsuite tests=\"1\" failures=\"0\">'"
            "'<testcase name=\"toy\"/></testsuite>', encoding='utf-8')\n"
        )
    elif case == "hostile":
        if not sibling_path:
            raise ValueError("hostile toy needs a generated sibling sentinel")
        body = dedent(f"""
            import json
            import socket
            checks = {{
                'host_home_absent': not Path('/home/visky').exists(),
                'host_socket_absent': not Path('/run/user/1000/docker.sock').exists(),
                'sibling_workspace_absent': not Path({sibling_path!r}).exists(),
                'only_loopback': all(name == 'lo' for _, name in socket.if_nameindex()),
            }}
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.3)
                checks['external_route_blocked'] = probe.connect_ex(('192.0.2.1', 9)) != 0
            proof = {{'checks': checks, 'all_blocked': all(checks.values())}}
            Path('/out/hostile.json').write_text(json.dumps(proof), encoding='utf-8')
            sys.exit(0 if proof['all_blocked'] else 1)
        """)
    elif case == "fork":
        body = dedent(f"""
            import errno
            import json
            import subprocess
            def counter():
                for line in Path('/sys/fs/cgroup/pids.events').read_text().splitlines():
                    key, value = line.split()
                    if key == 'max':
                        return int(value)
                return -1
            expected = {pids_limit}
            configured = Path('/sys/fs/cgroup/pids.max').read_text().strip()
            before = counter()
            children = []
            refused_errno = None
            try:
                for _ in range(expected + 16):
                    try:
                        children.append(subprocess.Popen(
                            ['/bin/sleep', '10'], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                    except OSError as error:
                        refused_errno = error.errno
                        break
                after = counter()
            finally:
                for child in children:
                    child.terminate()
                for child in children:
                    try:
                        child.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=1)
            enforced = (configured == str(expected) and before >= 0
                        and after > before and refused_errno == errno.EAGAIN)
            proof = {{'configured_pids_max': configured, 'expected_pids_max': expected,
                      'spawned': len(children), 'max_events_before': before,
                      'max_events_after': after, 'refused_errno': refused_errno,
                      'limit_enforced': enforced}}
            Path('/out/fork.json').write_text(json.dumps(proof), encoding='utf-8')
            sys.exit(0 if enforced else 1)
        """)
    elif case == "memory":
        worker = (
            "chunks=[]\n"
            "for _ in range(32):\n"
            "    block=bytearray(8*1024*1024)\n"
            "    for i in range(0,len(block),4096): block[i]=1\n"
            "    chunks.append(block)\n"
        )
        body = dedent(f"""
            import json
            import subprocess
            def oom_kills():
                for line in Path('/sys/fs/cgroup/memory.events').read_text().splitlines():
                    key, value = line.split()
                    if key == 'oom_kill':
                        return int(value)
                return -1
            expected_bytes = {memory_mb * 1024 * 1024}
            configured = Path('/sys/fs/cgroup/memory.max').read_text().strip()
            before = oom_kills()
            worker = {worker!r}
            try:
                completed = subprocess.run(
                    [sys.executable, '-B', '-c', worker], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=2.5, check=False)
                child_exit = completed.returncode
            except subprocess.TimeoutExpired:
                child_exit = None
            after = oom_kills()
            enforced = (configured == str(expected_bytes) and before >= 0
                        and after > before and child_exit is not None and child_exit != 0)
            proof = {{'configured_memory_max': configured,
                      'expected_memory_max': expected_bytes,
                      'oom_kills_before': before, 'oom_kills_after': after,
                      'child_exit_code': child_exit, 'limit_enforced': enforced}}
            Path('/out/memory.json').write_text(json.dumps(proof), encoding='utf-8')
            sys.exit(0 if enforced else 1)
        """)
    elif case == "log-volume":
        body = dedent(f"""
            block = b'L' * 65536
            for _ in range(({output_budget_mb} + 2) * 16):
                sys.stdout.buffer.write(block)
            sys.stdout.buffer.flush()
        """)
    elif case == "disk-full":
        body = dedent("""
            import errno
            import os
            fd = os.open('/out/fill.bin', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            chunk = b'D' * (1024 * 1024)
            try:
                for _ in range(80):
                    offset = 0
                    while offset < len(chunk):
                        try:
                            written = os.write(fd, chunk[offset:])
                        except OSError as error:
                            if error.errno != errno.ENOSPC:
                                raise
                            print('EP-03 toy observed ENOSPC', flush=True)
                            time.sleep(2)
                            sys.exit(0)
                        if written <= 0:
                            raise RuntimeError('disk write made no progress')
                        offset += written
            finally:
                os.close(fd)
            sys.exit(1)
        """)
    else:
        raise ValueError("unknown generated toy case")
    return (header + body).encode()


def _snapshot(temp_root: Path, run_id: str, case: str,
              profile: ApprovedProfile | None = None) -> SnapshotInput:
    sibling_path = ""
    if case == "hostile":
        sibling = temp_root / "other-workspace"
        sibling.mkdir(mode=0o700)
        marker = sibling / "marker.txt"
        marker.write_text("generated toy sentinel\n", encoding="utf-8")
        sibling_path = str(marker)
    repo = temp_root / "toy-git"
    repo.mkdir(mode=0o700)
    _git(repo, "init", "-q", "--template=/dev/null")
    content = _toy_source(
        case, pids_limit=profile.pids_limit if profile else 32,
        memory_mb=profile.memory_mb if profile else 128,
        output_budget_mb=profile.output_budget_mb if profile else 8,
        sibling_path=sibling_path,
    )
    (repo / "toy.py").write_bytes(content)
    _git(repo, "add", "--", "toy.py")
    _git(repo, "-c", "commit.gpgSign=false", "-c", "user.name=EP03 Toy",
         "-c", "user.email=toy@local.invalid", "commit", "-q", "-m", "EP03 toy")
    commit = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    sources = temp_root / "sources"
    sources.mkdir(mode=0o700)
    sealed = sources / run_id
    sealed.mkdir(mode=0o700)
    source_file = sealed / "toy.py"
    source_file.write_bytes(content)
    source_file.chmod(0o444)
    sealed.chmod(0o555)
    return SnapshotInput(str(uuid4()), commit, tree, sealed)


def _case_proof(case: str, result: BrokerResult, collection: CollectionResult,
                volume: Path) -> tuple[bool, dict[str, Any]]:
    """Check observed effects, not just a successful container exit."""
    if case in ("pass", "fail"):
        parsed = collection.parses.get("toy-junit")
        return parsed is not None and parsed.status == "PARSED", {}
    if case in ("timeout", "cancel"):
        return result.capture.termination_reason == EXPECTED[case], {}
    if case in ("hostile", "fork", "memory"):
        frozen = collection.files.get("toy-assertion")
        if frozen is None or frozen.stat().st_size > 4096:
            return False, {"assertion": "MISSING_OR_TOO_LARGE"}
        try:
            proof = json.loads(frozen.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, {"assertion": "MALFORMED"}
        if not isinstance(proof, dict):
            return False, {"assertion": "MALFORMED"}
        if case == "hostile":
            checks = proof.get("checks")
            okay = (proof.get("all_blocked") is True and isinstance(checks, dict)
                    and len(checks) == 5 and all(value is True for value in checks.values()))
        elif case == "fork":
            okay = (proof.get("limit_enforced") is True
                    and proof.get("configured_pids_max") == str(proof.get("expected_pids_max"))
                    and type(proof.get("spawned")) is int and proof["spawned"] > 0
                    and type(proof.get("max_events_before")) is int
                    and type(proof.get("max_events_after")) is int
                    and proof["max_events_after"] > proof["max_events_before"])
        else:
            okay = (proof.get("limit_enforced") is True
                    and proof.get("configured_memory_max") == str(proof.get("expected_memory_max"))
                    and type(proof.get("oom_kills_before")) is int
                    and type(proof.get("oom_kills_after")) is int
                    and proof["oom_kills_after"] > proof["oom_kills_before"]
                    and type(proof.get("child_exit_code")) is int
                    and proof["child_exit_code"] != 0)
        return okay, proof
    if case == "log-volume":
        stdout = next(stream for stream in result.capture.streams if stream.name == "stdout")
        proof = {"observed_bytes": stdout.observed_bytes,
                 "retained_bytes": stdout.retained_bytes,
                 "complete": stdout.complete,
                 "truncated_reason": stdout.truncated_reason}
        return (stdout.observed_bytes > stdout.retained_bytes
                and stdout.retained_bytes > 0 and stdout.complete is False
                and stdout.truncated_reason in ("stream_limit", "run_limit")), proof
    if case == "disk-full":
        filesystem = os.statvfs(volume)
        free_bytes = filesystem.f_bavail * filesystem.f_frsize
        fill = volume / "out" / "fill.bin"
        fill_bytes = fill.stat().st_size if fill.is_file() else 0
        proof = {"volume_free_bytes": free_bytes, "fill_bytes": fill_bytes}
        return (result.capture.termination_reason == "RESOURCE_LIMIT"
                and result.capture.capture_fault == "RESOURCE_LIMIT:RUN_VOLUME_FULL"
                and free_bytes < 1024 * 1024 and fill_bytes > 0), proof
    return False, {"assertion": "UNKNOWN_CASE"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--docker-socket", type=Path, required=True)
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--profile-sha256", required=True)
    parser.add_argument("--volume-base", type=Path, required=True)
    parser.add_argument("--volume-pool", type=Path, default=POOL)
    return parser


def run(args: argparse.Namespace) -> int:
    run_id = _canonical_uuid(args.request_id)
    if run_id not in _pool_ids(args.volume_pool):
        raise ValueError("request ID is outside the preprovisioned toy pool")
    expected_socket = Path(f"/run/user/{os.getuid()}/docker.sock")
    if args.docker_socket != expected_socket:
        raise ValueError("explicit socket must be this user's rootless Docker socket")
    if (not args.volume_base.is_absolute() or ".." in args.volume_base.parts
            or args.volume_base.is_symlink()):
        raise ValueError("volume base must be an absolute, non-linked owner path")
    if (not args.profile_dir.is_absolute() or ".." in args.profile_dir.parts
            or args.profile_dir.is_symlink()):
        raise ValueError("approved profile directory must be an absolute owner path")
    registry = ProfileRegistry(args.profile_dir, {args.profile_id: args.profile_sha256})
    profile = registry.require(args.profile_id)
    if profile.argv != TOY_ARGV or profile.timeout_seconds > 10:
        raise ValueError("toy profile must run python -B /src/toy.py with a short timeout")
    volume = args.volume_base / run_id
    _verified_run_volume(volume, run_id, _run_volume_budget(profile))
    evidence_dir = EVIDENCE_ROOT / run_id
    if evidence_dir.exists() or evidence_dir.is_symlink():
        raise ValueError("this toy request ID already has evidence; use a fresh pool row")
    broker = RootlessDockerBroker(registry, runtime_dir=args.docker_socket.parent)
    probe = broker.probe()
    if not probe.available:
        raise BrokerError(probe.code, "explicit user-owned rootless Docker is not ready")
    ensure_private_directory(EVIDENCE_ROOT)
    ensure_private_directory(evidence_dir)
    temp_root = Path(tempfile.mkdtemp(prefix=f"cdr-ep03-toy-{run_id}-", dir="/tmp"))
    temp_root.chmod(0o700)
    intent_recorded = False
    cleanup_recorded = False
    intended: ContainerIntent | None = None
    cancellation = Event()
    cancel_watcher: Thread | None = None
    stop_watcher = Event()
    safe_to_delete_temp = False
    try:
        snapshot = _snapshot(temp_root, run_id, args.case, profile)
        runs = temp_root / "runs"
        runs.mkdir(mode=0o700)
        run_root = runs / run_id
        _write_json(evidence_dir / "setup.json", {
            "schema_version": "1.0", "case": args.case, "request_id": run_id,
            "snapshot_id": snapshot.snapshot_id, "snapshot_commit": snapshot.commit,
            "snapshot_tree": snapshot.tree, "profile_id": profile.profile_id,
            "profile_revision_sha256": profile.revision_sha256,
            "image": profile.image, "volume": str(volume),
            "daemon_id": probe.daemon_id, "server_version": probe.server_version,
            "started_at": datetime.now(timezone.utc).isoformat(),
        })

        def watch_capture_then_cancel() -> None:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not stop_watcher.is_set():
                if (run_root / "capture").is_dir():
                    if not stop_watcher.wait(0.5):
                        cancellation.set()
                    return
                stop_watcher.wait(0.05)

        def record_intent(intent: ContainerIntent) -> None:
            nonlocal intended, intent_recorded, cancel_watcher
            if (intended is not None or intent.run_id != run_id
                    or intent.source_path != str(snapshot.path)
                    or intent.profile_revision_sha256 != profile.revision_sha256
                    or intent.image != profile.image or intent.daemon_id != probe.daemon_id):
                raise ValueError("broker intent changed toy identity")
            _write_json(evidence_dir / "intent.json", asdict(intent))
            intended = intent
            intent_recorded = True
            if args.case == "cancel":
                cancel_watcher = Thread(target=watch_capture_then_cancel, daemon=True)
                cancel_watcher.start()

        def record_cleanup(proof: ContainerReconciliation) -> None:
            nonlocal cleanup_recorded
            if (intended is None or cleanup_recorded or proof.run_id != intended.run_id
                    or proof.daemon_id != intended.daemon_id or proof.state != "REMOVED"
                    or proof.container_id is None):
                raise ValueError("broker cleanup proof changed toy identity")
            _write_json(evidence_dir / "cleanup.json", asdict(proof))
            cleanup_recorded = True

        result = broker.run(
            profile, snapshot, run_root, run_id=run_id, writable_volume=volume,
            cancel=cancellation if args.case == "cancel" else None,
            record_intent=record_intent, record_cleanup=record_cleanup,
        )
        if not intent_recorded or not cleanup_recorded:
            raise RuntimeError("broker returned without durable intent and cleanup proof")
        capture_outputs, _ = _freeze_capture(result.capture, run_root, evidence_dir)
        specs: tuple[OutputSpec, ...]
        if args.case in ("hostile", "fork", "memory"):
            specs = (OutputSpec("toy-assertion", "report", f"{args.case}.json",
                                "json", required=True),)
        elif args.case in ("pass", "fail", "timeout", "cancel"):
            specs = (OutputSpec("toy-junit", "test_result", "junit.xml", "junit-xml"),)
        else:
            specs = ()
        collection = collect_outputs(
            result.output_root, specs, evidence_dir / "collected",
            max_total_bytes=1024 * 1024,
        )
        expected = EXPECTED[args.case]
        case_ok, toy_proof = _case_proof(args.case, result, collection, volume)
        capture_ok = (result.capture.capture_fault is None
                      or args.case == "disk-full"
                      and result.capture.capture_fault == "RESOURCE_LIMIT:RUN_VOLUME_FULL")
        passed = (result.outcome == expected and capture_ok
                  and result.containment.get("verified") is True
                  and result.containment.get("cleanup_verified") is True
                  and collection.complete and case_ok)
        _write_json(evidence_dir / "result.json", {
            "schema_version": "1.0", "case": args.case, "request_id": run_id,
            "expected_outcome": expected, "observed_outcome": result.outcome,
            "exit_code": result.exit_code, "duration_seconds": result.duration_seconds,
            "capture_fault": result.capture.capture_fault,
            "termination_reason": result.capture.termination_reason,
            "capture_outputs": list(capture_outputs),
            "collection_outputs": list(collection.outputs),
            "collection_findings": list(collection.findings),
            "collection_complete": collection.complete,
            "toy_proof": toy_proof,
            "containment": dict(result.containment),
            "intent_recorded": intent_recorded, "cleanup_recorded": cleanup_recorded,
            "smoke_passed": passed,
            "ended_at": datetime.now(timezone.utc).isoformat(),
        })
        safe_to_delete_temp = True
        print(f"{args.case}: {result.outcome}; evidence: {evidence_dir}")
        return 0 if passed else 1
    except Exception as error:  # noqa: BLE001 -- retain a stable failure receipt
        code = getattr(error, "code", type(error).__name__)
        _write_json(evidence_dir / "failure.json", {
            "schema_version": "1.0", "case": args.case, "request_id": run_id,
            "error_code": str(code), "intent_recorded": intent_recorded,
            "cleanup_recorded": cleanup_recorded,
            "retained_temp_root": str(temp_root),
            "ended_at": datetime.now(timezone.utc).isoformat(),
        })
        print(f"{args.case}: {code}; evidence: {evidence_dir}", file=sys.stderr)
        return 1
    finally:
        stop_watcher.set()
        if cancel_watcher is not None:
            cancel_watcher.join(timeout=1)
        if safe_to_delete_temp:
            # Only the private directory created above is removed, and only after
            # durable cleanup proof and captured evidence are present.
            snapshot_path = temp_root / "sources" / run_id
            if snapshot_path.exists():
                snapshot_path.chmod(0o700)
            shutil.rmtree(temp_root)


def main() -> int:
    args = _parser().parse_args()
    try:
        return run(args)
    except (ValueError, ProfileError, BrokerError, OSError) as error:
        code = getattr(error, "code", type(error).__name__)
        print(f"EP-03 toy preflight refused: {code}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
