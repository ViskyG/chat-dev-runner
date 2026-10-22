"""Provider-neutral live observation and durable conditional acknowledgement."""
from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from runner_app.domain.commands import DomainError
from runner_app.domain.mailbox_v2 import FrozenMailbox, acknowledge_mailbox, parse_mailbox
from runner_app.storage.intake_v2 import AckProof, IntakeError, IntakeStore


class IntakeStopped(Exception):
    """An observation no longer has authority to admit or baseline intent."""


LocalGuard = Callable[[], AbstractContextManager[None]]


@dataclass(frozen=True)
class Observation:
    payload: bytes
    revision: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


@dataclass(frozen=True)
class AckResult:
    state: str  # APPLIED, CONFLICT, FAILED or UNKNOWN
    revision: str | None = None
    payload_sha256: str | None = None


class MailboxPort(Protocol):
    def read_current(self) -> Observation: ...
    def ack_if_current(self, observed: Observation, payload: bytes, /) -> AckResult: ...


@dataclass(frozen=True)
class IntakeResult:
    kind: str
    request_id: str | None = None
    sequence: int | None = None
    code: str | None = None


def notify_terminal(store: IntakeStore, observer: Callable[[str], None] | None,
                    request_id: str) -> None:
    if observer is not None:
        try:
            observer(request_id)
        except Exception:  # noqa: BLE001 -- delivery never replaces the execution outcome
            store.record_event(request_id, "REMOTE_STATE_FAILED", {"code": "STATE_PUBLICATION_FAILED"})


