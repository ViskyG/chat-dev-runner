"""Immediate finite dispatch; no executor pool, waiting list or automatic replay."""
from __future__ import annotations

import errno
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from runner_app.domain.commands import (
    DomainError,
    FrozenCommand,
    LocalPolicy,
    Provenance,
    freeze,
    parse_json,
    validate_shape,
    validate_time,
)
from runner_app.git.imports import ImportGrants
from runner_app.storage.ledger import Admission, Ledger


@dataclass(frozen=True)
class OperationResult:
    status: str = "COMPLETED"
    code: str | None = None
    result: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Context:
    cancel: threading.Event
    ledger: Ledger


Handler = Callable[[FrozenCommand, Context], OperationResult]

# Metadata and publication plans reserve repository ownership for their finite
# operation. VERIFY remains workspace-scoped so independent snapshots can run
# concurrently. EP-05 may narrow repository reservation to individual safe steps.
_REPOSITORY_ACTIONS = frozenset({
    "CREATE_PROJECT", "CREATE_WORKSPACE", "RETARGET_WORKSPACE", "DELIVER_VERIFY_PUBLISH",
    "PUBLISH", "FETCH", "SYNC_WORKSPACE", "ARCHIVE_WORKSPACE",
})


class Engine:
    def __init__(self, ledger: Ledger, *, policy: LocalPolicy | None = None,
                 handlers: dict[str, Handler] | None = None,
                 import_grants: ImportGrants | None = None) -> None:
        self.ledger = ledger
        self.policy = policy or LocalPolicy()
        self.import_grants = import_grants
        self.handlers = dict(handlers or {})
        self._workers: dict[str, tuple[threading.Thread, threading.Event]] = {}
        # Serializes only the tiny dispatch/cancel handoff, never handler execution or slot waiting.
        self._handoff = threading.RLock()
        self._storage_fault = threading.Event()

    def capabilities(self) -> list[str]:
        return sorted(self.handlers)

    @property
    def storage_faulted(self) -> bool:
        """A failed durable write stops admission for this process lifetime."""
        return self._storage_fault.is_set()

    def _trip_storage_fault(self) -> None:
        # An ambiguous COMMIT may have succeeded. Never retry it or advertise
        # a fabricated receipt; stop new work and signal in-flight handlers.
        with self._handoff:
            self._storage_fault.set()
            for _, cancel in self._workers.values():
                cancel.set()
        try:
            self.ledger.disarm()
        except (sqlite3.Error, OSError):
            # A physically unwritable database cannot persist even disarming.
            # The in-memory fence remains active until a fresh startup audit.
            pass

    def submit(self, raw: bytes, provenance: Provenance) -> Admission:
        try:
            return self._submit(raw, provenance)
        except sqlite3.Error:
            self._trip_storage_fault()
            raise DomainError("STORAGE_UNAVAILABLE", "Локальное хранилище недоступно.") from None
        except OSError as error:
            if error.errno not in {errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS}:
                raise
            self._trip_storage_fault()
            raise DomainError("STORAGE_UNAVAILABLE", "Локальное хранилище недоступно.") from None

    def _submit(self, raw: bytes, provenance: Provenance) -> Admission:
        if self._storage_fault.is_set():
            raise DomainError("STORAGE_UNAVAILABLE", "Локальное хранилище недоступно.")
        command = parse_json(raw)
        frozen = freeze(command)
        request_id = command.get("request_id")
        try:
            if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
                raise ValueError("No canonical request UUID")
        except (ValueError, AttributeError) as error:
            raise DomainError("INVALID_SCHEMA", "Требуется корректный идентификатор запроса.") from error
        rejection: str | None = None
        try:
            validate_shape(command)
            state = self.ledger.state()
            validate_time(command, runner_id=state["runner_id"], epoch=state["epoch"])
            policy = self.policy
            if command["action"] == "CREATE_PROJECT" and command["params"]["source"]["kind"] == "local":
                handle = command["params"]["source"]["import_handle"]
                if self.import_grants is not None:
                    # Revalidate the issued handle for this request only. Remote JSON
                    # never grants a path and shared policy is never mutated.
                    self.import_grants.resolve(handle)
                    policy = replace(policy, import_handles=policy.import_handles | {handle})
            policy.authorize(command, provenance)
            if command["action"] != "CANCEL_OPERATION" and command["action"] not in self.handlers:
                raise DomainError("ACTION_UNAVAILABLE", "Действие пока не настроено.")
        except DomainError as error:
            rejection = error.code
        project = command.get("project_id")
        workspace = command.get("workspace_id")
        action = command.get("action")
        cancel = action == "CANCEL_OPERATION"
        scopes: tuple[str, ...] = ()
        if rejection is None and not cancel:
            if action in _REPOSITORY_ACTIONS:
                scopes = (f"repo:{project}",)
                if action == "CREATE_PROJECT":
                    # Both account repository creation and shared Drive index
                    # mutation need a local global reservation before admission.
                    scopes += (f"global:{command['params']['github_identity']}",
                               "global:drive-installation")
            else:
                scopes = (f"workspace:{project}/{workspace}",)
        with self._handoff:
            if self._storage_fault.is_set():
                raise DomainError("STORAGE_UNAVAILABLE", "Локальное хранилище недоступно.")
            if rejection is None and cancel:
                target = self.ledger.get(command["params"]["target_request_id"])
                if (target is None or target["command"].get("project_id") != project
                        or target["command"].get("workspace_id") != workspace):
                    rejection = "CANCEL_SCOPE_MISMATCH"
                elif target["status"] != "RUNNING":
                    rejection = "TARGET_NOT_RUNNING"
            admitted = self.ledger.admit(command, frozen.digest, provenance.source_id, scopes,
                                         rejection_code=rejection, exempt_capacity=cancel)
            if not admitted.execute:
                return admitted
            if cancel:
                target_id = command["params"]["target_request_id"]
                worker = self._workers.get(target_id)
                if worker is None:
                    receipt = self.ledger.finish(request_id, "BLOCKED", code="WORKER_UNAVAILABLE")
                else:
                    self.ledger.journal(target_id, "cancellation", "CANCEL_REQUESTED", {})
                    worker[1].set()
                    receipt = self.ledger.finish(request_id, "COMPLETED")
                return Admission(receipt=receipt, execute=True, duplicate=False)
            event = threading.Event()
            worker_thread = threading.Thread(target=self._execute,
                                              args=(frozen, event), name=f"cdr-{request_id}",
                                              daemon=True)
            self._workers[request_id] = (worker_thread, event)
            try:
                worker_thread.start()
            except RuntimeError:
                del self._workers[request_id]
                receipt = self.ledger.finish(request_id, "FAULT", code="DISPATCH_FAILED")
                return Admission(receipt=receipt, execute=False, duplicate=False)
            return admitted

    def _execute(self, frozen: FrozenCommand, event: threading.Event) -> None:
        command = frozen.command
        request_id = command["request_id"]
        try:
            try:
                self.ledger.journal(request_id, "dispatch", "STARTED",
                                    {"action": command["action"]})
            except (sqlite3.Error, OSError):
                self._trip_storage_fault()
                return
            try:
                result = self.handlers[command["action"]](frozen, Context(event, self.ledger))
            except sqlite3.Error:
                # The handler may have observed an ambiguous database commit.
                self._trip_storage_fault()
                return
            except Exception:  # noqa: BLE001 -- never expose provider text to receipts
                try:
                    self.ledger.finish(request_id, "FAULT", code="HANDLER_FAILED")
                except (sqlite3.Error, OSError):
                    self._trip_storage_fault()
                return
            try:
                self.ledger.finish(request_id, result.status, code=result.code,
                                   result=result.result)
            except (sqlite3.Error, OSError):
                # Do not retry an ambiguous terminal COMMIT.
                self._trip_storage_fault()
            except Exception:  # noqa: BLE001 -- a malformed result gets a stable fault
                try:
                    self.ledger.finish(request_id, "FAULT", code="HANDLER_FAILED")
                except (sqlite3.Error, OSError):
                    self._trip_storage_fault()
        finally:
            with self._handoff:
                self._workers.pop(request_id, None)

    def shutdown(self, *, timeout: float = 2.0) -> None:
        with self._handoff:
            self._storage_fault.set()
            workers = list(self._workers.values())
            for _, cancel in workers:
                cancel.set()
        try:
            self.ledger.disarm()
        except (sqlite3.Error, OSError):
            # Worker cancellation still happens when durable disarm is unavailable.
            pass
        for worker, _ in workers:
            worker.join(timeout=timeout)
        # Surviving workers retain RUNNING state and locks. Startup conservatively interrupts them;
        # a real executor broker must prove termination before confirm_stopped releases ownership.
