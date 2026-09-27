"""Offline known-mailbox polling bridge; no OAuth, network loop, or request queue."""

from __future__ import annotations

import base64
import binascii
import ctypes
import errno
import hashlib
import json
import math
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from runner_app.adapters.drive_docs import (
    FrozenDriveInput,
    ReadProvider,
    RegisteredMailbox,
    read_ready_snapshot,
)
from runner_app.domain.commands import (
    PROJECT_ACTIONS,
    ROOT_ACTIONS,
    WORKSPACE_ACTIONS,
    DomainError,
    Provenance,
    parse_command,
    parse_json,
)
from runner_app.engine.service import Engine
from runner_app.git.changes import MAX_CHANGESET_BYTES, ChangeError, validate_changeset
from runner_app.storage.ledger import TERMINAL, Admission
from runner_app.storage.paths import UnsafePathError, ensure_private_directory

_MAX_SLOT_BYTES = 4_000_000
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_RENAME_NOREPLACE = 1


class PollError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PollResult:
    kind: str
    request_id: str | None = None
    admission: Admission | None = None


@dataclass(frozen=True)
class PollSchedule:
    """Pure scheduling state; caller owns sleeping and provider errors."""

    base_seconds: float = 15.0
    max_seconds: float = 300.0
    next_due: float = 0.0
    failures: int = 0
    last_success: float | None = None

    def __post_init__(self) -> None:
        if (not math.isfinite(self.base_seconds) or not math.isfinite(self.max_seconds)
                or self.base_seconds <= 0 or self.max_seconds < self.base_seconds
                or self.max_seconds > 3600 or self.failures < 0 or self.failures > 16
                or not math.isfinite(self.next_due)
                or (self.last_success is not None and not math.isfinite(self.last_success))):
            raise ValueError("Invalid poll schedule")

    def due(self, now: float) -> bool:
        return math.isfinite(now) and now >= self.next_due

    def success(self, now: float) -> PollSchedule:
        if not math.isfinite(now) or (self.last_success is not None and now < self.last_success):
            raise ValueError("Invalid poll time")
        return PollSchedule(self.base_seconds, self.max_seconds, now + self.base_seconds,
                            0, now)

    def failure(self, now: float, *, jitter: float = 0.0) -> PollSchedule:
        if (not math.isfinite(now) or not math.isfinite(jitter) or not 0 <= jitter <= 1
                or (self.last_success is not None and now < self.last_success)):
            raise ValueError("Invalid poll time or jitter")
        failures = min(self.failures + 1, 16)
        delay = min(self.max_seconds,
                    self.base_seconds * (2 ** min(failures, 8)) + self.base_seconds * jitter)
        return PollSchedule(self.base_seconds, self.max_seconds, now + delay,
                            failures, self.last_success)


def _scope_matches(command: Mapping[str, Any], provenance: Provenance) -> bool:
    if provenance.kind != "drive" or command["dispatch"] != "READY":
        return False
    action = command["action"]
    if provenance.project_id is None:
        return action in ROOT_ACTIONS and command["workspace_id"] is None
    if provenance.workspace_id is None:
        return (action in PROJECT_ACTIONS and command["project_id"] == provenance.project_id
                and command["workspace_id"] is None)
    return (action in WORKSPACE_ACTIONS and command["project_id"] == provenance.project_id
            and command["workspace_id"] == provenance.workspace_id)


