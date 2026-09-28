"""Offline dependency identity checks; no package hook or project test is run."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from runner_app.executors.broker import BrokerResult, SnapshotInput
from runner_app.executors.preparation_handoff import (
    OfflinePreparationCoordinator,
    attest_prepared_build,
    stage_prepared_build,
)
from runner_app.executors.preparation_plan import (
    PreparationError,
    PreparationGrant,
    execute_preparation,
    plan_preparation,
)
from runner_app.executors.profiles import ProfileRegistry
from runner_app.outputs.capture import CaptureResult, StreamCapture

REFERENCE = Path(__file__).resolve().parents[1] / "config" / "profile.python-quick.json"


def _profiles(root: Path) -> tuple[ProfileRegistry, PreparationGrant, Path]:
    directory = root / "owner-profiles"
    directory.mkdir(mode=0o700)
    reference: dict[str, Any] = json.loads(REFERENCE.read_text(encoding="utf-8"))
    grants: dict[str, str] = {}
    for profile_id, argv in (
        ("python-quick", ["python", "-m", "pytest", "-q"]),
        ("python-prepare", ["python", "-m", "pip", "--version"]),
    ):
        value = dict(reference)
        value.update(profile_id=profile_id, argv=argv, installed=True, approved=True, collect=[])
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        path = directory / f"{profile_id}.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, raw)
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        grants[profile_id] = hashlib.sha256(raw).hexdigest()
    registry = ProfileRegistry(directory, grants)
    grant = PreparationGrant(
        template_id="python-basic", execution_profile_id="python-quick",
        execution_revision_sha256=grants["python-quick"],
        preparation_profile_id="python-prepare",
        preparation_revision_sha256=grants["python-prepare"],
        lockfile_path="requirements.lock",
    )
    return registry, grant, directory


def _snapshot(root: Path, content: bytes = b"sample==1.0 --hash=sha256:abc\n", *,
              symlink: bool = False) -> SnapshotInput:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    if symlink:
        (repo / "actual.lock").write_bytes(content)
        (repo / "requirements.lock").symlink_to("actual.lock")
        subprocess.run(["git", "-C", str(repo), "add", "actual.lock", "requirements.lock"],
                       check=True)
    else:
        (repo / "requirements.lock").write_bytes(content)
        subprocess.run(["git", "-C", str(repo), "add", "requirements.lock"], check=True)
    tree = subprocess.check_output(["git", "-C", str(repo), "write-tree"], text=True).strip()
    snapshot = root / "snapshot"
    snapshot.mkdir(mode=0o700)
    if symlink:
        (snapshot / "actual.lock").write_bytes(content)
        (snapshot / "actual.lock").chmod(0o444)
        (snapshot / "requirements.lock").symlink_to("actual.lock")
    else:
        (snapshot / "requirements.lock").write_bytes(content)
        (snapshot / "requirements.lock").chmod(0o444)
    snapshot.chmod(0o555)
    return SnapshotInput(str(uuid4()), "a" * 40, tree, snapshot)


def _plan(snapshot: SnapshotInput, registry: ProfileRegistry,
          grant: PreparationGrant) -> object:
    return plan_preparation(
        snapshot, template_id="python-basic", execution_profile_id="python-quick",
        grant=grant, profiles=registry,
    )


def _refusal(code: str, call: Any) -> None:
    with pytest.raises(PreparationError) as raised:
        call()
    assert raised.value.code == code


def test_plan_binds_frozen_git_tree_lockfile_and_two_owner_grants(tmp_path: Path) -> None:
    registry, grant, _ = _profiles(tmp_path)
    snapshot = _snapshot(tmp_path)
    plan = _plan(snapshot, registry, grant)
    assert plan.snapshot_id == snapshot.snapshot_id
    assert plan.snapshot_tree == snapshot.tree
    assert plan.lockfile_sha256 == hashlib.sha256(b"sample==1.0 --hash=sha256:abc\n").hexdigest()
    assert plan.preparation_revision_sha256 == grant.preparation_revision_sha256
    assert plan.execution_revision_sha256 == grant.execution_revision_sha256
    assert plan.image == registry.require("python-quick").image
    assert plan.identity_sha256 == _plan(snapshot, registry, grant).identity_sha256
    _refusal("PREPARATION_UNAVAILABLE", lambda: execute_preparation(plan))


def test_remote_selection_cannot_change_local_pairing_or_lockfile_path(tmp_path: Path) -> None:
    registry, grant, _ = _profiles(tmp_path)
    snapshot = _snapshot(tmp_path)
    _refusal("PREPARATION_NOT_GRANTED", lambda: plan_preparation(
        snapshot, template_id="node-basic", execution_profile_id="python-quick",
        grant=grant, profiles=registry,
    ))
    _refusal("PREPARATION_NOT_GRANTED", lambda: plan_preparation(
        snapshot, template_id="python-basic", execution_profile_id="python-prepare",
        grant=grant, profiles=registry,
    ))
    _refusal("PREPARATION_GRANT_INVALID", lambda: _plan(
        snapshot, registry, replace(grant, lockfile_path="../outside.lock"),
    ))


def test_changed_or_unapproved_local_profile_refuses_before_source_read(tmp_path: Path) -> None:
    registry, grant, directory = _profiles(tmp_path)
    snapshot = _snapshot(tmp_path)
    _refusal("PREPARATION_GRANT_CHANGED", lambda: _plan(
        snapshot, registry, replace(grant, preparation_revision_sha256="0" * 64),
    ))
    path = directory / "python-prepare.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["argv"] = ["python", "-m", "pip", "list"]
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    _refusal("PREPARATION_PROFILE_UNAVAILABLE", lambda: _plan(snapshot, registry, grant))


def test_lockfile_must_exist_as_a_regular_frozen_file(tmp_path: Path) -> None:
    registry, grant, _ = _profiles(tmp_path)
    snapshot = _snapshot(tmp_path, symlink=True)
    _refusal("LOCKFILE_UNSAFE", lambda: _plan(snapshot, registry, grant))
    _refusal("LOCKFILE_REQUIRED", lambda: _plan(
        snapshot, registry, replace(grant, lockfile_path="absent.lock"),
    ))


def test_snapshot_mutation_and_oversized_lockfile_are_refused(tmp_path: Path) -> None:
    registry, grant, _ = _profiles(tmp_path)
    snapshot = _snapshot(tmp_path)
    lockfile = snapshot.path / "requirements.lock"
    lockfile.chmod(0o644)
    _refusal("SNAPSHOT_INVALID", lambda: _plan(snapshot, registry, grant))

    other = tmp_path / "other"
    other.mkdir()
    huge = _snapshot(other, b"a" * 1_048_577)
    _refusal("LOCKFILE_UNSAFE", lambda: _plan(huge, registry, grant))


def test_plan_identity_changes_with_lockfile_and_snapshot(tmp_path: Path) -> None:
    registry, grant, _ = _profiles(tmp_path)
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = _plan(_snapshot(first_root, b"a==1 --hash=sha256:abc\n"), registry, grant)
    second = _plan(_snapshot(second_root, b"a==2 --hash=sha256:def\n"), registry, grant)
    assert first.lockfile_sha256 != second.lockfile_sha256
    assert first.snapshot_tree != second.snapshot_tree
    assert first.identity_sha256 != second.identity_sha256


def _prepared_result(plan: Any, root: Path) -> BrokerResult:
    capture_root = root / "capture"
    capture_root.mkdir(mode=0o700)
    streams = tuple(
        StreamCapture(name, capture_root / f"{name}.bin", None, 0, 0,
                      hashlib.sha256(b"").hexdigest(), None, 0, True, None)
        for name in ("stdout", "stderr")
    )
    capture = CaptureResult(
        "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00", 1.0,
        0, None, None, streams, capture_root / "events.jsonl", "0" * 64,
        capture_root / "capture.json", "0" * 64, 0, 0,
    )
    return BrokerResult(
        root.name, plan.snapshot_id, plan.snapshot_commit, plan.snapshot_tree,
        root / "snapshot", plan.preparation_profile_id,
        plan.preparation_revision_sha256, plan.image, "0" * 64,
        ("<approved-profile>", plan.preparation_profile_id),
        "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00", 1.0,
        "PASSED", 0, root / "out", capture,
        {
            "verified": True, "backend": "rootless_docker",
            "image_repo_digest": plan.image, "run_volume_hard_limit_bytes": 64 * 1024 * 1024,
            "daemon_rootless": True, "cleanup_verified": True,
            "network": "none", "read_only_source": True, "read_only_root": True,
            "no_new_privileges": True, "capabilities_dropped": "ALL",
            "mounts_verified_before_start": True,
        },
    )


def _build_roots(root: Path) -> tuple[Path, Path]:
    source = root / str(uuid4()) / "build"
    target = root / str(uuid4()) / "build"
    source.mkdir(mode=0o700, parents=True)
    target.mkdir(mode=0o700, parents=True)
    source.parent.chmod(0o700)
    target.parent.chmod(0o700)
    (source.parent / "out").mkdir(mode=0o700)
    return source, target


def test_offline_handoff_copies_verified_bytes_to_distinct_build_root(tmp_path: Path) -> None:
    registry, grant, _ = _profiles(tmp_path)
    plan = _plan(_snapshot(tmp_path), registry, grant)
    source, target = _build_roots(tmp_path)
    package = source / "sample"
    package.mkdir(mode=0o700)
    (package / "__init__.py").write_bytes(b"VALUE = 7\n")
    (package / "__init__.py").chmod(0o600)
    script = source / "tool"
    script.write_bytes(b"#!/bin/sh\nexit 0\n")
    script.chmod(0o700)
    (source / "empty").mkdir(mode=0o700)
    result = _prepared_result(plan, source.parent)

    staged = OfflinePreparationCoordinator().attest_and_stage(
        plan, result, source, target,
    )
    assert staged.source_build == target
    assert staged.tree_sha256 == attest_prepared_build(plan, result, source).tree_sha256
    assert staged.total_bytes == len(b"VALUE = 7\n") + len(b"#!/bin/sh\nexit 0\n")
    assert (target / "sample" / "__init__.py").read_bytes() == b"VALUE = 7\n"
    assert (target / "tool").stat().st_mode & 0o111
    assert (target / "empty").is_dir()
    _refusal("PREPARATION_MANIFEST_MISMATCH",
             lambda: stage_prepared_build(plan, staged, source))


def test_handoff_traverses_searchable_unreadable_ancestors_without_following_links(
    tmp_path: Path,
) -> None:
    from runner_app.executors.preparation_handoff import _scan

    root_controlled = tmp_path / "root-controlled"
    root_controlled.mkdir(mode=0o711)
    root_controlled.chmod(0o711)
    source, target = _build_roots(root_controlled)
    (source / "module.py").write_bytes(b"VALUE = 7\n")
    (source / "module.py").chmod(0o600)
    registry, grant, _ = _profiles(tmp_path)
    plan = _plan(_snapshot(tmp_path), registry, grant)
    staged = OfflinePreparationCoordinator().attest_and_stage(
        plan, _prepared_result(plan, source.parent), source, target,
    )
    assert staged.total_bytes == len(b"VALUE = 7\n")
    assert (target / "module.py").read_bytes() == b"VALUE = 7\n"

    alias = tmp_path / "root-controlled-alias"
    alias.symlink_to(root_controlled, target_is_directory=True)
    _refusal("PREPARATION_ARTIFACT_UNSAFE",
             lambda: _scan(alias / source.parent.name / "build"))


def test_handoff_refuses_untrusted_or_incomplete_preparation(tmp_path: Path) -> None:
    registry, grant, _ = _profiles(tmp_path)
    plan = _plan(_snapshot(tmp_path), registry, grant)
    source, target = _build_roots(tmp_path)
    (source / "module.py").write_bytes(b"print('safe')\n")
    (source / "module.py").chmod(0o600)
    result = _prepared_result(plan, source.parent)
    _refusal("PREPARATION_RESULT_UNTRUSTED",
             lambda: attest_prepared_build(plan, replace(result, outcome="FAILED"), source))
    _refusal("PREPARATION_RESULT_UNTRUSTED",
             lambda: attest_prepared_build(
                 plan, replace(result, containment={**result.containment,
                                                    "cleanup_verified": False}), source,
             ))
    _refusal("PREPARATION_RESULT_UNTRUSTED",
             lambda: attest_prepared_build(
                 plan, replace(result, capture=replace(result.capture,
                                                       capture_fault="WRITE_FAILED")), source,
             ))
    manifest = attest_prepared_build(plan, result, source)
    _refusal("PREPARATION_MANIFEST_MISMATCH",
             lambda: stage_prepared_build(plan, replace(manifest,
                                                       preparation_run_id=str(uuid4())),
                                          target))
    _refusal("PREPARATION_MANIFEST_MISMATCH",
             lambda: stage_prepared_build(replace(plan, identity_sha256="0" * 64),
                                          manifest, target))
    assert not list(target.iterdir())


def test_handoff_refuses_links_special_files_and_changed_source(tmp_path: Path) -> None:
    registry, grant, _ = _profiles(tmp_path)
    plan = _plan(_snapshot(tmp_path), registry, grant)
    source, target = _build_roots(tmp_path)
    result = _prepared_result(plan, source.parent)
    original = source / "module.py"
    original.write_bytes(b"alpha")
    original.chmod(0o600)
    (source / "alias.py").symlink_to("module.py")
    _refusal("PREPARATION_ARTIFACT_UNSAFE",
             lambda: attest_prepared_build(plan, result, source))
    (source / "alias.py").unlink()
    os.link(original, source / "hardlink.py")
    _refusal("PREPARATION_ARTIFACT_UNSAFE",
             lambda: attest_prepared_build(plan, result, source))
    (source / "hardlink.py").unlink()
    manifest = attest_prepared_build(plan, result, source)
    original.write_bytes(b"bravo")
    _refusal("PREPARATION_ARTIFACT_CHANGED",
             lambda: stage_prepared_build(plan, manifest, target))
    assert not list(target.iterdir())


def test_handoff_refuses_existing_target_and_same_root(tmp_path: Path) -> None:
    registry, grant, _ = _profiles(tmp_path)
    plan = _plan(_snapshot(tmp_path), registry, grant)
    source, target = _build_roots(tmp_path)
    (source / "module.py").write_bytes(b"one")
    (source / "module.py").chmod(0o600)
    manifest = attest_prepared_build(plan, _prepared_result(plan, source.parent), source)
    _refusal("PREPARATION_ARTIFACT_UNSAFE",
             lambda: stage_prepared_build(plan, manifest, source))
    (target / "previous").write_bytes(b"anything")
    _refusal("PREPARATION_TARGET_NOT_EMPTY",
             lambda: stage_prepared_build(plan, manifest, target))


def test_handoff_refuses_unsafe_modes_and_special_entries(tmp_path: Path) -> None:
    registry, grant, _ = _profiles(tmp_path)
    plan = _plan(_snapshot(tmp_path), registry, grant)
    source, _ = _build_roots(tmp_path)
    result = _prepared_result(plan, source.parent)
    file = source / "module.py"
    file.write_bytes(b"one")
    file.chmod(0o666)
    _refusal("PREPARATION_ARTIFACT_UNSAFE",
             lambda: attest_prepared_build(plan, result, source))
    file.chmod(0o600)
    os.mkfifo(source / "pipe", 0o600)
    _refusal("PREPARATION_ARTIFACT_UNSAFE",
             lambda: attest_prepared_build(plan, result, source))
    (source / "pipe").unlink()
    missing = tmp_path / str(uuid4()) / "build"
    missing_result = replace(result, run_id=missing.parent.name,
                             output_root=missing.parent / "out")
    _refusal("PREPARATION_ARTIFACT_UNSAFE",
             lambda: attest_prepared_build(plan, missing_result, missing))
