"""Actual Compose planning/collection with a controlled Docker/quota boundary.

These tests never invoke Docker or the installed mount helper and do not qualify
live Compose. The separate fixture can be used for authorized host qualification.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import runner_app.executors.compose_recovery_v2 as recovery_module
import runner_app.executors.compose_v2 as module
from runner_app.executors.compose_v2 import ComposeTaskBackend, load_compose_profiles
from runner_app.executors.rootless_v2 import RootlessError, _write_json
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
                                          record_event=self.record,
                                          require_recovering=lambda _request: None)
        (root / "volumes").mkdir(mode=0o700)
        self.volume = root / "volumes" / self.request
        self.containers: dict[str, dict[str, Any]] = {}
        self.network: dict[str, Any] | None = None
        self.plan: dict[str, Any] = {}
        self.released = False
        monkeypatch.setattr(self.backend, "_daemon", self.daemon)
        monkeypatch.setattr(self.backend, "_allocate", self.allocate)
        monkeypatch.setattr(self.backend, "_release", self.release)
        monkeypatch.setattr(self.backend, "_command", self.command)
        monkeypatch.setattr(module, "_verified_retired_volume", lambda path, *_args: path)
        monkeypatch.setattr(module, "VOLUME_BASE", self.volume.parent)
        monkeypatch.setattr(recovery_module, "VOLUME_BASE", self.volume.parent)
        monkeypatch.setattr(recovery_module, "_verified_retired_volume", lambda path, *_args: path)
        monkeypatch.setattr(module, "_process_snapshot", lambda: ({}, True))
        monkeypatch.setattr(recovery_module, "_helpers_absent", lambda _request: None)

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
        assert any(name in {"COMPOSE_EVIDENCE_SEALED", "COMPOSE_RECOVERY_EVIDENCE_SEALED"}
                   for name, _ in self.events)
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
        if _kwargs.get("lease"):
            _write_json(_kwargs["lease"] / "process.json", {"pid": 999999999, "start": "1"})
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
        if action == "stop":
            for identifier in arguments[4:]:
                self.containers[identifier]["State"].update(Running=False, Status="exited")
            return 0, b"", b""
        if action == "rm":
            if kind == "container":
                for identifier in arguments[2:]:
                    assert self.containers[identifier]["State"]["Running"] is False
                    del self.containers[identifier]
            elif kind == "network":
                assert not self.containers and self.network is not None
                assert arguments[2] == self.network["Id"]
                self.network = None
            return 0, b"", b""
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

    def recover(self) -> Any:
        return self.backend.recover(self.snapshot, request_id=self.request,
                                    profile_id="compose-python", journal=list(self.events))


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


class ControllerCrash(BaseException):
    """Simulates controller death, bypassing the ordinary exception cleanup path."""


def crash_command(fixture: Fixture, monkeypatch: pytest.MonkeyPatch, action: str,
                  service: str | None = None) -> None:
    original = fixture.backend._command
    def cut(arguments: list[str], **kwargs: Any) -> tuple[int, bytes, bytes]:
        result = original(arguments, **kwargs)
        if arguments[0] == "compose" and action in arguments and (service is None or arguments[-1] == service):
            raise ControllerCrash
        return result
    monkeypatch.setattr(fixture.backend, "_command", cut)
    with pytest.raises(ControllerCrash):
        fixture.run()
    monkeypatch.setattr(fixture.backend, "_command", original)
    fixture.calls.clear()


@pytest.mark.parametrize(("action", "service", "outcome"), [
    ("create", None, "NOT_RUN"), ("start", "api", "NOT_RUN"),
    ("start", "test", "UNKNOWN"), ("down", None, "PASSED"),
])
def test_recovery_after_lost_control_ack_preserves_truth_and_never_starts(
        fixture: Fixture, monkeypatch: pytest.MonkeyPatch, action: str,
        service: str | None, outcome: str) -> None:
    crash_command(fixture, monkeypatch, action, service)
    recovered = fixture.recover()
    assert recovered.cleanup_verified and recovered.outcome == outcome and fixture.released
    assert not fixture.containers and fixture.network is None and not fixture.volume.exists()
    assert not any("create" in call or "start" in call or "down" in call for call in fixture.calls)
    assert recovered.stdout_path.is_file() and recovered.stderr_path.is_file()
    if action == "start" and service == "test":
        assert (recovered.output_root / "junit.xml").exists()
        assert recovered.provenance["test_started"] is None


def test_recovery_after_allocation_reply_loss_uses_preallocation_identity(
        fixture: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    original = fixture.backend._allocate
    def cut(*args: Any) -> Path:
        original(*args)
        raise ControllerCrash
    monkeypatch.setattr(fixture.backend, "_allocate", cut)
    with pytest.raises(ControllerCrash):
        fixture.run()
    assert fixture.volume.exists() and not fixture.containers
    assert fixture.recover().outcome == "NOT_RUN"
    assert fixture.released


def test_recovery_readonly_preflight_crash_has_no_execution_to_replay(fixture: Fixture) -> None:
    recovered = fixture.recover()
    assert recovered.cleanup_verified and recovered.outcome == "NOT_RUN"
    assert not fixture.volume.exists() and not fixture.containers


def test_recovery_preserves_already_sealed_result_and_repeated_calls_are_idempotent(
        fixture: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    original = fixture.backend.record_event
    def cut(request: str, event: str, detail: dict[str, object]) -> None:
        original(request, event, detail)
        if event == "COMPOSE_EVIDENCE_SEALED":
            raise ControllerCrash
    monkeypatch.setattr(fixture.backend, "record_event", cut)
    with pytest.raises(ControllerCrash):
        fixture.run()
    monkeypatch.setattr(fixture.backend, "record_event", original)
    first = fixture.recover()
    files = {path.name: path.read_bytes() for path in first.output_root.iterdir()}
    fixture.calls.clear()
    second = fixture.recover()
    assert first.outcome == second.outcome == "PASSED"
    assert first.output_root == second.output_root
    assert files == {path.name: path.read_bytes() for path in second.output_root.iterdir()}
    assert not any("stop" in call or "rm" in call for call in fixture.calls)


@pytest.mark.parametrize("cut", ["container", "network", "volume"])
def test_recovery_itself_can_resume_after_partial_exact_cleanup(
        fixture: Fixture, monkeypatch: pytest.MonkeyPatch, cut: str) -> None:
    crash_command(fixture, monkeypatch, "start", "test")
    original = fixture.backend._command
    release = fixture.backend._release
    if cut == "volume":
        def lost_release(*args: Any) -> None:
            release(*args)
            raise ControllerCrash
        monkeypatch.setattr(fixture.backend, "_release", lost_release)
    else:
        def lost_remove(arguments: list[str], **kwargs: Any) -> tuple[int, bytes, bytes]:
            result = original(arguments, **kwargs)
            if arguments[:2] == [cut, "rm"]:
                raise ControllerCrash
            return result
        monkeypatch.setattr(fixture.backend, "_command", lost_remove)
    with pytest.raises(ControllerCrash):
        fixture.recover()
    monkeypatch.setattr(fixture.backend, "_command", original)
    monkeypatch.setattr(fixture.backend, "_release", release)
    recovered = fixture.recover()
    assert recovered.cleanup_verified and recovered.outcome == "UNKNOWN"
    assert len(list((fixture.backend.root / fixture.request).glob("recovery-[0-9][0-9][0-9][0-9]"))) == 1
    assert (recovered.output_root / "junit.xml").exists()


@pytest.mark.parametrize("damage", ["daemon", "nonce", "container", "network", "profile", "config", "source"])
def test_recovery_identity_ambiguity_never_removes_resources(
        fixture: Fixture, monkeypatch: pytest.MonkeyPatch, damage: str) -> None:
    crash_command(fixture, monkeypatch, "start", "test")
    if damage == "daemon":
        fixture.mode = "daemon-change"
    elif damage == "nonce":
        next(iter(fixture.containers.values()))["Config"]["Labels"]["cdr.compose-nonce"] = "e" * 32
    elif damage == "container":
        identifier = next(iter(fixture.containers))
        item = fixture.containers.pop(identifier)
        item["Id"] = "e" * 64
        fixture.containers[item["Id"]] = item
    elif damage == "network":
        assert fixture.network is not None
        fixture.network["Id"] = "e" * 64
    elif damage == "profile":
        (fixture.profile_dir / "compose-python.json").write_bytes(b"{}")
    elif damage == "config":
        config = fixture.backend.root / fixture.request / "resolved.json"
        config.chmod(0o600)
        config.write_bytes(b"{}")
    else:
        source = fixture.snapshot.path / "compose.json"
        source.chmod(0o600)
        source.write_bytes(b"{}")
    with pytest.raises(RootlessError) as caught:
        fixture.recover()
    assert not caught.value.cleanup_verified and fixture.volume.exists() and fixture.containers
    assert not any("rm" in call or "stop" in call for call in fixture.calls)


def test_recovery_refuses_missing_original_before_any_cleanup_intent(
        fixture: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    crash_command(fixture, monkeypatch, "start", "test")
    fixture.containers.pop(next(iter(fixture.containers)))
    with pytest.raises(RootlessError, match="COMPOSE_RECOVERY_RESOURCE_MISSING"):
        fixture.recover()
    assert fixture.volume.exists() and not fixture.released


@pytest.mark.parametrize("damage", ["capture", "output", "metadata"])
def test_recovery_damaged_sealed_evidence_keeps_stopped_resources(
        fixture: Fixture, monkeypatch: pytest.MonkeyPatch, damage: str) -> None:
    fixture.mode = "cleanup-failure"
    with pytest.raises(RootlessError):
        fixture.run()
    fixture.mode = "pass"
    root = fixture.backend.root / fixture.request
    selected = {"capture": root / "stdout.bin", "output": root / "outputs/junit.xml",
                "metadata": root / "backend.json"}[damage]
    selected.chmod(0o600)
    selected.write_bytes(b"tampered")
    fixture.calls.clear()
    with pytest.raises(RootlessError):
        fixture.recover()
    assert fixture.containers and fixture.volume.exists()
    assert not any("rm" in call for call in fixture.calls)


def test_recovery_stops_when_session_guard_expires_before_cleanup(
        fixture: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    crash_command(fixture, monkeypatch, "start", "test")
    expired = [False]
    original = fixture.backend.record_event
    def guard(_request: str) -> None:
        if expired[0]:
            raise RootlessError("RECOVERY_SESSION_EXPIRED", cleanup_verified=False)
    def record(request: str, event: str, detail: dict[str, object]) -> None:
        original(request, event, detail)
        if event == "COMPOSE_RECOVERY_EVIDENCE_SEALED":
            expired[0] = True
    monkeypatch.setattr(fixture.backend, "require_recovering", guard)
    monkeypatch.setattr(fixture.backend, "record_event", record)
    with pytest.raises(RootlessError, match="RECOVERY_SESSION_EXPIRED"):
        fixture.recover()
    assert fixture.containers and fixture.volume.exists() and not fixture.released
    assert not any("rm" in call for call in fixture.calls)


def test_recovery_active_or_unrecorded_control_cli_is_not_replayed(
        fixture: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    crash_command(fixture, monkeypatch, "create")
    controls = fixture.backend.root / fixture.request / "control"
    lease = next(path for path in controls.iterdir() if not (path / "complete.json").exists())
    original = fixture.backend._control_group_absent
    monkeypatch.setattr(fixture.backend, "_control_group_absent", lambda *_args: (
        _ for _ in ()).throw(RootlessError("COMPOSE_CONTROL_NOT_QUIESCENT", cleanup_verified=False)))
    with pytest.raises(RootlessError, match="COMPOSE_CONTROL_NOT_QUIESCENT"):
        fixture.recover()
    monkeypatch.setattr(fixture.backend, "_control_group_absent", original)
    (lease / "process.json").unlink()
    with pytest.raises(RootlessError, match="COMPOSE_CONTROL_PROOF_MISSING"):
        fixture.recover()
    assert fixture.volume.exists() and fixture.containers and not fixture.released


def test_actual_control_child_records_identity_and_completes_before_recovery_permission(
        tmp_path: Path) -> None:
    backend = object.__new__(ComposeTaskBackend)
    backend.broker = SimpleNamespace(  # type: ignore[assignment]
        _docker_argv=lambda *args: [sys.executable, *args],
        _docker_env=lambda: {"PATH": os.defpath})
    code, output, error = backend._mutate(["-c", "print('controlled child')"], root=tmp_path,
                                         timeout=3, guard=lambda: None)
    assert code == 0 and output == b"controlled child\n" and not error
    lease = next((tmp_path / "control").iterdir())
    process = json.loads((lease / "process.json").read_bytes())
    assert process["pid"] > 0 and process["start"].isdecimal()
    assert json.loads((lease / "complete.json").read_bytes()) == {"exit_code": 0}
    backend._control_group_absent(process["pid"], process["start"])


def test_control_quiescence_checks_leader_reuse_and_live_descendants(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = object.__new__(ComposeTaskBackend)
    cases = [
        ({50: SimpleNamespace(pgid=50, state="R", start="2")}, True),
        ({51: SimpleNamespace(pgid=50, state="S", start="3")}, True),
        ({}, False),
    ]
    for processes, complete in cases:
        monkeypatch.setattr(module, "_process_snapshot", lambda items=processes, ready=complete: (items, ready))
        with pytest.raises(RootlessError, match="COMPOSE_CONTROL_NOT_QUIESCENT"):
            backend._control_group_absent(50, "1")
    monkeypatch.setattr(module, "_process_snapshot", lambda: ({}, True))
    backend._control_group_absent(50, "1")


def test_recovery_requires_explicit_guard_even_when_resources_are_absent(fixture: Fixture) -> None:
    fixture.backend.require_recovering = None
    with pytest.raises(RootlessError, match="COMPOSE_RECOVERY_NOT_AUTHORIZED"):
        fixture.recover()
    assert not fixture.calls and not (fixture.backend.root / fixture.request).exists()


def test_recovery_no_mutation_intent_does_not_require_a_cached_image(
        fixture: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fixture.backend, "_image", lambda *_args: pytest.fail("preflight image recheck"))
    assert fixture.recover().outcome == "NOT_RUN"
