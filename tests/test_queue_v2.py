from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from test_intake_v2 import Port, accept, setup

from runner_app.domain.commands import freeze
from runner_app.engine.queue_v2 import QueueWorker, TaskOutcome, V2Service
from runner_app.storage.ledger import Ledger, LedgerError


def legacy_command(ledger):
    now = datetime.now(timezone.utc)
    state = ledger.state()
    return {"schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
            "runner_id": state["runner_id"], "epoch": state["epoch"],
            "issued_at": now.isoformat(), "expires_at": (now + timedelta(minutes=5)).isoformat(),
            "action": "FETCH", "project_id": "project-1", "workspace_id": None,
            "expected_head": None, "params": {}, "intent": "Fetch one managed project"}


def test_v1_v2_share_one_atomic_lane_and_v1_keeps_explicit_admission(tmp_path):
    store, session, coordinator, port = setup(tmp_path)
    ledger = Ledger(store.database, runner_id="runner-1", single_execution_lane=True)
    ledger.startup()
    ledger.arm()
    coordinator.poll_once("slot", port)
    old = legacy_command(ledger)
    assert ledger.admit(old, freeze(old).digest, "owner", ("repo:project-1",)).execute
    queued = accept(coordinator, port)
    assert store.claim_next(session) is None
    ledger.finish(old["request_id"], "COMPLETED")
    assert store.claim_next(session)["request_id"] == queued
    rejected = legacy_command(ledger)
    assert ledger.admit(rejected, freeze(rejected).digest, "owner", ("repo:project-1",)).receipt["code"] == "BUSY"
    store.finish(queued, status="COMPLETED")
    next_old = legacy_command(ledger)
    assert ledger.admit(next_old, freeze(next_old).digest, "owner", ("repo:project-1",)).execute


def test_remote_cancel_is_processed_while_handler_holds_execution_lane(tmp_path):
    store, session, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    first, second = accept(coordinator, port), accept(coordinator, port)
    started = threading.Event()
    calls = []

    def handler(request, cancel):
        calls.append(request["request_id"])
        store.mark_running(request["request_id"], {"fixture": "held-handler"})
        started.set()
        assert cancel.wait(3), "cancellation did not bypass lane"
        return TaskOutcome("CANCELLED", "CANCEL_REQUESTED")

    worker = QueueWorker(store, session, handler)
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    assert started.wait(2)
    port.ready(request={"request_id": None, "action": "CANCEL", "target_request_id": first})
    assert coordinator.poll_once("slot", port).kind == "ACCEPTED"
    thread.join(3)
    assert not thread.is_alive()
    assert calls == [first]
    assert store.get(first)["status"] == "CANCELLED"
    assert store.get(second)["status"] == "QUEUED"
    assert store.claim_next(session)["request_id"] == second


def test_handler_fault_after_running_fences_next_task(tmp_path):
    store, session, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    first, _ = accept(coordinator, port), accept(coordinator, port)

    def handler(request, cancel):
        store.mark_running(request["request_id"], {"fixture": True})
        raise RuntimeError("possible child survived")

    assert QueueWorker(store, session, handler).run_once()
    assert store.get(first)["status"] == "NEEDS_RECONCILIATION"
    assert store.claim_next(session) is None
    ledger = Ledger(store.database, runner_id="runner-1", single_execution_lane=True)
    with pytest.raises(LedgerError, match="Reconcile"):
        ledger.arm()


def test_stopping_service_suspends_pending_without_new_poll_or_execution(tmp_path):
    import time

    store, session, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    request = accept(coordinator, port)
    writes = port.writes
    service = V2Service(store, session, [("slot", port)], lambda *_: TaskOutcome("COMPLETED"),
                        intake_enabled=lambda: True)
    service.close(deadline=time.monotonic() + 1)
    assert store.get(request)["status"] == "SUSPENDED_RESTART"
    assert port.writes == writes


def test_intake_disarm_breaks_continuity_and_requires_new_live_submission(tmp_path):
    store, session, _, port = setup(tmp_path)
    armed = [True]
    service = V2Service(store, session, [("slot", port)], lambda *_: TaskOutcome("COMPLETED"),
                        intake_enabled=lambda: armed[0])
    service.poll_once()
    armed[0] = False
    service.poll_once()
    port.ready()
    armed[0] = True
    service.poll_once()
    assert service.last_states["slot"] == "NEEDS_RESUBMIT"
    assert store.list_requests() == []


def test_provider_outage_preserves_bounded_intake_and_remote_cancel(tmp_path):
    store, session, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    request_id = accept(coordinator, port)
    running, cancelled, saturated, release = (threading.Event() for _ in range(4))
    reads: dict[str, int] = {}
    lock = threading.Lock()

    class BlockedDocs(Port):
        def __init__(self, binding_id):
            super().__init__()
            self.binding_id = binding_id

        def read_current(self):
            with lock:
                reads[self.binding_id] = reads.get(self.binding_id, 0) + 1
                if sum(reads.values()) == 3:
                    saturated.set()
            assert release.wait(3)
            return super().read_current()

    bindings = []
    for index in range(8):
        binding_id = f"docs-{index}"
        store.bind(binding_id, runner_id="runner-1", project_id="project-1", provider="drive",
                   coordinates={"document_id": binding_id, "tab_id": "t.0"}, session_id=session)
        bindings.append((binding_id, BlockedDocs(binding_id)))
    bindings.append(("slot", port))

    def handler(request, cancel):
        store.mark_running(request["request_id"], {"fixture": "provider-outage"})
        running.set()
        assert cancel.wait(3)
        cancelled.set()
        return TaskOutcome("CANCELLED")

    service = V2Service(store, session, bindings, handler,
                        intake_enabled=lambda: True, poll_seconds=0.1)
    try:
        service.start()
        assert running.wait(1)
        assert saturated.wait(1)
        port.ready(request={"request_id": None, "action": "CANCEL", "target_request_id": request_id})
        assert cancelled.wait(1), "Docs outage blocked healthy GitHub cancellation"
        assert len(service._polls) <= 4
        assert len(reads) == 3 and set(reads.values()) == {1}, "A blocked binding overlapped itself"
    finally:
        service.request_stop()
        release.set()
        service.close(deadline=time.monotonic() + 2)
    assert store.get(request_id)["status"] == "CANCELLED"