def _decode_bytes(value: Any, expected_sha256: Any, *, limit: int) -> bytes:
    if not isinstance(value, str) or not isinstance(expected_sha256, str):
        raise PollError("FROZEN_SLOT_INVALID")
    # Bound encoded bytes before decoding so malformed local state cannot expand memory.
    if len(value) > 4 * ((limit + 2) // 3):
        raise PollError("FROZEN_SLOT_INVALID")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise PollError("FROZEN_SLOT_INVALID") from error
    if (len(raw) > limit or base64.b64encode(raw).decode("ascii") != value
            or hashlib.sha256(raw).hexdigest() != expected_sha256):
        raise PollError("FROZEN_SLOT_TAMPERED")
    return raw


def _rename_no_replace(directory_fd: int, temporary: str, target: str) -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    if function is None:
        raise PollError("ATOMIC_SLOT_RENAME_UNAVAILABLE")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                         ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(directory_fd, os.fsencode(temporary), directory_fd,
                os.fsencode(target), _RENAME_NOREPLACE) == 0:
        return True
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        return False
    if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        raise PollError("ATOMIC_SLOT_RENAME_UNAVAILABLE")
    raise PollError("FROZEN_SLOT_IO")


class FrozenSlotStore:
    """One durable immutable freeze and one admission marker per request UUID.

    A persisted freeze without an admission marker is an explicit reconciliation
    state. Its command is never submitted again merely because a process restarted.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        try:
            ensure_private_directory(self.directory)
            with self._open_dir(check_identity=False) as descriptor:
                info = os.fstat(descriptor)
                self._identity = (info.st_dev, info.st_ino)
        except (OSError, UnsafePathError) as error:
            raise PollError("FROZEN_STORE_NOT_PRIVATE") from error

    @contextmanager
    def _open_dir(self, *, check_identity: bool = True) -> Iterator[int]:
        try:
            descriptor = os.open(self.directory, _DIRECTORY_FLAGS)
        except OSError as error:
            raise PollError("FROZEN_STORE_NOT_PRIVATE") from error
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700
                    or (check_identity and (info.st_dev, info.st_ino) != self._identity)):
                raise PollError("FROZEN_STORE_NOT_PRIVATE")
            yield descriptor
        finally:
            os.close(descriptor)

    @staticmethod
    def _name(request_id: str, suffix: str) -> str:
        try:
            if str(UUID(request_id)) != request_id:
                raise ValueError("Noncanonical UUID")
        except (TypeError, ValueError, AttributeError) as error:
            raise PollError("INVALID_REQUEST_ID") from error
        return f"{request_id}.{suffix}.json"

    @staticmethod
    def _read(directory_fd: int, name: str) -> bytes:
        try:
            descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
        except FileNotFoundError:
            raise
        except OSError as error:
            raise PollError("FROZEN_SLOT_UNSAFE") from error
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size > _MAX_SLOT_BYTES):
                raise PollError("FROZEN_SLOT_UNSAFE")
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    raise PollError("FROZEN_SLOT_CHANGED")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise PollError("FROZEN_SLOT_CHANGED")
            after = os.fstat(descriptor)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                    before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size,
                                            after.st_mtime_ns, after.st_ctime_ns):
                raise PollError("FROZEN_SLOT_CHANGED")
            if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise PollError("FROZEN_SLOT_CHANGED")
            return b"".join(chunks)
        except OSError as error:
            raise PollError("FROZEN_SLOT_CHANGED") from error
        finally:
            os.close(descriptor)

    def _create_once(self, name: str, content: bytes) -> bool:
        if len(content) > _MAX_SLOT_BYTES:
            raise PollError("FROZEN_SLOT_TOO_LARGE")
        temporary = f".{uuid4().hex}.tmp"
        with self._open_dir() as directory_fd:
            try:
                descriptor = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600, dir_fd=directory_fd,
                )
                try:
                    os.fchmod(descriptor, 0o600)
                    view = memoryview(content)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise PollError("FROZEN_SLOT_IO")
                        view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                created = _rename_no_replace(directory_fd, temporary, name)
                if created:
                    os.fsync(directory_fd)
                return created
            except OSError as error:
                raise PollError("FROZEN_SLOT_IO") from error
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise PollError("FROZEN_SLOT_IO") from error

    def _load(self, request_id: str, expected_provenance: Provenance) -> FrozenDriveInput:
        name = self._name(request_id, "frozen")
        with self._open_dir() as directory_fd:
            try:
                raw_record = self._read(directory_fd, name)
            except FileNotFoundError as error:
                raise PollError("FROZEN_SLOT_MISSING") from error
        try:
            record = parse_json(raw_record, max_bytes=_MAX_SLOT_BYTES)
            if set(record) != {"schema_version", "request_id", "source", "command", "changeset"}:
                raise PollError("FROZEN_SLOT_INVALID")
            if record["schema_version"] != "1.0" or record["request_id"] != request_id:
                raise PollError("FROZEN_SLOT_INVALID")
            source = record["source"]
            if (not isinstance(source, dict)
                    or source != {"kind": expected_provenance.kind,
                                  "command_id": expected_provenance.source_id,
                                  "project_id": expected_provenance.project_id,
                                  "workspace_id": expected_provenance.workspace_id}
                    or expected_provenance.kind != "drive"):
                raise PollError("FROZEN_SOURCE_MISMATCH")
            command_info = record["command"]
            if (not isinstance(command_info, dict)
                    or set(command_info) != {"file_id", "revision", "sha256", "digest", "raw_base64"}
                    or command_info["file_id"] != expected_provenance.source_id
                    or not isinstance(command_info["revision"], str)
                    or not command_info["revision"]):
                raise PollError("FROZEN_SLOT_INVALID")
            raw_command = _decode_bytes(command_info["raw_base64"], command_info["sha256"],
                                        limit=262_144)
            command = parse_command(raw_command)
            parsed = command.command
            if (parsed["request_id"] != request_id or command.digest != command_info["digest"]
                    or not _scope_matches(parsed, expected_provenance)):
                raise PollError("FROZEN_SLOT_TAMPERED")
            change_info = record["changeset"]
            raw_changeset: bytes | None = None
            plan = None
            if parsed["action"] == "DELIVER_VERIFY_PUBLISH":
                if (not isinstance(change_info, dict)
                        or set(change_info) != {"file_id", "revision", "sha256", "raw_base64"}
                        or change_info["file_id"] != parsed["params"]["changeset_doc_id"]
                        or change_info["revision"] != parsed["params"]["changeset_revision"]
                        or not isinstance(change_info["revision"], str)
                        or not change_info["revision"]):
                    raise PollError("FROZEN_SLOT_INVALID")
                raw_changeset = _decode_bytes(change_info["raw_base64"], change_info["sha256"],
                                               limit=MAX_CHANGESET_BYTES)
                parsed_changeset = parse_json(raw_changeset, max_bytes=MAX_CHANGESET_BYTES)
                plan = validate_changeset(
                    parsed_changeset, project_id=parsed["project_id"],
                    workspace_id=parsed["workspace_id"], expected_head=parsed["expected_head"],
                )
            elif change_info is not None:
                raise PollError("FROZEN_SLOT_INVALID")
            return FrozenDriveInput(
                command=command, raw_command=raw_command,
                command_doc_id=command_info["file_id"],
                command_revision=command_info["revision"],
                command_text_sha256=command_info["sha256"],
                raw_changeset=raw_changeset,
                changeset_doc_id=None if change_info is None else change_info["file_id"],
                changeset_revision=None if change_info is None else change_info["revision"],
                changeset_text_sha256=None if change_info is None else change_info["sha256"],
                change_plan=plan,
            )
        except PollError:
            raise
        except (DomainError, ChangeError, KeyError, TypeError, ValueError) as error:
            raise PollError("FROZEN_SLOT_INVALID") from error

    def load_frozen(self, request_id: str, expected_provenance: Provenance) -> FrozenDriveInput:
        """Read exact persisted bytes/revisions only for their trusted mailbox scope."""
        return self._load(request_id, expected_provenance)

    def freeze(self, snapshot: FrozenDriveInput, provenance: Provenance) -> tuple[str, bool]:
        try:
            command = parse_command(snapshot.raw_command)
            parsed = command.command
            request_id = parsed["request_id"]
            self._name(request_id, "frozen")
            if (command != snapshot.command or not _scope_matches(parsed, provenance)
                    or snapshot.command_doc_id != provenance.source_id
                    or not snapshot.command_revision
                    or snapshot.command_text_sha256 != hashlib.sha256(snapshot.raw_command).hexdigest()):
                raise PollError("FROZEN_INPUT_INCONSISTENT")
            if parsed["action"] == "DELIVER_VERIFY_PUBLISH":
                if (snapshot.raw_changeset is None or snapshot.changeset_doc_id is None
                        or snapshot.changeset_revision is None
                        or snapshot.changeset_text_sha256 is None
                        or snapshot.changeset_doc_id != parsed["params"]["changeset_doc_id"]
                        or snapshot.changeset_revision != parsed["params"]["changeset_revision"]
                        or snapshot.changeset_text_sha256 != hashlib.sha256(
                            snapshot.raw_changeset).hexdigest()):
                    raise PollError("FROZEN_INPUT_INCONSISTENT")
                parsed_change = parse_json(snapshot.raw_changeset, max_bytes=MAX_CHANGESET_BYTES)
                plan = validate_changeset(
                    parsed_change, project_id=parsed["project_id"],
                    workspace_id=parsed["workspace_id"], expected_head=parsed["expected_head"],
                )
                if plan != snapshot.change_plan:
                    raise PollError("FROZEN_INPUT_INCONSISTENT")
            elif (snapshot.raw_changeset is not None or snapshot.changeset_doc_id is not None
                  or snapshot.changeset_revision is not None
                  or snapshot.changeset_text_sha256 is not None or snapshot.change_plan is not None):
                raise PollError("FROZEN_INPUT_INCONSISTENT")
        except PollError:
            raise
        except (DomainError, ChangeError, KeyError, TypeError, ValueError) as error:
            raise PollError("FROZEN_INPUT_INCONSISTENT") from error
        payload = {
            "schema_version": "1.0", "request_id": request_id,
            "source": {"kind": provenance.kind, "command_id": provenance.source_id,
                       "project_id": provenance.project_id,
                       "workspace_id": provenance.workspace_id},
            "command": {"file_id": snapshot.command_doc_id,
                        "revision": snapshot.command_revision,
                        "sha256": snapshot.command_text_sha256,
                        "digest": command.digest,
                        "raw_base64": base64.b64encode(snapshot.raw_command).decode("ascii")},
            "changeset": None if snapshot.raw_changeset is None else {
                "file_id": snapshot.changeset_doc_id,
                "revision": snapshot.changeset_revision,
                "sha256": snapshot.changeset_text_sha256,
                "raw_base64": base64.b64encode(snapshot.raw_changeset).decode("ascii")},
        }
        content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        name = self._name(request_id, "frozen")
        created = self._create_once(name, content)
        if not created:
            with self._open_dir() as directory_fd:
                if self._read(directory_fd, name) != content:
                    raise PollError("REQUEST_ID_REUSED")
        return request_id, created

    def admission_recorded(self, request_id: str, expected_provenance: Provenance) -> bool:
        frozen = self.load_frozen(request_id, expected_provenance)
        name = self._name(request_id, "admission")
        with self._open_dir() as directory_fd:
            try:
                content = self._read(directory_fd, name)
            except FileNotFoundError:
                return False
        try:
            marker = parse_json(content, max_bytes=_MAX_SLOT_BYTES)
            if (set(marker) != {"request_id", "request_digest", "status", "code",
                               "execute", "duplicate"}
                    or marker["request_id"] != request_id
                    or marker["request_digest"] != frozen.command.digest
                    or marker["status"] not in (TERMINAL | {"RUNNING"})
                    or not isinstance(marker["execute"], bool)
                    or not isinstance(marker["duplicate"], bool)
                    or (marker["code"] is not None and not isinstance(marker["code"], str))):
                raise PollError("ADMISSION_RECORD_INVALID")
        except PollError:
            raise
        except (DomainError, KeyError, TypeError, ValueError) as error:
            raise PollError("ADMISSION_RECORD_INVALID") from error
        return True

    def record_admission(self, request_id: str, admission: Admission,
                         expected_provenance: Provenance) -> None:
        frozen = self.load_frozen(request_id, expected_provenance)
        receipt = admission.receipt
        if (receipt.get("request_id") != request_id
                or receipt.get("request_digest") != frozen.command.digest
                or receipt.get("status") not in (TERMINAL | {"RUNNING"})
                or (receipt.get("code") is not None and not isinstance(receipt.get("code"), str))):
            raise PollError("ADMISSION_RECORD_INVALID")
        name = self._name(request_id, "admission")
        content = json.dumps({
            "request_id": request_id,
            "request_digest": frozen.command.digest,
            "status": receipt["status"],
            "code": receipt.get("code"),
            "execute": admission.execute,
            "duplicate": admission.duplicate,
        }, sort_keys=True, separators=(",", ":")).encode()
        if not self._create_once(name, content):
            with self._open_dir() as directory_fd:
                if self._read(directory_fd, name) != content:
                    raise PollError("ADMISSION_RECORD_CONFLICT")


class DrivePoller:
    def __init__(self, provider: ReadProvider, mailbox: RegisteredMailbox,
                 engine: Engine, slots: FrozenSlotStore) -> None:
        self.provider = provider
        self.mailbox = mailbox
        self.engine = engine
        self.slots = slots
        self.provenance = Provenance(
            "drive", mailbox.command_id, mailbox.project_id, mailbox.workspace_id)
        if engine.policy.mailboxes.get(mailbox.command_id) != self.provenance:
            raise PollError("MAILBOX_NOT_REGISTERED")

    def poll_once(self) -> PollResult:
        snapshot = read_ready_snapshot(self.provider, self.mailbox)
        if snapshot is None:
            return PollResult("IDLE")
        request_id, created = self.slots.freeze(snapshot, self.provenance)
        if not created:
            return PollResult(
                "ALREADY_OBSERVED" if self.slots.admission_recorded(request_id, self.provenance)
                else "NEEDS_RECONCILIATION", request_id)
        # The durable freeze precedes immediate EP-01 admission. A crash in this
        # interval is never replayed automatically after restart or another poll.
        admission = self.engine.submit(snapshot.raw_command, self.provenance)
        self.slots.record_admission(request_id, admission, self.provenance)
        return PollResult("SUBMITTED", request_id, admission)
