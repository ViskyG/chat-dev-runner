"""Exact-resource Compose crash reconciliation. This module never starts workloads.

Only the startup owner holding the retained execution lane may call this port.
Each recovery capture is append-only; a sealed capture is reused after a second
crash, including partial resource removal and an unacknowledged quota release.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from runner_app.git.source_v2 import SourceSnapshot
from runner_app.outputs.collection import OutputSpec, collect_outputs
from runner_app.storage.paths import ensure_private_directory

from . import compose_v2 as compose
from .broker import _snapshot_tree
from .profiles import _constant, _pairs
from .rootless_v2 import RootlessError, RootlessTaskResult, _write_json
from .volume_provider import HELPER, VOLUME_BASE, _verified_retired_volume

if TYPE_CHECKING:
    from .compose_v2 import ComposeTaskBackend


def _fail(code: str = "COMPOSE_RECOVERY_EVIDENCE_INVALID") -> RootlessError:
    return RootlessError(code, cleanup_verified=False)


def _file(path: Path, limit: int) -> tuple[bytes, str]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > limit):
            raise _fail()
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise _fail()
    return raw, hashlib.sha256(raw).hexdigest()


def _record(path: Path) -> tuple[dict[str, Any], str]:
    raw, digest = _file(path, 4 * compose._MIB)
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    if not isinstance(value, dict):
        raise _fail()
    return value, digest


def _hash(path: Path, limit: int) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > limit):
            raise _fail()
        digest, count = hashlib.sha256(), 0
        while chunk := stream.read(65536):
            count += len(chunk)
            if count > limit:
                raise _fail()
            digest.update(chunk)
        if count != info.st_size:
            raise _fail()
    return digest.hexdigest()


def _helpers_absent(request_id: str) -> None:
    """A privileged quota call may outlive its caller; never race its exact UUID."""
    entries = list(Path("/proc").iterdir())
    if len(entries) > 65536:
        raise _fail("COMPOSE_CONTROL_NOT_QUIESCENT")
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            with (entry / "cmdline").open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise _fail("COMPOSE_CONTROL_NOT_QUIESCENT")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            raise _fail("COMPOSE_CONTROL_NOT_QUIESCENT") from None
        tokens = raw.split(b"\0")
        if str(HELPER).encode() in tokens and request_id.encode() in tokens:
            raise _fail("COMPOSE_CONTROL_NOT_QUIESCENT")


class _Recovery:
    def __init__(self, backend: ComposeTaskBackend, snapshot: SourceSnapshot, request: str,
                 profile_id: str, journal: Sequence[tuple[str, Mapping[str, Any]]]) -> None:
        self.backend, self.snapshot, self.request = backend, snapshot, request
        if str(UUID(request)) != request or snapshot.request_id != request or len(journal) > 10000:
            raise _fail("COMPOSE_REQUEST_SCOPE_INVALID")
        self.guard()
        self.profile = backend.profile(profile_id)
        _snapshot_tree(snapshot.path, snapshot.workload_tree)
        self.source = compose._source_config(snapshot, self.profile)
        self.root = backend.root / request
        self.volume = VOLUME_BASE / request
        self.project = "cdr-compose-" + request
        self.events = [(name, dict(detail)) for name, detail in journal if name.startswith("COMPOSE_")]
        self.started = time.monotonic()
        self.identifiers: dict[str, str] = {}
        self.network_id: str | None = None
        self.nonce = self.daemon = ""
        self.intent: dict[str, Any] = {}
        self.config_digest: str | None = None
        self.sealed_root: Path | None = None
        self.sealed: dict[str, Any] | None = None
        self.cleanup_intended = False

    def guard(self) -> None:
        if self.backend.require_recovering is None:
            raise _fail("COMPOSE_RECOVERY_NOT_AUTHORIZED")
        self.backend.require_recovering(self.request)

    def event(self, name: str, detail: dict[str, object]) -> None:
        self.guard()
        self.backend.record_event(self.request, name, detail)

    def one(self, name: str) -> dict[str, Any] | None:
        records = [detail for event, detail in self.events if event == name]
        if len(records) > 1 and any(item != records[0] for item in records):
            raise _fail()
        return records[-1] if records else None

    def controls_absent(self) -> None:
        _helpers_absent(self.request)
        directory = self.root / "control"
        if not directory.exists():
            # Legacy runs have no process leases. Do not guess if creation/start
            # could have been in flight when the controller disappeared.
            if self.one("COMPOSE_CREATE_INTENDED") and not self.one("COMPOSE_EVIDENCE_SEALED"):
                raise _fail("COMPOSE_CONTROL_PROOF_MISSING")
            return
        ensure_private_directory(directory)
        leases = list(directory.iterdir())
        if len(leases) > 128:
            raise _fail()
        for lease in leases:
            if not re.fullmatch(r"[0-9a-f]{32}", lease.name):
                raise _fail()
            ensure_private_directory(lease)
            intent, _ = _record(lease / "intent.json")
            argv = intent.get("argv")
            if not isinstance(argv, list) or not argv or any(not isinstance(x, str) for x in argv):
                raise _fail()
            if (lease / "complete.json").exists():
                complete, _ = _record(lease / "complete.json")
                if type(complete.get("exit_code")) is not int:
                    raise _fail()
            if not (lease / "process.json").exists():
                raise _fail("COMPOSE_CONTROL_PROOF_MISSING")
            process, _ = _record(lease / "process.json")
            pid, start = process.get("pid"), process.get("start")
            if type(pid) is not int or pid < 1 or not isinstance(start, str) or not start.isdecimal():
                raise _fail()
            self.backend._control_group_absent(pid, start)

    def identity(self) -> None:
        self.guard()
        ensure_private_directory(self.root)
        volume_event = self.one("COMPOSE_VOLUME_INTENDED")
        create = self.one("COMPOSE_CREATE_INTENDED")
        if (self.root / "intent.json").exists():
            self.intent, intent_digest = _record(self.root / "intent.json")
            if volume_event and volume_event.get("intent_sha256", intent_digest) != intent_digest:
                raise _fail()
            expected = {"schema_version": "cdr-compose-intent/1", "request_id": self.request,
                        "project": self.project, "profile_id": self.profile.profile_id,
                        "profile_revision_sha256": self.profile.revision_sha256,
                        "source_sha256": self.snapshot.workload_sha256,
                        "source_path": str(self.snapshot.path), "source_tree": self.snapshot.workload_tree,
                        "volume": str(self.volume), "volume_mb": self.profile.volume_mb}
            if any(self.intent.get(key) != value for key, value in expected.items()):
                raise _fail()
        elif create is not None:
            self.intent = dict(create)
        elif volume_event is None:
            # The normal path commits volume intent before invoking its helper.
            # A crash during read-only preflight therefore has no backend effects.
            if self.volume.exists() or self.volume.is_symlink():
                raise _fail("COMPOSE_RECOVERY_RESOURCE_UNEXPECTED")
            daemon = self.backend._daemon()
            if any(self.backend._inventory(kind, self.project) for kind in ("container", "network", "volume")):
                raise _fail("COMPOSE_RECOVERY_RESOURCE_UNEXPECTED")
            self.intent = {"schema_version": "cdr-compose-intent/1", "request_id": self.request,
                "project": self.project, "profile_id": self.profile.profile_id,
                "profile_revision_sha256": self.profile.revision_sha256,
                "source_sha256": self.snapshot.workload_sha256, "source_path": str(self.snapshot.path),
                "source_tree": self.snapshot.workload_tree, "volume": str(self.volume),
                "volume_mb": self.profile.volume_mb, "nonce": uuid4().hex, "daemon_id": daemon,
                "image_ids": {}, "recovery_origin": "read_only_preflight"}
            self.guard()
            _write_json(self.root / "intent.json", self.intent)
        else:
            raise _fail("COMPOSE_RECOVERY_IDENTITY_MISSING")
        self.nonce, self.daemon = self.intent["nonce"], self.intent["daemon_id"]
        if (not isinstance(self.nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", self.nonce)
                or not isinstance(self.daemon, str) or not self.daemon):
            raise _fail()
        if (self.intent.get("project") != self.project or self.intent.get("volume") != str(self.volume)
                or self.intent.get("source_sha256") != self.snapshot.workload_sha256
                or self.intent.get("profile_revision_sha256") != self.profile.revision_sha256):
            raise _fail()
        images = self.intent.get("image_ids")
        expected_images = {item["image"] for item in self.source["services"].values()}
        if self.intent.get("recovery_origin") == "read_only_preflight":
            if volume_event is not None or create is not None:
                raise _fail()
            expected_images = set()
        if (not isinstance(images, dict) or set(images) != expected_images
                or any(not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value)
                       for value in images.values())):
            raise _fail()
        if create:
            for key in ("project", "nonce", "daemon_id", "image_ids", "profile_revision_sha256",
                        "source_sha256", "volume"):
                if create.get(key) != self.intent.get(key):
                    raise _fail()
            _, self.config_digest = _record(self.root / "resolved.json")
            if self.config_digest != create.get("config_sha256"):
                raise _fail()
            plan, _ = _record(self.root / "input.json")
            if plan != compose._compose_plan(self.source, self.profile, self.snapshot,
                                            self.project, self.nonce, self.volume):
                raise _fail()
        self.backend._daemon(self.daemon)
        self.controls_absent()
        created = self.one("COMPOSE_RESOURCES_CREATED")
        adopted_path = self.root / "recovery-owned.json"
        adopted = _record(adopted_path)[0] if adopted_path.exists() else None
        if adopted and (adopted.get("request_id") != self.request or adopted.get("nonce") != self.nonce):
            raise _fail()
        if created and adopted and any(created.get(key) != adopted.get(key) for key in ("containers", "network")):
            raise _fail()
        ownership = created or adopted
        if ownership:
            self.identifiers, self.network_id = ownership["containers"], ownership["network"]
            if (not isinstance(self.identifiers, dict) or set(self.identifiers) - set(self.profile.services)
                    or any(not isinstance(value, str) or not compose._SHA.fullmatch(value)
                           for value in self.identifiers.values())
                    or self.network_id is not None and (
                        not isinstance(self.network_id, str) or not compose._SHA.fullmatch(self.network_id))):
                raise _fail()
        self.cleanup_intended = bool(self.one("COMPOSE_TEARDOWN_INTENDED")
                                     or (self.root / "recovery-cleanup-intent.json").exists())
        if (self.root / "recovery-cleanup-intent.json").exists():
            cleanup, _ = _record(self.root / "recovery-cleanup-intent.json")
            if cleanup != {"request_id": self.request, "nonce": self.nonce,
                           "containers": self.identifiers, "network": self.network_id}:
                raise _fail()

    def inventory(self, *, adopting: bool = False) -> tuple[dict[str, Any], dict[str, Any] | None]:
        self.guard()
        self.backend._daemon(self.daemon)
        containers, network = self.backend._owned(self.project, self.request, self.nonce, self.profile)
        self.backend._verify_created(containers, self.profile, self.source, self.snapshot,
                                     self.volume, self.intent["image_ids"], self.project, recovering=True)
        if not adopting:
            if (any(self.identifiers.get(name) != item["Id"] for name, item in containers.items())
                    or network is not None and network["Id"] != self.network_id):
                raise _fail("COMPOSE_RESOURCE_IDENTITY_MISMATCH")
            for kind, expected, present in (
                ("container", set(self.identifiers.values()), {item["Id"] for item in containers.values()}),
                ("network", {self.network_id} if self.network_id else set(), {network["Id"]} if network else set()),
            ):
                missing = expected - present
                if missing:
                    code, raw, _ = self.backend._command(
                        [kind, "ls", "--quiet", "--no-trunc"] + (["--all"] if kind == "container" else []))
                    all_ids = raw.decode("ascii").splitlines()
                    if code or any(not compose._SHA.fullmatch(item) for item in all_ids) or missing.intersection(all_ids):
                        raise _fail("COMPOSE_RESOURCE_IDENTITY_MISMATCH")
                    if not self.cleanup_intended:
                        raise _fail("COMPOSE_RECOVERY_RESOURCE_MISSING")
        return containers, network

    def seal_existing(self) -> bool:
        candidates = [self.root]
        attempts = sorted(self.root.glob("recovery-[0-9][0-9][0-9][0-9]"))
        if len(attempts) > 16:
            raise _fail("COMPOSE_RECOVERY_BUDGET_EXCEEDED")
        candidates.extend(attempts)
        for directory in reversed(candidates):
            if not (directory / "backend.json").exists():
                continue
            ensure_private_directory(directory)
            value, digest = _record(directory / "backend.json")
            for key, expected in {"request_id": self.request, "project": self.project, "nonce": self.nonce,
                                  "daemon_id": self.daemon, "profile_revision_sha256": self.profile.revision_sha256,
                                  "workload_sha256": self.snapshot.workload_sha256,
                                  "source_compose_sha256": self.profile.compose_sha256,
                                  "image_ids": self.intent["image_ids"], "containers": self.identifiers,
                                  "network_id": self.network_id}.items():
                if value.get(key) != expected:
                    raise _fail()
            normal_seal = self.one("COMPOSE_EVIDENCE_SEALED") if directory == self.root else None
            if normal_seal and normal_seal.get("sha256") != digest:
                raise _fail()
            for event, detail in self.events:
                if (event == "COMPOSE_RECOVERY_EVIDENCE_SEALED" and detail.get("directory") == directory.name
                        and detail.get("sha256") != digest):
                    raise _fail()
            if value.get("recovery", {}).get("interrupted") and value.get("outcome") not in {"UNKNOWN", "NOT_RUN"}:
                raise _fail()
            if not value.get("capture_complete") or not value.get("evidence_complete", value.get("collection_complete")):
                raise _fail("COMPOSE_RECOVERY_EVIDENCE_INCOMPLETE")
            captures = value.get("capture_sha256", {})
            if set(captures) != {"stdout.bin", "stderr.bin"}:
                raise _fail("COMPOSE_RECOVERY_EVIDENCE_UNVERIFIED")
            for name, expected in captures.items():
                if _hash(directory / name, 2 * len(self.profile.services) * (self.profile.max_log_bytes + 128)) != expected:
                    raise _fail()
            outputs = value.get("outputs")
            if not isinstance(outputs, list) or len(outputs) > 256:
                raise _fail()
            ensure_private_directory(directory / "outputs")
            for output in outputs:
                relative = output["relative_path"]
                if (not isinstance(relative, str) or len(Path(relative).parts) != 2
                        or Path(relative).parts[0] != "outputs" or Path(relative).name in {".", ".."}
                        or not output.get("complete")
                        or _hash(directory / relative, self.profile.volume_mb * compose._MIB) != output["sha256"]):
                    raise _fail()
            if value.get("outcome") not in {"PASSED", "FAILED", "NOT_RUN", "UNKNOWN", "TIMED_OUT", "CANCELLED"}:
                raise _fail()
            self.sealed, self.sealed_root = value, directory
            return True
        return False

    def capture(self, containers: dict[str, Any], network: dict[str, Any] | None) -> None:
        # Without a sealed copy, a missing original container would lose its log.
        if {name: item["Id"] for name, item in containers.items()} != self.identifiers:
            raise _fail("COMPOSE_RECOVERY_EVIDENCE_INCOMPLETE")
        attempts = list(self.root.glob("recovery-[0-9][0-9][0-9][0-9]"))
        if len(attempts) >= 16:
            raise _fail("COMPOSE_RECOVERY_BUDGET_EXCEEDED")
        directory = self.root / f"recovery-{len(attempts) + 1:04d}"
        self.guard()
        directory.mkdir(mode=0o700)
        capture_hashes = {}
        for channel in ("stdout", "stderr"):
            (directory / (channel + ".bin")).touch(mode=0o600, exist_ok=False)
        for name, item in containers.items():
            code, stdout, stderr = self.backend._command(["container", "logs", item["Id"]],
                timeout=8, limit=self.profile.max_log_bytes, allow_truncate=True)
            if code:
                raise _fail("COMPOSE_RECOVERY_EVIDENCE_INCOMPLETE")
            for channel, raw in (("stdout", stdout), ("stderr", stderr)):
                with (directory / (channel + ".bin")).open("ab") as stream:
                    stream.write(("[" + name + "]\n").encode() + raw)
                    stream.flush()
                    os.fsync(stream.fileno())
        for channel in ("stdout", "stderr"):
            path = directory / (channel + ".bin")
            capture_hashes[path.name] = _hash(path, 2 * len(self.profile.services) * (self.profile.max_log_bytes + 128))
        if self.volume.exists():
            _verified_retired_volume(self.volume, self.request, self.profile.volume_mb * compose._MIB)
            output_source = self.volume / "out"
            if not output_source.exists() and not self.one("COMPOSE_CREATE_INTENDED"):
                output_source = directory / "empty"
                output_source.mkdir(mode=0o700)
        else:
            if containers or self.one("COMPOSE_CREATE_INTENDED"):
                raise _fail("COMPOSE_RECOVERY_VOLUME_MISSING")
            output_source = directory / "empty"
            output_source.mkdir(mode=0o700)
        specs = [OutputSpec(item.id, item.kind, item.path, item.format, item.required) for item in self.profile.collect]
        collection = collect_outputs(output_source, specs, directory / "outputs",
                                     max_file_bytes=self.profile.volume_mb * compose._MIB,
                                     max_total_bytes=self.profile.volume_mb * compose._MIB)
        parser_findings = {f"{identifier}:{finding}" for identifier, parsed in collection.parses.items()
                           for finding in parsed.findings}
        complete = collection.complete or (all(item["complete"] for item in collection.outputs)
            and all(finding in parser_findings or finding.endswith((":REQUIRED_OUTPUT_MISSING", ":OPTIONAL_OUTPUT_MISSING"))
                    for finding in collection.findings))
        if not complete:
            raise _fail("COMPOSE_RECOVERY_EVIDENCE_INCOMPLETE")
        test_intended = any(event == "COMPOSE_PHASE_START_INTENDED" and detail.get("phase") == "test"
                            for event, detail in self.events)
        test = containers.get(self.profile.test_service)
        test_never_started = not test_intended and (test is None or test.get("State", {}).get("Status") == "created")
        phases = [detail for event, detail in self.events if event == "COMPOSE_PHASE_FINISHED"]
        value = {"schema_version": "cdr-compose-backend/1", "backend": "rootless_compose",
            "request_id": self.request, "project": self.project, "nonce": self.nonce,
            "daemon_id": self.daemon, "image_ids": self.intent["image_ids"], "image": self.profile.image,
            "profile_revision_sha256": self.profile.revision_sha256,
            "source_compose_sha256": self.profile.compose_sha256, "resolved_config_sha256": self.config_digest,
            "workload_sha256": self.snapshot.workload_sha256, "containers": self.identifiers,
            "network_id": self.network_id, "stopped_containers": containers,
            "test_started": False if test_never_started else None, "test_start_intended": test_intended,
            "outcome": "NOT_RUN" if test_never_started else "UNKNOWN", "exit_code": None,
            "phases": phases, "recovery": {"interrupted": True, "workload_restarted": False},
            "outputs": list(collection.outputs), "collection_findings": list(collection.findings),
            "collection_complete": collection.complete, "evidence_complete": complete,
            "capture_complete": True, "capture_sha256": capture_hashes,
            "log_retention": {"policy": "bounded-docker-tail", "original_history_complete": None},
            "error_code": "COMPOSE_RECOVERED_AFTER_INTERRUPTION"}
        self.guard()
        digest = _write_json(directory / "backend.json", value)
        self.event("COMPOSE_RECOVERY_EVIDENCE_SEALED", {"directory": directory.name, "sha256": digest})
        self.sealed, self.sealed_root = value, directory

    def execute(self) -> RootlessTaskResult:
        self.identity()
        ownership_known = bool(self.identifiers or self.network_id or (self.root / "recovery-owned.json").exists())
        containers, network = self.inventory(adopting=not ownership_known)
        if not ownership_known:
            if (containers or network) and not self.one("COMPOSE_CREATE_INTENDED"):
                raise _fail("COMPOSE_RECOVERY_RESOURCE_UNEXPECTED")
            self.identifiers = {name: item["Id"] for name, item in containers.items()}
            self.network_id = network["Id"] if network else None
            self.guard()
            _write_json(self.root / "recovery-owned.json", {"request_id": self.request, "nonce": self.nonce,
                        "containers": self.identifiers, "network": self.network_id})
            self.event("COMPOSE_RECOVERY_RESOURCES_CONFIRMED", {
                "containers": self.identifiers, "network": self.network_id})
        sealed = self.seal_existing()
        if sealed and any(item.get("State", {}).get("Running") for item in containers.values()):
            raise _fail("COMPOSE_RECOVERY_SEALED_RESOURCE_RUNNING")
        if not sealed:
            running = [item["Id"] for item in containers.values() if item.get("State", {}).get("Running") is True]
            if running:
                self.controls_absent()
                code, _, _ = self.backend._mutate(
                    ["container", "stop", "--time", str(self.profile.cleanup_seconds), *running],
                    root=self.root, timeout=self.profile.cleanup_seconds + 15, guard=self.guard)
                if code:
                    raise _fail("COMPOSE_STOP_UNCERTAIN")
                containers, network = self.inventory()
            if any(item.get("State", {}).get("Running") is not False for item in containers.values()):
                raise _fail("COMPOSE_STOP_UNCERTAIN")
            self.capture(containers, network)
        assert self.sealed is not None and self.sealed_root is not None
        if not self.cleanup_intended:
            self.guard()
            _write_json(self.root / "recovery-cleanup-intent.json", {
                "request_id": self.request, "nonce": self.nonce, "containers": self.identifiers,
                "network": self.network_id})
            self.cleanup_intended = True
            self.event("COMPOSE_RECOVERY_TEARDOWN_INTENDED", {"containers": self.identifiers,
                                                            "network": self.network_id})
        self.controls_absent()
        containers, network = self.inventory()
        if containers:
            code, _, _ = self.backend._mutate(["container", "rm", *[item["Id"] for item in containers.values()]],
                                              root=self.root, timeout=15, guard=self.guard)
            if code:
                raise _fail("COMPOSE_CLEANUP_UNCERTAIN")
        containers, network = self.inventory()
        if containers:
            raise _fail("COMPOSE_CLEANUP_UNCERTAIN")
        if network is not None:
            code, _, _ = self.backend._mutate(["network", "rm", network["Id"]], root=self.root,
                                              timeout=15, guard=self.guard)
            if code:
                raise _fail("COMPOSE_CLEANUP_UNCERTAIN")
        containers, network = self.inventory()
        if containers or network is not None:
            raise _fail("COMPOSE_CLEANUP_UNCERTAIN")
        self.event("COMPOSE_RECOVERY_RESOURCES_REMOVED", {"containers": self.identifiers, "network": self.network_id})
        self.controls_absent()
        if self.volume.exists():
            self.guard()
            _verified_retired_volume(self.volume, self.request, self.profile.volume_mb * compose._MIB)
            self.backend._release(self.request, self.profile, self.volume)
        if os.path.ismount(self.volume) or self.volume.exists() or self.volume.is_symlink():
            raise _fail("COMPOSE_VOLUME_CLEANUP_UNCERTAIN")
        self.event("COMPOSE_RECOVERY_VOLUME_RELEASED", {"run_id": self.request})
        cleanup_path = self.sealed_root / "recovery-cleanup.json"
        cleanup = {"request_id": self.request, "daemon_id": self.daemon, "confirmed": True,
                   "containers": self.identifiers, "network": self.network_id}
        self.guard()
        if cleanup_path.exists():
            if _record(cleanup_path)[0] != cleanup:
                raise _fail()
        else:
            _write_json(cleanup_path, cleanup)
        execution_os, _, execution_arch = self.profile.platform.partition("/")
        return RootlessTaskResult(self.sealed["outcome"], self.sealed.get("exit_code"), True,
            self.sealed_root / "outputs", self.sealed_root / "stdout.bin", self.sealed_root / "stderr.bin",
            True, time.monotonic() - self.started, self.profile.profile_id, self.profile.argv,
            self.profile.image, execution_os, execution_arch, self.sealed,
            self.sealed.get("error_code") or "COMPOSE_RECOVERED_AFTER_INTERRUPTION")


def recover(backend: ComposeTaskBackend, snapshot: SourceSnapshot, *, request_id: str,
            profile_id: str, journal: Sequence[tuple[str, Mapping[str, Any]]]) -> RootlessTaskResult:
    try:
        return _Recovery(backend, snapshot, request_id, profile_id, journal).execute()
    except RootlessError as error:
        raise _fail(error.code) from None
    except Exception:  # noqa: BLE001 -- malformed evidence never grants cleanup or replay authority.
        raise _fail() from None
