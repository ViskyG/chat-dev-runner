#!/usr/bin/env python3
"""One disposable rootless run for EP-07 hostile-output acceptance.

Generated toy files exercise the installed broker's unchanged boundary and the
production output collector. No service, Drive, GitHub or real secret is used.
An uncertain run retains its exact volume and receipts for investigation.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner_app.executors.broker import (
    ContainerIntent,
    ContainerReconciliation,
    RootlessDockerBroker,
    SnapshotInput,
    _run_volume_budget,
    _verified_run_volume,
)
from runner_app.executors.profiles import ProfileRegistry
from runner_app.executors.volume_provider import (
    HELPER,
    VOLUME_BASE,
    HardQuotaVolumeProvider,
)
from runner_app.outputs.collection import OutputError, OutputSpec, collect_outputs
from runner_app.outputs.inspection import inspect_registered_output
from runner_app.outputs.redaction import redact_text_export
from runner_app.storage.paths import ensure_private_directory
from scripts.ep03_live_toy import _git, _write_json

EVIDENCE_ROOT = ROOT / "verification/implementation/EP-07/artifact-boundary"
IMAGE = ("docker.io/library/python@sha256:"
         "4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111")
PROFILE_ID = "ep07-artifact-proof"
SYNTHETIC_SECRET = "token=SYNTHETIC_EP07_NOT_A_CREDENTIAL_123456789"
SOURCE = dedent('''\
    from pathlib import Path
    import os

    out = Path('/out')
    (out / 'large.bin').write_bytes(b'L' * (3 * 1024 * 1024 + 7))
    (out / 'junit.xml').write_bytes(
        b'<!DOCTYPE testsuite [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b'<testsuite>&xxe;</testsuite>')
    (out / 'active.html').write_text(
        '<script>fetch("http://127.0.0.1:8765/api/v1/system")</script>',
        encoding='utf-8')
    (out / 'active.svg').write_text('<svg onload="alert(1)"/>', encoding='utf-8')
    (out / 'report.txt').write_text(
        'Synthetic fixture only\\n' + SYNTHETIC_SECRET + '\\n', encoding='utf-8')
    (out / 'unknown.custom').write_text('unknown format', encoding='utf-8')
    (out / 'symlink.txt').symlink_to('/src/synthetic-marker.txt')
    (out / 'hardlink-source.txt').write_text('synthetic marker', encoding='utf-8')
    os.link(out / 'hardlink-source.txt', out / 'hardlink.txt')
    print('generated hostile-output fixture complete', flush=True)
'''.replace("SYNTHETIC_SECRET", repr(SYNTHETIC_SECRET))).encode("utf-8")


def _profile_bytes() -> bytes:
    profile: dict[str, Any] = {
        "schema_version": "1.0", "profile_id": PROFILE_ID,
        "description": "Disposable EP-07 hostile-output fixture",
        "execution_backend": "rootless_container", "image": IMAGE,
        "argv": ["python", "-B", "/src/artifact_toy.py"], "cwd": "/src", "user": "0:0",
        "timeout_seconds": 15, "network": "none", "memory_mb": 128,
        "cpu_limit": 1.0, "pids_limit": 32, "output_budget_mb": 8,
        "capabilities": "drop_all", "no_new_privileges": True,
        "read_only_root": True, "secrets": [], "platform": "linux/amd64",
        "collect": [
            {"id": "large", "kind": "artifact", "path": "large.bin", "format": "binary", "required": True},
            {"id": "junit", "kind": "test_result", "path": "junit.xml", "format": "junit-xml", "required": True},
            {"id": "html", "kind": "report", "path": "active.html", "format": "html", "required": True},
            {"id": "svg", "kind": "screenshot", "path": "active.svg", "format": "svg", "required": True},
            {"id": "report", "kind": "report", "path": "report.txt", "format": "text", "required": True},
            {"id": "unknown", "kind": "test_result", "path": "unknown.custom", "format": "custom-v1", "required": True},
            {"id": "symlink", "kind": "report", "path": "symlink.txt", "format": "text", "required": True},
            {"id": "hardlink", "kind": "report", "path": "hardlink.txt", "format": "text", "required": True},
        ],
        "approval": "Owner-authorized local EP-07 host acceptance fixture only",
        "installed": True, "approved": True,
    }
    return (json.dumps(profile, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _snapshot(root: Path, run_id: str) -> SnapshotInput:
    repo = root / "toy-git"
    repo.mkdir(mode=0o700)
    _git(repo, "init", "-q", "--template=/dev/null")
    (repo / "artifact_toy.py").write_bytes(SOURCE)
    (repo / "synthetic-marker.txt").write_text("synthetic marker\n", encoding="utf-8")
    _git(repo, "add", "--", "artifact_toy.py", "synthetic-marker.txt")
    _git(repo, "-c", "commit.gpgSign=false", "-c", "user.name=EP07 Artifact Toy",
         "-c", "user.email=toy@local.invalid", "commit", "-q", "-m", "EP07 artifact toy")
    commit = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    source = root / "sources" / run_id
    source.mkdir(parents=True, mode=0o700)
    (source / "artifact_toy.py").write_bytes(SOURCE)
    (source / "synthetic-marker.txt").write_text("synthetic marker\n", encoding="utf-8")
    for item in source.iterdir():
        item.chmod(0o444)
    source.chmod(0o555)
    return SnapshotInput(str(uuid4()), commit, tree, source)


def _assertions(collected: Any, output_root: Path, evidence_dir: Path) -> dict[str, Any]:
    outputs = {item["output_id"]: item for item in collected.outputs}
    inspected = {
        output_id: inspect_registered_output(item, collected.files[output_id].read_bytes())
        for output_id, item in outputs.items() if item["size_bytes"] <= 2 * 1024 * 1024
    }
    export = redact_text_export(outputs["report"], collected.files["report"],
                                evidence_dir / "exports", allowlisted=True)
    export_text = export.file.read_text(encoding="utf-8")
    refused: dict[str, str] = {}
    for output_id in ("html", "svg", "large"):
        try:
            redact_text_export(outputs[output_id], collected.files[output_id],
                               evidence_dir / f"exports-{output_id}", allowlisted=True)
        except OutputError as error:
            refused[output_id] = error.code
    source_large = output_root / "large.bin"
    checks = {
        "large_source_intact": source_large.stat().st_size == 3 * 1024 * 1024 + 7,
        "large_frozen_truncated": outputs["large"]["size_bytes"] == 128 * 1024
                                  and outputs["large"]["complete"] is False
                                  and outputs["large"]["truncated_reason"] == "file_limit",
        "large_export_refused": refused.get("large") == "EXPORT_FORMAT_UNSUPPORTED",
        "malformed_xml_unparsed": collected.parses["junit"].status == "UNPARSED"
                                  and "junit:XML_DTD_FORBIDDEN" in collected.findings
                                  and inspected["junit"]["status"] == "UNPARSED",
        "html_export_refused": refused.get("html") == "EXPORT_FORMAT_UNSUPPORTED",
        "svg_preview_refused": inspected["svg"].get("safe_preview") is False
                               and collected.provenance["svg"]["image"]["safe_preview"] is False,
        "svg_export_refused": refused.get("svg") == "EXPORT_FORMAT_UNSUPPORTED",
        "unknown_opaque": outputs["unknown"]["kind"] == "artifact"
                          and inspected["unknown"]["status"] == "UNSUPPORTED",
        "links_refused": "symlink" not in outputs and "hardlink" not in outputs
                         and "symlink:UNSAFE_OR_MISSING_OUTPUT" in collected.findings
                         and "hardlink:UNSAFE_OR_MISSING_OUTPUT" in collected.findings,
        "raw_only_local": all(item["publication"] == "local_only" for item in outputs.values()),
        "synthetic_secret_quarantined": export.quarantined and export.replacements >= 1
                                        and export.output["publication"] == "local_only"
                                        and SYNTHETIC_SECRET not in export_text
                                        and "[REDACTED]" in export_text,
        "separate_export_hash": export.raw_sha256 == outputs["report"]["sha256"]
                                and export.export_sha256 != export.raw_sha256,
    }
    return {"checks": checks, "all_passed": all(checks.values()),
            "findings": list(collected.findings), "export_refusals": refused,
            "inspection": inspected, "outputs": list(collected.outputs),
            "redacted_export": {"output": export.output, "replacements": export.replacements,
                                "quarantined": export.quarantined,
                                "raw_sha256": export.raw_sha256,
                                "export_sha256": export.export_sha256}}


def run() -> int:
    run_id = str(uuid4())
    ensure_private_directory(EVIDENCE_ROOT)
    evidence_dir = EVIDENCE_ROOT / run_id
    ensure_private_directory(evidence_dir)
    root = Path(tempfile.mkdtemp(prefix=f"cdr-ep07-artifacts-{run_id}-", dir="/tmp"))
    root.chmod(0o700)
    volume = VOLUME_BASE / run_id
    allocated = False
    intent: ContainerIntent | None = None
    cleanup: ContainerReconciliation | None = None
    released = False
    try:
        if not HardQuotaVolumeProvider._installed_helper():
            raise RuntimeError("VOLUME_HELPER_UNAVAILABLE")
        profile_dir = root / "profiles"
        profile_dir.mkdir(mode=0o700)
        profile_path = profile_dir / f"{PROFILE_ID}.json"
        fd = os.open(profile_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as file:
                file.write(_profile_bytes())
                file.flush()
                os.fsync(file.fileno())
        finally:
            os.close(fd)
        profile_sha = hashlib.sha256(_profile_bytes()).hexdigest()
        registry = ProfileRegistry(profile_dir, {PROFILE_ID: profile_sha})
        profile = registry.require(PROFILE_ID)
        broker = RootlessDockerBroker(registry, runtime_dir=Path(f"/run/user/{os.getuid()}"))
        probe = broker.probe()
        if not probe.available:
            raise RuntimeError(f"ROOTLESS_UNAVAILABLE:{probe.code}")
        if HardQuotaVolumeProvider._helper("probe") != {
                "status": "READY", "base": str(VOLUME_BASE), "max_slots": 8}:
            raise RuntimeError("VOLUME_HELPER_UNAVAILABLE")
        snapshot = _snapshot(root, run_id)
        budget = _run_volume_budget(profile)
        _write_json(evidence_dir / "setup.json", {
            "schema_version": "1.0", "run_id": run_id,
            "source_commit": _git(ROOT, "rev-parse", "HEAD"),
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "toy_source_sha256": hashlib.sha256(SOURCE).hexdigest(),
            "snapshot_id": snapshot.snapshot_id, "snapshot_commit": snapshot.commit,
            "snapshot_tree": snapshot.tree, "profile_revision_sha256": profile_sha,
            "image": IMAGE, "volume": str(volume), "quota_bytes": budget,
            "daemon_id": probe.daemon_id, "server_version": probe.server_version,
            "helper_sha256": hashlib.sha256(HELPER.read_bytes()).hexdigest(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        })
        size_mb = budget // (1024 * 1024)
        if HardQuotaVolumeProvider._helper("allocate", run_id, str(size_mb)) != {
                "status": "ALLOCATED", "run_id": run_id,
                "size_mb": size_mb, "path": str(volume)}:
            raise RuntimeError("VOLUME_HELPER_RESPONSE_INVALID")
        allocated = True
        _verified_run_volume(volume, run_id, budget)
        run_root = root / "runs" / run_id
        run_root.parent.mkdir(mode=0o700)

        def record_intent(value: ContainerIntent) -> None:
            nonlocal intent
            if (intent is not None or value.run_id != run_id
                    or value.daemon_id != probe.daemon_id
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
        collected = collect_outputs(
            result.output_root,
            tuple(OutputSpec(item.id, item.kind, item.path, item.format, item.required)
                  for item in profile.collect),
            evidence_dir / "collected", max_file_bytes=128 * 1024,
            max_total_bytes=512 * 1024,
        )
        observed = _assertions(collected, result.output_root, evidence_dir)
        containment = result.containment
        passed = (intent is not None and cleanup is not None and observed["all_passed"]
                  and result.outcome == "PASSED" and result.exit_code == 0
                  and result.capture.capture_fault is None
                  and containment.get("verified") is True
                  and containment.get("daemon_rootless") is True
                  and containment.get("network") == "none"
                  and containment.get("read_only_source") is True
                  and containment.get("read_only_root") is True
                  and containment.get("no_new_privileges") is True
                  and containment.get("capabilities_dropped") == "ALL"
                  and containment.get("cleanup_verified") is True)
        _write_json(evidence_dir / "result.json", {
            "schema_version": "1.0", "run_id": run_id, "passed": passed,
            "outcome": result.outcome, "exit_code": result.exit_code,
            "capture_fault": result.capture.capture_fault,
            "duration_seconds": result.duration_seconds,
            "containment": dict(containment), "observed": observed,
            "intent_recorded": intent is not None,
            "cleanup_recorded": cleanup is not None,
            "ended_at": datetime.now(timezone.utc).isoformat(),
        })
        if not passed:
            return 1
        HardQuotaVolumeProvider._clear_verified_mount(volume, run_id, budget)
        released = HardQuotaVolumeProvider._helper("release", run_id) == {
            "status": "RELEASED", "run_id": run_id}
        _write_json(evidence_dir / "retirement.json", {
            "schema_version": "1.0", "run_id": run_id, "released": released,
            "ended_at": datetime.now(timezone.utc).isoformat(),
        })
        print(f"Artifact boundary {'verified' if released else 'release failed'}: {evidence_dir}")
        return 0 if released else 1
    except Exception as error:  # noqa: BLE001 -- retain stable local diagnostic
        _write_json(evidence_dir / "failure.json", {
            "schema_version": "1.0", "run_id": run_id,
            "code": str(getattr(error, "code", type(error).__name__)),
            "intent_recorded": intent is not None,
            "cleanup_recorded": cleanup is not None,
            "volume_allocated": allocated, "volume_released": released,
            "retained_temp_root": str(root),
            "ended_at": datetime.now(timezone.utc).isoformat(),
        })
        print(f"Artifact proof error: {type(error).__name__}; evidence: {evidence_dir}",
              file=sys.stderr)
        return 1
    finally:
        if released:
            source = root / "sources" / run_id
            if source.exists():
                source.chmod(0o700)
            shutil.rmtree(root)


if __name__ == "__main__":
    raise SystemExit(run())
