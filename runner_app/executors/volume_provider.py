"""On-demand, root-controlled hard-quota volumes for admitted finite runs.

The runner never mounts filesystems itself. A root-owned, narrowly validated
helper under `/usr/local/libexec` creates a distinct tmpfs for each run UUID.
Only an admitted operation can request the owner-local profile's TEST and
PREPARATION slots; the broker independently re-verifies every mount before use.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from runner_app.git.registry import SourceError
from runner_app.git.templates import _python_quick_assets
from runner_app.storage.ledger import TERMINAL, Ledger, LedgerError, _container_state
from runner_app.storage.paths import UnsafePathError, open_private_file
from runner_app.storage.preparation_evidence import PreparationEvidenceStore
from runner_app.storage.run_evidence import EvidenceError, RunEvidenceStore

from .broker import (
    BrokerError,
    RootlessDockerBroker,
    _run_volume_budget,
    _safe_path,
    _verified_run_volume,
)
from .preparation_coordinator import TrustedPreparationCoordinator, preparation_run_id
from .preparation_handoff import _scan, _tree_digest
from .preparation_plan import PreparationError, PreparationGrant
from .profiles import ProfileError, ProfileRegistry
from .run_service import TrustedPreparation

VOLUME_BASE = Path("/var/lib/chat-dev-runner/volumes")
HELPER = Path("/usr/local/libexec/chat-dev-runner-volume-helper")
_MAX_HELPER_OUTPUT = 8192


class VolumeProviderError(ValueError):
    """Stable local infrastructure refusal without helper stderr or host file bytes."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class VolumeReadiness:
    available: bool
    code: str
    image: str | None = None
    base: Path = VOLUME_BASE


def load_python_quick_grant(profile_dir: Path) -> tuple[ProfileRegistry, PreparationGrant]:
    """Load owner-only profile revisions and exact preparation pairing from setup."""
    try:
        if not profile_dir.is_absolute() or ".." in profile_dir.parts:
            raise ValueError
        descriptor = open_private_file(profile_dir / "python-quick-grant.json")
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(16_385)
        if len(raw) > 16_384:
            raise ValueError
        value = json.loads(raw.decode("utf-8", "strict"))
        if (not isinstance(value, dict) or value.get("schema_version") != "1.0"
                or value.get("installed") is not True
                or value.get("profile_dir") != str(profile_dir)
                or not isinstance(value.get("profile_revision_sha256"), dict)
                or set(value["profile_revision_sha256"]) != {"python-quick", "python-prepare"}
                or any(not isinstance(digest, str)
                       or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                       for digest in value["profile_revision_sha256"].values())
                or not isinstance(value.get("preparation_grant"), dict)
                or set(value["preparation_grant"]) != {
                    "template_id", "execution_profile_id", "execution_revision_sha256",
                    "preparation_profile_id", "preparation_revision_sha256", "lockfile_path",
                }
                or value.get("starter_lockfile_sha256") != hashlib.sha256(
                    _python_quick_assets()["requirements.lock"],
                ).hexdigest()):
            raise ValueError
        hashes = value["profile_revision_sha256"]
        binding = value["preparation_grant"]
        if (binding != {
                "template_id": "python-basic", "execution_profile_id": "python-quick",
                "execution_revision_sha256": hashes["python-quick"],
                "preparation_profile_id": "python-prepare",
                "preparation_revision_sha256": hashes["python-prepare"],
                "lockfile_path": "requirements.lock",
            }):
            raise ValueError
        profiles = ProfileRegistry(profile_dir, hashes)
        execution = profiles.require("python-quick")
        preparation = profiles.require("python-prepare")
        if (execution.image != preparation.image
                or execution.image != value.get("image")
                or execution.platform != preparation.platform):
            raise ValueError
        return profiles, PreparationGrant(**binding)
    except (OSError, UnsafePathError, ValueError, TypeError, AttributeError,
            UnicodeError, ProfileError, SourceError, KeyError):
        raise VolumeProviderError("OWNER_PROFILE_UNAVAILABLE") from None


