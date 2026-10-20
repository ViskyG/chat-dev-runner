"""One durable execution lane with independent intake and cancellation observation."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from runner_app.engine.intake_v2 import IntakeCoordinator, MailboxPort, notify_terminal
from runner_app.storage.intake_v2 import IntakeError, IntakeStore

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskOutcome:
    status: str
    code: str | None = None
    result: Mapping[str, Any] = field(default_factory=dict)
    cleanup_verified: bool = True


class TaskHandler(Protocol):
    def __call__(self, request: dict[str, Any], cancel: threading.Event) -> TaskOutcome: ...


class QueueWorker:
    def __init__(self, store: IntakeStore, session_id: str, handler: TaskHandler, *,
                 terminal_observer: Callable[[str], None] | None = None) -> None:
        self.store, self.session_id, self.handler = store, session_id, handler
        self.terminal_observer = terminal_observer
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_cancel: threading.Event | None = None

    def _watch_cancel(self, request_id: str, cancel: threading.Event,
                      finished: threading.Event) -> None:
        while not finished.wait(0.1):
            if self._stop.is_set():
                cancel.set()
                return
            try:
                request = self.store.get(request_id)
                if request is None or request["cancel_requested"]:
                    cancel.set()
                    return
            except (OSError, IntakeError):
                cancel.set()
                return

    def run_once(self) -> bool:
        if self._stop.is_set():
            return False
        request = self.store.claim_next(self.session_id)
        if request is None:
            return False
        request_id = request["request_id"]
        cancel, finished = threading.Event(), threading.Event()
        self._active_cancel = cancel
        watcher = threading.Thread(target=self._watch_cancel, args=(request_id, cancel, finished),
                                   name="cdr-v2-cancel", daemon=True)
        watcher.start()
        try:
            if request["cancel_requested"]:
                outcome = TaskOutcome("CANCELLED", "CANCELLED_BEFORE_START")
            else:
                try:
                    outcome = self.handler(request, cancel)
                except Exception:  # noqa: BLE001 -- isolate handler failure without leaking project data
                    current = self.store.get(request_id)
                    outcome = TaskOutcome("FAILED", "TASK_HANDLER_FAILED",
                                          cleanup_verified=bool(current and current["status"] == "STARTING"))
            self.store.finish(request_id, status=outcome.status, code=outcome.code,
                              result=outcome.result, cleanup_verified=outcome.cleanup_verified)
            notify_terminal(self.store, self.terminal_observer, request_id)
        finally:
            finished.set()
            watcher.join(timeout=1)
            self._active_cancel = None
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.run_once():
                    self._stop.wait(0.2)
            except Exception:  # noqa: BLE001 -- storage ambiguity fences the worker; no automatic replay
                _LOG.error("V2_WORKER_STORAGE_OR_STATE_FAULT")
                self._stop.set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="cdr-v2-worker", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()
        if self._active_cancel is not None:
            self._active_cancel.set()

    def close(self, *, deadline: float) -> None:
        self.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, deadline - time.monotonic()))


class V2Service:
    def __init__(self, store: IntakeStore, session_id: str,
                 bindings: Sequence[tuple[str, MailboxPort]], handler: TaskHandler,
                 *, intake_enabled: Callable[[], bool], poll_seconds: float = 2.0,
                 terminal_observer: Callable[[str], None] | None = None) -> None:
        if not 0.1 <= poll_seconds <= 60:
            raise ValueError("Invalid polling interval")
        self.store, self.session_id = store, session_id
        self.bindings = tuple(bindings)
        self.coordinator = IntakeCoordinator(store, session_id, terminal_observer=terminal_observer)
        self.worker = QueueWorker(store, session_id, handler, terminal_observer=terminal_observer)
        self.intake_enabled, self.poll_seconds = intake_enabled, poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._was_enabled = False
        self.last_states: dict[str, str] = {}

    def poll_once(self) -> None:
        enabled = self.intake_enabled()
        if not enabled:
            if self._was_enabled:
                for binding_id, _ in self.bindings:
                    self.store.observation_gap(binding_id)
            self._was_enabled = False
            return
        self._was_enabled = True
        for binding_id, port in self.bindings:
            if self._stop.is_set():
                break
            result = self.coordinator.poll_once(binding_id, port)
            self.last_states[binding_id] = result.code or result.kind

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 -- retain history and fence intake on storage/state failure
                _LOG.error("V2_INTAKE_STORAGE_OR_STATE_FAULT")
                self.request_stop()
                return
            self._stop.wait(self.poll_seconds)

    def start(self) -> None:
        if self._thread is not None:
            return
        self.worker.start()
        self._thread = threading.Thread(target=self._loop, name="cdr-v2-intake", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()
        self.worker.request_stop()

    def close(self, *, deadline: float) -> None:
        self.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self.worker.close(deadline=deadline)
        self.store.end_session(self.session_id)
