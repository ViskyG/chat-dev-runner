from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from test_intake_v2 import accept, setup

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
