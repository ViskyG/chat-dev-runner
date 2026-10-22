"""One durable execution lane with independent intake and cancellation observation."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from runner_app.engine.intake_v2 import (
    AckResult,
    IntakeCoordinator,
    IntakeStopped,
    MailboxPort,
    Observation,
    notify_terminal,
)
from runner_app.storage.intake_v2 import IntakeError, IntakeStore

_LOG = logging.getLogger(__name__)
_MAX_INTAKE_POLLS = 4


@dataclass(frozen=True)
class _LiveMailbox:
    port: MailboxPort
    live: Callable[[], bool]

    def _check(self) -> None:
        if not self.live():
            raise IntakeStopped

    def read_current(self) -> Observation:
        self._check()
        try:
            observed = self.port.read_current()
        except OSError:
            self._check()
            raise
        self._check()
        return observed

    def ack_if_current(self, observed: Observation, payload: bytes, /) -> AckResult:
        self._check()
        try:
            result = self.port.ack_if_current(observed, payload)
        except OSError:
            self._check()
            raise
        self._check()
        return result


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
        self._generation = 0
        self._authority_lock = threading.RLock()
        self._polls: dict[str, threading.Thread] = {}
        self._next_due: dict[str, float] = {}
        self._cursor = 0
        self._providers = {binding_id: store.mailbox(binding_id)["provider"]
                           for binding_id, _ in self.bindings}
        # Leave capacity for other providers during an outage of one provider.
        self._provider_limit = max(1, _MAX_INTAKE_POLLS - len(set(self._providers.values())) + 1)
        self.last_states: dict[str, str] = {}

    def _enabled(self) -> bool:
        with self._authority_lock:
            return self._enabled_locked()

    def _enabled_locked(self) -> bool:
        if self._stop.is_set():
            return False
        enabled = self.intake_enabled()
        if not enabled:
            if self._was_enabled:
                self._generation += 1
                for binding_id, _ in self.bindings:
                    self.store.observation_gap(binding_id, session_id=self.session_id)
            self._was_enabled = False
            return False
        self._was_enabled = True
        return True

    def _poll(self, binding_id: str, port: MailboxPort, generation: int) -> None:
        guarded = _LiveMailbox(port, lambda: not self._stop.is_set()
                               and generation == self._generation and self.intake_enabled())

        @contextmanager
        def local_guard() -> Iterator[None]:
            # Serialize only local admission/baseline commits with revocation.
            # Provider I/O remains outside this short critical section.
            with self._authority_lock:
                guarded._check()
                yield

        try:
            result = self.coordinator.poll_once(binding_id, guarded, local_guard=local_guard)
        except IntakeStopped:
            self.store.observation_gap(binding_id, session_id=self.session_id)
            return
        self.last_states[binding_id] = result.code or result.kind

    def poll_once(self) -> None:
        """Perform one synchronous pass for explicit callers before start()."""
        if self._thread is not None:
            raise RuntimeError("V2_SERVICE_ALREADY_STARTED")
        if not self._enabled():
            return
        for binding_id, port in self.bindings:
            if self._stop.is_set():
                break
            self._poll(binding_id, port, self._generation)

    def _run_poll(self, binding_id: str, port: MailboxPort, generation: int) -> None:
        try:
            self._poll(binding_id, port, generation)
        except Exception:  # noqa: BLE001 -- provider ports sanitize their own observation failures
            _LOG.error("V2_INTAKE_STORAGE_OR_STATE_FAULT")
            self.request_stop()

    def _schedule_once(self) -> None:
        now = time.monotonic()
        for binding_id, thread in tuple(self._polls.items()):
            if not thread.is_alive():
                del self._polls[binding_id]
                self._next_due[binding_id] = now + self.poll_seconds
        if not self._enabled():
            return
        active: dict[str, int] = {}
        for binding_id in self._polls:
            provider = self._providers[binding_id]
            active[provider] = active.get(provider, 0) + 1
        count = len(self.bindings)
        for _ in range(count):
            if len(self._polls) >= _MAX_INTAKE_POLLS or self._stop.is_set():
                break
            binding_id, port = self.bindings[self._cursor]
            self._cursor = (self._cursor + 1) % count
            provider = self._providers[binding_id]
            if (binding_id in self._polls or self._next_due.get(binding_id, 0) > now
                    or active.get(provider, 0) >= self._provider_limit):
                continue
            thread = threading.Thread(target=self._run_poll,
                                      args=(binding_id, port, self._generation),
                                      name="cdr-v2-poll", daemon=True)
            self._polls[binding_id] = thread
            active[provider] = active.get(provider, 0) + 1
            thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._schedule_once()
            except Exception:  # noqa: BLE001 -- retain history and fence intake on storage/state failure
                _LOG.error("V2_INTAKE_STORAGE_OR_STATE_FAULT")
                self.request_stop()
                return
            self._stop.wait(min(0.05, self.poll_seconds))

    def start(self) -> None:
        if self._thread is not None:
            return
        self.worker.start()
        self._thread = threading.Thread(target=self._loop, name="cdr-v2-intake", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        with self._authority_lock:
            self._stop.set()
            self.worker.request_stop()

    def close(self, *, deadline: float) -> None:
        self.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        for thread in tuple(self._polls.values()):
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self.worker.close(deadline=deadline)
        self.store.end_session(self.session_id)
