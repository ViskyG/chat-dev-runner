"""Fail-closed rootless Docker execution broker for frozen source snapshots.

Only a locally approved profile may enter this boundary.  This module never
uses the default Docker context: it talks to the current user's rootless socket
and verifies the daemon, image and *created* container before starting code.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any

from runner_app.outputs.capture import CaptureResult, capture_process

from .profiles import ApprovedProfile, ProfileRegistry

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_MOUNT_PATH = re.compile(r"/[A-Za-z0-9._/-]+\Z")
_MAX_SNAPSHOT_FILES = 20_000
_MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
_DOCKER_OUTPUT_LIMIT = 128 * 1024
_COMMAND_TIMEOUT = 8.0
_MIN_HOST_DISK_RESERVE = 1024 * 1024 * 1024
_MAX_RUN_VOLUME_BYTES = 4 * 1024 * 1024 * 1024


class BrokerError(RuntimeError):
    """A stable infrastructure refusal, never a project test failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class SnapshotInput:
    snapshot_id: str
    commit: str
    tree: str
    path: Path


@dataclass(frozen=True)
class RuntimeProbe:
    available: bool
    code: str
    socket_path: Path
    daemon_id: str | None
    server_version: str | None
    cgroup_version: str | None
    cgroup_driver: str | None
    delegated_controllers: tuple[str, ...]


@dataclass(frozen=True)
class BrokerResult:
    run_id: str
    snapshot_id: str
    snapshot_commit: str
    snapshot_tree: str
    snapshot_path: Path
    profile_id: str
    profile_revision_sha256: str
    image: str
    argv_sha256: str
    sanitized_argv: tuple[str, ...]
    started_at: str
    ended_at: str
    duration_seconds: float
    outcome: str
    exit_code: int | None
    output_root: Path
    capture: CaptureResult
    containment: Mapping[str, Any]

    @property
    def backend(self) -> str:
        return "rootless_docker"

    @property
    def image_digest(self) -> str:
        """Full digest-pinned image reference for the run evidence contract."""
        return self.image

    @property
    def image_sha256(self) -> str:
        return self.image.rsplit("@sha256:", 1)[1]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_argv(argv: tuple[str, ...]) -> str:
    framed = b"".join(len(arg.encode("utf-8")).to_bytes(8, "big") + arg.encode("utf-8")
                      for arg in argv)
    return hashlib.sha256(framed).hexdigest()


def _safe_path(path: Path, *, require_private_parent: bool = False) -> Path:
    if (not path.is_absolute() or ".." in path.parts
            or not _SAFE_MOUNT_PATH.fullmatch(str(path))):
        raise BrokerError("UNSAFE_MOUNT_PATH", "Container mount path is not a safe absolute path")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise BrokerError("MISSING_MOUNT_PATH", "Container mount source is absent") from exc
        if stat.S_ISLNK(info.st_mode):
            raise BrokerError("SYMLINK_MOUNT_PATH", "Container mount source traverses a symlink")
        if current == path and not stat.S_ISDIR(info.st_mode):
            raise BrokerError("UNSAFE_MOUNT_PATH", "Container mount source is not a directory")
    if require_private_parent:
        parent = path.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise BrokerError("UNSAFE_RUN_ROOT", "Run parent must be owner-only")
    return path


def _run_volume_budget(profile: ApprovedProfile) -> int:
    return min(max(profile.output_budget_mb * 4 * 1024 * 1024, 64 * 1024 * 1024),
               _MAX_RUN_VOLUME_BYTES)