def _verified_retired_volume(path: Path, run_id: str, budget: int) -> Path:
    """Prove exact helper mount after execution, even when the quota is full.

    The broker's pre-run verifier correctly requires an empty mount with spare
    space. Those conditions cannot be used for retiring completed output.
    """
    source = _safe_path(path, require_private_parent=True)
    if source != VOLUME_BASE / run_id or not os.path.ismount(source):
        raise BrokerError("DISK_QUOTA_UNAVAILABLE", "Run volume identity changed")
    info = source.stat()
    parent = source.parent.stat()
    filesystem = os.statvfs(source)
    if (info.st_uid != os.getuid() or info.st_gid != os.getgid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_dev == parent.st_dev
            or filesystem.f_blocks * filesystem.f_frsize != budget
            or filesystem.f_files != 16_384):
        raise BrokerError("DISK_QUOTA_UNAVAILABLE", "Run volume quota changed")
    records = []
    for line in Path("/proc/self/mountinfo").read_text(encoding="ascii").splitlines():
        parts = line.split()
        if len(parts) > 9 and parts[4] == str(source) and "-" in parts:
            separator = parts.index("-")
            if separator + 2 < len(parts):
                records.append((parts[separator + 1], parts[separator + 2]))
    if records != [("tmpfs", "cdr-run-quota")]:
        raise BrokerError("DISK_QUOTA_UNAVAILABLE", "Run volume source changed")
    return source


