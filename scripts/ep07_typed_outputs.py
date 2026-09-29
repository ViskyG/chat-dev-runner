#!/usr/bin/env python3
"""One disposable, real rootless run covering the v1 typed-output boundary.

Run with the installed wheel's Python in isolated mode. Generated toy source is
the only code mounted into the container. No service, Drive, or GitHub state is
changed. An uncertain container or output result retains its exact quota volume.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker

from runner_app.executors.broker import (
    ContainerIntent,
    ContainerReconciliation,
    RootlessDockerBroker,
    SnapshotInput,
    _run_volume_budget,
    _verified_run_volume,
)
from runner_app.executors.profiles import ProfileRegistry
from runner_app.executors.run_service import _collection_metadata, _freeze_capture, _specs
from runner_app.executors.volume_provider import (
    HELPER,
    VOLUME_BASE,
    HardQuotaVolumeProvider,
)
from runner_app.outputs.collection import collect_outputs
from runner_app.outputs.inspection import inspect_registered_output
from runner_app.resources import load_schema
from runner_app.storage.paths import ensure_private_directory

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "verification/implementation/EP-07/typed-outputs"
IMAGE = (
    "docker.io/library/python@sha256:"
    "4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111"
)
PROFILE_ID = "ep07-typed-output-proof"
TOY = dedent("""\
    import binascii
    import json
    import struct
    import sys
    import zlib
    from pathlib import Path

    out = Path('/out')
    out.joinpath('app.jsonl').write_text('\\n'.join(json.dumps({
        'timestamp': '2026-09-29T00:00:00Z', 'level': level,
        'service': 'typed-toy', 'event': event, 'message': message,
    }, sort_keys=True) for level, event, message in (
        ('INFO', 'start', 'fixture started'),
        ('ERROR', 'expected', 'synthetic handled error event'),
    )) + '\\n', encoding='utf-8')
    out.joinpath('junit.xml').write_text(
        '<testsuite name="typed-toy" tests="2" failures="0" errors="0" skipped="0">'
        '<testcase classname="toy" name="text" time="0.01"/>'
        '<testcase classname="toy" name="binary" time="0.02"/>'
        '</testsuite>\\n', encoding='utf-8')
    out.joinpath('coverage.xml').write_text(
        '<coverage line-rate="0.75" branch-rate="0.5" lines-covered="3" '
        'lines-valid="4" branches-covered="1" branches-valid="2"/>\\n',
        encoding='utf-8')
    out.joinpath('report.md').write_text(
        '# Typed output fixture\\n\\nOne bounded rootless run.\\n', encoding='utf-8')

    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack(
            '!I', binascii.crc32(kind + data) & 0xffffffff)

    png = (b'\\x89PNG\\r\\n\\x1a\\n'
           + chunk(b'IHDR', struct.pack('!2I5B', 1, 1, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(b'\\x00\\x30\\x60\\x90'))
           + chunk(b'IEND', b''))
    out.joinpath('screenshot.png').write_bytes(png)
    out.joinpath('payload.dat').write_bytes(bytes(range(32)) + b'\\xff\\x00typed-toy')
    print('typed-output fixture complete')
    print('fixture stderr diagnostic', file=sys.stderr)
""").encode("utf-8")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, value: Any) -> None:
    raw = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)


def _git(root: Path, *args: str) -> str:
    command = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(root), "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return command.stdout.decode("ascii").strip()


def _profile() -> bytes:
    outputs = [
        ("app-log", "application_log", "app.jsonl", "application-log/v1"),
        ("junit", "test_result", "junit.xml", "junit-xml"),
        ("coverage", "coverage", "coverage.xml", "cobertura-xml"),
        ("report", "report", "report.md", "markdown"),
        ("image", "screenshot", "screenshot.png", "png"),
        ("binary", "artifact", "payload.dat", "binary"),
    ]
    value = {
        "schema_version": "1.0",
        "profile_id": PROFILE_ID,
        "description": "Disposable EP-07 typed-output acceptance fixture",
        "execution_backend": "rootless_container",
        "image": IMAGE,
        "argv": ["python", "-B", "/src/typed_toy.py"],
        "cwd": "/src",
        "user": "0:0",
        "timeout_seconds": 20,
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
                "id": output_id,
                "kind": kind,
                "path": path,
                "format": format_name,
                "required": True,
                "upload_policy": "local_only",
            }
            for output_id, kind, path, format_name in outputs
        ],
        "approval": "Owner-authorized local EP-07 host acceptance fixture only",
        "installed": True,
        "approved": True,
    }
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def _snapshot(root: Path, run_id: str) -> SnapshotInput:
    git_dir = root / "toy-git"
    git_dir.mkdir(mode=0o700)
    _git(git_dir, "init", "-q", "--template=/dev/null")
    (git_dir / "typed_toy.py").write_bytes(TOY)
    _git(git_dir, "add", "--", "typed_toy.py")
    _git(
        git_dir,
        "-c",
        "commit.gpgSign=false",
        "-c",
        "user.name=EP07 Typed Toy",
        "-c",
        "user.email=toy@local.invalid",
        "commit",
        "-q",
        "-m",
        "EP07 typed toy",
    )
    commit = _git(git_dir, "rev-parse", "HEAD")
    tree = _git(git_dir, "rev-parse", "HEAD^{tree}")
    source = root / "sources" / run_id
    source.mkdir(parents=True, mode=0o700)
    (source / "typed_toy.py").write_bytes(TOY)
    (source / "typed_toy.py").chmod(0o444)
    source.chmod(0o555)
    return SnapshotInput(str(uuid4()), commit, tree, source)


def _validate_outputs(
    outputs: list[dict[str, Any]], files: dict[str, Path], provenance: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    validator = Draft202012Validator(load_schema("output"), format_checker=FormatChecker())
    expected = {
        "stdout-raw",
        "stdout-view",
        "stderr-raw",
        "stderr-view",
        "capture-events",
        "capture-metadata",
        "app-log",
        "junit",
        "coverage",
        "report",
        "image",
        "binary",
        "runner-collection",
    }
    ids = {value["output_id"] for value in outputs}
    if ids != expected or len(outputs) != len(expected) or ids != set(files):
        raise AssertionError("OUTPUT_SET_CHANGED")
    facts: dict[str, Any] = {}
    for item in outputs:
        validator.validate(item)
        output_id = item["output_id"]
        path = files[output_id]
        raw = path.read_bytes()
        if (
            item["size_bytes"] != len(raw)
            or item["sha256"] != hashlib.sha256(raw).hexdigest()
            or item["complete"] is not True
            or item["truncated_reason"] is not None
            or item["publication"] != "local_only"
            or path.stat().st_mode & 0o777 != 0o600
        ):
            raise AssertionError("OUTPUT_HASH_SIZE_OR_COMPLETENESS_CHANGED:" + output_id)
        facts[output_id] = {
            "kind": item["kind"],
            "format": item["format"],
            "media_type": item["media_type"],
            "size_bytes": len(raw),
            "sha256": item["sha256"],
            "complete": True,
            "relative_path": item["relative_path"],
        }
    expected_types = {
        "app-log": ("application_log", "application-log/v1", "application/x-ndjson"),
        "junit": ("test_result", "junit-xml", "application/xml"),
        "coverage": ("coverage", "cobertura-xml", "application/xml"),
        "report": ("report", "markdown", "text/markdown"),
        "image": ("screenshot", "png", "image/png"),
        "binary": ("artifact", "binary", "application/octet-stream"),
    }
    for output_id, triple in expected_types.items():
        if tuple(facts[output_id][key] for key in ("kind", "format", "media_type")) != triple:
            raise AssertionError("OUTPUT_TYPE_CHANGED:" + output_id)
        if provenance[output_id] != {
            "source_relative_path": {
                "app-log": "app.jsonl",
                "junit": "junit.xml",
                "coverage": "coverage.xml",
                "report": "report.md",
                "image": "screenshot.png",
                "binary": "payload.dat",
            }[output_id],
            "profile_output_id": output_id,
            "requested_publication": "local_only",
            **(
                {"image": {"format": "png", "width": 1, "height": 1, "safe_preview": True}}
                if output_id == "image"
                else {}
            ),
        }:
            raise AssertionError("OUTPUT_PROVENANCE_CHANGED:" + output_id)
    return facts


def run() -> int:
    run_id = str(uuid4())
    ensure_private_directory(EVIDENCE)
    evidence = EVIDENCE / run_id
    ensure_private_directory(evidence)
    root = Path(tempfile.mkdtemp(prefix=f"cdr-ep07-typed-{run_id}-", dir="/tmp"))
    root.chmod(0o700)
    volume = VOLUME_BASE / run_id
    allocated = False
    released = False
    intent: ContainerIntent | None = None
    cleanup: ContainerReconciliation | None = None
    try:
        if not HardQuotaVolumeProvider._installed_helper():
            raise RuntimeError("VOLUME_HELPER_UNAVAILABLE")
        profile_dir = root / "profiles"
        profile_dir.mkdir(mode=0o700)
        profile_bytes = _profile()
        (profile_dir / f"{PROFILE_ID}.json").write_bytes(profile_bytes)
        (profile_dir / f"{PROFILE_ID}.json").chmod(0o600)
        profile_sha = hashlib.sha256(profile_bytes).hexdigest()
        registry = ProfileRegistry(profile_dir, {PROFILE_ID: profile_sha})
        profile = registry.require(PROFILE_ID)
        broker = RootlessDockerBroker(registry, runtime_dir=Path(f"/run/user/{os.getuid()}"))
        probe = broker.probe()
        if not probe.available:
            raise RuntimeError("ROOTLESS_DOCKER_UNAVAILABLE:" + probe.code)
        if HardQuotaVolumeProvider._helper("probe") != {
            "status": "READY",
            "base": str(VOLUME_BASE),
            "max_slots": 8,
        }:
            raise RuntimeError("VOLUME_HELPER_UNAVAILABLE")
        snapshot = _snapshot(root, run_id)
        budget = _run_volume_budget(profile)
        _write(
            evidence / "setup.json",
            {
                "schema_version": "1.0",
                "run_id": run_id,
                "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "toy_source_sha256": hashlib.sha256(TOY).hexdigest(),
                "installed_package": str(Path(sys.modules["runner_app"].__file__).resolve()),
                "source_checkout_commit": _git(ROOT, "rev-parse", "HEAD"),
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_commit": snapshot.commit,
                "snapshot_tree": snapshot.tree,
                "profile_id": PROFILE_ID,
                "profile_revision_sha256": profile_sha,
                "image": profile.image,
                "volume": str(volume),
                "quota_bytes": budget,
                "daemon_id": probe.daemon_id,
                "server_version": probe.server_version,
                "helper_sha256": hashlib.sha256(HELPER.read_bytes()).hexdigest(),
                "started_at": _utc(),
            },
        )
        allocated_reply = HardQuotaVolumeProvider._helper(
            "allocate", run_id, str(budget // (1024 * 1024))
        )
        if allocated_reply != {
            "status": "ALLOCATED",
            "run_id": run_id,
            "size_mb": budget // (1024 * 1024),
            "path": str(volume),
        }:
            raise RuntimeError("VOLUME_HELPER_RESPONSE_INVALID")
        allocated = True
        _verified_run_volume(volume, run_id, budget)
        runs = root / "runs"
        runs.mkdir(mode=0o700)

        def record_intent(value: ContainerIntent) -> None:
            nonlocal intent
            if (
                intent is not None
                or value.run_id != run_id
                or value.daemon_id != probe.daemon_id
                or value.profile_revision_sha256 != profile_sha
                or value.source_path != str(snapshot.path)
            ):
                raise RuntimeError("BROKER_INTENT_CHANGED")
            _write(evidence / "intent.json", asdict(value))
            intent = value

        def record_cleanup(value: ContainerReconciliation) -> None:
            nonlocal cleanup
            if (
                intent is None
                or cleanup is not None
                or value.run_id != run_id
                or value.daemon_id != probe.daemon_id
                or value.state != "REMOVED"
                or value.container_id is None
            ):
                raise RuntimeError("BROKER_CLEANUP_CHANGED")
            _write(evidence / "cleanup.json", asdict(value))
            cleanup = value

        result = broker.run(
            profile,
            snapshot,
            runs / run_id,
            run_id=run_id,
            writable_volume=volume,
            record_intent=record_intent,
            record_cleanup=record_cleanup,
        )
        capture, capture_files = _freeze_capture(result.capture, runs / run_id, evidence)
        collected = collect_outputs(
            result.output_root,
            _specs(profile),
            evidence / "collected",
            max_total_bytes=profile.output_budget_mb * 1024 * 1024,
        )
        metadata = _collection_metadata(
            collected,
            result.capture,
            evidence / "collection.json",
            complete=collected.complete,
            findings=collected.findings,
        )
        outputs = [*capture, *collected.outputs, metadata]
        files = {
            **capture_files,
            **collected.files,
            "runner-collection": evidence / "collection.json",
        }
        facts = _validate_outputs(outputs, files, collected.provenance)
        parse = {
            key: {
                "status": value.status,
                "summary": value.summary,
                "findings": list(value.findings),
            }
            for key, value in collected.parses.items()
        }
        if (
            not collected.complete
            or collected.findings != ("report:FORMAT_NOT_REGISTERED",)
            or result.outcome != "PASSED"
            or result.exit_code != 0
            or result.capture.capture_fault is not None
            or intent is None
            or cleanup is None
            or set(parse) != {"app-log", "junit", "coverage", "report"}
            or any(
                parse[key]["status"] != "PARSED" or parse[key]["findings"]
                for key in ("app-log", "junit", "coverage")
            )
            or parse["report"]["status"] != "UNSUPPORTED"
            or parse["report"]["findings"] != ["FORMAT_NOT_REGISTERED"]
            or parse["app-log"]["summary"]
            != {
                "valid_events": 2,
                "invalid_lines": 0,
                "levels": {"INFO": 1, "ERROR": 1},
            }
            or parse["junit"]["summary"]["passed"] != 2
            or parse["coverage"]["summary"]["line_rate"] != 0.75
        ):
            raise AssertionError("REAL_RUN_OR_PARSER_GATE_FAILED")
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
            or inspections["junit"]["summary"]["passed"] != 2
            or inspections["image"]["safe_preview"] is not True
            or inspections["image"]["width"] != 1
            or inspections["image"]["height"] != 1
            or any(
                inspections[key]["status"] != "UNSUPPORTED"
                for key in ("coverage", "report", "binary")
            )
        ):
            raise AssertionError("INSPECTION_GATE_FAILED")
        manifest = {
            "schema_version": "1.0",
            "run_id": run_id,
            "request_id": run_id,
            "project_id": "ep07-typed-proof",
            "workspace_id": "fixture",
            "snapshot": {
                "commit": snapshot.commit,
                "tree": snapshot.tree,
                "source_dirty_at_capture": False,
            },
            "operation_status": "COMPLETED",
            "execution": {
                "outcome": result.outcome,
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
            },
            "delivery": {"status": "NOT_ATTEMPTED", "reason": None},
            "outputs": outputs,
            "created_at": _utc(),
        }
        Draft202012Validator(load_schema("run-manifest"), format_checker=FormatChecker()).validate(
            manifest
        )
        _write(evidence / "run-manifest.json", manifest)
        _write(
            evidence / "result.json",
            {
                "schema_version": "1.0",
                "run_id": run_id,
                "passed": True,
                "outcome": result.outcome,
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
                "containment": dict(result.containment),
                "collection_complete": collected.complete,
                "capture_complete": all(stream.complete for stream in result.capture.streams),
                "outputs": facts,
                "parses": parse,
                "inspections": inspections,
                "provenance": collected.provenance,
                "intent_recorded": True,
                "cleanup_recorded": True,
                "ended_at": _utc(),
            },
        )
        HardQuotaVolumeProvider._clear_verified_mount(volume, run_id, budget)
        released_reply = HardQuotaVolumeProvider._helper("release", run_id)
        released = released_reply == {"status": "RELEASED", "run_id": run_id}
        _write(
            evidence / "retirement.json",
            {"run_id": run_id, "released": released, "ended_at": _utc()},
        )
        if not released:
            return 1
        print(f"Typed-output real gate passed: {evidence}")
        return 0
    except Exception as error:  # noqa: BLE001 -- preserve uncertain mount and stable evidence
        _write(
            evidence / "failure.json",
            {
                "schema_version": "1.0",
                "run_id": run_id,
                "code": str(getattr(error, "code", type(error).__name__)),
                "intent_recorded": intent is not None,
                "cleanup_recorded": cleanup is not None,
                "volume_allocated": allocated,
                "volume_released": released,
                "retained_temp_root": str(root),
                "ended_at": _utc(),
            },
        )
        print(
            f"Typed-output gate failed: {type(error).__name__}; evidence: {evidence}",
            file=sys.stderr,
        )
        return 1
    finally:
        if released:
            source = root / "sources" / run_id
            if source.exists():
                source.chmod(0o700)
            shutil.rmtree(root)


if __name__ == "__main__":
    raise SystemExit(run())