def _verified_run_volume(volume: Path | None, run_id: str, budget: int) -> Path:
    """Require a dedicated, bounded filesystem before granting any write mount."""
    if volume is None:
        raise BrokerError("DISK_QUOTA_UNAVAILABLE", "A dedicated quota-limited run volume is required")
    source = _safe_path(volume, require_private_parent=True)
    if source.name != run_id or not os.path.ismount(source):
        raise BrokerError("DISK_QUOTA_UNAVAILABLE", "Run volume must be its own mountpoint")
    info = source.stat()
    parent = source.parent.stat()
    if (info.st_uid != os.getuid() or info.st_mode & 0o077
            or info.st_dev == parent.st_dev):
        raise BrokerError("DISK_QUOTA_UNAVAILABLE", "Run volume is not private and distinct")
    try:
        filesystem = os.statvfs(source)
        total_bytes = filesystem.f_blocks * filesystem.f_frsize
        available_bytes = filesystem.f_bavail * filesystem.f_frsize
        if (total_bytes < 16 * 1024 * 1024 or total_bytes > budget
                or available_bytes < total_bytes // 2
                or filesystem.f_files > 100_000 or any(source.iterdir())):
            raise BrokerError("DISK_QUOTA_UNAVAILABLE", "Run volume exceeds budget or is not empty")
    except OSError as exc:
        raise BrokerError("DISK_QUOTA_UNAVAILABLE", "Run volume capacity is unavailable") from exc
    return source


def _git_object_digest(kind: str, payload: bytes, length: int) -> bytes:
    header = f"{kind} {len(payload)}\0".encode("ascii")
    digest = hashlib.sha1() if length == 40 else hashlib.sha256()
    digest.update(header)
    digest.update(payload)
    return digest.digest()


def _file_blob_digest(fd: int, size: int, length: int) -> bytes:
    digest = hashlib.sha1() if length == 40 else hashlib.sha256()
    digest.update(f"blob {size}\0".encode("ascii"))
    remaining = size
    while remaining:
        chunk = os.read(fd, min(1024 * 1024, remaining))
        if not chunk:
            raise BrokerError("SNAPSHOT_CHANGED", "Snapshot file was truncated")
        digest.update(chunk)
        remaining -= len(chunk)
    if os.read(fd, 1):
        raise BrokerError("SNAPSHOT_CHANGED", "Snapshot file grew during verification")
    return digest.digest()


def _snapshot_tree(path: Path, expected_tree: str) -> str:
    """Verify materialized bytes against the immutable Git tree without filters/hooks."""
    if not _OID.fullmatch(expected_tree):
        raise BrokerError("INVALID_SNAPSHOT", "Snapshot tree ID is invalid")
    root = _safe_path(path)
    budget = {"files": 0, "bytes": 0}
    digest_length = len(expected_tree)

    def walk(directory: Path) -> bytes:
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o222:
            raise BrokerError("MUTABLE_SNAPSHOT", "Snapshot directory is not owner-owned read-only")
        entries: list[tuple[bytes, bytes]] = []
        with os.scandir(directory) as iterator:
            children = list(iterator)
        for child in children:
            name = os.fsencode(child.name)
            if name == b".git" or b"\x00" in name or b"/" in name:
                raise BrokerError("UNSAFE_SNAPSHOT", "Snapshot contains a protected path")
            child_path = directory / child.name
            child_info = child.stat(follow_symlinks=False)
            budget["files"] += 1
            if budget["files"] > _MAX_SNAPSHOT_FILES:
                raise BrokerError("SNAPSHOT_TOO_LARGE", "Snapshot file-count budget exceeded")
            if stat.S_ISDIR(child_info.st_mode):
                object_id = walk(child_path)
                mode = b"40000"
                sort_name = name + b"/"
            elif stat.S_ISLNK(child_info.st_mode):
                target = os.readlink(child_path)
                if os.path.isabs(target):
                    raise BrokerError("UNSAFE_SNAPSHOT", "Absolute source symlink is refused")
                try:
                    resolved = (child_path.parent / target).resolve(strict=False)
                except (OSError, RuntimeError) as exc:
                    raise BrokerError("UNSAFE_SNAPSHOT", "Source symlink is invalid") from exc
                if not resolved.is_relative_to(root):
                    raise BrokerError("UNSAFE_SNAPSHOT", "Source symlink escapes snapshot")
                payload = os.fsencode(target)
                budget["bytes"] += len(payload)
                object_id = _git_object_digest("blob", payload, digest_length)
                mode = b"120000"
                sort_name = name
            elif stat.S_ISREG(child_info.st_mode):
                if (child_info.st_uid != os.getuid() or child_info.st_mode & 0o222
                        or child_info.st_nlink != 1):
                    raise BrokerError("MUTABLE_SNAPSHOT", "Snapshot file is writable or linked")
                budget["bytes"] += child_info.st_size
                if budget["bytes"] > _MAX_SNAPSHOT_BYTES:
                    raise BrokerError("SNAPSHOT_TOO_LARGE", "Snapshot byte budget exceeded")
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(child_path, flags)
                try:
                    opened = os.fstat(fd)
                    if (opened.st_ino != child_info.st_ino or opened.st_dev != child_info.st_dev
                            or opened.st_size != child_info.st_size):
                        raise BrokerError("SNAPSHOT_CHANGED", "Snapshot changed during verification")
                    object_id = _file_blob_digest(fd, opened.st_size, digest_length)
                    finished = os.fstat(fd)
                    if (finished.st_ino != opened.st_ino or finished.st_dev != opened.st_dev
                            or finished.st_size != opened.st_size
                            or finished.st_mtime_ns != opened.st_mtime_ns):
                        raise BrokerError("SNAPSHOT_CHANGED", "Snapshot changed during verification")
                finally:
                    os.close(fd)
                mode = b"100755" if child_info.st_mode & 0o111 else b"100644"
                sort_name = name
            else:
                raise BrokerError("UNSAFE_SNAPSHOT", "Snapshot contains a special file")
            entries.append((sort_name, mode + b" " + name + b"\0" + object_id))
        payload = b"".join(entry for _, entry in sorted(entries, key=lambda item: item[0]))
        return _git_object_digest("tree", payload, digest_length)

    actual = walk(root).hex()
    if actual != expected_tree:
        raise BrokerError("SNAPSHOT_TREE_MISMATCH", "Materialized source does not match Git tree")
    return actual


