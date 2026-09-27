"""EP-03 broker refusals and policy checks; no untrusted process runs on host."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import runner_app.executors.broker as broker_module
from runner_app.executors.broker import (
    BrokerError,
    ContainerIntent,
    RootlessDockerBroker,
    RuntimeProbe,
    SnapshotInput,
    _snapshot_tree,
    _verified_run_volume,
)
from runner_app.executors.profiles import ApprovedProfile, ProfileMount


@pytest.fixture
def profile() -> ApprovedProfile:
    return ApprovedProfile(
        profile_id="toy", revision_sha256="a" * 64,
        execution_backend="rootless_container",
        image="docker.io/library/python@sha256:" + "b" * 64,
        argv=("python", "-m", "pytest", "-q"), cwd="/src", user="0:0",
        mounts=(
            ProfileMount("snapshot", "/src", True),
            ProfileMount("build", "/build", False),
            ProfileMount("output", "/out", False),
            ProfileMount("scratch", "/tmp", False),
        ),
        timeout_seconds=30, network="none", memory_mb=256, cpu_limit=1.0,
        pids_limit=32, output_budget_mb=4, collect=(), platform="linux/amd64",
    )


@pytest.fixture
def broker(profile: ApprovedProfile, tmp_path: Path) -> RootlessDockerBroker:
    registry = SimpleNamespace(require=lambda profile_id: profile if profile_id == "toy" else None)
    return RootlessDockerBroker(registry, docker_binary="/usr/bin/docker",
                                runtime_dir=tmp_path / "runtime")  # type: ignore[arg-type]


def _created(profile: ApprovedProfile, snapshot: Path, run_root: Path, name: str,
             nonce: str = "f" * 32) -> dict[str, object]:
    return {
        "Id": "d" * 64, "Name": "/" + name, "Image": "sha256:" + "c" * 64,
        "State": {"Status": "created"},
        "Config": {
            "Image": profile.image, "User": profile.user, "WorkingDir": profile.cwd,
            "Entrypoint": [profile.argv[0]], "Cmd": list(profile.argv[1:]),
            "Healthcheck": {"Test": ["NONE"]},
            "Tty": False, "OpenStdin": False,
            "AttachStdout": True, "AttachStderr": True,
            "Labels": {"cdr.run-id": name.removeprefix("cdr-run-"),
                       "cdr.profile-revision": profile.revision_sha256,
                       "cdr.intent-nonce": nonce},
        },
        "HostConfig": {
            "Privileged": False, "NetworkMode": "none", "ReadonlyRootfs": True,
            "CgroupnsMode": "private", "PidMode": "private", "IpcMode": "private",
            "AutoRemove": False, "Memory": profile.memory_mb * 1024 * 1024,
            "MemorySwap": profile.memory_mb * 1024 * 1024,
            "NanoCpus": int(profile.cpu_limit * 1_000_000_000),
            "PidsLimit": profile.pids_limit, "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"], "RestartPolicy": {"Name": "no"},
            "LogConfig": {"Type": "none"}, "CapAdd": None, "Devices": [],
            "DeviceRequests": [],
        },
        "Mounts": [
            {"Type": "bind", "Destination": dest, "Source": str(src), "RW": writable}
            for dest, src, writable in (
                ("/src", snapshot, False), ("/build", run_root / "build", True),
                ("/out", run_root / "out", True), ("/tmp", run_root / "tmp", True),
            )
        ],
    }


def test_current_host_denies_without_rootless_socket(profile: ApprovedProfile, tmp_path: Path) -> None:
    registry = SimpleNamespace(require=lambda _profile_id: profile)
    broker = RootlessDockerBroker(registry, docker_binary="/usr/bin/docker")  # type: ignore[arg-type]
    probe = broker.probe()
    if broker.socket_path.exists():
        pytest.skip("This host has a configured rootless Docker socket; live test belongs to gate suite")
    assert probe.available is False
    assert probe.code == "ROOTLESS_SOCKET_MISSING"
    run_id = str(uuid.uuid4())
    snapshot = SnapshotInput(str(uuid.uuid4()), "a" * 40, "b" * 40, tmp_path / "snapshot")
    with pytest.raises(BrokerError, match="Rootless container runtime") as error:
        broker.run(profile, snapshot, tmp_path / run_id, run_id=run_id)
    assert error.value.code == "ROOTLESS_SOCKET_MISSING"
    assert not (tmp_path / run_id).exists()


def test_rootful_daemon_is_rejected_even_on_user_owned_socket(
    broker: RootlessDockerBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker.runtime_dir.mkdir(mode=0o700)
    with socket.socket(socket.AF_UNIX) as endpoint:
        endpoint.bind(str(broker.socket_path))
        monkeypatch.setattr(broker, "_docker_json", lambda *_args: {
            "SecurityOptions": ["name=apparmor", "name=cgroupns"],
            "CgroupVersion": "2", "CgroupDriver": "systemd", "OSType": "linux",
        })
        probe = broker.probe()
    assert probe.available is False
    assert probe.code == "ROOTFUL_DAEMON_REFUSED"


def test_unavailable_cpu_controller_is_rejected(
    broker: RootlessDockerBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker.runtime_dir.mkdir(mode=0o700)
    with socket.socket(socket.AF_UNIX) as endpoint:
        endpoint.bind(str(broker.socket_path))
        monkeypatch.setattr(broker, "_docker_json", lambda *_args: {
            "SecurityOptions": ["name=rootless"], "CgroupVersion": "2",
            "CgroupDriver": "systemd", "OSType": "linux",
        })
        probe = broker.probe()
    assert probe.available is False
    assert probe.code == "CGROUP_CONTROLLER_MISSING"
    assert "cpu" not in probe.delegated_controllers


def test_create_flags_and_prestart_inspection_reject_extra_mount(
    profile: ApprovedProfile, broker: RootlessDockerBroker, tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    root = tmp_path / "run"
    root.mkdir()
    for name in ("build", "out", "tmp"):
        (root / name).mkdir()
    argv = broker._create_argv(profile, "sha256:" + "c" * 64, source, root,
                               "cdr-run-test", "f" * 32)
    assert argv.count("--mount") == 4
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert argv.count("--attach") == 2
    assert "--read-only" in argv and "--cap-drop" in argv
    assert "--pull" in argv and argv[argv.index("--pull") + 1] == "never"
    assert "--privileged" not in argv and "--publish" not in argv
    assert "--entrypoint" in argv and argv[argv.index("--entrypoint") + 1] == "python"
    created = _created(profile, source, root, "cdr-run-test")
    broker._validate_created(created, profile, "sha256:" + "c" * 64, source,
                             root, "cdr-run-test", "f" * 32)
    mounts = created["Mounts"]
    assert isinstance(mounts, list)
    mounts.append({"Type": "bind", "Destination": "/home", "Source": "/home", "RW": True})
    with pytest.raises(BrokerError) as error:
        broker._validate_created(created, profile, "sha256:" + "c" * 64, source,
                                 root, "cdr-run-test", "f" * 32)
    assert error.value.code == "CONTAINER_POLICY_MISMATCH"


def test_materialized_tree_matches_git_and_rejects_mutation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "entry.py").write_text("print('safe fixture')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "entry.py"], check=True)
    tree = subprocess.check_output(["git", "-C", str(repo), "write-tree"], text=True).strip()
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    target = frozen / "entry.py"
    target.write_text((repo / "entry.py").read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o444)
    frozen.chmod(0o555)
    assert _snapshot_tree(frozen, tree) == tree
    target.chmod(0o644)
    with pytest.raises(BrokerError) as error:
        _snapshot_tree(frozen, tree)
    assert error.value.code == "MUTABLE_SNAPSHOT"


def test_snapshot_symlink_escape_refused(tmp_path: Path) -> None:
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    (frozen / "escape").symlink_to("/home")
    frozen.chmod(0o555)
    with pytest.raises(BrokerError) as error:
        _snapshot_tree(frozen, "a" * 40)
    assert error.value.code == "UNSAFE_SNAPSHOT"


def test_docker_cli_is_pinned_to_user_socket(broker: RootlessDockerBroker) -> None:
    command = broker._docker_argv("info")
    assert command[:3] == ["/usr/bin/docker", "--host", f"unix://{broker.socket_path}"]
    env = broker._docker_env()
    assert set(env) == {"PATH", "HOME", "DOCKER_CONFIG", "XDG_RUNTIME_DIR", "LANG"}
    assert env["HOME"] != os.environ.get("HOME")


def test_materialized_nested_tree_and_safe_symlink_match_git(tmp_path: Path) -> None:
    repo = tmp_path / "repo-nested"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "main.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (repo / "pkg" / "main.sh").chmod(0o755)
    (repo / "readme").write_text("hello\n", encoding="utf-8")
    (repo / "link").symlink_to("pkg/main.sh")
    subprocess.run(["git", "-C", str(repo), "add", "pkg/main.sh", "readme", "link"],
                   check=True)
    tree = subprocess.check_output(["git", "-C", str(repo), "write-tree"], text=True).strip()
    frozen = tmp_path / "frozen-nested"
    frozen.mkdir()
    (frozen / "pkg").mkdir()
    (frozen / "pkg" / "main.sh").write_bytes((repo / "pkg" / "main.sh").read_bytes())
    (frozen / "pkg" / "main.sh").chmod(0o555)
    (frozen / "readme").write_bytes((repo / "readme").read_bytes())
    (frozen / "readme").chmod(0o444)
    (frozen / "link").symlink_to("pkg/main.sh")
    (frozen / "pkg").chmod(0o555)
    frozen.chmod(0o555)
    assert _snapshot_tree(frozen, tree) == tree


def test_prestart_policy_rejects_false_no_new_privileges(
    profile: ApprovedProfile, broker: RootlessDockerBroker, tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    root = tmp_path / "run"
    root.mkdir()
    created = _created(profile, source, root, "cdr-run-test")
    host = created["HostConfig"]
    assert isinstance(host, dict)
    host["SecurityOpt"] = ["no-new-privileges:false"]
    with pytest.raises(BrokerError) as error:
        broker._validate_created(created, profile, "sha256:" + "c" * 64, source,
                                 root, "cdr-run-test", "f" * 32)
    assert error.value.code == "CONTAINER_POLICY_MISMATCH"


def test_nonzero_container_uid_requires_future_idmapped_mount_support(
    profile: ApprovedProfile,
) -> None:
    from dataclasses import replace

    with pytest.raises(BrokerError) as error:
        RootlessDockerBroker._validate_profile(replace(profile, user="1000:1000"))
    assert error.value.code == "PROFILE_INVALID"


def test_ordinary_directory_cannot_stand_in_for_hard_quota(tmp_path: Path) -> None:
    run_id = str(uuid.uuid4())
    volume = tmp_path / run_id
    volume.mkdir(mode=0o700)
    with pytest.raises(BrokerError) as error:
        _verified_run_volume(volume, run_id, 64 * 1024 * 1024)
    assert error.value.code == "DISK_QUOTA_UNAVAILABLE"


def test_run_refuses_missing_quota_before_image_or_snapshot_access(
    broker: RootlessDockerBroker, profile: ApprovedProfile, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = RuntimeProbe(True, "READY", broker.socket_path, "daemon", "29.1", "2",
                         "systemd", ("cpu", "memory", "pids"))
    monkeypatch.setattr(broker, "probe", lambda: ready)
    monkeypatch.setattr(broker, "_image_id", lambda _profile: pytest.fail("image inspected"))
    run_id = str(uuid.uuid4())
    snapshot = SnapshotInput(str(uuid.uuid4()), "a" * 40, "b" * 40, tmp_path / "missing")
    with pytest.raises(BrokerError) as error:
        broker.run(profile, snapshot, tmp_path / run_id, run_id=run_id)
    assert error.value.code == "DISK_QUOTA_UNAVAILABLE"
    assert not (tmp_path / run_id).exists()


def test_cancel_kill_race_accepts_already_exited_container(
    broker: RootlessDockerBroker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker, "_run_docker", lambda *_args, **_kwargs:
                        subprocess.CompletedProcess([], 1, b"", b"already exited"))
    monkeypatch.setattr(broker, "_docker_json", lambda *_args: {"State": {"Status": "exited"}})
    broker._kill_container("d" * 64, "SIGTERM")


def _fake_create_attempt(
    profile: ApprovedProfile, broker: RootlessDockerBroker, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    create_reply: subprocess.CompletedProcess[bytes] | BrokerError,
    *, inspected: str = "matching", rm_returncode: int = 0,
    intent_error: bool = False,
    cleanup_proofs: list[broker_module.ContainerReconciliation] | None = None,
    post_rm_present: bool = False,
) -> tuple[BrokerError, list[tuple[str, ...]], str]:
    """Exercise the real create/finally flow with a fake rootless Docker CLI."""
    run_id = str(uuid.uuid4())
    name = "cdr-run-" + run_id
    source = tmp_path / "source"
    source.mkdir()
    volume = tmp_path / "volume"
    volume.mkdir()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    run_root = parent / run_id
    snapshot = SnapshotInput(str(uuid.uuid4()), "a" * 40, "b" * 40, source)
    image_id = "sha256:" + "c" * 64
    ready = RuntimeProbe(True, "READY", broker.socket_path, "daemon", "29.1", "2",
                         "systemd", ("cpu", "memory", "pids"))
    monkeypatch.setattr(broker, "probe", lambda: ready)
    monkeypatch.setattr(broker, "_image_id", lambda _profile: image_id)
    monkeypatch.setattr(broker_module, "_verified_run_volume",
                        lambda _volume, _run_id, _budget: volume)
    monkeypatch.setattr(broker_module, "_snapshot_tree", lambda _path, tree: tree)
    monkeypatch.setattr(broker_module.shutil, "disk_usage",
                        lambda _path: SimpleNamespace(free=2**40))
    container = _created(profile, source, volume, name)
    if inspected == "wrong_label":
        config = container["Config"]
        assert isinstance(config, dict)
        labels = config["Labels"]
        assert isinstance(labels, dict)
        labels["cdr.run-id"] = "another-run"
    elif inspected == "wrong_profile_label":
        config = container["Config"]
        assert isinstance(config, dict)
        labels = config["Labels"]
        assert isinstance(labels, dict)
        labels["cdr.profile-revision"] = "f" * 64
    elif inspected == "wrong_name":
        container["Name"] = "/another-container"
    elif inspected == "wrong_image":
        container["Image"] = "sha256:" + "f" * 64
    elif inspected == "wrong_id":
        container["Id"] = "e" * 64
    elif inspected not in ("matching", "missing"):
        raise AssertionError("unknown fake inspection mode")
    calls: list[tuple[str, ...]] = []
    intents: list[ContainerIntent] = []

    def fake_docker(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        if args[:2] == ("container", "create"):
            assert len(intents) == 1
            assert intents[0].run_id == run_id
            assert intents[0].daemon_id == "daemon"
            if isinstance(create_reply, BrokerError):
                raise create_reply
            return create_reply
        if args[:2] == ("container", "inspect"):
            if inspected == "missing":
                return subprocess.CompletedProcess([], 1, b"", b"No such object")
            return subprocess.CompletedProcess([], 0, json.dumps(container).encode(), b"")
        if args[:2] == ("container", "rm"):
            return subprocess.CompletedProcess([], rm_returncode, b"", b"")
        if args[:2] == ("container", "ls"):
            remaining = ("d" * 64 + "\n").encode() if post_rm_present else b""
            return subprocess.CompletedProcess([], 0, remaining, b"")
        pytest.fail(f"unexpected Docker call: {args[:2]}")

    def record_intent(intent: ContainerIntent) -> None:
        intents.append(intent)
        config = container["Config"]
        assert isinstance(config, dict)
        labels = config["Labels"]
        assert isinstance(labels, dict)
        labels["cdr.intent-nonce"] = intent.nonce
        if intent_error:
            raise BrokerError("INTENT_PERSIST_FAILED", "Synthetic journal failure")

    monkeypatch.setattr(broker, "_run_docker", fake_docker)
    with pytest.raises(BrokerError) as failure:
        broker.run(profile, snapshot, run_root, run_id=run_id, writable_volume=volume,
                   record_intent=record_intent,
                   record_cleanup=(cleanup_proofs.append if cleanup_proofs is not None else None))
    return failure.value, calls, name


def test_cleanup_proof_is_emitted_after_exact_removal_on_original_daemon(
    profile: ApprovedProfile, broker: RootlessDockerBroker, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proofs: list[broker_module.ContainerReconciliation] = []
    failure, calls, name = _fake_create_attempt(
        profile, broker, tmp_path, monkeypatch,
        subprocess.CompletedProcess([], 1, b"", b"synthetic create refusal"),
        cleanup_proofs=proofs,
    )
    assert failure.code == "CONTAINER_CREATE_FAILED"
    assert len(proofs) == 1
    assert proofs[0].run_id == name.removeprefix("cdr-run-")
    assert proofs[0].daemon_id == "daemon"
    assert proofs[0].state == "REMOVED"
    assert proofs[0].container_id == "d" * 64
    assert [call[:2] for call in calls][-2:] == [
        ("container", "rm"), ("container", "ls"),
    ]


def test_cleanup_proof_is_withheld_when_container_remains_visible(
    profile: ApprovedProfile, broker: RootlessDockerBroker, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proofs: list[broker_module.ContainerReconciliation] = []
    failure, calls, _ = _fake_create_attempt(
        profile, broker, tmp_path, monkeypatch,
        subprocess.CompletedProcess([], 1, b"", b"synthetic create refusal"),
        cleanup_proofs=proofs, post_rm_present=True,
    )
    assert failure.code == "CONTAINER_CLEANUP_UNCERTAIN"
    assert proofs == []
    assert any(call[:2] == ("container", "ls") for call in calls)


def test_intent_persistence_failure_prevents_docker_create(
    profile: ApprovedProfile, broker: RootlessDockerBroker, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure, calls, _ = _fake_create_attempt(
        profile, broker, tmp_path, monkeypatch,
        subprocess.CompletedProcess([], 0, ("d" * 64).encode(), b""),
        intent_error=True,
    )
    assert failure.code == "INTENT_PERSIST_FAILED"
    assert all(call[:2] != ("container", "create") for call in calls)


@pytest.mark.parametrize(
    ("create_reply", "expected_code"),
    [
        (BrokerError("RUNTIME_UNAVAILABLE", "timed out"), "RUNTIME_UNAVAILABLE"),
        (subprocess.CompletedProcess([], 1, b"", b"failed"), "CONTAINER_CREATE_FAILED"),
        (subprocess.CompletedProcess([], 0, b"invalid-id\n", b""), "INVALID_RUNTIME_RESPONSE"),
        (subprocess.CompletedProcess([], 0, b"\xff", b""), "INVALID_RUNTIME_RESPONSE"),
    ],
)
def test_uncertain_create_cleans_only_verified_named_container(
    profile: ApprovedProfile, broker: RootlessDockerBroker, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    create_reply: subprocess.CompletedProcess[bytes] | BrokerError, expected_code: str,
) -> None:
    failure, calls, name = _fake_create_attempt(
        profile, broker, tmp_path, monkeypatch, create_reply)
    assert failure.code == expected_code
    assert [call[2] for call in calls if call[:2] == ("container", "inspect")] == [name]
    assert [call[-1] for call in calls if call[:2] == ("container", "rm")] == ["d" * 64]
    assert all(call[:2] != ("container", "start") for call in calls)


@pytest.mark.parametrize(
    "inspected", ["missing", "wrong_label", "wrong_profile_label", "wrong_name", "wrong_image"]
)
def test_uncertain_create_never_removes_unverified_container(
    profile: ApprovedProfile, broker: RootlessDockerBroker, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, inspected: str,
) -> None:
    failure, calls, name = _fake_create_attempt(
        profile, broker, tmp_path, monkeypatch,
        subprocess.CompletedProcess([], 1, b"", b"failed"), inspected=inspected)
    assert failure.code == "CONTAINER_CLEANUP_UNCERTAIN"
    assert [call[2] for call in calls if call[:2] == ("container", "inspect")] == [name]
    assert all(call[:2] != ("container", "rm") for call in calls)


def test_uncertain_create_reports_failed_removal(
    profile: ApprovedProfile, broker: RootlessDockerBroker, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure, calls, _ = _fake_create_attempt(
        profile, broker, tmp_path, monkeypatch,
        subprocess.CompletedProcess([], 0, b"invalid-id", b""), rm_returncode=1)
    assert failure.code == "CONTAINER_CLEANUP_UNCERTAIN"
    assert [call[-1] for call in calls if call[:2] == ("container", "rm")] == ["d" * 64]


def test_reported_create_id_must_match_named_container_before_cleanup(
    profile: ApprovedProfile, broker: RootlessDockerBroker, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure, calls, name = _fake_create_attempt(
        profile, broker, tmp_path, monkeypatch,
        subprocess.CompletedProcess([], 0, ("d" * 64).encode(), b""), inspected="wrong_id")
    assert failure.code == "CONTAINER_CLEANUP_UNCERTAIN"
    assert [call[2] for call in calls if call[:2] == ("container", "inspect")] == [
        "d" * 64, name]
    assert all(call[:2] != ("container", "rm") for call in calls)


def _fake_recovery(
    broker: RootlessDockerBroker, profile: ApprovedProfile, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, *, mismatch: str | None = None,
    initially_present: bool = True, daemon_id: str = "daemon",
    record_calls: list[tuple[str, ...]] | None = None,
) -> tuple[broker_module.ContainerReconciliation, list[tuple[str, ...]]]:
    run_id = str(uuid.uuid4())
    intent = ContainerIntent(
        run_id, "daemon", profile.profile_id, profile.revision_sha256,
        profile.image, "sha256:" + "c" * 64, broker_module._sha256_argv(profile.argv),
        "f" * 32,
    )
    ready = RuntimeProbe(True, "READY", broker.socket_path, daemon_id, "29.1", "2",
                         "systemd", ("cpu", "memory", "pids"))
    monkeypatch.setattr(broker, "probe", lambda: ready)
    name = "cdr-run-" + run_id
    owned = _created(profile, tmp_path / "sources" / run_id,
                       tmp_path / "volumes" / run_id, name)
    if mismatch == "label":
        owned["Config"]["Labels"]["cdr.run-id"] = "another-run"  # type: ignore[index]
    elif mismatch == "image":
        owned["Image"] = "sha256:" + "f" * 64
    elif mismatch == "name":
        owned["Name"] = "/renamed-container"
    elif mismatch == "nonce":
        owned["Config"]["Labels"]["cdr.intent-nonce"] = "0" * 32  # type: ignore[index]
    elif mismatch == "mount":
        owned["Mounts"].append({"Type": "bind", "Destination": "/home",  # type: ignore[union-attr]
                                "Source": "/home", "RW": True})
    elif mismatch == "argv":
        owned["Config"]["Cmd"] = ["unexpected"]  # type: ignore[index]
    unrelated = {
        "Id": "e" * 64, "Name": "/unrelated", "Image": "sha256:" + "e" * 64,
        "Config": {"Labels": {"other": "data"}},
    }
    present = initially_present
    calls: list[tuple[str, ...]] = record_calls if record_calls is not None else []

    def fake_docker(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal present
        calls.append(args)
        if args[:2] == ("container", "ls"):
            ids = (["d" * 64] if present else []) + ["e" * 64]
            return subprocess.CompletedProcess([], 0, ("\n".join(ids) + "\n").encode(), b"")
        if args[:2] == ("container", "rm"):
            assert args[-1] == "d" * 64
            present = False
            return subprocess.CompletedProcess([], 0, b"", b"")
        pytest.fail(f"unexpected Docker call: {args[:2]}")

    def fake_json(*args: str) -> dict[str, object]:
        assert args[:2] == ("container", "inspect")
        return owned if args[2] == "d" * 64 else unrelated

    monkeypatch.setattr(broker, "_run_docker", fake_docker)
    monkeypatch.setattr(broker, "_docker_json", fake_json)
    return broker.reconcile_container(intent), calls


def test_recovery_removes_only_exact_owned_container(
    broker: RootlessDockerBroker, profile: ApprovedProfile, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof, calls = _fake_recovery(broker, profile, tmp_path, monkeypatch)
    assert proof.state == "REMOVED"
    assert proof.container_id == "d" * 64
    assert [call[-1] for call in calls if call[:2] == ("container", "rm")] == ["d" * 64]
    assert len([call for call in calls if call[:2] == ("container", "ls")]) == 2


def test_recovery_keeps_lock_when_ambiguous_create_is_not_yet_visible(
    broker: RootlessDockerBroker, profile: ApprovedProfile, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    with pytest.raises(BrokerError) as error:
        _fake_recovery(broker, profile, tmp_path, monkeypatch,
                       initially_present=False, record_calls=calls)
    assert error.value.code == "CONTAINER_RECONCILIATION_UNCERTAIN"
    assert all(call[:2] != ("container", "rm") for call in calls)


@pytest.mark.parametrize("mismatch", ["label", "image", "name", "nonce", "mount", "argv"])
def test_recovery_refuses_identity_collision_without_removal(
    broker: RootlessDockerBroker, profile: ApprovedProfile, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, mismatch: str,
) -> None:
    calls: list[tuple[str, ...]] = []
    with pytest.raises(BrokerError) as error:
        _fake_recovery(broker, profile, tmp_path, monkeypatch,
                       mismatch=mismatch, record_calls=calls)
    assert error.value.code == "CONTAINER_RECONCILIATION_UNCERTAIN"
    assert all(call[:2] != ("container", "rm") for call in calls)


def test_recovery_refuses_changed_daemon_before_inventory(
    broker: RootlessDockerBroker, profile: ApprovedProfile, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(BrokerError) as error:
        _fake_recovery(broker, profile, tmp_path, monkeypatch, daemon_id="different")
    assert error.value.code == "CONTAINER_RECONCILIATION_UNCERTAIN"


def test_preparation_recovery_uses_exact_source_mount_not_phase_run_name(
    broker: RootlessDockerBroker, profile: ApprovedProfile, tmp_path: Path,
) -> None:
    preparation_run_id = str(uuid.uuid4())
    test_run_id = str(uuid.uuid4())
    source = tmp_path / "sources" / test_run_id
    owned = _created(profile, source, tmp_path / "volumes" / preparation_run_id,
                     "cdr-run-" + preparation_run_id)
    intended = ContainerIntent(
        preparation_run_id, "daemon", profile.profile_id, profile.revision_sha256,
        profile.image, "sha256:" + "c" * 64, broker_module._sha256_argv(profile.argv),
        "f" * 32, str(source),
    )
    assert broker._recovery_policy_matches(owned, intended)
    assert not broker._recovery_policy_matches(
        owned, replace(intended, source_path=str(tmp_path / "sources" / str(uuid.uuid4()))),
    )
