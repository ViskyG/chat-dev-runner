"""Finite Drive mailbox polling while an owner-armed runner is serving locally.

The bridge watches only a caller-registered mailbox. The poller owns durable
freezing and admission; this service owns scheduling and process lifetime. A
disarmed runner never reads or freezes a READY command.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from runner_app.adapters.drive_poller import DrivePoller, PollSchedule

_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class BridgeObservation:
    kind: str
    request_id: str | None = None
    code: str | None = None


def _error_code(error: Exception) -> str:
    candidate = getattr(error, "code", None)
    return candidate if isinstance(candidate, str) and _CODE.fullmatch(candidate) else "POLL_FAILED"


class DrivePollingBridge:
    """Run one known-mailbox poll at a time, with bounded retry backoff."""

    def __init__(self, poller: DrivePoller, *,
                 state_publisher: Callable[[Mapping[str, Any]], None] | None = None,
                 schedule: PollSchedule | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.poller = poller
        self._state_publisher = state_publisher
        self._clock = clock
        self._schedule = schedule or PollSchedule()
        self._state_schedule = PollSchedule()
        self._seen_state: tuple[str, str, bool, tuple[str, ...], tuple[str, ...]] | None = None
        self._published_state: tuple[str, str, bool, tuple[str, ...], tuple[str, ...]] | None = None
        self._tick_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last = BridgeObservation("DISARMED")

    def poll_once_if_due(self) -> BridgeObservation:
        """Publish current authority, then poll only when armed and due."""
        with self._tick_lock:
            now = self._clock()
            try:
                state = self.poller.engine.ledger.state()
                fingerprint = (state["runner_id"], state["epoch"], state["armed"],
                               tuple(self.poller.engine.capabilities()),
                               tuple(sorted(self.poller.engine.policy.approved_profiles)))
                if (not isinstance(fingerprint[0], str)
                        or not isinstance(fingerprint[1], str)
                        or type(fingerprint[2]) is not bool):
                    raise ValueError("Invalid Ledger state")
            except Exception:  # noqa: BLE001 - never expose storage internals in logs
                self.last = BridgeObservation("ERROR", code="STORAGE_UNAVAILABLE")
                return self.last

            if fingerprint != self._seen_state:
                previously_armed = self._seen_state is not None and self._seen_state[2]
                self._seen_state = fingerprint
                self._state_schedule = PollSchedule(
                    self._state_schedule.base_seconds, self._state_schedule.max_seconds,
                    next_due=now,
                )
                if fingerprint[2] and not previously_armed:
                    self._schedule = PollSchedule(
                        self._schedule.base_seconds, self._schedule.max_seconds,
                        next_due=now,
                    )

            if self._state_publisher is not None and fingerprint != self._published_state:
                if not self._state_schedule.due(now):
                    return BridgeObservation("WAITING")
                try:
                    self._state_publisher(state)
                except Exception as error:  # noqa: BLE001 - provider details may contain credentials
                    self._state_schedule = self._state_schedule.failure(now)
                    self.last = BridgeObservation("ERROR", code=_error_code(error))
                    return self.last
                self._state_schedule = self._state_schedule.success(now)
                self._published_state = fingerprint

            if not fingerprint[2]:
                self.last = BridgeObservation("DISARMED")
                return self.last
            if not self._schedule.due(now):
                return BridgeObservation("WAITING")
            try:
                result = self.poller.poll_once()
            except Exception as error:  # noqa: BLE001 - provider details may contain credentials
                self._schedule = self._schedule.failure(now)
                self.last = BridgeObservation("ERROR", code=_error_code(error))
                return self.last
            self._schedule = self._schedule.success(now)
            receipt_code = result.admission.receipt.get("code") if result.admission else None
            result_code = receipt_code if isinstance(receipt_code, str) else None
            self.last = BridgeObservation(result.kind, result.request_id, result_code)
            return self.last

    def _run(self) -> None:
        previous: BridgeObservation | None = None
        while not self._stop.is_set():
            observed = self.poll_once_if_due()
            if observed != previous and observed.kind not in {"DISARMED", "WAITING", "IDLE"}:
                if observed.kind == "ERROR":
                    _LOG.warning("Drive mailbox poll: %s", observed.code)
                else:
                    _LOG.info("Drive mailbox poll: %s request_id=%s code=%s",
                              observed.kind, observed.request_id, observed.code)
            previous = observed
            self._stop.wait(1.0)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("DRIVE_BRIDGE_ALREADY_STARTED")
        self._thread = threading.Thread(target=self._run, name="cdr-drive-poll", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30.0)