class RootlessDockerBroker:
    """Finite execution on an explicitly verified rootless Docker daemon."""

    def __init__(self, registry: ProfileRegistry, *, docker_binary: str | None = None,
                 runtime_dir: Path | None = None) -> None:
        self.registry = registry
        executable = docker_binary or shutil.which("docker")
        self.docker_binary = str(Path(executable).resolve()) if executable else None
        uid = os.getuid()
        self.runtime_dir = runtime_dir or Path(f"/run/user/{uid}")
        self.socket_path = self.runtime_dir / "docker.sock"

    def _docker_env(self, private_home: Path | None = None) -> dict[str, str]:
        home = str(private_home) if private_home is not None else "/nonexistent"
        return {
            "PATH": "/usr/bin:/bin", "HOME": home, "DOCKER_CONFIG": home,
            "XDG_RUNTIME_DIR": str(self.runtime_dir), "LANG": "C.UTF-8",
        }

    def _docker_argv(self, *args: str) -> list[str]:
        if self.docker_binary is None:
            raise BrokerError("RUNTIME_UNAVAILABLE", "Docker CLI is not installed")
        return [self.docker_binary, "--host", f"unix://{self.socket_path}", *args]

    def _run_docker(self, *args: str, timeout: float = _COMMAND_TIMEOUT,
                    env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(self._docker_argv(*args), shell=False, stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=timeout,
                                    env=dict(env or self._docker_env()), check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BrokerError("RUNTIME_UNAVAILABLE", "Rootless Docker request failed") from exc
        if len(result.stdout) > _DOCKER_OUTPUT_LIMIT or len(result.stderr) > _DOCKER_OUTPUT_LIMIT:
            raise BrokerError("RUNTIME_RESPONSE_TOO_LARGE", "Docker response exceeded local limit")
        return result

    def _docker_json(self, *args: str) -> dict[str, Any]:
        result = self._run_docker(*args)
        if result.returncode != 0:
            raise BrokerError("RUNTIME_UNAVAILABLE", "Rootless Docker request was refused")
        try:
            value = json.loads(result.stdout.decode("utf-8", "strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrokerError("INVALID_RUNTIME_RESPONSE", "Docker returned malformed JSON") from exc
        if not isinstance(value, dict):
            raise BrokerError("INVALID_RUNTIME_RESPONSE", "Docker returned an unexpected response")
        return value

    def probe(self) -> RuntimeProbe:
        """Read-only capability probe; never contacts the default/rootful socket."""
        absent = RuntimeProbe(False, "RUNTIME_UNAVAILABLE", self.socket_path, None, None,
                              None, None, ())
        if self.docker_binary is None:
            return absent
        try:
            parent = self.runtime_dir.lstat()
            endpoint = self.socket_path.lstat()
        except OSError:
            return RuntimeProbe(False, "ROOTLESS_SOCKET_MISSING", self.socket_path, None, None,
                                None, None, ())
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or parent.st_mode & 0o077 or not stat.S_ISSOCK(endpoint.st_mode)
                or endpoint.st_uid != os.getuid()):
            return RuntimeProbe(False, "UNSAFE_ROOTLESS_SOCKET", self.socket_path, None, None,
                                None, None, ())
        try:
            info = self._docker_json("info", "--format", "{{json .}}")
        except BrokerError as exc:
            return RuntimeProbe(False, exc.code, self.socket_path, None, None, None, None, ())
        options = info.get("SecurityOptions")
        daemon_id = info.get("ID") if isinstance(info.get("ID"), str) else None
        version = info.get("ServerVersion") if isinstance(info.get("ServerVersion"), str) else None
        cgroup_version = str(info.get("CgroupVersion", ""))
        cgroup_driver = str(info.get("CgroupDriver", ""))
        controllers_path = Path(f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice"
                                f"/user@{os.getuid()}.service/cgroup.controllers")
        try:
            controllers = tuple(sorted(controllers_path.read_text(encoding="ascii").split()))
        except (OSError, UnicodeError):
            controllers = ()
        facts = (daemon_id, version, cgroup_version, cgroup_driver, controllers)
        if not isinstance(options, list) or "name=rootless" not in options:
            return RuntimeProbe(False, "ROOTFUL_DAEMON_REFUSED", self.socket_path, *facts)
        if info.get("OSType") != "linux" or cgroup_version != "2" or cgroup_driver != "systemd":
            return RuntimeProbe(False, "CGROUP_UNAVAILABLE", self.socket_path, *facts)
        if not {"cpu", "memory", "pids"}.issubset(controllers):
            return RuntimeProbe(False, "CGROUP_CONTROLLER_MISSING", self.socket_path, *facts)
        return RuntimeProbe(True, "READY", self.socket_path, *facts)

    def _image_id(self, profile: ApprovedProfile) -> str:
        image = self._docker_json("image", "inspect", profile.image, "--format", "{{json .}}")
        digests = image.get("RepoDigests")
        identifier = image.get("Id")
        architecture = image.get("Architecture")
        os_name = image.get("Os")
        if (not isinstance(digests, list) or profile.image not in digests
                or not isinstance(identifier, str) or not identifier.startswith("sha256:")
                or f"{os_name}/{architecture}" != profile.platform):
            raise BrokerError("IMAGE_NOT_APPROVED", "Installed image does not match pinned profile")
        return identifier

    @staticmethod
    def _validate_profile(profile: ApprovedProfile) -> None:
        # Rootless container UID 0 maps to the owner UID; other mappings need
        # explicit idmapped bind support before private 0700 roots can be used.
        if (profile.execution_backend != "rootless_container" or profile.network != "none"
                or not profile.image.endswith("@sha256:" + profile.image_digest)
                or len(profile.image_digest) != 64 or not profile.argv
                or profile.user != "0:0" or profile.timeout_seconds < 1
                or profile.memory_mb < 32
                or profile.pids_limit < 1 or not math.isfinite(profile.cpu_limit)
                or profile.cpu_limit <= 0 or profile.output_budget_mb < 1
                or profile.cwd != "/src" and not profile.cwd.startswith("/src/")):
            raise BrokerError("PROFILE_INVALID", "Approved profile has unsupported authority")
        expected = (("snapshot", "/src", True), ("build", "/build", False),
                    ("output", "/out", False), ("scratch", "/tmp", False))
        actual = tuple((m.source, m.destination, m.read_only) for m in profile.mounts)
        if actual != expected:
            raise BrokerError("PROFILE_INVALID", "Approved profile mount layout changed")

    def _create_argv(self, profile: ApprovedProfile, image_id: str, snapshot: Path,
                     writable_volume: Path, container_name: str) -> list[str]:
        host_mounts = ((snapshot, "/src", True),
                       (writable_volume / "build", "/build", False),
                       (writable_volume / "out", "/out", False),
                       (writable_volume / "tmp", "/tmp", False))
        argv = [
            "container", "create", "--name", container_name, "--pull", "never",
            "--attach", "stdout", "--attach", "stderr",
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--pids-limit", str(profile.pids_limit),
            "--memory", f"{profile.memory_mb}m", "--memory-swap", f"{profile.memory_mb}m",
            "--cpus", str(profile.cpu_limit), "--cgroupns", "private", "--ipc", "private",
            "--pid", "private", "--log-driver", "none", "--restart", "no",
            "--no-healthcheck", "--stop-signal", "SIGTERM", "--stop-timeout", "1",
            "--label", f"cdr.run-id={container_name.removeprefix('cdr-run-')}",
            "--label", f"cdr.profile-revision={profile.revision_sha256}",
            "--user", profile.user, "--workdir", profile.cwd, "--platform", profile.platform,
            "--env", "HOME=/tmp", "--env", "LANG=C.UTF-8",
            "--env", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", profile.argv[0],
        ]
        for source, destination, read_only in host_mounts:
            source = _safe_path(source)
            mount = f"type=bind,src={source},dst={destination},bind-propagation=rprivate"
            if read_only:
                mount += ",readonly,bind-recursive=disabled"
            argv.extend(("--mount", mount))
        argv.extend((profile.image, *profile.argv[1:]))
        if not image_id.startswith("sha256:"):
            raise BrokerError("IMAGE_NOT_APPROVED", "Image ID is invalid")
        return argv

    @staticmethod
    def _validate_created(container: Mapping[str, Any], profile: ApprovedProfile,
                          image_id: str, snapshot: Path, writable_volume: Path,
                          name: str) -> None:
        config = container.get("Config")
        host = container.get("HostConfig")
        state = container.get("State")
        mounts = container.get("Mounts")
        if not all(isinstance(value, dict) for value in (config, host, state)) or not isinstance(mounts, list):
            raise BrokerError("CONTAINER_POLICY_MISMATCH", "Container inspection is incomplete")
        assert isinstance(config, dict) and isinstance(host, dict) and isinstance(state, dict)
        if (container.get("Name") != "/" + name or state.get("Status") != "created"
                or container.get("Image") != image_id or config.get("Image") != profile.image
                or config.get("User") != profile.user
                or config.get("WorkingDir") != profile.cwd
                or config.get("Entrypoint") != [profile.argv[0]]
                or config.get("Cmd") != list(profile.argv[1:])
                or config.get("Tty") is not False
                or config.get("OpenStdin") is not False
                or config.get("AttachStdout") is not True
                or config.get("AttachStderr") is not True
                or config.get("Labels", {}).get("cdr.run-id") != name.removeprefix("cdr-run-")
                or config.get("Labels", {}).get("cdr.profile-revision") != profile.revision_sha256):
            raise BrokerError("CONTAINER_POLICY_MISMATCH", "Created container identity changed")
        security_options = host.get("SecurityOpt")
        cap_drop = host.get("CapDrop")
        if (host.get("Privileged") is not False or host.get("NetworkMode") != "none"
                or host.get("ReadonlyRootfs") is not True or host.get("CgroupnsMode") != "private"
                or host.get("PidMode") not in ("", "private")
                or host.get("IpcMode") != "private" or host.get("AutoRemove") is not False
                or host.get("Memory") != profile.memory_mb * 1024 * 1024
                or host.get("MemorySwap") != profile.memory_mb * 1024 * 1024
                or host.get("NanoCpus") != int(profile.cpu_limit * 1_000_000_000)
                or host.get("PidsLimit") != profile.pids_limit
                or not isinstance(cap_drop, list) or "ALL" not in cap_drop
                or not isinstance(security_options, list)
                or "no-new-privileges:true" not in security_options
                or host.get("RestartPolicy", {}).get("Name") != "no"
                or host.get("LogConfig", {}).get("Type") != "none"
                or host.get("CapAdd") not in (None, [])
                or host.get("Devices") not in (None, [])
                or host.get("DeviceRequests") not in (None, [])):
            raise BrokerError("CONTAINER_POLICY_MISMATCH", "Created container lost a containment limit")
        expected = {
            ("/src", str(snapshot), False),
            ("/build", str(writable_volume / "build"), True),
            ("/out", str(writable_volume / "out"), True),
            ("/tmp", str(writable_volume / "tmp"), True),
        }
        actual = set()
        for mount in mounts:
            if not isinstance(mount, dict) or mount.get("Type") != "bind":
                raise BrokerError("CONTAINER_POLICY_MISMATCH", "Unexpected container volume")
            actual.add((mount.get("Destination"), mount.get("Source"), mount.get("RW")))
        if actual != expected or len(mounts) != len(expected):
            raise BrokerError("CONTAINER_POLICY_MISMATCH", "Container mount set changed")
        if config.get("Healthcheck") not in (None, {}, {"Test": ["NONE"]}):
            raise BrokerError("CONTAINER_POLICY_MISMATCH", "Image healthcheck was not disabled")

    def _kill_container(self, name: str, signal_name: str) -> None:
        result = self._run_docker("container", "kill", "--signal", signal_name, name,
                                  timeout=3.0)
        if result.returncode != 0:
            inspected = self._docker_json("container", "inspect", name,
                                           "--format", "{{json .}}")
            state = inspected.get("State")
            if isinstance(state, dict) and state.get("Status") == "exited":
                return
            raise BrokerError("CONTAINER_KILL_FAILED", "Contained process could not be signalled")

    def _created_cleanup_id(self, name: str, profile: ApprovedProfile,
                            image_id: str, expected_id: str | None) -> str:
        """Resolve an uncertain create by name, without trusting that name alone for removal."""
        container = self._docker_json("container", "inspect", name,
                                      "--format", "{{json .}}")
        identifier = container.get("Id")
        config = container.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if (not isinstance(identifier, str) or not _CONTAINER_ID.fullmatch(identifier)
                or container.get("Name") != "/" + name
                or (expected_id is not None and identifier != expected_id)
                or container.get("Image") != image_id
                or not isinstance(config, dict) or config.get("Image") != profile.image
                or not isinstance(labels, dict)
                or labels.get("cdr.run-id") != name.removeprefix("cdr-run-")
                or labels.get("cdr.profile-revision") != profile.revision_sha256):
            raise BrokerError("CONTAINER_CLEANUP_UNCERTAIN",
                              "Created container identity could not be verified for cleanup")
        return identifier

    def run(self, profile: ApprovedProfile, snapshot: SnapshotInput, run_root: Path,
            *, run_id: str, writable_volume: Path | None = None,
            cancel: Event | None = None) -> BrokerResult:
        """Execute once; raised BrokerError means no tested-run attestation is issued."""
        try:
            canonical_run = str(uuid.UUID(run_id))
            canonical_snapshot = str(uuid.UUID(snapshot.snapshot_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise BrokerError("INVALID_RUN_ID", "Run or snapshot ID is invalid") from exc
        if canonical_run != run_id or canonical_snapshot != snapshot.snapshot_id:
            raise BrokerError("INVALID_RUN_ID", "Run or snapshot ID is not canonical")
        if (not _OID.fullmatch(snapshot.commit) or not _OID.fullmatch(snapshot.tree)
                or len(snapshot.commit) != len(snapshot.tree)):
            raise BrokerError("INVALID_SNAPSHOT", "Snapshot object identity is invalid")
        approved = self.registry.require(profile.profile_id)
        if approved != profile:
            raise BrokerError("PROFILE_CHANGED", "Profile changed since local approval")
        self._validate_profile(profile)
        probe = self.probe()
        if not probe.available:
            raise BrokerError(probe.code, "Rootless container runtime is unavailable")
        if cancel is not None and cancel.is_set():
            raise BrokerError("CANCELLED_BEFORE_START", "Run was cancelled before execution")
        budget_bytes = _run_volume_budget(profile)
        volume = _verified_run_volume(writable_volume, run_id, budget_bytes)
        image_id = self._image_id(profile)
        source = _safe_path(snapshot.path)
        _snapshot_tree(source, snapshot.tree)
        if run_root.name != run_id or run_root.exists():
            raise BrokerError("UNSAFE_RUN_ROOT", "Run root must be a fresh run-ID directory")
        _safe_path(run_root.parent)
        parent = run_root.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise BrokerError("UNSAFE_RUN_ROOT", "Run parent must be owner-only")
        if shutil.disk_usage(run_root.parent).free < budget_bytes + _MIN_HOST_DISK_RESERVE:
            raise BrokerError("DISK_RESERVE_LOW", "Host disk reserve is below execution policy")
        run_root.mkdir(mode=0o700)
        (run_root / "docker-home").mkdir(mode=0o700)
        for name in ("build", "out", "tmp"):
            (volume / name).mkdir(mode=0o700)
        container_name = "cdr-run-" + run_id
        create_args = self._create_argv(profile, image_id, source, volume, container_name)
        create_attempted = False
        verified_container_id: str | None = None
        reported_container_id: str | None = None
        container_ref = container_name
        process: subprocess.Popen[bytes] | None = None
        candidate: BrokerResult | None = None
        cleanup_error: BrokerError | None = None
        try:
            create_attempted = True
            create = self._run_docker(*create_args, timeout=30.0,
                                      env=self._docker_env(run_root / "docker-home"))
            if create.returncode != 0:
                raise BrokerError("CONTAINER_CREATE_FAILED", "Rootless container creation failed")
            try:
                created_id = create.stdout.decode("ascii", "strict").strip()
            except UnicodeDecodeError as exc:
                raise BrokerError("INVALID_RUNTIME_RESPONSE", "Docker returned an invalid container ID") from exc
            if not _CONTAINER_ID.fullmatch(created_id):
                raise BrokerError("INVALID_RUNTIME_RESPONSE", "Docker returned an invalid container ID")
            reported_container_id = created_id
            container_ref = created_id
            inspected = self._docker_json("container", "inspect", container_ref,
                                          "--format", "{{json .}}")
            self._validate_created(inspected, profile, image_id, source, volume,
                                   container_name)
            if inspected.get("Id") != container_ref:
                raise BrokerError("CONTAINER_POLICY_MISMATCH", "Container ID changed before start")
            verified_container_id = container_ref
            _snapshot_tree(source, snapshot.tree)
            if cancel is not None and cancel.is_set():
                raise BrokerError("CANCELLED_BEFORE_START", "Run was cancelled before execution")
            started_at = _utc_now()
            started = time.monotonic()
            try:
                process = subprocess.Popen(
                    self._docker_argv("container", "start", "--attach", container_ref),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=self._docker_env(run_root / "docker-home"), shell=False,
                    start_new_session=True,
                )
            except OSError as exc:
                raise BrokerError("CONTAINER_START_FAILED", "Rootless container failed to start") from exc

            def terminate() -> None:
                self._kill_container(container_ref, "SIGTERM")

            def kill() -> None:
                try:
                    self._kill_container(container_ref, "SIGKILL")
                finally:
                    if process is not None and process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

            budget = profile.output_budget_mb * 1024 * 1024
            checked_at = 0.0

            def observe() -> str | None:
                nonlocal checked_at
                now = time.monotonic()
                if now - checked_at < 0.25:
                    return None
                checked_at = now
                if shutil.disk_usage(run_root.parent).free < _MIN_HOST_DISK_RESERVE:
                    return "HOST_DISK_RESERVE"
                filesystem = os.statvfs(volume)
                if filesystem.f_bavail * filesystem.f_frsize < 1024 * 1024:
                    return "RUN_VOLUME_FULL"
                return None

            capture = capture_process(
                process, run_root / "capture", timeout_seconds=float(profile.timeout_seconds),
                max_stream_bytes=min(budget, 16 * 1024 * 1024), max_total_bytes=budget,
                terminate=terminate, kill=kill, cancel=cancel, observe=observe,
            )
            ended_at = _utc_now()
            duration = round(time.monotonic() - started, 6)
            final = self._docker_json("container", "inspect", container_ref,
                                      "--format", "{{json .}}")
            final_state = final.get("State")
            if final.get("Id") != container_ref:
                raise BrokerError("CONTAINER_EXIT_UNKNOWN", "Container ID changed during execution")
            if (not isinstance(final_state, dict) or final_state.get("Status") != "exited"
                    or not isinstance(final_state.get("ExitCode"), int)
                    or final_state.get("Error")):
                raise BrokerError("CONTAINER_EXIT_UNKNOWN", "Container exit could not be verified")
            attach_exit_mismatch = (capture.termination_reason is None
                                    and process.returncode != final_state["ExitCode"])
            if attach_exit_mismatch and capture.capture_fault is None:
                raise BrokerError("CONTAINER_EXIT_UNKNOWN", "Docker CLI and container exit disagree")
            _snapshot_tree(source, snapshot.tree)
            # The final container inspect establishes its exit code. A capture fault
            # must not discard retained raw bytes; an uncertain attach/termination
            # still has an UNKNOWN execution outcome and no publication authority.
            outcome = "UNKNOWN" if attach_exit_mismatch else capture.outcome_hint
            containment: dict[str, Any] = {
                "verified": True, "backend": "rootless_docker", "socket_path": str(probe.socket_path),
                "daemon_id": probe.daemon_id, "daemon_rootless": True,
                "server_version": probe.server_version, "cgroup_version": probe.cgroup_version,
                "cgroup_driver": probe.cgroup_driver,
                "delegated_controllers": list(probe.delegated_controllers),
                "image_id": image_id, "image_repo_digest": profile.image,
                "network": "none", "read_only_source": True, "read_only_root": True,
                "capabilities_dropped": "ALL", "no_new_privileges": True,
                "memory_bytes": profile.memory_mb * 1024 * 1024,
                "pids_limit": profile.pids_limit, "cpu_nano": int(profile.cpu_limit * 1_000_000_000),
                "mounts_verified_before_start": True, "cleanup_verified": True,
                "run_volume_hard_limit_bytes": os.statvfs(volume).f_blocks * os.statvfs(volume).f_frsize,
            }
            candidate = BrokerResult(
                run_id, snapshot.snapshot_id, snapshot.commit, snapshot.tree, source,
                profile.profile_id, profile.revision_sha256, profile.image,
                _sha256_argv(profile.argv), ("<approved-profile>", profile.profile_id),
                started_at, ended_at, duration,
                outcome, final_state["ExitCode"], volume / "out", capture, containment,
            )
        finally:
            if create_attempted:
                cleanup_code = ("CONTAINER_CLEANUP_FAILED" if verified_container_id is not None
                                else "CONTAINER_CLEANUP_UNCERTAIN")
                try:
                    cleanup_id = (verified_container_id or self._created_cleanup_id(
                        container_name, profile, image_id, reported_container_id))
                    removed = self._run_docker("container", "rm", "--force", "--volumes",
                                               cleanup_id, timeout=8.0,
                                               env=self._docker_env(run_root / "docker-home"))
                    if removed.returncode != 0:
                        raise BrokerError(cleanup_code,
                                          "Contained process cleanup was not verified")
                except BrokerError as error:
                    cleanup_error = BrokerError(cleanup_code,
                                                "Contained process cleanup was not verified")
                    cleanup_error.__cause__ = error
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    cleanup_error = BrokerError("CONTAINER_CLEANUP_FAILED",
                                                "Docker attach process could not be reaped")
            if cleanup_error is not None:
                raise cleanup_error
        if candidate is None:
            raise BrokerError("EXECUTION_UNKNOWN", "Contained execution did not produce an attestation")
        return candidate
