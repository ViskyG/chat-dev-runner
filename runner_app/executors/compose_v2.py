"""Finite, owner-enrolled Linux Compose tasks on the existing rootless daemon.

The initial profile deliberately accepts a small JSON Compose subset: one pinned
image, healthy dependency services, ordered migrations and one finite test client.
All writable workload paths share the existing hard-quota helper mount. Neither
the project nor a request selects host paths, privileges, downloads or ports.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import selectors
import signal
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID, uuid4

from runner_app.domain.contracts_v2 import parse_contract_json, validate_relative_path
from runner_app.git.source_v2 import SourceSnapshot
from runner_app.outputs.collection import OutputSpec, collect_outputs
from runner_app.storage.paths import ensure_private_directory, open_private_file

from .broker import RootlessDockerBroker, _snapshot_tree, _verified_run_volume
from .native_v2 import _snapshot as _process_snapshot
from .profiles import ProfileOutput, ProfileRegistry, _constant, _pairs
from .rootless_v2 import JournalCallback, RootlessError, RootlessTaskResult, _write_json
from .volume_provider import VOLUME_BASE, HardQuotaVolumeProvider, _verified_retired_volume

_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"[a-z0-9][a-z0-9.:/-]*@sha256:[0-9a-f]{64}\Z")
_LABEL = "cdr.compose-run"
_NONCE = "cdr.compose-nonce"
_MIB = 1024 * 1024


def _fail(code: str, *, cleanup: bool = True) -> RootlessError:
    return RootlessError(code, cleanup_verified=cleanup)


def _object(value: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if (not isinstance(value, dict) or not required <= value.keys()
            or value.keys() - required - (optional or set())):
        raise _fail("COMPOSE_PROFILE_INVALID")
    return value


def _integer(value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise _fail("COMPOSE_PROFILE_INVALID")
    return value


def _tokens(value: Any) -> tuple[str, ...]:
    if (not isinstance(value, list) or not 1 <= len(value) <= 64
            or any(not isinstance(x, str) or not x or len(x) > 16384
                   or "\x00" in x or "$" in x for x in value)):
        raise _fail("COMPOSE_COMMAND_INVALID")
    return tuple(value)


def _names(value: Any) -> tuple[str, ...]:
    if (not isinstance(value, list) or len(value) > 5
            or any(not isinstance(x, str) or not _ID.fullmatch(x) for x in value)
            or len(set(value)) != len(value)):
        raise _fail("COMPOSE_PROFILE_INVALID")
    return tuple(value)


def _read_private(path: Path, limit: int = 65536) -> bytes:
    descriptor = open_private_file(path)
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise _fail("COMPOSE_PROFILE_INVALID")
    return raw


@dataclass(frozen=True)
class ComposeProfile:
    profile_id: str
    revision_sha256: str
    image: str
    platform: str
    compose_file: str
    compose_sha256: str
    test_service: str
    ready_services: tuple[str, ...]
    migration_services: tuple[str, ...]
    argv: tuple[str, ...]
    timeout_seconds: int
    readiness_seconds: int
    migration_seconds: int
    cleanup_seconds: int
    volume_mb: int
    memory_mb: int
    cpu_limit: float
    pids_limit: int
    max_log_bytes: int
    collect: tuple[ProfileOutput, ...]
    allowed_images: tuple[str, ...] = ()

    @property
    def services(self) -> tuple[str, ...]:
        return (*self.ready_services, *self.migration_services, self.test_service)


def _parse_profile(raw: bytes, identifier: str, digest: str) -> ComposeProfile:
    # Unlike remote intent contracts, an owner resource profile permits a
    # fractional CPU budget. Duplicate keys and non-finite JSON remain invalid.
    value = _object(json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant), {
        "schema_version", "profile_id", "image", "platform", "compose_file", "compose_sha256",
        "test_service", "ready_services", "migration_services", "argv", "timeout_seconds",
        "readiness_seconds", "migration_seconds", "cleanup_seconds", "volume_mb", "memory_mb",
        "cpu_limit", "pids_limit", "max_log_bytes", "mount_policy", "collect"}, {"allowed_images"})
    if (value["schema_version"] != "cdr-compose-profile/1" or value["profile_id"] != identifier
            or not _ID.fullmatch(identifier) or not isinstance(value["image"], str)
            or not _IMAGE.fullmatch(value["image"])
            or value["platform"] not in {"linux/amd64", "linux/arm64"}
            or value["mount_policy"] != "frozen-source-and-run-quota-v1"
            or not isinstance(value["compose_sha256"], str)
            or not _SHA.fullmatch(value["compose_sha256"])
            or not isinstance(value["compose_file"], str)
            or not value["compose_file"].endswith(".json")):
        raise _fail("COMPOSE_PROFILE_INVALID")
    validate_relative_path(value["compose_file"])
    images = value.get("allowed_images", [value["image"]])
    if (not isinstance(images, list) or not 1 <= len(images) <= 8
            or any(not isinstance(image, str) or not _IMAGE.fullmatch(image) for image in images)
            or len(set(images)) != len(images) or value["image"] not in images):
        raise _fail("COMPOSE_PROFILE_INVALID")
    ready, migration = _names(value["ready_services"]), _names(value["migration_services"])
    test = value["test_service"]
    if (not ready or not isinstance(test, str) or not _ID.fullmatch(test)
            or len({*ready, *migration, test}) != len(ready) + len(migration) + 1
            or len(ready) + len(migration) + 1 > 6):
        raise _fail("COMPOSE_PROFILE_INVALID")
    cpu = value["cpu_limit"]
    if type(cpu) not in (int, float) or not math.isfinite(cpu) or not 0.1 <= cpu <= 8:
        raise _fail("COMPOSE_PROFILE_INVALID")
    outputs: list[ProfileOutput] = []
    if not isinstance(value["collect"], list) or len(value["collect"]) > 32:
        raise _fail("COMPOSE_PROFILE_INVALID")
    for declaration in value["collect"]:
        item = _object(declaration, {"id", "kind", "path", "format", "required", "upload_policy"})
        if (not isinstance(item["id"], str) or not _ID.fullmatch(item["id"])
                or item["kind"] not in {"test_result", "report", "artifact", "application_log"}
                or not isinstance(item["format"], str) or not item["format"]
                or not isinstance(item["path"], str)
                or type(item["required"]) is not bool
                or item["upload_policy"] not in {"local_only", "sanitized_only"}):
            raise _fail("COMPOSE_PROFILE_INVALID")
        validate_relative_path(item["path"])
        outputs.append(ProfileOutput(**item))
    if len({item.id for item in outputs}) != len(outputs):
        raise _fail("COMPOSE_PROFILE_INVALID")
    return ComposeProfile(
        identifier, digest, value["image"], value["platform"], value["compose_file"],
        value["compose_sha256"], test, ready, migration, _tokens(value["argv"]),
        _integer(value["timeout_seconds"], 1, 7200),
        _integer(value["readiness_seconds"], 1, 300),
        _integer(value["migration_seconds"], 1, 600),
        _integer(value["cleanup_seconds"], 1, 30),
        _integer(value["volume_mb"], 16, 1024),
        _integer(value["memory_mb"], 32, 2048), float(cpu),
        _integer(value["pids_limit"], 8, 512),
        _integer(value["max_log_bytes"], 1024, 16 * _MIB), tuple(outputs), tuple(images))


def load_compose_profiles(profile_dir: Path) -> dict[str, ComposeProfile]:
    """Load only local private files with hashes from the private enrollment grant."""
    try:
        if not profile_dir.is_absolute() or ".." in profile_dir.parts:
            raise ValueError
        grant = _object(parse_contract_json(_read_private(profile_dir / "compose-grant.json")),
                        {"schema_version", "profile_revision_sha256"})
        revisions = grant["profile_revision_sha256"]
        if (grant["schema_version"] != "cdr-compose-grant/1" or not isinstance(revisions, dict)
                or not 1 <= len(revisions) <= 32):
            raise ValueError
        result: dict[str, ComposeProfile] = {}
        for identifier, digest in revisions.items():
            if (not isinstance(identifier, str) or not _ID.fullmatch(identifier)
                    or not isinstance(digest, str) or not _SHA.fullmatch(digest)):
                raise ValueError
            raw = _read_private(profile_dir / (identifier + ".json"))
            if hashlib.sha256(raw).hexdigest() != digest:
                raise _fail("COMPOSE_PROFILE_CHANGED")
            result[identifier] = _parse_profile(raw, identifier, digest)
        return result
    except RootlessError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise _fail("COMPOSE_PROFILE_UNAVAILABLE") from None


def _source_config(snapshot: SourceSnapshot, profile: ComposeProfile) -> dict[str, Any]:
    path = snapshot.path / profile.compose_file
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
        raise _fail("COMPOSE_SOURCE_INVALID")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(65537)
    if hashlib.sha256(raw).hexdigest() != profile.compose_sha256:
        raise _fail("COMPOSE_SOURCE_NOT_APPROVED")
    value = _object(parse_contract_json(raw), {"services"})
    services = value["services"]
    if not isinstance(services, dict) or set(services) != set(profile.services):
        raise _fail("COMPOSE_SERVICE_SCOPE_INVALID")
    for name, candidate in services.items():
        service = _object(candidate, {"image", "command"}, {"healthcheck"})
        if (service["image"] not in profile.allowed_images
                or name == profile.test_service and service["image"] != profile.image):
            raise _fail("COMPOSE_IMAGE_NOT_APPROVED")
        command = _tokens(service["command"])
        if name == profile.test_service and command != profile.argv:
            raise _fail("COMPOSE_COMMAND_INVALID")
        health = service.get("healthcheck")
        if name in profile.ready_services:
            health = _object(health, {"test", "interval", "timeout", "retries"}, {"start_period"})
            if _tokens(health["test"])[0] != "CMD":
                raise _fail("COMPOSE_HEALTHCHECK_REQUIRED")
            for key in {"interval", "timeout", "start_period"} & health.keys():
                if not isinstance(health[key], str) or not re.fullmatch(r"[1-9][0-9]?(ms|s)", health[key]):
                    raise _fail("COMPOSE_HEALTHCHECK_REQUIRED")
            _integer(health["retries"], 1, 100)
        elif health is not None:
            raise _fail("COMPOSE_HEALTHCHECK_REQUIRED")
    return value


def _compose_plan(source: Mapping[str, Any], profile: ComposeProfile, snapshot: SourceSnapshot,
                  project: str, nonce: str, volume: Path) -> dict[str, Any]:
    labels = {_LABEL: snapshot.request_id, _NONCE: nonce}
    services: dict[str, Any] = {}
    for name, original in source["services"].items():
        mounts = [(snapshot.path, "/src", True), (volume / "out", "/out", False),
                  (volume / "data", "/data", False), (volume / ("tmp-" + name), "/tmp", False)]
        services[name] = {
            **original, "entrypoint": [], "platform": profile.platform, "user": "0:0",
            "working_dir": "/src", "read_only": True, "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"], "restart": "no",
            "mem_limit": profile.memory_mb * _MIB, "cpus": profile.cpu_limit,
            "memswap_limit": profile.memory_mb * _MIB,
            "pids_limit": profile.pids_limit, "shm_size": 8 * _MIB,
            "labels": labels, "network_mode": None,
            "environment": {"CDR_RUN_ID": snapshot.request_id, "CDR_SOURCE_SHA": snapshot.source_commit,
                            "CDR_OUTPUT_DIR": "/out", "PYTHONDONTWRITEBYTECODE": "1"},
            "volumes": [{"type": "bind", "source": str(path), "target": target,
                         "read_only": read_only, "bind": {"create_host_path": False}}
                        for path, target, read_only in mounts],
            "logging": {"driver": "json-file", "options": {
                "max-size": str(profile.max_log_bytes), "max-file": "1"}},
        }
        del services[name]["network_mode"]
        if name not in profile.ready_services:
            services[name]["healthcheck"] = {"disable": True}
    return {"name": project, "services": services,
            "networks": {"default": {"internal": True, "labels": labels}}}


class ComposeTaskBackend:
    """Fixed profile adapter; admission and durable one-lane ownership stay in Engine."""

    def __init__(self, profile_dir: Path, runs_root: Path, *,
                 require_running: Callable[[str], None], record_event: JournalCallback,
                 require_recovering: Callable[[str], None] | None = None) -> None:
        self.profile_dir = profile_dir
        self.profiles = load_compose_profiles(profile_dir)
        self.require_running, self.record_event = require_running, record_event
        self.require_recovering = require_recovering
        self.root = runs_root / "compose-v2"
        ensure_private_directory(self.root)
        self.broker = RootlessDockerBroker(ProfileRegistry(profile_dir, {}))

    def profile(self, profile_id: str) -> ComposeProfile:
        known = self.profiles.get(profile_id)
        current = load_compose_profiles(self.profile_dir).get(profile_id)
        if known is None or current is None or known != current:
            raise _fail("COMPOSE_PROFILE_CHANGED")
        return current

    def timeout_seconds(self, profile_id: str) -> int:
        profile = self.profile(profile_id)
        return (profile.readiness_seconds + len(profile.migration_services) * profile.migration_seconds
                + profile.timeout_seconds + 2 * profile.cleanup_seconds + 120)

    def _command(self, arguments: list[str], *, timeout: float = 8,
                 limit: int = _MIB, allow_truncate: bool = False,
                 lease: Path | None = None) -> tuple[int, bytes, bytes]:
        """Bound memory while draining Docker pipes; kill only this control CLI on failure."""
        try:
            process = subprocess.Popen(self.broker._docker_argv(*arguments), stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=self.broker._docker_env(), start_new_session=True)
        except OSError:
            raise _fail("COMPOSE_CLI_UNAVAILABLE", cleanup=False) from None
        streams = [bytearray(), bytearray()]
        truncated = False
        deadline = time.monotonic() + timeout
        try:
            if lease is not None:
                processes, complete = _process_snapshot()
                observed = processes.get(process.pid)
                if not complete or observed is None:
                    raise _fail("COMPOSE_CONTROL_IDENTITY_UNCERTAIN", cleanup=False)
                _write_json(lease / "process.json", {"pid": process.pid, "start": observed.start})
            with selectors.DefaultSelector() as selector:
                for index, pipe in enumerate((process.stdout, process.stderr)):
                    assert pipe is not None
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, index)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise _fail("COMPOSE_CLI_TIMEOUT", cleanup=False)
                    for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        remaining = max(0, limit - len(streams[key.data]))
                        streams[key.data].extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            truncated = True
                            if not allow_truncate:
                                raise _fail("COMPOSE_CLI_OUTPUT_LIMIT", cleanup=False)
                code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
                if lease is not None:
                    self._control_group_absent(process.pid, None)
                return -1000 if truncated else code, bytes(streams[0]), bytes(streams[1])
        except (OSError, subprocess.TimeoutExpired):
            raise _fail("COMPOSE_CLI_UNAVAILABLE", cleanup=False) from None
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

    @staticmethod
    def _control_group_absent(pid: int, start: str | None) -> None:
        processes, complete = _process_snapshot()
        leader = processes.get(pid)
        if (not complete or leader is not None and start is not None and leader.start != start
                or any(item.pgid == pid and not item.state.startswith(("Z", "X"))
                       for item in processes.values())):
            raise _fail("COMPOSE_CONTROL_NOT_QUIESCENT", cleanup=False)

    def _mutate(self, arguments: list[str], *, root: Path, timeout: float,
                guard: Callable[[], None]) -> tuple[int, bytes, bytes]:
        guard()
        leases = root / "control"
        ensure_private_directory(leases)
        if len(list(leases.iterdir())) >= 128:
            raise _fail("COMPOSE_CONTROL_BUDGET_EXCEEDED", cleanup=False)
        lease = leases / uuid4().hex
        lease.mkdir(mode=0o700)
        _write_json(lease / "intent.json", {"argv": arguments})
        guard()
        result = self._command(arguments, timeout=timeout, lease=lease)
        _write_json(lease / "complete.json", {"exit_code": result[0]})
        return result

    def recover(self, snapshot: SourceSnapshot, *, request_id: str, profile_id: str,
                journal: Sequence[tuple[str, Mapping[str, Any]]]) -> RootlessTaskResult:
        """Reconcile one retained lane without re-executing a workload command."""
        from .compose_recovery_v2 import recover
        return recover(self, snapshot, request_id=request_id, profile_id=profile_id, journal=journal)

    def _json(self, *arguments: str, timeout: float = 8) -> Any:
        code, raw, _ = self._command(list(arguments), timeout=timeout)
        if code:
            raise _fail("COMPOSE_RUNTIME_UNAVAILABLE", cleanup=False)
        try:
            return json.loads(raw)
        except (UnicodeError, ValueError):
            raise _fail("COMPOSE_RUNTIME_RESPONSE_INVALID", cleanup=False) from None

    def _daemon(self, expected: str | None = None, *, timeout: float = 8) -> str:
        if expected is not None:
            current = self._json("info", "--format", "{{json .}}", timeout=timeout)
            if (not isinstance(current, dict) or current.get("ID") != expected
                    or "name=rootless" not in (current.get("SecurityOptions") or [])
                    or current.get("OSType") != "linux"):
                raise _fail("COMPOSE_DAEMON_CHANGED", cleanup=False)
            return expected
        probe = self.broker.probe()
        if not probe.available or not probe.daemon_id:
            raise _fail(probe.code, cleanup=expected is None)
        if expected is not None and probe.daemon_id != expected:
            raise _fail("COMPOSE_DAEMON_CHANGED", cleanup=False)
        return probe.daemon_id

    def _image(self, reference: str, platform: str) -> str:
        code, raw, _ = self._command(["image", "inspect", reference, "--format", "{{json .}}"])
        if code:
            raise _fail("COMPOSE_IMAGE_UNAVAILABLE")
        try:
            image = json.loads(raw)
        except (UnicodeError, ValueError):
            raise _fail("COMPOSE_IMAGE_NOT_APPROVED") from None
        accepted = {reference, reference.removeprefix("docker.io/library/")}
        if (not isinstance(image, dict) or not isinstance(image.get("RepoDigests"), list)
                or not accepted.intersection(image["RepoDigests"])
                or not isinstance(image.get("Id"), str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", image["Id"])
                or f"{image.get('Os')}/{image.get('Architecture')}" != platform
                or image.get("Config", {}).get("Volumes")):
            raise _fail("COMPOSE_IMAGE_NOT_APPROVED")
        return str(image["Id"])

    @staticmethod
    def _compose(path: Path, project: str) -> list[str]:
        return ["compose", "--ansi", "never", "--project-name", project,
                "--project-directory", str(path.parent), "--env-file", "/dev/null", "-f", str(path)]

    def _inventory(self, kind: str, project: str) -> list[str]:
        arguments = [kind, "ls", "--quiet", "--filter", f"label=com.docker.compose.project={project}"]
        if kind == "container":
            arguments.extend(["--all", "--no-trunc"])
        elif kind == "network":
            arguments.append("--no-trunc")
        code, raw, _ = self._command(arguments)
        if code:
            raise _fail("COMPOSE_INVENTORY_UNCERTAIN", cleanup=False)
        names = raw.decode("ascii", "strict").splitlines()
        if len(names) != len(set(names)) or len(names) > 8:
            raise _fail("COMPOSE_INVENTORY_UNCERTAIN", cleanup=False)
        if kind != "volume" and any(not _SHA.fullmatch(name) for name in names):
            raise _fail("COMPOSE_INVENTORY_UNCERTAIN", cleanup=False)
        return names

    def _owned(self, project: str, request_id: str, nonce: str,
               profile: ComposeProfile, *, complete: bool = False) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
        containers: dict[str, dict[str, Any]] = {}
        for identifier in self._inventory("container", project):
            item = self._json("container", "inspect", identifier, "--format", "{{json .}}")
            labels = item.get("Config", {}).get("Labels", {})
            service = labels.get("com.docker.compose.service")
            if (item.get("Id") != identifier or labels.get(_LABEL) != request_id
                    or labels.get(_NONCE) != nonce or service not in profile.services
                    or service in containers or labels.get("com.docker.compose.oneoff") not in {"False", "false"}
                    or item.get("Name") != f"/{project}-{service}-1"):
                raise _fail("COMPOSE_RESOURCE_IDENTITY_MISMATCH", cleanup=False)
            containers[service] = item
        networks = self._inventory("network", project)
        if len(networks) > 1 or self._inventory("volume", project):
            raise _fail("COMPOSE_RESOURCE_IDENTITY_MISMATCH", cleanup=False)
        network = None
        if networks:
            network = self._json("network", "inspect", networks[0], "--format", "{{json .}}")
            labels = network.get("Labels", {})
            if (network.get("Id") != networks[0] or network.get("Name") != project + "_default"
                    or labels.get(_LABEL) != request_id or labels.get(_NONCE) != nonce
                    or network.get("Internal") is not True or network.get("Driver") != "bridge"):
                raise _fail("COMPOSE_RESOURCE_IDENTITY_MISMATCH", cleanup=False)
            allowed = {item["Id"] for item in containers.values()}
            if set(network.get("Containers", {})) - allowed:
                raise _fail("COMPOSE_RESOURCE_IDENTITY_MISMATCH", cleanup=False)
        if complete and (set(containers) != set(profile.services) or network is None):
            raise _fail("COMPOSE_RESOURCE_SET_INCOMPLETE", cleanup=False)
        return containers, network

    @staticmethod
    def _verify_created(containers: Mapping[str, Any], profile: ComposeProfile,
                        source: Mapping[str, Any], snapshot: SourceSnapshot,
                        volume: Path, image_ids: Mapping[str, str], project: str, *,
                        recovering: bool = False) -> None:
        for name, item in containers.items():
            config, host = item.get("Config", {}), item.get("HostConfig", {})
            reference = source["services"][name]["image"]
            if (item.get("Image") != image_ids[reference] or config.get("Image") != reference
                    or config.get("Cmd") != source["services"][name]["command"]
                    or config.get("Entrypoint") not in ([], None) or config.get("User") != "0:0"
                    or config.get("WorkingDir") != "/src" or config.get("Volumes")
                    or not recovering and item.get("State", {}).get("Status") != "created"
                    or host.get("ReadonlyRootfs") is not True or host.get("Privileged") is not False
                    or host.get("CapDrop") != ["ALL"] or host.get("CapAdd")
                    or set(host.get("SecurityOpt") or []) != {"no-new-privileges:true"}
                    or host.get("Memory") != profile.memory_mb * _MIB
                    or host.get("MemorySwap") != profile.memory_mb * _MIB
                    or host.get("NanoCpus") != int(profile.cpu_limit * 1_000_000_000)
                    or host.get("PidsLimit") != profile.pids_limit
                    or host.get("NetworkMode") != project + "_default"
                    or host.get("PortBindings") or host.get("PublishAllPorts")
                    or host.get("PidMode") or host.get("IpcMode") not in {"private", ""}
                    or host.get("Devices") or host.get("DeviceRequests")
                    or host.get("UTSMode") or host.get("CgroupParent")
                    or host.get("UsernsMode") or host.get("VolumesFrom")
                    or host.get("ShmSize") != 8 * _MIB
                    or host.get("RestartPolicy", {}).get("Name") not in {"no", ""}
                    or host.get("LogConfig") != {"Type": "json-file", "Config": {
                        "max-size": str(profile.max_log_bytes), "max-file": "1"}}):
                raise _fail("COMPOSE_CREATED_POLICY_MISMATCH", cleanup=False)
            actual = {(m.get("Source"), m.get("Destination"), m.get("RW"), m.get("Type"))
                      for m in item.get("Mounts", [])}
            expected = {(str(snapshot.path), "/src", False, "bind"),
                        (str(volume / "out"), "/out", True, "bind"),
                        (str(volume / "data"), "/data", True, "bind"),
                        (str(volume / ("tmp-" + name)), "/tmp", True, "bind")}
            if actual != expected or len(item.get("Mounts", [])) != 4:
                raise _fail("COMPOSE_CREATED_POLICY_MISMATCH", cleanup=False)
            health = config.get("Healthcheck", {})
            if name in profile.ready_services:
                if health.get("Test") != source["services"][name]["healthcheck"]["test"]:
                    raise _fail("COMPOSE_CREATED_POLICY_MISMATCH", cleanup=False)
            elif health.get("Test") != ["NONE"]:
                raise _fail("COMPOSE_CREATED_POLICY_MISMATCH", cleanup=False)

    def _allocate(self, request_id: str, profile: ComposeProfile) -> Path:
        if not HardQuotaVolumeProvider._installed_helper():
            raise _fail("VOLUME_HELPER_UNAVAILABLE")
        response = HardQuotaVolumeProvider._helper("allocate", request_id, str(profile.volume_mb))
        volume = VOLUME_BASE / request_id
        if response != {"status": "ALLOCATED", "run_id": request_id,
                        "size_mb": profile.volume_mb, "path": str(volume)}:
            raise _fail("VOLUME_HELPER_RESPONSE_INVALID", cleanup=False)
        _verified_run_volume(volume, request_id, profile.volume_mb * _MIB)
        for directory in ("out", "data", *("tmp-" + name for name in profile.services)):
            (volume / directory).mkdir(mode=0o700)
        return volume

    def _release(self, request_id: str, profile: ComposeProfile, volume: Path) -> None:
        HardQuotaVolumeProvider._clear_verified_mount(volume, request_id, profile.volume_mb * _MIB)
        response = HardQuotaVolumeProvider._helper("release", request_id)
        if response != {"status": "RELEASED", "run_id": request_id} or os.path.ismount(volume):
            raise _fail("COMPOSE_VOLUME_CLEANUP_UNCERTAIN", cleanup=False)

    def _wait(self, identifiers: Mapping[str, str], *, health: bool, timeout: float,
              cancel: Event | None, daemon: str) -> tuple[str, int | None]:
        deadline = time.monotonic() + timeout
        while True:
            if cancel is not None and cancel.is_set():
                return "CANCELLED", None
            if time.monotonic() >= deadline:
                return "TIMED_OUT", None
            self._daemon(daemon, timeout=max(0.01, min(8, deadline - time.monotonic())))
            states = []
            for identifier in identifiers.values():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "TIMED_OUT", None
                states.append(self._json("container", "inspect", identifier, "--format", "{{json .State}}",
                                         timeout=min(8, remaining)))
            if cancel is not None and cancel.is_set():
                return "CANCELLED", None
            if time.monotonic() >= deadline:
                return "TIMED_OUT", None
            if health:
                if all(state.get("Running") is True and state.get("Health", {}).get("Status") == "healthy"
                       for state in states):
                    return "PASSED", 0
                if any(state.get("Status") in {"exited", "dead"}
                       or state.get("Health", {}).get("Status") == "unhealthy" for state in states):
                    return "NOT_RUN", None
            elif len(states) == 1 and states[0].get("Status") == "exited":
                exit_code = states[0].get("ExitCode")
                if type(exit_code) is not int:
                    raise _fail("COMPOSE_EXIT_UNKNOWN", cleanup=False)
                return ("PASSED" if exit_code == 0 else "FAILED"), exit_code
            if cancel is not None:
                cancel.wait(0.1)
            else:
                time.sleep(0.1)

    def run(self, snapshot: SourceSnapshot, *, request_id: str, profile_id: str,
            cancel: Event | None = None) -> RootlessTaskResult:
        try:
            if str(UUID(request_id)) != request_id or snapshot.request_id != request_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise _fail("COMPOSE_REQUEST_SCOPE_INVALID") from None
        self.require_running(request_id)
        profile = self.profile(profile_id)
        if cancel is not None and cancel.is_set():
            raise _fail("CANCELLED_BEFORE_START")
        if self.root.is_relative_to(snapshot.path) or snapshot.path.is_relative_to(self.root):
            raise _fail("COMPOSE_SOURCE_ROOT_OVERLAP")
        project, nonce = "cdr-compose-" + request_id, uuid4().hex
        try:
            _snapshot_tree(snapshot.path, snapshot.workload_tree)
            source = _source_config(snapshot, profile)
            daemon = self._daemon()
            image_ids = {reference: self._image(reference, profile.platform)
                         for reference in sorted({item["image"] for item in source["services"].values()})}
            code, version, _ = self._command(["compose", "version", "--short"])
            if code or not version.strip():
                raise _fail("COMPOSE_UNAVAILABLE")
            for kind in ("container", "network", "volume"):
                if self._inventory(kind, project):
                    raise _fail("COMPOSE_PROJECT_COLLISION", cleanup=False)
        except Exception as error:  # noqa: BLE001 -- these probes cannot have created resources.
            failure_code = str(getattr(error, "code", "COMPOSE_PREFLIGHT_FAILED"))
            raise _fail(failure_code, cleanup=failure_code != "COMPOSE_PROJECT_COLLISION") from None
        root = self.root / request_id
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            raise _fail("COMPOSE_REQUEST_REUSED", cleanup=False) from None
        intent_digest = _write_json(root / "intent.json", {
            "schema_version": "cdr-compose-intent/1", "request_id": request_id,
            "project": project, "nonce": nonce, "daemon_id": daemon, "image_ids": image_ids,
            "compose_version": version.strip().decode("ascii"), "profile_id": profile_id,
            "profile_revision_sha256": profile.revision_sha256,
            "source_sha256": snapshot.workload_snapshot_sha256,
            "source_path": str(snapshot.path), "source_tree": snapshot.workload_tree,
            "volume": str(VOLUME_BASE / request_id), "volume_mb": profile.volume_mb})
        def event(name: str, detail: dict[str, object]) -> None:
            self.require_running(request_id)
            self.record_event(request_id, name, detail)
        event("COMPOSE_VOLUME_INTENDED", {"run_id": request_id, "volume_mb": profile.volume_mb,
                                          "intent_sha256": intent_digest})
        try:
            volume = self._allocate(request_id, profile)
        except Exception as error:  # noqa: BLE001 -- allocation uncertainty must retain the lane.
            raise _fail(getattr(error, "code", "COMPOSE_VOLUME_ALLOCATION_UNCERTAIN"), cleanup=False) from None
        started = time.monotonic()
        try:
            return self._run_allocated(snapshot, profile, request_id, project, nonce, daemon, image_ids,
                                       version.strip().decode("ascii"), source, root, volume, event, cancel, started)
        except Exception as error:  # noqa: BLE001 -- callbacks may fail after an external mutation.
            # Any unproven mutation, evidence write or journal failure retains the
            # lane and quota mount. The immutable marker prevents automatic replay.
            raise _fail(getattr(error, "code", "COMPOSE_EXECUTION_UNCERTAIN"), cleanup=False) from None

    def _run_allocated(self, snapshot: SourceSnapshot, profile: ComposeProfile, request_id: str,
                       project: str, nonce: str, daemon: str, image_ids: dict[str, str], version: str,
                       source: dict[str, Any], root: Path, volume: Path,
                       event: Callable[[str, dict[str, object]], None], cancel: Event | None,
                       started: float) -> RootlessTaskResult:
        _write_json(root / "input.json", _compose_plan(source, profile, snapshot, project, nonce, volume))
        code, resolved, _ = self._command(self._compose(root / "input.json", project)
                                         + ["config", "--format", "json", "--no-interpolate"])
        if code:
            raise _fail("COMPOSE_CONFIG_INVALID", cleanup=False)
        canonical = json.loads(resolved)
        config = root / "resolved.json"
        config_digest = _write_json(config, canonical)
        config.chmod(0o400)
        compose = self._compose(config, project)
        event("COMPOSE_CREATE_INTENDED", {
            "project": project, "nonce": nonce, "daemon_id": daemon, "image_ids": image_ids,
            "profile_revision_sha256": profile.revision_sha256, "config_sha256": config_digest,
            "source_sha256": snapshot.workload_snapshot_sha256, "volume": str(volume)})
        self._daemon(daemon)
        create_code, _, _ = self._mutate(compose + ["create", "--no-build", "--pull", "never"],
                                          root=root, timeout=30, guard=lambda: self.require_running(request_id))
        containers, network = self._owned(project, request_id, nonce, profile, complete=create_code == 0)
        if create_code:
            raise _fail("COMPOSE_CREATE_FAILED", cleanup=False)
        self._verify_created(containers, profile, source, snapshot, volume, image_ids, project)
        identifiers = {name: str(item["Id"]) for name, item in containers.items()}
        event("COMPOSE_RESOURCES_CREATED", {"containers": identifiers, "network": network["Id"] if network else None})
        outcome, exit_code, error_code = "NOT_RUN", None, None
        test_started: bool | None = False
        test_start_intended = False
        phases: list[dict[str, object]] = []
        phase_deadline: float | None = None
        try:
            sequence = [("readiness", profile.ready_services, profile.readiness_seconds),
                        *(("migration", (name,), profile.migration_seconds) for name in profile.migration_services),
                        ("test", (profile.test_service,), profile.timeout_seconds)]
            for phase, names, timeout in sequence:
                if cancel is not None and cancel.is_set():
                    outcome, exit_code = "CANCELLED", None
                    break
                self._daemon(daemon)
                event("COMPOSE_PHASE_START_INTENDED", {"phase": phase, "services": list(names)})
                if phase == "test":
                    test_start_intended, test_started = True, None
                phase_deadline = time.monotonic() + timeout
                code, _, _ = self._mutate(compose + ["start", *names], root=root,
                                          timeout=min(15, timeout), guard=lambda: self.require_running(request_id))
                if code:
                    error_code = "COMPOSE_SERVICE_START_FAILED"
                    if phase == "test":
                        outcome = "UNKNOWN"
                    break
                if phase == "test":
                    test_started = True
                phase_outcome, phase_exit = self._wait({name: identifiers[name] for name in names},
                                                       health=phase == "readiness",
                                                       timeout=max(0, phase_deadline - time.monotonic()),
                                                       cancel=cancel, daemon=daemon)
                phases.append({"phase": phase, "services": list(names), "outcome": phase_outcome,
                               "exit_code": phase_exit})
                event("COMPOSE_PHASE_FINISHED", phases[-1])
                if phase == "test":
                    outcome, exit_code = phase_outcome, phase_exit
                elif phase_outcome != "PASSED":
                    outcome = phase_outcome if phase_outcome in {"TIMED_OUT", "CANCELLED"} else "NOT_RUN"
                    error_code = "COMPOSE_READINESS_FAILED" if phase == "readiness" else "COMPOSE_MIGRATION_FAILED"
                if phase_outcome != "PASSED":
                    break
        except Exception as error:  # noqa: BLE001 -- stop exact owned services even after journal failure.
            error_code = getattr(error, "code", "COMPOSE_PHASE_UNCERTAIN")
            outcome = "UNKNOWN" if test_start_intended else "NOT_RUN"
            if cancel is not None and cancel.is_set():
                outcome = "CANCELLED"
            elif phase_deadline is not None and time.monotonic() >= phase_deadline:
                outcome = "TIMED_OUT"
        self._daemon(daemon)
        owned, current_network = self._owned(project, request_id, nonce, profile, complete=True)
        if ({name: item["Id"] for name, item in owned.items()} != identifiers
                or current_network is None or network is None or current_network["Id"] != network["Id"]):
            raise _fail("COMPOSE_RESOURCE_IDENTITY_MISMATCH", cleanup=False)
        stop_code, _, _ = self._mutate(compose + ["stop", "--timeout", str(profile.cleanup_seconds)],
                                        root=root, timeout=profile.cleanup_seconds + 15,
                                        guard=lambda: self.require_running(request_id))
        stopped, stopped_network = self._owned(project, request_id, nonce, profile, complete=True)
        if ({name: item["Id"] for name, item in stopped.items()} != identifiers
                or stopped_network is None or stopped_network["Id"] != network["Id"]):
            raise _fail("COMPOSE_RESOURCE_IDENTITY_MISMATCH", cleanup=False)
        if stop_code or any(item.get("State", {}).get("Running") is not False for item in stopped.values()):
            raise _fail("COMPOSE_STOP_UNCERTAIN", cleanup=False)
        _verified_retired_volume(volume, request_id, profile.volume_mb * _MIB)
        logs_complete = True
        stdout_path, stderr_path = root / "stdout.bin", root / "stderr.bin"
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            os.fchmod(stdout.fileno(), 0o600)
            os.fchmod(stderr.fileno(), 0o600)
            for name in profile.services:
                code, out, err = self._command(["container", "logs", identifiers[name]],
                                               timeout=8, limit=profile.max_log_bytes, allow_truncate=True)
                stdout.write(("[" + name + "]\n").encode() + out)
                stderr.write(("[" + name + "]\n").encode() + err)
                logs_complete = logs_complete and code == 0
            stdout.flush()
            stderr.flush()
            os.fsync(stdout.fileno())
            os.fsync(stderr.fileno())
        specs = [OutputSpec(item.id, item.kind, item.path, item.format, item.required)
                 for item in profile.collect]
        collection = collect_outputs(volume / "out", specs, root / "outputs",
                                     max_file_bytes=profile.volume_mb * _MIB,
                                     max_total_bytes=profile.volume_mb * _MIB)
        details: dict[str, object] = {
            "schema_version": "cdr-compose-backend/1", "backend": "rootless_compose",
            "project": project, "request_id": request_id, "nonce": nonce, "daemon_id": daemon,
            "image": profile.image, "image_ids": image_ids, "compose_version": version,
            "resolved_config_sha256": config_digest, "source_compose_sha256": profile.compose_sha256,
            "profile_revision_sha256": profile.revision_sha256,
            "workload_sha256": snapshot.workload_snapshot_sha256,
            "test_started": test_started, "test_start_intended": test_start_intended,
            "phases": phases, "containers": identifiers,
            "network_id": network["Id"], "outcome": outcome, "exit_code": exit_code,
            "outputs": list(collection.outputs), "collection_complete": collection.complete,
            "collection_findings": list(collection.findings), "capture_complete": logs_complete,
            "log_retention": {"policy": "bounded-docker-tail", "original_history_complete": None,
                              "max_bytes_per_service": profile.max_log_bytes,
                              "max_files_per_service": 1},
            "limits": {"hard_volume_bytes": profile.volume_mb * _MIB,
                       "per_service_memory_bytes": profile.memory_mb * _MIB,
                       "per_service_cpu": profile.cpu_limit, "per_service_pids": profile.pids_limit},
            "stopped_containers": stopped,
            "error_code": error_code,
            "capture_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in (stdout_path, stderr_path)},
        }
        # Missing test reports are quality findings, not evidence loss. Unsafe
        # paths/copy failures retain the mount for explicit reconciliation.
        parser_findings = {f"{output_id}:{finding}"
                           for output_id, parsed in collection.parses.items()
                           for finding in parsed.findings}
        evidence_complete = collection.complete or (
            all(item["complete"] for item in collection.outputs)
            and all(finding in parser_findings
                    or finding.endswith((":REQUIRED_OUTPUT_MISSING", ":OPTIONAL_OUTPUT_MISSING"))
                    for finding in collection.findings))
        details["evidence_complete"] = evidence_complete
        evidence_digest = _write_json(root / "backend.json", details)
        event("COMPOSE_EVIDENCE_SEALED", {"sha256": evidence_digest})
        execution_os, _, execution_arch = profile.platform.partition("/")
        if not logs_complete or not evidence_complete:
            return RootlessTaskResult(outcome, exit_code, False, root / "outputs", stdout_path,
                                      stderr_path, logs_complete, time.monotonic() - started,
                                      profile.profile_id, profile.argv, profile.image, execution_os, execution_arch,
                                      details, "COMPOSE_EVIDENCE_INCOMPLETE")
        self._daemon(daemon)
        final_containers, final_network = self._owned(project, request_id, nonce, profile, complete=True)
        if ({name: item["Id"] for name, item in final_containers.items()} != identifiers
                or final_network is None or final_network["Id"] != network["Id"]):
            raise _fail("COMPOSE_RESOURCE_IDENTITY_MISMATCH", cleanup=False)
        event("COMPOSE_TEARDOWN_INTENDED", {"project": project, "containers": identifiers,
                                             "network": network["Id"]})
        code, _, _ = self._mutate(compose + ["down", "--volumes", "--timeout", str(profile.cleanup_seconds)],
                                   root=root, timeout=profile.cleanup_seconds + 15,
                                   guard=lambda: self.require_running(request_id))
        self._daemon(daemon)
        remaining, remaining_network = self._owned(project, request_id, nonce, profile)
        if code or remaining or remaining_network is not None:
            raise _fail("COMPOSE_CLEANUP_UNCERTAIN", cleanup=False)
        # An ID-specific inspect complements project-label enumeration: removing
        # labels can never masquerade as absence on the original daemon.
        for kind, identifier in [("container", value) for value in identifiers.values()] + [("network", network["Id"])]:
            code, _, _ = self._command([kind, "inspect", str(identifier)])
            if code == 0:
                raise _fail("COMPOSE_CLEANUP_UNCERTAIN", cleanup=False)
            # Docker error alone is insufficient: independently re-read its full inventory.
            args = [kind, "ls", "--quiet", "--no-trunc"] + (["--all"] if kind == "container" else [])
            list_code, raw, _ = self._command(args)
            inventory = raw.decode("ascii").splitlines()
            if list_code or any(not _SHA.fullmatch(item) for item in inventory) or str(identifier) in inventory:
                raise _fail("COMPOSE_CLEANUP_UNCERTAIN", cleanup=False)
        event("COMPOSE_RESOURCES_REMOVED", {"containers": identifiers, "network": network["Id"]})
        self._release(request_id, profile, volume)
        event("COMPOSE_VOLUME_RELEASED", {"run_id": request_id})
        _write_json(root / "cleanup.json", {"daemon_id": daemon, "confirmed": True,
                                            "containers": identifiers, "network": network["Id"]})
        return RootlessTaskResult(outcome, exit_code, True, root / "outputs", stdout_path, stderr_path,
                                  logs_complete, time.monotonic() - started, profile.profile_id,
                                  profile.argv, profile.image, execution_os, execution_arch, details, error_code)