def test_delayed_read_after_disarm_requires_fresh_submission(tmp_path, monkeypatch):
    store, session, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    port.ready()
    observed, release, gap, rejected = (threading.Event() for _ in range(4))
    original_read, original_ack = port.read_current, port.ack_if_current
    original_gap = store.observation_gap
    armed = [True]

    def delayed_read():
        value = original_read()
        if not observed.is_set():
            observed.set()
            assert release.wait(3)
        return value

    def observation_gap(binding_id, **kwargs):
        original_gap(binding_id, **kwargs)
        gap.set()

    def ack(current, payload):
        result = original_ack(current, payload)
        rejected.set()
        return result

    monkeypatch.setattr(port, "read_current", delayed_read)
    monkeypatch.setattr(port, "ack_if_current", ack)
    monkeypatch.setattr(store, "observation_gap", observation_gap)
    service = V2Service(store, session, [("slot", port)], lambda *_: TaskOutcome("COMPLETED"),
                        intake_enabled=lambda: armed[0], poll_seconds=0.1)
    try:
        service.start()
        assert observed.wait(1)
        armed[0] = False
        assert gap.wait(1)
        armed[0] = True
        release.set()
        assert rejected.wait(1)
    finally:
        release.set()
        service.close(deadline=time.monotonic() + 2)
    assert port.value["receipt"]["status"] == "NEEDS_RESUBMIT"
    assert store.list_requests() == []


@pytest.mark.parametrize("phase", ["provider_ack", "after_ack_check"])
@pytest.mark.parametrize("ready", [False, True])
def test_revoked_ack_cannot_restore_baseline_or_enqueue(tmp_path, monkeypatch, phase, ready):
    store, session, coordinator, port = setup(tmp_path)
    if ready:
        coordinator.poll_once("slot", port)
        port.ready()
    armed = [True]
    entered, release = threading.Event(), threading.Event()
    service = V2Service(store, session, [("slot", port)], lambda *_: TaskOutcome("COMPLETED"),
                        intake_enabled=lambda: armed[0])
    original = port.ack_if_current if phase == "provider_ack" else service.coordinator._write

    def delayed(*args):
        result = original(*args)
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
        return result

    monkeypatch.setattr(port if phase == "provider_ack" else service.coordinator,
                        "ack_if_current" if phase == "provider_ack" else "_write", delayed)
    poll = threading.Thread(target=service.poll_once)
    try:
        poll.start()
        assert entered.wait(1)
        armed[0] = False
        assert not service._enabled()
        assert store.mailbox("slot")["continuous"] == 0
        port.ready()  # A fresh token is visible remotely, but intake is disarmed.
        release.set()
        poll.join(timeout=1)
        assert not poll.is_alive()
        assert store.mailbox("slot")["continuous"] == 0
        assert store.claim_next(session) is None
        requests = store.list_requests()
        if ready:
            assert len(requests) == 1
            assert requests[0]["status"] == "NEEDS_RECONCILIATION"
            assert requests[0]["code"] == "INTAKE_REVOKED"
        else:
            assert requests == []
        armed[0] = True
        service.poll_once()
        assert service.last_states["slot"] == "NEEDS_RESUBMIT"
        assert store.claim_next(session) is None
        assert store.list_requests() == requests
    finally:
        release.set()
        poll.join(timeout=1)
        service.close(deadline=time.monotonic() + 1)


def test_shutdown_does_not_wait_for_blocked_read_or_ack_after_return(tmp_path, monkeypatch):
    store, session, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    port.ready()
    observed, release = threading.Event(), threading.Event()
    original_read = port.read_current

    def delayed_read():
        value = original_read()
        observed.set()
        assert release.wait(3)
        return value

    monkeypatch.setattr(port, "read_current", delayed_read)
    service = V2Service(store, session, [("slot", port)], lambda *_: TaskOutcome("COMPLETED"),
                        intake_enabled=lambda: True, poll_seconds=0.1)
    writes = port.writes
    try:
        service.start()
        assert observed.wait(1)
        started = time.monotonic()
        service.close(deadline=started + 0.05)
        assert time.monotonic() - started < 0.5
        # A late old poll must not clear a replacement session's baseline.
        replacement = store.start_session("runner-1")
        store.bind("slot", runner_id="runner-1", project_id="project-1", provider="github",
                   coordinates={"repository": "owner/project", "branch": "commands", "path": "slot.json"},
                   session_id=replacement)
        store.establish_baseline("slot", session_id=replacement,
                                 slot_token=port.value["slot_token"], revision=str(port.revision))
    finally:
        release.set()
        for thread in tuple(service._polls.values()):
            thread.join(timeout=1)
    assert port.writes == writes
    assert store.list_requests() == []
    assert store.mailbox("slot")["continuous"] == 1
