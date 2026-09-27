"""EP-03 offline staged-build admission; Docker and project code stay fake-backed."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import runner_app.executors.broker as broker_module
from runner_app.executors.broker import (
    BrokerError,
    ContainerIntent,
    RootlessDockerBroker,
    RuntimeProbe,
    SnapshotInput,
    _verified_run_volume,
)
from runner_app.executors.preparation_handoff import (
    BuildManifest,
    _scan,
    _tree_digest,
    stage_prepared_build,
)
from runner_app.executors.preparation_plan import PreparationPlan
from runner_app.executors.profiles import ApprovedProfile, ProfileMount


@dataclass(frozen=True)
class PreparedCase:
    broker: RootlessDockerBroker
    execution: ApprovedProfile
    snapshot: SnapshotInput
    run_id: str
    volume: Path
    run_root: Path
    plan: PreparationPlan
    manifest: BuildManifest


def _profile(profile_id: str, revision: str, argv: tuple[str, ...]) -> ApprovedProfile:
    return ApprovedProfile(
        profile_id=profile_id, revision_sha256=revision,
        execution_backend="rootless_container",
        image="docker.io/library/python@sha256:" + "b" * 64,
        argv=argv, cwd="/src", user="0:0",
        mounts=(
            ProfileMount("snapshot", "/src", True),
            ProfileMount("build", "/build", False),
            ProfileMount("output", "/out", False),
            ProfileMount("scratch", "/tmp", False),
        ),
        timeout_seconds=30, network="none", memory_mb=256, cpu_limit=1.0,
        pids_limit=32, output_budget_mb=4, collect=(), platform="linux/amd64",
    )


def _rebind(plan: PreparationPlan, **changes: str) -> PreparationPlan:
    fields = asdict(plan)
    fields.pop("identity_sha256")
    fields.update(changes)
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return PreparationPlan(**fields, identity_sha256=hashlib.sha256(encoded).hexdigest())


@pytest.fixture
def prepared(tmp_path: Path) -> PreparedCase:
    execution = _profile("python-quick", "a" * 64, ("python", "-m", "pytest", "-q"))
    preparation = _profile("python-prepare", "c" * 64, ("python", "-m", "pip", "--version"))
    approved = {item.profile_id: item for item in (execution, preparation)}
    broker = RootlessDockerBroker(
        SimpleNamespace(require=approved.get), docker_binary="/usr/bin/docker",
        runtime_dir=tmp_path / "runtime",
    )  # type: ignore[arg-type]
    snapshot_path = tmp_path / "snapshot"
    snapshot_path.mkdir(mode=0o700)
    snapshot = SnapshotInput(str(uuid4()), "d" * 40, "e" * 40, snapshot_path)
    plan = _rebind(PreparationPlan(
        template_id="python-basic", snapshot_id=snapshot.snapshot_id,
        snapshot_commit=snapshot.commit, snapshot_tree=snapshot.tree,
        lockfile_path="requirements.lock", lockfile_sha256="f" * 64,
        execution_profile_id=execution.profile_id,
        execution_revision_sha256=execution.revision_sha256,
        preparation_profile_id=preparation.profile_id,
        preparation_revision_sha256=preparation.revision_sha256,
        image=execution.image, platform=execution.platform, identity_sha256="",
    ))
    run_id = str(uuid4())
    prep_run_id = str(uuid4())
    source = tmp_path / "preparation" / prep_run_id / "build"
    volume = tmp_path / "volumes" / run_id
    target = volume / "build"
    source.mkdir(mode=0o700, parents=True)
    target.mkdir(mode=0o700, parents=True)
    source.parent.chmod(0o700)
    volume.chmod(0o700)
    (source / "module.py").write_bytes(b"VALUE = 7\n")
    (source / "module.py").chmod(0o600)
    entries, total = _scan(source)
    attested = BuildManifest(
        plan.identity_sha256, prep_run_id, preparation.revision_sha256,
        source, entries, total, _tree_digest(entries),
    )
    staged = stage_prepared_build(plan, attested, target)
    run_parent = tmp_path / "runs"
    run_parent.mkdir(mode=0o700)
    return PreparedCase(broker, execution, snapshot, run_id, volume,
                        run_parent / run_id, plan, staged)



def test_only_copied_build_is_allowed_in_preloaded_quota_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = str(uuid4())
    parent = tmp_path / "volumes"
    parent.mkdir(mode=0o700)
    volume = parent / run_id
    volume.mkdir(mode=0o700)
    (volume / "build").mkdir(mode=0o700)
    (volume / "build" / "module.py").write_bytes(b"VALUE = 7\n")
    original_stat = Path.stat

    def fake_stat(path: Path, *args: object, **kwargs: object) -> object:
        if path == parent:
            return SimpleNamespace(st_uid=os.getuid(), st_mode=0o40700,
                                   st_dev=original_stat(parent).st_dev - 1)
        return original_stat(path, *args, **kwargs)

    with monkeypatch.context() as fake:
        fake.setattr(Path, "stat", fake_stat)
        fake.setattr(broker_module.os.path, "ismount", lambda path: Path(path) == volume)
        fake.setattr(broker_module.os, "statvfs", lambda _path: SimpleNamespace(
            f_blocks=16_384, f_bavail=16_384, f_frsize=4_096, f_files=10_000,
        ))
        with pytest.raises(BrokerError) as ordinary:
            _verified_run_volume(volume, run_id, 64 * 1024 * 1024)
        assert ordinary.value.code == "DISK_QUOTA_UNAVAILABLE"
        assert _verified_run_volume(volume, run_id, 64 * 1024 * 1024,
                                    preloaded_build=True) == volume
        (volume / "out").mkdir(mode=0o700)
        with pytest.raises(BrokerError) as extra:
            _verified_run_volume(volume, run_id, 64 * 1024 * 1024,
                                 preloaded_build=True)
        assert extra.value.code == "DISK_QUOTA_UNAVAILABLE"
        (volume / "out").rmdir()
        fake.setattr(broker_module.os.path, "ismount",
                     lambda path: Path(path) in {volume, volume / "build"})
        with pytest.raises(BrokerError) as nested:
            _verified_run_volume(volume, run_id, 64 * 1024 * 1024,
                                 preloaded_build=True)
        assert nested.value.code == "DISK_QUOTA_UNAVAILABLE"


def _fake_preflight(case: PreparedCase, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    ready = RuntimeProbe(True, "READY", case.broker.socket_path, "daemon", "29.1", "2",
                         "systemd", ("cpu", "memory", "pids"))
    monkeypatch.setattr(case.broker, "probe", lambda: ready)
    monkeypatch.setattr(case.broker, "_image_id", lambda _profile: "sha256:" + "c" * 64)
    monkeypatch.setattr(broker_module, "_verified_run_volume",
                        lambda _volume, _run_id, _budget, *, preloaded_build=False:
                        case.volume if preloaded_build else pytest.fail("empty-volume path used"))
    monkeypatch.setattr(broker_module, "_snapshot_tree", lambda _path, tree: tree)
    monkeypatch.setattr(broker_module.shutil, "disk_usage",
                        lambda _path: SimpleNamespace(free=2**40))
    calls: list[tuple[str, ...]] = []

    def fake_docker(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        if args[:2] == ("container", "create"):
            return subprocess.CompletedProcess([], 1, b"", b"synthetic create refusal")
        if args[:2] == ("container", "rm"):
            return subprocess.CompletedProcess([], 0, b"", b"")
        pytest.fail(f"unexpected Docker action: {args[:2]}")

    monkeypatch.setattr(case.broker, "_run_docker", fake_docker)
    monkeypatch.setattr(case.broker, "_created_cleanup_id", lambda *_args: "d" * 64)
    return calls


def test_copied_manifest_is_accepted_and_ordinary_empty_path_stays_default(
    prepared: PreparedCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = prepared
    case.broker._verify_preloaded_build(case.execution, case.snapshot, case.run_id,
                                        case.volume, case.plan, case.manifest)
    calls = _fake_preflight(case, monkeypatch)
    intents: list[ContainerIntent] = []
    with pytest.raises(BrokerError) as error:
        case.broker.run(
            case.execution, case.snapshot, case.run_root, run_id=case.run_id,
            writable_volume=case.volume, record_intent=intents.append,
            preparation_plan=case.plan, staged_build=case.manifest,
        )
    assert error.value.code == "CONTAINER_CREATE_FAILED"
    assert len(intents) == 1
    assert [call[:2] for call in calls].count(("container", "create")) == 1
    assert (case.volume / "build" / "module.py").read_bytes() == b"VALUE = 7\n"
    assert (case.volume / "out").is_dir() and (case.volume / "tmp").is_dir()


@pytest.mark.parametrize("change", ["snapshot", "execution", "preparation", "manifest", "volume"])
def test_identity_mismatch_refuses_before_container_create(
    prepared: PreparedCase, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    case = prepared
    calls = _fake_preflight(case, monkeypatch)
    plan = case.plan
    manifest = case.manifest
    if change == "snapshot":
        plan = _rebind(plan, snapshot_tree="0" * 40)
        manifest = replace(manifest, plan_identity_sha256=plan.identity_sha256)
    elif change == "execution":
        plan = _rebind(plan, execution_revision_sha256="0" * 64)
        manifest = replace(manifest, plan_identity_sha256=plan.identity_sha256)
    elif change == "preparation":
        plan = _rebind(plan, preparation_revision_sha256="0" * 64)
        manifest = replace(manifest, plan_identity_sha256=plan.identity_sha256,
                           preparation_profile_revision_sha256="0" * 64)
    elif change == "manifest":
        manifest = replace(manifest, preparation_run_id=case.run_id)
    else:
        manifest = replace(manifest, source_build=case.volume.parent / "another" / "build")
    with pytest.raises(BrokerError) as error:
        case.broker.run(
            case.execution, case.snapshot, case.run_root, run_id=case.run_id,
            writable_volume=case.volume, record_intent=lambda _intent: None,
            preparation_plan=plan, staged_build=manifest,
        )
    assert error.value.code == "PREPARATION_HANDOFF_INVALID"
    assert not case.run_root.exists()
    assert all(call[:2] != ("container", "create") for call in calls)


@pytest.mark.parametrize("change", ["content", "link", "extra"])
def test_modified_or_unsafe_preloaded_bytes_refuse_before_create(
    prepared: PreparedCase, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    case = prepared
    calls = _fake_preflight(case, monkeypatch)
    build = case.volume / "build"
    if change == "content":
        (build / "module.py").write_bytes(b"VALUE = 8\n")
    elif change == "link":
        (build / "module.py").unlink()
        (build / "module.py").symlink_to("another.py")
    else:
        (build / "extra.py").write_bytes(b"extra\n")
    with pytest.raises(BrokerError) as error:
        case.broker.run(
            case.execution, case.snapshot, case.run_root, run_id=case.run_id,
            writable_volume=case.volume, record_intent=lambda _intent: None,
            preparation_plan=case.plan, staged_build=case.manifest,
        )
    assert error.value.code == "PREPARATION_ARTIFACT_CHANGED"
    assert not case.run_root.exists()
    assert all(call[:2] != ("container", "create") for call in calls)


def test_prepared_build_rechecked_after_create_before_start(
    prepared: PreparedCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = prepared
    calls = _fake_preflight(case, monkeypatch)
    identifier = "d" * 64

    def fake_docker(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        if args[:2] == ("container", "create"):
            return subprocess.CompletedProcess([], 0, identifier.encode(), b"")
        if args[:2] == ("container", "rm"):
            return subprocess.CompletedProcess([], 0, b"", b"")
        pytest.fail(f"unexpected Docker action: {args[:2]}")

    def fake_inspect(*_args: str) -> dict[str, str]:
        (case.volume / "build" / "module.py").write_bytes(b"VALUE = 8\n")
        return {"Id": identifier}

    monkeypatch.setattr(case.broker, "_run_docker", fake_docker)
    monkeypatch.setattr(case.broker, "_docker_json", fake_inspect)
    monkeypatch.setattr(case.broker, "_validate_created", lambda *_args: None)
    with pytest.raises(BrokerError) as error:
        case.broker.run(
            case.execution, case.snapshot, case.run_root, run_id=case.run_id,
            writable_volume=case.volume, record_intent=lambda _intent: None,
            preparation_plan=case.plan, staged_build=case.manifest,
        )
    assert error.value.code == "PREPARATION_ARTIFACT_CHANGED"
    assert [call[:2] for call in calls].count(("container", "create")) == 1
    assert [call[:2] for call in calls].count(("container", "rm")) == 1
    assert all(call[:2] != ("container", "start") for call in calls)