class HardQuotaVolumeProvider:
    """Trusted handler port; mount allocation starts only after Engine admission."""

    def __init__(self, ledger: Ledger, profiles: ProfileRegistry,
                 preparation: TrustedPreparationCoordinator,
                 grant: PreparationGrant, *, preparation_runs_root: Path) -> None:
        if preparation.ledger is not ledger or preparation.profiles is not profiles:
            raise ValueError("Volume provider and preparation must share local authority")
        if (not preparation_runs_root.is_absolute()
                or ".." in preparation_runs_root.parts):
            raise ValueError("Preparation run root must be absolute")
        self.ledger = ledger
        self.profiles = profiles
        self.preparation = preparation
        self.grant = grant
        self.preparation_runs_root = preparation_runs_root

    @staticmethod
    def _uuid(value: str) -> str:
        try:
            if isinstance(value, str) and str(UUID(value)) == value:
                return value
        except ValueError:
            pass
        raise VolumeProviderError("INVALID_RUN_ID")

    def _admitted(self, request_id: str, profile_id: str) -> None:
        operation = self.ledger.get(self._uuid(request_id))
        if operation is None or operation.get("status") != "RUNNING":
            raise VolumeProviderError("OPERATION_NOT_RUNNING")
        command = operation.get("command")
        if (not isinstance(command, dict)
                or command.get("action") not in {"VERIFY", "DELIVER_VERIFY_PUBLISH"}
                or not isinstance(command.get("params"), dict)
                or command["params"].get("profile_id") != profile_id
                or profile_id != self.grant.execution_profile_id):
            raise VolumeProviderError("PROFILE_NOT_GRANTED")

    @staticmethod
    def _helper(action: str, *arguments: str) -> dict[str, Any]:
        try:
            result = subprocess.run(
                ["/usr/bin/sudo", "-n", str(HELPER), action, *arguments],
                check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=10, env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            )
        except (OSError, subprocess.TimeoutExpired):
            raise VolumeProviderError("VOLUME_HELPER_UNAVAILABLE") from None
        if (result.returncode != 0 or len(result.stdout) > _MAX_HELPER_OUTPUT):
            raise VolumeProviderError("VOLUME_HELPER_REFUSED")
        try:
            value = json.loads(result.stdout.decode("utf-8", "strict"))
        except (UnicodeError, json.JSONDecodeError):
            raise VolumeProviderError("VOLUME_HELPER_RESPONSE_INVALID") from None
        if not isinstance(value, dict):
            raise VolumeProviderError("VOLUME_HELPER_RESPONSE_INVALID")
        return value

    @staticmethod
    def _installed_helper() -> bool:
        try:
            info = HELPER.lstat()
            parent = HELPER.parent.lstat()
        except OSError:
            return False
        return (stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_gid == 0
                and stat.S_IMODE(info.st_mode) == 0o755 and info.st_nlink == 1
                and stat.S_ISDIR(parent.st_mode) and parent.st_uid == 0
                and parent.st_gid == 0 and stat.S_IMODE(parent.st_mode) == 0o755)

    def readiness(self) -> VolumeReadiness:
        """Read-only capability probe; never allocates a mount or starts a test."""
        if not self._installed_helper():
            return VolumeReadiness(False, "VOLUME_HELPER_UNAVAILABLE")
        try:
            execution = self.profiles.require(self.grant.execution_profile_id)
            preparation = self.profiles.require(self.grant.preparation_profile_id)
        except ProfileError:
            return VolumeReadiness(False, "PROFILE_UNAVAILABLE")
        if (execution.revision_sha256 != self.grant.execution_revision_sha256
                or preparation.revision_sha256 != self.grant.preparation_revision_sha256
                or execution.image != preparation.image
                or execution.platform != preparation.platform):
            return VolumeReadiness(False, "PREPARATION_GRANT_CHANGED")
        broker = RootlessDockerBroker(self.profiles, runtime_dir=Path(f"/run/user/{os.getuid()}"))
        probe = broker.probe()
        if not probe.available:
            return VolumeReadiness(False, probe.code)
        try:
            broker._image_id(execution)
            response = self._helper("probe")
        except BrokerError as error:
            return VolumeReadiness(False, error.code)
        except VolumeProviderError as error:
            return VolumeReadiness(False, error.code)
        if response != {"status": "READY", "base": str(VOLUME_BASE), "max_slots": 8}:
            return VolumeReadiness(False, "VOLUME_HELPER_RESPONSE_INVALID")
        return VolumeReadiness(True, "READY", execution.image)

    def _allocate(self, run_id: str, profile_id: str) -> Path:
        profile = self.profiles.require(profile_id)
        budget = _run_volume_budget(profile)
        if budget % (1024 * 1024):
            raise VolumeProviderError("VOLUME_BUDGET_INVALID")
        size_mb = budget // (1024 * 1024)
        response = self._helper("allocate", self._uuid(run_id), str(size_mb))
        path = VOLUME_BASE / run_id
        if response != {"status": "ALLOCATED", "run_id": run_id,
                        "size_mb": size_mb, "path": str(path)}:
            raise VolumeProviderError("VOLUME_HELPER_RESPONSE_INVALID")
        try:
            _verified_run_volume(path, run_id, budget)
        except BrokerError:
            raise VolumeProviderError("DISK_QUOTA_UNAVAILABLE") from None
        return path

    def volume_for_run(self, request_id: str) -> Path:
        self._admitted(request_id, self.grant.execution_profile_id)
        return self._allocate(request_id, self.grant.execution_profile_id)

    def preparation_for_run(self, request_id: str,
                            profile_id: str) -> TrustedPreparation:
        self._admitted(request_id, profile_id)
        preparation_id = preparation_run_id(request_id)
        volume = self._allocate(preparation_id, self.grant.preparation_profile_id)
        return TrustedPreparation(
            self.preparation, self.grant, volume,
            self.preparation_runs_root / preparation_id,
        )


    def _phase_state(self, request_id: str
                     ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        connection = self.ledger.database.connect()
        try:
            return _container_state(connection, request_id)
        except (ValueError, TypeError, OSError):
            raise VolumeProviderError("CONTAINER_HISTORY_UNCERTAIN") from None
        finally:
            connection.close()

    @staticmethod
    def _check_absent_phase_volume(path: Path, run_id: str, budget: int) -> None:
        """Retire only an empty phase volume after a container-absence proof."""
        try:
            _verified_retired_volume(path, run_id, budget)
            entries = {child.name: child for child in path.iterdir()}
            if set(entries) - {"build", "out", "tmp"}:
                raise VolumeProviderError("ABSENT_VOLUME_HAS_DATA")
            for child in entries.values():
                info = child.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_dev != path.stat().st_dev
                        or any(child.iterdir())):
                    raise VolumeProviderError("ABSENT_VOLUME_HAS_DATA")
        except BrokerError:
            raise VolumeProviderError("DISK_QUOTA_UNAVAILABLE") from None
        except OSError:
            raise VolumeProviderError("VOLUME_CLEAR_UNSAFE") from None

    def _absent_test_build_digest(self, request_id: str, prep_id: str,
                                  path: Path, preparation_result: dict[str, str]
                                  ) -> tuple[str, bool, dict[str, str]]:
        """Bind copied build bytes to the immutable, successful phase handoff."""
        try:
            connection = self.ledger.database.connect()
            try:
                row = connection.execute(
                    "SELECT h.preparation_run_id,h.snapshot_id,h.test_run_id,"
                    "h.plan_identity_sha256,h.preparation_profile_revision_sha256,"
                    "h.image,h.build_tree_sha256,h.evidence_sha256,h.test_volume_path,"
                    "p.run_id AS result_run_id,p.snapshot_id AS result_snapshot_id,"
                    "p.plan_identity_sha256 AS result_plan_identity_sha256,"
                    "p.profile_revision_sha256 AS result_profile_revision_sha256,"
                    "p.image AS result_image,p.outcome,p.exit_code,p.capture_complete,"
                    "p.cleanup_verified,p.evidence_sha256 AS result_evidence_sha256 "
                    "FROM preparation_handoffs h JOIN preparation_results p "
                    "ON p.request_id=h.request_id WHERE h.request_id=?",
                    (request_id,),
                ).fetchone()
                markers = connection.execute(
                    "SELECT detail_json FROM journal WHERE request_id=? "
                    "AND event_type='ABSENT_TEST_BUILD_CLEAR_INTENDED'",
                    (request_id,),
                ).fetchall()
            finally:
                connection.close()
            profile = self.profiles.require(self.grant.preparation_profile_id)
        except (sqlite3.Error, OSError, ProfileError):
            raise VolumeProviderError("ABSENT_TEST_HANDOFF_UNVERIFIED") from None
        if (row is None or row["preparation_run_id"] != prep_id
                or row["result_run_id"] != prep_id
                or row["test_run_id"] != request_id
                or row["test_volume_path"] != str(path)
                or row["snapshot_id"] != row["result_snapshot_id"]
                or row["plan_identity_sha256"] != row["result_plan_identity_sha256"]
                or row["preparation_profile_revision_sha256"] !=
                row["result_profile_revision_sha256"]
                or row["preparation_profile_revision_sha256"] !=
                self.grant.preparation_revision_sha256
                or row["image"] != row["result_image"] or row["image"] != profile.image
                or row["evidence_sha256"] != row["result_evidence_sha256"]
                or row["evidence_sha256"] != preparation_result.get("evidence_sha256")
                or row["outcome"] != "PASSED" or row["exit_code"] != 0
                or row["capture_complete"] != 1 or row["cleanup_verified"] != 1
                or not isinstance(row["build_tree_sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", row["build_tree_sha256"]) is None):
            raise VolumeProviderError("ABSENT_TEST_HANDOFF_UNVERIFIED")
        digest = str(row["build_tree_sha256"])
        try:
            mount_info = path.stat()
        except OSError:
            raise VolumeProviderError("DISK_QUOTA_UNAVAILABLE") from None
        marker = {
            "run_id": request_id, "preparation_run_id": prep_id,
            "build_tree_sha256": digest,
            "volume_path_sha256": hashlib.sha256(str(path).encode("utf-8")).hexdigest(),
            "mount_device": str(mount_info.st_dev),
            "mount_inode": str(mount_info.st_ino),
        }
        try:
            if any(json.loads(item["detail_json"]) != marker for item in markers):
                raise ValueError
        except (TypeError, ValueError, KeyError):
            raise VolumeProviderError("ABSENT_TEST_CLEAR_INTENT_INVALID") from None
        return digest, bool(markers), marker

    @staticmethod
    def _check_attested_absent_test_volume(path: Path, run_id: str, budget: int,
                                            build_digest: str, *, clearing: bool) -> None:
        """Before clearing, prove the copied build or resume a journaled clear."""
        try:
            _verified_retired_volume(path, run_id, budget)
            prefix = str(path) + "/"
            for line in Path("/proc/self/mountinfo").read_text(encoding="ascii").splitlines():
                parts = line.split()
                if len(parts) > 4 and parts[4].startswith(prefix):
                    raise VolumeProviderError("NESTED_VOLUME_MOUNT")
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            root = os.open(path, flags)
            try:
                root_info = os.fstat(root)
                names = set(os.listdir(root))
                if (root_info.st_uid != os.getuid() or root_info.st_gid != os.getgid()
                        or stat.S_IMODE(root_info.st_mode) != 0o700
                        or names - {"build", "out", "tmp"}
                        or not clearing and names != {"build", "out", "tmp"}):
                    raise VolumeProviderError("ABSENT_VOLUME_HAS_DATA")
                for name in names:
                    before = os.stat(name, dir_fd=root, follow_symlinks=False)
                    if (not stat.S_ISDIR(before.st_mode)
                            or before.st_dev != root_info.st_dev
                            or before.st_uid != os.getuid()
                            or before.st_gid != os.getgid()
                            or stat.S_IMODE(before.st_mode) != 0o700):
                        raise VolumeProviderError("ABSENT_VOLUME_HAS_DATA")
                    child = os.open(name, flags, dir_fd=root)
                    try:
                        after = os.fstat(child)
                        if ((after.st_dev, after.st_ino, after.st_mode,
                             after.st_uid, after.st_gid) !=
                            (before.st_dev, before.st_ino, before.st_mode,
                             before.st_uid, before.st_gid)):
                            raise VolumeProviderError("VOLUME_CHANGED")
                        if name in {"out", "tmp"} and os.listdir(child):
                            raise VolumeProviderError("ABSENT_VOLUME_HAS_DATA")
                    finally:
                        os.close(child)
                if "build" in names:
                    entries, _ = _scan(path / "build")
                    if (not clearing and _tree_digest(entries) != build_digest
                            or any(stat.S_IMODE((path / "build" / entry.relative_path).lstat().st_mode)
                                   != (0o700 if entry.kind == "directory" or entry.executable
                                       else 0o600) for entry in entries)):
                        raise VolumeProviderError("ABSENT_TEST_BUILD_CHANGED")
            finally:
                os.close(root)
        except BrokerError:
            raise VolumeProviderError("DISK_QUOTA_UNAVAILABLE") from None
        except PreparationError:
            raise VolumeProviderError("ABSENT_TEST_BUILD_CHANGED") from None
        except OSError:
            raise VolumeProviderError("VOLUME_CLEAR_UNSAFE") from None

    @staticmethod
    def _clear_verified_mount(path: Path, run_id: str, budget: int) -> None:
        """Remove only bytes inside this exact retired tmpfs, using nofollow dirfds.

        A nested mount or any changed directory identity stops the cleanup. An
        interrupted clear may be retried after the same durable proofs are read.
        """
        try:
            _verified_retired_volume(path, run_id, budget)
            prefix = str(path) + "/"
            for line in Path("/proc/self/mountinfo").read_text(encoding="ascii").splitlines():
                parts = line.split()
                if len(parts) > 4 and parts[4].startswith(prefix):
                    raise VolumeProviderError("NESTED_VOLUME_MOUNT")
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            root = os.open(path, flags)
            try:
                device = os.fstat(root).st_dev
                visited = [0]

                def clear(directory: int, depth: int) -> None:
                    if depth > 64:
                        raise VolumeProviderError("VOLUME_CLEAR_LIMIT")
                    with os.scandir(directory) as entries:
                        names = [entry.name for entry in entries]
                    for name in names:
                        visited[0] += 1
                        if visited[0] > 16_384:
                            raise VolumeProviderError("VOLUME_CLEAR_LIMIT")
                        before = os.stat(name, dir_fd=directory, follow_symlinks=False)
                        if before.st_dev != device:
                            raise VolumeProviderError("NESTED_VOLUME_MOUNT")
                        if stat.S_ISDIR(before.st_mode):
                            child = os.open(name, flags, dir_fd=directory)
                            try:
                                current = os.fstat(child)
                                if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                                    raise VolumeProviderError("VOLUME_CHANGED")
                                clear(child, depth + 1)
                            finally:
                                os.close(child)
                            os.rmdir(name, dir_fd=directory)
                        else:
                            os.unlink(name, dir_fd=directory)

                clear(root, 0)
            finally:
                os.close(root)
        except BrokerError:
            raise VolumeProviderError("DISK_QUOTA_UNAVAILABLE") from None
        except OSError:
            raise VolumeProviderError("VOLUME_CLEAR_UNSAFE") from None

    @staticmethod
    def _clear_unstarted_scaffold(path: Path, run_id: str, budget: int) -> None:
        """Remove only empty, owner-private directories created before a phase starts."""
        try:
            _verified_retired_volume(path, run_id, budget)
            prefix = str(path) + "/"
            for line in Path("/proc/self/mountinfo").read_text(encoding="ascii").splitlines():
                parts = line.split()
                if len(parts) > 4 and parts[4].startswith(prefix):
                    raise VolumeProviderError("NESTED_VOLUME_MOUNT")
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            root = os.open(path, flags)
            try:
                root_device = os.fstat(root).st_dev
                names = tuple(sorted(os.listdir(root)))
                if set(names) - {"build", "out", "tmp"}:
                    raise VolumeProviderError("UNSTARTED_VOLUME_NOT_EMPTY")
                for name in names:
                    before = os.stat(name, dir_fd=root, follow_symlinks=False)
                    if (not stat.S_ISDIR(before.st_mode)
                            or before.st_dev != root_device
                            or before.st_uid != os.getuid()
                            or before.st_gid != os.getgid()
                            or stat.S_IMODE(before.st_mode) != 0o700):
                        raise VolumeProviderError("UNSTARTED_VOLUME_NOT_EMPTY")
                    child = os.open(name, flags, dir_fd=root)
                    try:
                        opened = os.fstat(child)
                        if ((opened.st_dev, opened.st_ino, opened.st_mode,
                             opened.st_uid, opened.st_gid) !=
                            (before.st_dev, before.st_ino, before.st_mode,
                             before.st_uid, before.st_gid)
                                or os.listdir(child)):
                            raise VolumeProviderError("UNSTARTED_VOLUME_NOT_EMPTY")
                    finally:
                        os.close(child)
                for name in names:
                    os.rmdir(name, dir_fd=root)
                if os.listdir(root):
                    raise VolumeProviderError("UNSTARTED_VOLUME_NOT_EMPTY")
            finally:
                os.close(root)
        except BrokerError:
            raise VolumeProviderError("DISK_QUOTA_UNAVAILABLE") from None
        except OSError:
            raise VolumeProviderError("VOLUME_CLEAR_UNSAFE") from None

    def release_evidenced(self, request_id: str,
                          run_evidence: RunEvidenceStore) -> tuple[str, ...]:
        """Retire proven finite-phase mounts after all retained evidence is checked.

        This is an owner-local housekeeping operation, never a remote command.
        Uncertain container cleanup or missing frozen evidence preserves mounts.
        """
        request_id = self._uuid(request_id)
        operation = self.ledger.get(request_id)
        if (operation is None or operation.get("status") not in TERMINAL
                or operation.get("reconcile_required")):
            raise VolumeProviderError("OPERATION_NOT_SETTLED")
        command = operation.get("command")
        if (not isinstance(command, dict)
                or command.get("action") not in {"VERIFY", "DELIVER_VERIFY_PUBLISH"}
                or not isinstance(command.get("params"), dict)
                or command["params"].get("profile_id") != self.grant.execution_profile_id
                or run_evidence.database.path != self.ledger.database.path):
            raise VolumeProviderError("VOLUME_AUTHORITY_MISMATCH")
        prep_id = preparation_run_id(request_id)
        intents, proofs = self._phase_state(request_id)
        if set(intents) != set(proofs):
            raise VolumeProviderError("CONTAINER_CLEANUP_UNCERTAIN")
        if ("PREPARATION" in intents and intents["PREPARATION"].get("run_id") != prep_id
                or "TEST" in intents and intents["TEST"].get("run_id") != request_id):
            raise VolumeProviderError("CONTAINER_HISTORY_UNCERTAIN")
        if ("PREPARATION" in intents and proofs["PREPARATION"]["state"] == "ABSENT"
                and self.ledger.get_preparation_result(request_id) is not None
                or "TEST" in intents and proofs["TEST"]["state"] == "ABSENT"
                and self.ledger.get_run(request_id) is not None):
            raise VolumeProviderError("ABSENT_PHASE_HAS_RESULT")
        try:
            if "PREPARATION" in intents and proofs["PREPARATION"]["state"] == "REMOVED":
                result = self.ledger.get_preparation_result(request_id)
                if result is None or result.get("run_id") != prep_id:
                    raise VolumeProviderError("PREPARATION_EVIDENCE_UNSETTLED")
                preparation_evidence = PreparationEvidenceStore(
                    self.ledger.database, self.preparation.evidence_root,
                )
                receipt = preparation_evidence.receipt(prep_id)
                if (receipt.get("request_id") != request_id
                        or receipt.get("run_id") != prep_id
                        or receipt.get("outcome") != result.get("outcome")):
                    raise VolumeProviderError("PREPARATION_EVIDENCE_UNSETTLED")
                prep_outputs = preparation_evidence.retention_index(prep_id)
                if not prep_outputs or any(row["state"] != "AVAILABLE" for row in prep_outputs):
                    raise VolumeProviderError("PREPARATION_EVIDENCE_UNSETTLED")
                for row in prep_outputs:
                    preparation_evidence.open_output(prep_id, row["output_id"]).close()
            if "TEST" in intents and proofs["TEST"]["state"] == "REMOVED":
                manifest = self.ledger.get_run(request_id)
                if manifest is None or manifest.get("run_id") != request_id:
                    raise VolumeProviderError("RUN_EVIDENCE_UNSETTLED")
                outputs = run_evidence.output_retention_index(request_id)
                if not outputs or any(row["state"] != "AVAILABLE" for row in outputs):
                    raise VolumeProviderError("RUN_EVIDENCE_UNSETTLED")
                for row in outputs:
                    run_evidence.open_output(request_id, row["output_id"]).close()
        except (EvidenceError, OSError, UnsafePathError):
            raise VolumeProviderError("FROZEN_EVIDENCE_CHANGED") from None

        released: list[str] = []
        for phase, run_id, profile_id in (
            ("PREPARATION", prep_id, self.grant.preparation_profile_id),
            ("TEST", request_id, self.grant.execution_profile_id),
        ):
            path = VOLUME_BASE / run_id
            if not os.path.lexists(path):
                continue
            try:
                profile = self.profiles.require(profile_id)
            except ProfileError:
                raise VolumeProviderError("PROFILE_UNAVAILABLE") from None
            budget = _run_volume_budget(profile)
            if phase in intents:
                if phase == "TEST" and proofs[phase]["state"] == "ABSENT" and (
                        "PREPARATION" in intents and
                        proofs["PREPARATION"]["state"] == "REMOVED"):
                    result = self.ledger.get_preparation_result(request_id)
                    if result is None:
                        raise VolumeProviderError("ABSENT_TEST_HANDOFF_UNVERIFIED")
                    digest, clearing, marker = self._absent_test_build_digest(
                        request_id, prep_id, path, result,
                    )
                    self._check_attested_absent_test_volume(
                        path, run_id, budget, digest, clearing=clearing,
                    )
                    if not clearing:
                        try:
                            self.ledger.journal(
                                request_id, "absent-test-build-clear",
                                "ABSENT_TEST_BUILD_CLEAR_INTENDED", marker,
                            )
                        except LedgerError:
                            raise VolumeProviderError(
                                "ABSENT_TEST_CLEAR_INTENT_UNAVAILABLE",
                            ) from None
                    self._clear_verified_mount(path, run_id, budget)
                else:
                    if proofs[phase]["state"] == "ABSENT":
                        self._check_absent_phase_volume(path, run_id, budget)
                    self._clear_verified_mount(path, run_id, budget)
            else:
                self._clear_unstarted_scaffold(path, run_id, budget)
            response = self._helper("release", run_id)
            if response != {"status": "RELEASED", "run_id": run_id}:
                raise VolumeProviderError("VOLUME_HELPER_RESPONSE_INVALID")
            released.append(run_id)
        return tuple(released)