class IntakeCoordinator:
    def __init__(self, store: IntakeStore, session_id: str, *,
                 terminal_observer: Callable[[str], None] | None = None) -> None:
        self.store, self.session_id = store, session_id
        self.terminal_observer = terminal_observer
        self._locks: dict[str, threading.RLock] = {}
        self._registry_lock = threading.Lock()

    def _lock(self, binding_id: str) -> threading.RLock:
        with self._registry_lock:
            return self._locks.setdefault(binding_id, threading.RLock())

    def _parse(self, binding_id: str, raw: bytes) -> FrozenMailbox:
        frozen = parse_mailbox(raw, installation_id=self.store.installation_id,
                               mailbox_id=binding_id)
        binding = self.store.mailbox(binding_id)
        env = frozen.envelope
        if (env["runner_id"] != binding["runner_id"]
                or env["project_id"] != binding["project_id"]):
            raise IntakeError("MAILBOX_SCOPE_MISMATCH")
        return frozen

    @staticmethod
    def _write(port: MailboxPort, observation: Observation, desired: bytes) -> AckResult:
        try:
            result = port.ack_if_current(observation, desired)
        except OSError:
            return AckResult("UNKNOWN")
        if result.state == "APPLIED" and (
                not result.revision
                or result.payload_sha256 != hashlib.sha256(desired).hexdigest()):
            return AckResult("UNKNOWN")
        if result.state not in {"APPLIED", "CONFLICT", "FAILED", "UNKNOWN"}:
            return AckResult("UNKNOWN")
        return result

    def _baseline(self, binding_id: str, port: MailboxPort,
                  observed: Observation, frozen: FrozenMailbox,
                  local_guard: LocalGuard) -> IntakeResult:
        if frozen.ready:
            desired = acknowledge_mailbox(frozen, status="NEEDS_RESUBMIT", code="OBSERVATION_BASELINE")
        else:
            env = frozen.envelope
            env["slot_token"] = str(uuid4())
            desired = json.dumps(env, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False).encode()
        result = self._write(port, observed, desired)
        if result.state != "APPLIED":
            return IntakeResult("BASELINE_PENDING", code=result.state)
        with local_guard():
            self.store.establish_baseline(
                binding_id, session_id=self.session_id,
                slot_token=json.loads(desired)["slot_token"], revision=str(result.revision),
            )
        return IntakeResult("NEEDS_RESUBMIT" if frozen.ready else "BASELINED")

    def _control(self, request_id: str) -> None:
        if not self.store.claim_control(request_id):
            return
        actor = self.store.get(request_id)
        assert actor is not None
        request = actor["payload"]["request"]
        target = self.store.get(request["target_request_id"])
        if (target is None or target["runner_id"] != actor["runner_id"]
                or target["project_id"] != actor["project_id"]):
            self.store.finish(request_id, status="FAILED", code="TARGET_SCOPE_MISMATCH")
            notify_terminal(self.store, self.terminal_observer, request_id)
            return
        status = (self.store.cancel(target["request_id"]) if request["action"] == "CANCEL"
                  else target["status"])
        self.store.finish(request_id, status="COMPLETED", result={
            "target_request_id": target["request_id"], "status": status,
            "sequence": target["sequence"],
        })
        notify_terminal(self.store, self.terminal_observer, request_id)

    def _finish_ack(self, binding_id: str, candidate: dict[str, Any], proof: AckProof,
                    local_guard: LocalGuard) -> IntakeResult:
        with local_guard():
            sequence = self.store.finalize_ack(candidate["candidate_id"], proof)
        self._control(candidate["request_id"])
        return IntakeResult("ACCEPTED", candidate["request_id"], sequence)

    def _pending(self, binding_id: str, port: MailboxPort, observed: Observation,
                 candidate: dict[str, Any], local_guard: LocalGuard) -> IntakeResult:
        try:
            return self._reconcile_pending(binding_id, port, observed, candidate, local_guard)
        except IntakeStopped:
            # A provider ACK may already have applied. Keep this intent visible
            # for reconciliation, without queuing it or replaying a sent write.
            self.store.ack_proof_lost(candidate["candidate_id"], code="INTAKE_REVOKED")
            raise

    def _reconcile_pending(self, binding_id: str, port: MailboxPort, observed: Observation,
                           candidate: dict[str, Any], local_guard: LocalGuard) -> IntakeResult:
        if observed.sha256 == candidate["ack_sha256"]:
            return self._finish_ack(binding_id, candidate,
                                    AckProof(observed.revision, observed.sha256, binding_id), local_guard)
        if observed.sha256 != candidate["observed_sha256"]:
            if candidate["state"] == "ACK_UNKNOWN":
                self.store.ack_proof_lost(candidate["candidate_id"])
                return IntakeResult("ACK_UNKNOWN", candidate["request_id"], code="ACK_PROOF_LOST")
            self.store.ack_failed(candidate["candidate_id"], code="INPUT_CHANGED")
            return IntakeResult("INPUT_CHANGED", candidate["request_id"])
        desired = bytes(candidate["ack_payload"])
        result = self._write(port, observed, desired)
        if result.state == "APPLIED":
            return self._finish_ack(binding_id, candidate,
                                    AckProof(str(result.revision), str(result.payload_sha256), binding_id),
                                    local_guard)
        code = "INPUT_CHANGED" if result.state == "CONFLICT" else "ACK_FAILED"
        self.store.ack_failed(candidate["candidate_id"], code=code, uncertain=result.state == "UNKNOWN")
        return IntakeResult("ACK_UNKNOWN" if result.state == "UNKNOWN" else code,
                            candidate["request_id"], code=code)

    def poll_once(self, binding_id: str, port: MailboxPort, *,
                  local_guard: LocalGuard = nullcontext) -> IntakeResult:
        with self._lock(binding_id):
            frozen: FrozenMailbox | None = None
            try:
                observed = port.read_current()
            except OSError:
                self.store.observation_gap(binding_id)
                return IntakeResult("OBSERVATION_GAP")
            if not observed.revision:
                self.store.observation_gap(binding_id)
                return IntakeResult("OBSERVATION_GAP", code="REVISION_MISSING")
            try:
                candidate = self.store.pending_candidate(binding_id)
                if candidate is not None:
                    return self._pending(binding_id, port, observed, candidate, local_guard)
                frozen = self._parse(binding_id, observed.payload)
                binding = self.store.mailbox(binding_id)
                if not binding["continuous"]:
                    return self._baseline(binding_id, port, observed, frozen, local_guard)
                if not frozen.ready:
                    return IntakeResult("IDLE")
                desired = acknowledge_mailbox(frozen)
                with local_guard():
                    reservation = self.store.reserve(
                        binding_id, frozen, revision=observed.revision,
                        observed_sha256=observed.sha256, ack_payload=desired,
                    )
                return self._pending(binding_id, port, observed,
                                     self.store.candidate(reservation.candidate_id), local_guard)
            except (DomainError, IntakeError) as error:
                self.store.record_diagnostic(binding_id, error.code, observed.sha256)
                if frozen is not None and error.code in {"QUEUE_FULL", "REQUEST_ID_REUSED", "STALE_SLOT"}:
                    rejected = acknowledge_mailbox(frozen, status="REJECTED", code=error.code)
                    result = self._write(port, observed, rejected)
                    if result.state == "APPLIED":
                        with local_guard():
                            self.store.establish_baseline(
                                binding_id, session_id=self.session_id,
                                slot_token=json.loads(rejected)["slot_token"],
                                revision=str(result.revision),
                            )
                    elif result.state == "UNKNOWN":
                        self.store.observation_gap(binding_id)
                return IntakeResult("REJECTED", code=error.code)
