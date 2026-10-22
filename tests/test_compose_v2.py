"""Actual Compose planning/collection with a controlled Docker/quota boundary.

These tests never invoke Docker or the installed mount helper and do not qualify
live Compose. The separate fixture can be used for authorized host qualification.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import pytest

import runner_app.executors.compose_v2 as module
from runner_app.executors.compose_v2 import ComposeTaskBackend, load_compose_profiles
from runner_app.executors.rootless_v2 import RootlessError
from runner_app.git.argv import run_git
from runner_app.git.source_v2 import LocalWorkspace, SourceProject, SourceResolver

IMAGE = "docker.io/library/python@sha256:" + "a" * 64
OTHER_IMAGE = "docker.io/library/python@sha256:" + "b" * 64
IMAGE_ID = "sha256:" + "c" * 64


def private(path: Path, value: Any) -> bytes:
    payload = json.dumps(value).encode()
    path.write_bytes(payload)
    path.chmod(0o600)
    return payload


def owner_profiles(root: Path, compose: bytes, **changes: Any) -> Path:
    directory = root / "profiles"
    directory.mkdir(mode=0o700)
    profile = {
        "schema_version": "cdr-compose-profile/1", "profile_id": "compose-python",
        "image": IMAGE, "platform": "linux/amd64", "compose_file": "compose.json",
        "compose_sha256": hashlib.sha256(compose).hexdigest(), "test_service": "test",
        "ready_services": ["api"], "migration_services": ["migrate"],
        "argv": ["python", "/src/client.py"], "timeout_seconds": 2,
        "readiness_seconds": 2, "migration_seconds": 2, "cleanup_seconds": 1,
        "volume_mb": 16, "memory_mb": 64, "cpu_limit": 0.5, "pids_limit": 32,
        "max_log_bytes": 4096, "mount_policy": "frozen-source-and-run-quota-v1",
        "collect": [{"id": "junit", "kind": "test_result", "path": "junit.xml",
                     "format": "junit-xml", "required": True, "upload_policy": "local_only"}],
        **changes,
    }
    raw = private(directory / "compose-python.json", profile)
    private(directory / "compose-grant.json", {
        "schema_version": "cdr-compose-grant/1",
        "profile_revision_sha256": {"compose-python": hashlib.sha256(raw).hexdigest()}})
    return directory


def source_compose() -> dict[str, Any]:
    return {"services": {
        "api": {"image": IMAGE, "command": ["python", "/src/api.py"], "healthcheck": {
            "test": ["CMD", "python", "-c", "print('healthy')"],
            "interval": "1s", "timeout": "1s", "retries": 2}},
        "migrate": {"image": IMAGE, "command": ["python", "/src/migrate.py"]},
        "test": {"image": IMAGE, "command": ["python", "/src/client.py"]}}}


class Fixture:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, *,
                 source: dict[str, Any] | None = None, profile_changes: dict[str, Any] | None = None) -> None:
        self.root, self.request = root, str(uuid4())
        self.events: list[tuple[str, dict[str, object]]] = []
        self.calls: list[list[str]] = []
        self.mode = "pass"
        self.cancel = Event()
        original = root / "original"
        original.mkdir(mode=0o700)
        raw = private(original / "compose.json", source or source_compose())
        self.profile_dir = owner_profiles(root, raw, **(profile_changes or {}))
        run_git(original, ["init", "-b", "tested"])
        run_git(original, ["add", "--all"])
        run_git(original, ["-c", "user.name=Fixture", "-c", "user.email=test@localhost",
                           "commit", "-m", "Fixture"])
        repo = root / "controller.git"
        run_git(root, ["clone", "--bare", "--no-local", str(original), str(repo)])
        self.snapshot = SourceResolver({"demo": SourceProject(
            repo, root / "sources", {"editor": LocalWorkspace(original)})}).resolve_current(
                "demo", self.request, {"kind": "local_workspace", "branch": "tested",
                                       "workspace_id": "editor"})
        self.backend = ComposeTaskBackend(self.profile_dir, root / "runs",
                                          require_running=lambda _request: None,
                                          record_event=self.record)
        self.volume = root / "quota"
        self.containers: dict[str, dict[str, Any]] = {}
        self.network: dict[str, Any] | None = None
        self.plan: dict[str, Any] = {}
        self.released = False
        monkeypatch.setattr(self.backend, "_daemon", self.daemon)
        monkeypatch.setattr(self.backend, "_allocate", self.allocate)
        monkeypatch.setattr(self.backend, "_release", self.release)
        monkeypatch.setattr(self.backend, "_command", self.command)
        monkeypatch.setattr(module, "_verified_retired_volume", lambda path, *_args: path)

    def record(self, _request: str, event: str, details: dict[str, object]) -> None:
        self.events.append((event, details))
        if self.mode == "journal-failure" and event == "COMPOSE_EVIDENCE_SEALED":
            raise OSError("durable journal unavailable")

    def daemon(self, expected: str | None = None, **_kwargs: Any) -> str:
        if self.mode == "daemon-change" and self.containers:
            raise RootlessError("COMPOSE_DAEMON_CHANGED", cleanup_verified=False)
        assert expected in {None, "daemon"}
        return "daemon"

    def allocate(self, _request: str, profile: Any) -> Path:
        self.volume.mkdir(mode=0o700)
        for name in ("out", "data", *("tmp-" + service for service in profile.services)):
            (self.volume / name).mkdir(mode=0o700)
        return self.volume

    def release(self, _request: str, _profile: Any, volume: Path) -> None:
        assert not self.containers and self.network is None
        assert any(name == "COMPOSE_EVIDENCE_SEALED" for name, _ in self.events)
        if self.mode == "release-failure":
            raise OSError("release unavailable")
        shutil.rmtree(volume)
        self.released = True

    def _create(self) -> None:
        project = self.plan["name"]
        network_id = "d" * 64
        self.network = {"Id": network_id, "Name": project + "_default", "Driver": "bridge",
                        "Internal": True, "Containers": {},
                        "Labels": {**self.plan["networks"]["default"]["labels"],
                                   "com.docker.compose.project": project}}
        for name, service in self.plan["services"].items():
            identifier = hashlib.sha256((project + name).encode()).hexdigest()
            host = {"ReadonlyRootfs": True, "Privileged": False, "CapDrop": ["ALL"],
                    "CapAdd": None, "SecurityOpt": service["security_opt"],
                    "Memory": service["mem_limit"], "NanoCpus": int(service["cpus"] * 1e9),
                    "MemorySwap": service["memswap_limit"],
                    "PidsLimit": service["pids_limit"], "NetworkMode": project + "_default",
                    "PortBindings": {}, "PublishAllPorts": False, "PidMode": "", "IpcMode": "private",
                    "Devices": [], "DeviceRequests": None, "ShmSize": 8 * 1024 * 1024,
                    "RestartPolicy": {"Name": "no"},
                    "LogConfig": {"Type": "json-file", "Config": service["logging"]["options"]}}
            if self.mode == "unsafe-created":
                host["Privileged"] = True
            self.containers[identifier] = {
                "Id": identifier, "Name": f"/{project}-{name}-1", "Image": IMAGE_ID,
                "Config": {"Image": service["image"], "Cmd": service["command"], "Entrypoint": [],
                           "User": "0:0", "WorkingDir": "/src", "Volumes": None,
                           "Healthcheck": {"Test": service["healthcheck"].get("test", ["NONE"])},
                           "Labels": {**service["labels"], "com.docker.compose.service": name,
                                      "com.docker.compose.project": project, "com.docker.compose.oneoff": "False"}},
                "HostConfig": host,
                "Mounts": [{"Source": mount["source"], "Destination": mount["target"],
                            "Type": "bind", "RW": not mount["read_only"]} for mount in service["volumes"]],
                "State": {"Status": "created", "Running": False, "ExitCode": 0},
            }

    def command(self, arguments: list[str], **_kwargs: Any) -> tuple[int, bytes, bytes]:
        self.calls.append(arguments)
        if arguments[:2] == ["compose", "version"]:
            return 0, b"5.5.1\n", b""
        if arguments[:2] == ["image", "inspect"]:
            return 0, json.dumps({"Id": IMAGE_ID, "RepoDigests": [arguments[2]],
                                  "Os": "linux", "Architecture": "amd64", "Config": {}}).encode(), b""
        if arguments[0] == "compose":
            index = arguments.index("-f")
            path, action, rest = Path(arguments[index + 1]), arguments[index + 2], arguments[index + 3:]
            if action == "config":
                return 0, path.read_bytes(), b""
            if action == "create":
                self.plan = json.loads(path.read_bytes())
                self._create()
            elif action == "start":
                for item in self.containers.values():
                    service = item["Config"]["Labels"]["com.docker.compose.service"]
                    if service not in rest:
                        continue
                    if service == "api":
                        item["State"] = {"Status": "running", "Running": True,
                                         "Health": {"Status": "unhealthy" if self.mode == "unhealthy" else "healthy"}}
                    else:
                        failed = (service == "test" and self.mode == "fail"
                                  or service == "migrate" and self.mode == "migration-fail")
                        item["State"] = {"Status": "exited", "Running": False, "ExitCode": int(failed)}
                        if service == "test":
                            if self.mode not in {"missing-output", "cancel", "timeout"}:
                                xml = '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="actual"/></testsuite>'
                                (self.volume / "out/junit.xml").write_text(xml)
                            if self.mode == "cancel":
                                item["State"] = {"Status": "running", "Running": True}
                                self.cancel.set()
                            if self.mode == "timeout":
                                item["State"] = {"Status": "running", "Running": True}
                                (self.volume / "out/partial.json").write_text('{"started":true}')
                            if self.mode == "unsafe-output":
                                (self.volume / "out/junit.xml").unlink()
                                (self.volume / "out/junit.xml").symlink_to(self.root / "profiles/compose-grant.json")
            elif action == "stop":
                for item in self.containers.values():
                    item["State"].update(Running=False, Status="exited")
            elif action == "down":
                assert (self.backend.root / self.request / "backend.json").is_file()
                if self.mode != "cleanup-failure":
                    self.containers = {}
                    self.network = None
            else:
                raise AssertionError(arguments)
            return 0, b"", b""
        kind, action = arguments[:2]
        if action == "ls":
            values = list(self.containers) if kind == "container" else (
                [self.network["Id"]] if kind == "network" and self.network else [])
            return 0, ("\n".join(values) + ("\n" if values else "")).encode(), b""
        if action == "inspect":
            item = self.containers.get(arguments[2]) if kind == "container" else self.network
            if item is None:
                return 1, b"", b"No such object"
            if arguments[-1] == "{{json .State}}":
                item = item["State"]
            return 0, json.dumps(item).encode(), b""
        if action == "logs":
            return (-1000 if self.mode == "truncated-log" else 0), b"retained log\n", b""
        raise AssertionError(arguments)

    def run(self) -> Any:
        return self.backend.run(self.snapshot, request_id=self.request,
                                profile_id="compose-python", cancel=self.cancel)


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    return Fixture(tmp_path, monkeypatch)


def test_finite_health_migration_test_collection_then_exact_cleanup(fixture: Fixture) -> None:
    result = fixture.run()
    assert result.outcome == "PASSED" and result.cleanup_verified and fixture.released
    assert [phase["phase"] for phase in result.provenance["phases"]] == ["readiness", "migration", "test"]
    assert (result.output_root / "junit.xml").read_text().startswith("<testsuite")
    names = [name for name, _ in fixture.events]
    assert names.index("COMPOSE_CREATE_INTENDED") < names.index("COMPOSE_RESOURCES_CREATED")
    assert names.index("COMPOSE_EVIDENCE_SEALED") < names.index("COMPOSE_TEARDOWN_INTENDED")
    assert names.index("COMPOSE_RESOURCES_REMOVED") < names.index("COMPOSE_VOLUME_RELEASED")
    config = json.loads((fixture.backend.root / fixture.request / "resolved.json").read_bytes())
    assert config["networks"]["default"]["internal"] is True
    assert config["name"] == "cdr-compose-" + fixture.request
    assert result.provenance["compose_version"] == "5.5.1"
    assert all("--pull" not in call or "never" in call for call in fixture.calls)


@pytest.mark.parametrize("mode,outcome,started", [
    ("fail", "FAILED", True), ("unhealthy", "NOT_RUN", False),
    ("migration-fail", "NOT_RUN", False), ("cancel", "CANCELLED", True),
    ("timeout", "TIMED_OUT", True),
    ("missing-output", "PASSED", True)])
def test_failure_cancel_and_missing_report_still_collect_and_cleanup(
        fixture: Fixture, mode: str, outcome: str, started: bool) -> None:
    fixture.mode = mode
    result = fixture.run()
    assert result.outcome == outcome and result.cleanup_verified
    assert result.provenance["test_started"] is started
    assert result.stdout_path.read_bytes()
    assert fixture.released


@pytest.mark.parametrize("mode", ["cleanup-failure", "release-failure", "journal-failure", "daemon-change", "unsafe-created"])
def test_uncertainty_retains_lane_and_never_replays(fixture: Fixture, mode: str) -> None:
    fixture.mode = mode
    with pytest.raises(RootlessError) as caught:
        fixture.run()
    assert not caught.value.cleanup_verified and not fixture.released
    fixture.mode = "pass"
    with pytest.raises(RootlessError):
        fixture.run()


@pytest.mark.parametrize("mode", ["unsafe-output", "truncated-log"])
def test_incomplete_evidence_preserves_stopped_resources_and_mount(fixture: Fixture, mode: str) -> None:
    fixture.mode = mode
    result = fixture.run()
    assert not result.cleanup_verified and result.error_code == "COMPOSE_EVIDENCE_INCOMPLETE"
    assert not fixture.released and fixture.containers
    assert all(item["State"]["Running"] is False for item in fixture.containers.values())


def test_profile_file_changed_after_enrollment_is_rejected_before_allocate(fixture: Fixture) -> None:
    (fixture.profile_dir / "compose-python.json").write_bytes(b"{}")
    with pytest.raises(RootlessError, match="COMPOSE_PROFILE_CHANGED"):
        fixture.run()
    assert not fixture.volume.exists() and not fixture.calls


@pytest.mark.parametrize("key,value", [
    ("privileged", True), ("ports", ["8080:8080"]), ("volumes", ["/:/host"]),
    ("network_mode", "host"), ("build", "."), ("post_start", [{"command": "touch /tmp/x"}])])
def test_even_hash_approved_source_cannot_grant_privilege(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: Any) -> None:
    source = source_compose()
    source["services"]["test"][key] = value
    fixture = Fixture(tmp_path, monkeypatch, source=source)
    with pytest.raises(RootlessError):
        fixture.run()
    assert not fixture.volume.exists() and not fixture.calls


def test_approved_multiple_images_are_checked_and_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = source_compose()
    source["services"]["api"]["image"] = OTHER_IMAGE
    fixture = Fixture(tmp_path, monkeypatch, source=source,
                      profile_changes={"allowed_images": [IMAGE, OTHER_IMAGE]})
    result = fixture.run()
    assert result.cleanup_verified and set(result.provenance["image_ids"]) == {IMAGE, OTHER_IMAGE}
    assert len([call for call in fixture.calls if call[:2] == ["image", "inspect"]]) == 2


def test_nonprivate_grant_is_not_authority(fixture: Fixture) -> None:
    (fixture.profile_dir / "compose-grant.json").chmod(0o644)
    with pytest.raises(RootlessError, match="COMPOSE_PROFILE_UNAVAILABLE"):
        load_compose_profiles(fixture.profile_dir)


def test_prespawn_cancel_never_allocates(fixture: Fixture) -> None:
    fixture.cancel.set()
    with pytest.raises(RootlessError, match="CANCELLED_BEFORE_START") as caught:
        fixture.run()
    assert caught.value.cleanup_verified and not fixture.volume.exists()


def test_missing_cached_image_is_precise_preallocation_refusal(fixture: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    original = fixture.command
    def unavailable(arguments: list[str], **kwargs: Any) -> tuple[int, bytes, bytes]:
        if arguments[:2] == ["image", "inspect"]:
            return 1, b"", b"No such image"
        return original(arguments, **kwargs)
    monkeypatch.setattr(fixture.backend, "_command", unavailable)
    with pytest.raises(RootlessError) as caught:
        fixture.run()
    assert caught.value.cleanup_verified and caught.value.code == "COMPOSE_IMAGE_UNAVAILABLE"
    assert not fixture.volume.exists() and not fixture.containers


def test_deadline_and_cancel_rechecked_after_runtime_io(fixture: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    def completed(*_args: str, **_kwargs: Any) -> dict[str, Any]:
        clock[0] = 2.0
        return {"Status": "exited", "Running": False, "ExitCode": 0}
    monkeypatch.setattr(fixture.backend, "_json", completed)
    assert fixture.backend._wait({"test": "a" * 64}, health=False, timeout=1,
                                 cancel=fixture.cancel, daemon="daemon") == ("TIMED_OUT", None)
    clock[0] = 0.0
    def cancelled(*_args: str, **_kwargs: Any) -> dict[str, Any]:
        fixture.cancel.set()
        return {"Status": "exited", "Running": False, "ExitCode": 0}
    monkeypatch.setattr(fixture.backend, "_json", cancelled)
    assert fixture.backend._wait({"test": "a" * 64}, health=False, timeout=1,
                                 cancel=fixture.cancel, daemon="daemon") == ("CANCELLED", None)


def test_lost_start_reply_never_claims_test_not_run(fixture: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    original = fixture.command
    def lost(arguments: list[str], **kwargs: Any) -> tuple[int, bytes, bytes]:
        result = original(arguments, **kwargs)
        if arguments[0] == "compose" and arguments[-2:] == ["start", "test"]:
            raise RootlessError("COMPOSE_CLI_TIMEOUT", cleanup_verified=False)
        return result
    monkeypatch.setattr(fixture.backend, "_command", lost)
    result = fixture.run()
    assert result.outcome == "UNKNOWN" and result.cleanup_verified
    assert result.provenance["test_started"] is None
    assert result.provenance["test_start_intended"] is True
    assert (result.output_root / "junit.xml").is_file()


def test_distinct_requests_never_share_compose_names_or_mounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plans = []
    for name in ("first", "second"):
        root = tmp_path / name
        root.mkdir(mode=0o700)
        fixture = Fixture(root, monkeypatch)
        assert fixture.run().cleanup_verified
        plans.append(fixture.plan)
    assert plans[0]["name"] != plans[1]["name"]
    mounts = [{mount["source"] for service in plan["services"].values() for mount in service["volumes"]}
              for plan in plans]
    assert not mounts[0].intersection(mounts[1])


@pytest.mark.parametrize("when", ["after-stop", "before-down"])
@pytest.mark.parametrize("resource", ["container", "network"])
def test_replaced_resource_is_never_removed_as_original(
        fixture: Fixture, monkeypatch: pytest.MonkeyPatch, when: str, resource: str) -> None:
    original = fixture.backend._owned
    checks = [0]
    def replace(*args: Any, **kwargs: Any) -> Any:
        checks[0] += 1
        # create/readback, before stop, after stop, then before down.
        selected = 3 if when == "after-stop" else 4
        if checks[0] == selected:
            if resource == "container":
                identifier = next(iter(fixture.containers))
                replacement = fixture.containers.pop(identifier)
                replacement["Id"] = "e" * 64
                fixture.containers[replacement["Id"]] = replacement
            else:
                assert fixture.network is not None
                fixture.network["Id"] = "e" * 64
        return original(*args, **kwargs)
    monkeypatch.setattr(fixture.backend, "_owned", replace)
    with pytest.raises(RootlessError, match="COMPOSE_RESOURCE_IDENTITY_MISMATCH") as caught:
        fixture.run()
    assert not caught.value.cleanup_verified and not fixture.released
    assert not any("down" in call for call in fixture.calls)


def test_timeout_partial_opaque_report_does_not_turn_missing_junit_into_cleanup_loss(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = [{"id": "junit", "kind": "test_result", "path": "junit.xml",
                "format": "junit-xml", "required": True, "upload_policy": "local_only"},
               {"id": "partial", "kind": "report", "path": "partial.json",
                "format": "json", "required": False, "upload_policy": "local_only"}]
    fixture = Fixture(tmp_path, monkeypatch, profile_changes={"collect": outputs})
    fixture.mode = "timeout"
    result = fixture.run()
    assert result.outcome == "TIMED_OUT" and result.cleanup_verified and fixture.released
    assert "partial:FORMAT_NOT_REGISTERED" in result.provenance["collection_findings"]
    assert "junit:REQUIRED_OUTPUT_MISSING" in result.provenance["collection_findings"]
    assert (result.output_root / "partial.json").read_text() == '{"started":true}'
    assert not (result.output_root / "junit.xml").exists()
