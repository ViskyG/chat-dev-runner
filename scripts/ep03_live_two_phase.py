#!/usr/bin/env python3
"""One-shot live EP-03 prepared VERIFY on a generated, offline toy project.

The isolated toy Engine uses real Git, SQLite, PreparationCoordinator, RunService
and the explicit user-owned rootless Docker broker. It never registers VERIFY in
the production runtime, touches a cloud provider, or discovers existing projects.
Two distinct, preprovisioned hard-quota mounts and owner-approved profiles are
required. The generated preparation step checks an immutable local lockfile,
stages one module, and the test runs that module from the independent /build.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from runner_app.domain.commands import LocalPolicy, Provenance
from runner_app.engine.service import Engine
from runner_app.executors.broker import (
    BrokerError,
    RootlessDockerBroker,
    _run_volume_budget,
    _verified_run_volume,
)
from runner_app.executors.preparation_coordinator import (
    TrustedPreparationCoordinator,
    preparation_run_id,
)
from runner_app.executors.preparation_plan import PreparationGrant
from runner_app.executors.profiles import ProfileRegistry
from runner_app.executors.run_service import RunService, TrustedPreparation
from runner_app.executors.verify_handler import VerifyHandler
from runner_app.git.argv import run_git
from runner_app.git.registry import SourceRegistry
from runner_app.git.service import SourceService
from runner_app.git.worktrees import create_repository, create_workspace
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger
from runner_app.storage.paths import ensure_private_directory
from runner_app.storage.run_evidence import RunEvidenceStore

POOL = PROJECT_ROOT / "verification/implementation/EP-03/toy-volume-pool.tsv"
EVIDENCE_ROOT = PROJECT_ROOT / "verification/implementation/EP-03/live-two-phase"
PREPARATION_ARGV = ("python", "-B", "/src/prepare.py")
TEST_ARGV = ("python", "-B", "/src/toy.py")
CASES = ("pass", "test-fail", "preparation-fail")
_MODULE = b"VALUE = 7\n"
_LOCK = f"sha256:{hashlib.sha256(_MODULE).hexdigest()}\n".encode("ascii")


def _canonical_uuid(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise ValueError("request ID must be a canonical UUID") from None
    return value


def _pool_ids(path: Path) -> set[str]:
    if path != POOL or path.is_symlink() or not path.is_file():
        raise ValueError("use the checked-in EP-03 toy volume plan")
    requests: set[str] = set()
    all_ids: set[str] = set()
    for line in path.read_text(encoding="ascii").splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 2:
            raise ValueError("invalid toy volume plan row")
        request_id, prep_id = (_canonical_uuid(field) for field in fields)
        if prep_id != str(uuid5(UUID(request_id), "preparation")):
            raise ValueError("preparation ID differs from deterministic plan")
        if request_id in all_ids or prep_id in all_ids:
            raise ValueError("duplicate toy volume ID")
        requests.add(request_id)
        all_ids.update((request_id, prep_id))
    if len(requests) != 12:
        raise ValueError("toy volume plan count changed")
    return requests


def _private_absolute(path: Path, description: str) -> Path:
    if not path.is_absolute() or ".." in path.parts or path.is_symlink():
        raise ValueError(f"{description} must be an absolute owner path")
    return path


def _write_json(path: Path, value: dict[str, Any]) -> None:
    raw = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False,
                      allow_nan=False) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
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


def _source_files(case: str) -> dict[str, bytes]:
    prepare = (
        "from pathlib import Path\n"
        "import hashlib\n"
        "import os\n"
        "import sys\n"
        "blob = Path('/src/vendor/module.py').read_bytes()\n"
        "locked = Path('/src/requirements.lock').read_text(encoding='ascii').strip()\n"
        "assert locked == 'sha256:' + hashlib.sha256(blob).hexdigest()\n"
        "assert blob == b'VALUE = 7\\n'\n"
        f"FAIL = {case == 'preparation-fail'!r}\n"
        "if FAIL:\n"
        "    print('intentional toy preparation failure', flush=True)\n"
        "    sys.exit(3)\n"
        "destination = Path('/build/module.py')\n"
        "destination.write_bytes(blob)\n"
        "os.chmod(destination, 0o600)\n"
        "print('prepared locked module', flush=True)\n"
    ).encode()
    toy = (
        "from pathlib import Path\n"
        "import runpy\n"
        "import sys\n"
        "module = runpy.run_path('/build/module.py')\n"
        "assert module['VALUE'] == 7\n"
        "print('staged value 7', flush=True)\n"
        f"FAIL = {case == 'test-fail'!r}\n"
        "if FAIL:\n"
        "    Path('/out/junit.xml').write_text("
        "'<testsuite tests=\"1\" failures=\"1\">'"
        "'<testcase name=\"staged-build\"><failure/></testcase></testsuite>', "
        "encoding='utf-8')\n"
        "    sys.exit(1)\n"
        "Path('/out/junit.xml').write_text("
        "'<testsuite tests=\"1\" failures=\"0\">'"
        "'<testcase name=\"staged-build\"/></testsuite>', encoding='utf-8')\n"
    ).encode()
    return {
        "prepare.py": prepare,
        "toy.py": toy,
        "requirements.lock": _LOCK,
        "vendor/module.py": _MODULE,
    }


def _create_toy_source(managed: Path, case: str) -> tuple[Path, Path, str]:
    repo = create_repository(managed, "ep03-toy")
    initial = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()
    workspace = create_workspace(managed, "ep03-toy", repo, "toy", "feature/toy", initial)
    for relative, content in _source_files(case).items():
        destination = workspace / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_bytes(content)
    run_git(workspace, ["add", "--", "prepare.py", "toy.py", "requirements.lock",
                        "vendor/module.py"])
    run_git(workspace, ["-c", "commit.gpgSign=false", "-c", "user.name=EP03 Toy",
                        "-c", "user.email=toy@local.invalid", "commit", "-m",
                        "Generated prepared toy"])
    return repo, workspace, run_git(workspace, ["rev-parse", "HEAD"]).strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--docker-socket", type=Path, required=True)
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--preparation-profile-id", required=True)
    parser.add_argument("--preparation-profile-sha256", required=True)
    parser.add_argument("--test-profile-id", required=True)
    parser.add_argument("--test-profile-sha256", required=True)
    parser.add_argument("--volume-base", type=Path, required=True)
    parser.add_argument("--volume-pool", type=Path, default=POOL)
    return parser


def run(args: argparse.Namespace) -> int:
    request_id = _canonical_uuid(args.request_id)
    if request_id not in _pool_ids(args.volume_pool):
        raise ValueError("request ID is outside the preprovisioned toy pool")
    expected_socket = Path(f"/run/user/{os.getuid()}/docker.sock")
    if args.docker_socket != expected_socket:
        raise ValueError("Docker socket must be this user's explicit rootless socket")
    _private_absolute(args.profile_dir, "Profile directory")
    _private_absolute(args.volume_base, "Volume base")
    if args.preparation_profile_id == args.test_profile_id:
        raise ValueError("preparation and test profiles must differ")
    profile_registry = ProfileRegistry(args.profile_dir, {
        args.preparation_profile_id: args.preparation_profile_sha256,
        args.test_profile_id: args.test_profile_sha256,
    })
    prep_profile = profile_registry.require(args.preparation_profile_id)
    test_profile = profile_registry.require(args.test_profile_id)
    if (prep_profile.argv != PREPARATION_ARGV or test_profile.argv != TEST_ARGV
            or prep_profile.timeout_seconds > 10 or test_profile.timeout_seconds > 10
            or prep_profile.image != test_profile.image
            or prep_profile.platform != test_profile.platform):
        raise ValueError("approved toy profiles do not match the prepared run")
    prep_id = preparation_run_id(request_id)
    prep_volume = args.volume_base / prep_id
    test_volume = args.volume_base / request_id
    _verified_run_volume(prep_volume, prep_id, _run_volume_budget(prep_profile))
    _verified_run_volume(test_volume, request_id, _run_volume_budget(test_profile))
    if prep_volume.stat().st_dev == test_volume.stat().st_dev:
        raise ValueError("preparation and test require separate quota filesystems")
    evidence_dir = EVIDENCE_ROOT / request_id
    if evidence_dir.exists() or evidence_dir.is_symlink():
        raise ValueError("this request ID already has evidence; use a fresh pool row")
    broker = RootlessDockerBroker(profile_registry, runtime_dir=args.docker_socket.parent)
    probe = broker.probe()
    if not probe.available:
        raise ValueError(f"rootless Docker probe refused: {probe.code}")

    ensure_private_directory(EVIDENCE_ROOT)
    ensure_private_directory(evidence_dir)
    temp_root = Path(tempfile.mkdtemp(prefix=f"cdr-ep03-two-phase-{request_id}-", dir="/tmp"))
    temp_root.chmod(0o700)
    engine: Engine | None = None
    try:
        managed = temp_root / "managed"
        repo, workspace, head = _create_toy_source(managed, args.case)
        state = evidence_dir / "state"
        ensure_private_directory(state)
        database = Database(state / "toy.sqlite")
        registry = SourceRegistry(database, managed)
        registry.register_project("ep03-toy", "EP03 generated toy", repo, origin_kind="new")
        registry.register_workspace("ep03-toy", "toy", "Toy", workspace,
                                    branch="feature/toy", head=head)
        ledger = Ledger(database)
        ledger.startup()
        ledger.arm()  # Only this separate, disposable SQLite runner is armed.
        run_evidence = RunEvidenceStore(database, registry, evidence_dir / "test-evidence")
        sources_root = temp_root / "sealed-sources"
        runs_root = temp_root / "run-slots"
        service = RunService(ledger, registry, profile_registry, broker, run_evidence,
                             sources_root=sources_root, runs_root=runs_root)
        coordinator = TrustedPreparationCoordinator(
            ledger, profile_registry, broker,
            evidence_root=evidence_dir / "preparation-evidence",
        )
        grant = PreparationGrant(
            template_id="ep03-toy", execution_profile_id=test_profile.profile_id,
            execution_revision_sha256=test_profile.revision_sha256,
            preparation_profile_id=prep_profile.profile_id,
            preparation_revision_sha256=prep_profile.revision_sha256,
            lockfile_path="requirements.lock",
        )

        expected_request_id = request_id

        def preparation_for(request_id: str, profile_id: str) -> TrustedPreparation | None:
            if request_id != expected_request_id or profile_id != test_profile.profile_id:
                raise ValueError("toy preparation request/profile mismatch")
            return TrustedPreparation(coordinator, grant, prep_volume, runs_root / prep_id)

        handler = VerifyHandler(
            SourceService(ledger, registry), service,
            volume_for_run=lambda run_id: test_volume if run_id == request_id else None,
            preparation_for_run=preparation_for,
        )
        policy = LocalPolicy(
            project_ids=frozenset({"ep03-toy"}),
            workspace_pairs=frozenset({("ep03-toy", "toy")}),
            approved_profiles=frozenset({test_profile.profile_id}),
        )
        engine = Engine(ledger, policy=policy, handlers={"VERIFY": handler})
        now = datetime.now(timezone.utc)
        command = {
            "schema_version": "1.0", "request_id": request_id,
            "dispatch": "READY", "runner_id": ledger.state()["runner_id"],
            "epoch": ledger.state()["epoch"], "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
            "action": "VERIFY", "project_id": "ep03-toy", "workspace_id": "toy",
            "expected_head": head, "params": {"profile_id": test_profile.profile_id},
            "intent": "Verify one generated, offline prepared toy",
        }
        _write_json(evidence_dir / "setup.json", {
            "schema_version": "1.0", "case": args.case, "request_id": request_id,
            "preparation_run_id": prep_id, "source_head": head,
            "source_tree": run_git(workspace, ["rev-parse", "HEAD^{tree}"]).strip(),
            "preparation_profile_id": prep_profile.profile_id,
            "preparation_profile_sha256": prep_profile.revision_sha256,
            "test_profile_id": test_profile.profile_id,
            "test_profile_sha256": test_profile.revision_sha256,
            "image": test_profile.image, "daemon_id": probe.daemon_id,
            "server_version": probe.server_version,
            "preparation_volume": str(prep_volume), "test_volume": str(test_volume),
            "temp_root": str(temp_root), "started_at": now.isoformat(),
        })
        admitted = engine.submit(json.dumps(command).encode("utf-8"),
                                 Provenance(kind="local", source_id="ep03-toy-owner"))
        if not admitted.execute:
            raise RuntimeError("generated toy VERIFY was not admitted")
        deadline = time.monotonic() + 90
        operation: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            operation = ledger.get(request_id)
            if operation is not None and operation["status"] != "RUNNING":
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("generated toy VERIFY did not finish before watchdog")
        assert operation is not None
        preparation = ledger.get_preparation_result(request_id)
        manifest = ledger.get_run(request_id)
        connection = database.connect()
        try:
            attempts = [list(row) for row in connection.execute(
                "SELECT phase,run_id FROM run_attempts WHERE request_id=? ORDER BY phase",
                (request_id,),
            ).fetchall()]
            handoff = connection.execute(
                "SELECT preparation_run_id,test_run_id,build_tree_sha256 "
                "FROM preparation_handoffs WHERE request_id=?", (request_id,),
            ).fetchone()
        finally:
            connection.close()
        events = [event for event in ledger.events() if event["request_id"] == request_id]
        intent_phases = [event["detail"]["phase"] for event in events
                         if event["type"] == "CONTAINER_INTENDED"]
        receipt = evidence_dir / "preparation-evidence" / prep_id / "preparation.json"
        receipt_sha256 = hashlib.sha256(receipt.read_bytes()).hexdigest() if receipt.is_file() else None
        staged = test_volume / "build" / "module.py"
        staged_sha256 = hashlib.sha256(staged.read_bytes()).hexdigest() if staged.is_file() else None
        junit = test_volume / "out" / "junit.xml"
        junit_sha256 = hashlib.sha256(junit.read_bytes()).hexdigest() if junit.is_file() else None
        expected_outcome = "FAILED" if args.case == "test-fail" else "PASSED"
        expected_status = "BLOCKED" if args.case == "preparation-fail" else "COMPLETED"
        expected_phases = ["PREPARATION"] if args.case == "preparation-fail" else [
            "PREPARATION", "TEST",
        ]
        passed = (
            operation["status"] == expected_status
            and preparation is not None
            and preparation["outcome"] == ("FAILED" if args.case == "preparation-fail" else "PASSED")
            and preparation["evidence_sha256"] == receipt_sha256
            and attempts == [["PREPARATION", prep_id], ["TEST", request_id]]
            and intent_phases == expected_phases
        )
        if args.case == "preparation-fail":
            passed = (passed and operation["code"] == "PREPARATION_FAILED"
                      and manifest is None and handoff is None and staged_sha256 is None
                      and junit_sha256 is None and not list(test_volume.iterdir()))
        else:
            passed = (passed and operation["code"] is None and manifest is not None
                      and manifest["execution"]["outcome"] == expected_outcome
                      and operation["result"]["execution"]["outcome"] == expected_outcome
                      and staged_sha256 == hashlib.sha256(_MODULE).hexdigest()
                      and handoff is not None and handoff[0] == prep_id
                      and handoff[1] == request_id and len(handoff[2]) == 64
                      and junit_sha256 is not None)
        _write_json(evidence_dir / "result.json", {
            "schema_version": "1.0", "case": args.case, "request_id": request_id,
            "expected_operation_status": expected_status,
            "observed_operation_status": operation["status"],
            "operation_code": operation["code"],
            "expected_test_outcome": (None if args.case == "preparation-fail" else expected_outcome),
            "observed_test_outcome": (manifest["execution"]["outcome"] if manifest else None),
            "preparation_result": preparation,
            "preparation_receipt_sha256": receipt_sha256,
            "test_attempts": attempts, "container_intent_phases": intent_phases,
            "handoff_build_tree_sha256": handoff[2] if handoff else None,
            "staged_module_sha256": staged_sha256,
            "junit_sha256": junit_sha256,
            "test_output_ids": ([item["output_id"] for item in manifest["outputs"]]
                                if manifest else []),
            "live_acceptance_passed": passed,
            "retained_temp_root": str(temp_root),
            "ended_at": datetime.now(timezone.utc).isoformat(),
        })
        print(f"{args.case}: {operation['status']}; evidence: {evidence_dir}")
        return 0 if passed else 1
    except Exception as error:  # noqa: BLE001 -- retain a bounded, stable failure receipt
        code = getattr(error, "code", type(error).__name__)
        _write_json(evidence_dir / "failure.json", {
            "schema_version": "1.0", "case": args.case, "request_id": request_id,
            "error_code": str(code), "retained_temp_root": str(temp_root),
            "ended_at": datetime.now(timezone.utc).isoformat(),
        })
        print(f"{args.case}: {code}; evidence: {evidence_dir}", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.shutdown(timeout=5)


def main() -> int:
    args = _parser().parse_args()
    try:
        return run(args)
    except (ValueError, OSError, BrokerError) as error:
        code = getattr(error, "code", type(error).__name__)
        print(f"EP-03 prepared toy preflight refused: {code}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
