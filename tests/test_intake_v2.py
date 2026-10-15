from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from runner_app.engine.intake_v2 import AckResult, IntakeCoordinator, Observation
from runner_app.storage.database import _MIGRATIONS, Database
from runner_app.storage.intake_v2 import IntakeStore


class Port:
    def __init__(self, *, dispatch="DRAFT"):
        self.value = {"protocol": "cdr-mailbox/2", "runner_id": "runner-1",
                      "project_id": "project-1", "slot_token": str(uuid4()),
                      "dispatch": dispatch, "receipt": None,
                      "request": {"request_id": None, "action": "RUN_TASK", "task": "test",
                                  "source": {"kind": "github_branch", "branch": "feature/test"},
                                  "execution": {"mode": "native", "expected_os": "linux",
                                                "timeout_seconds": 10}}}
        self.revision = 1
        self.writes = 0
        self.lost = False
        self.unavailable = False
        self.conflict = False

    def read_current(self):
        if self.unavailable:
            raise OSError("offline")
        return Observation(json.dumps(self.value, sort_keys=True, separators=(",", ":")).encode(),
                           str(self.revision))

    def ack_if_current(self, observed, payload):
        if self.conflict:
            self.value["request"]["task"] = "changed-by-author"
            self.revision += 1
            return AckResult("CONFLICT")
        if observed.revision != str(self.revision):
            return AckResult("CONFLICT")
        self.value = json.loads(payload)
        self.revision += 1
        self.writes += 1
        if self.lost:
            self.lost = False
            return AckResult("UNKNOWN")
        return AckResult("APPLIED", str(self.revision), hashlib.sha256(payload).hexdigest())

    def ready(self, *, request=None):
        if request is not None:
            self.value["request"] = request
        self.value["dispatch"] = "READY"
        self.revision += 1


def setup(tmp_path, *, limit=100, dispatch="DRAFT"):
    store = IntakeStore(Database(tmp_path / "state.sqlite"), queue_limit=limit)
    session = store.start_session("runner-1")
    store.bind("slot", runner_id="runner-1", project_id="project-1", provider="github",
               coordinates={"repository": "owner/project", "branch": "commands", "path": "slot.json"},
               session_id=session)
    return store, session, IntakeCoordinator(store, session), Port(dispatch=dispatch)


def accept(coordinator, port):
    port.ready()
    result = coordinator.poll_once("slot", port)
    assert result.kind == "ACCEPTED", result
    return result.request_id


def test_schema_9_history_survives_additive_migration(tmp_path):
    path = tmp_path / "state.sqlite"
    c = sqlite3.connect(path)
    path.chmod(0o600)
    for version in range(1, 10):
        for statement in _MIGRATIONS[version]:
            c.execute(statement)
    c.execute("PRAGMA user_version=9")
    c.execute("INSERT INTO projects VALUES('old','Old project','READY','{}','before','before')")
    c.commit()
    c.close()
    store = IntakeStore(Database(path))
    with store._connection() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 10
        assert tuple(c.execute("SELECT * FROM projects").fetchone()) == (
            "old", "Old project", "READY", "{}", "before", "before")
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert store.installation_id == IntakeStore(Database(path)).installation_id


def test_baseline_ready_is_not_an_event_and_new_ready_queues_once(tmp_path):
    store, session, coordinator, port = setup(tmp_path, dispatch="READY")
    assert coordinator.poll_once("slot", port).kind == "NEEDS_RESUBMIT"
    assert store.list_requests() == []
    assert store.claim_next(session) is None
    request = accept(coordinator, port)
    assert store.get(request)["status"] == "QUEUED"
    assert coordinator.poll_once("slot", port).kind == "IDLE"
    assert len(store.list_requests()) == 1
    assert store.claim_next(session)["request_id"] == request
    assert store.claim_next(session) is None


def test_lost_ack_response_requires_exact_current_proof(tmp_path):
    store, session, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    port.ready()
    port.lost = True
    result = coordinator.poll_once("slot", port)
    assert result.kind == "ACK_UNKNOWN"
    assert store.claim_next(session) is None
    writes = port.writes
    assert coordinator.poll_once("slot", port).kind == "ACCEPTED"
    assert port.writes == writes
    assert store.claim_next(session)["request_id"] == result.request_id


def test_lost_ack_then_new_draft_does_not_guess_acceptance(tmp_path):
    store, session, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    port.ready()
    port.lost = True
    old = coordinator.poll_once("slot", port).request_id
    port.value["dispatch"] = "DRAFT"
    port.value["request"]["task"] = "next"
    port.revision += 1
    assert coordinator.poll_once("slot", port).code == "ACK_PROOF_LOST"
    assert store.get(old)["status"] == "NEEDS_RECONCILIATION"
    assert store.claim_next(session) is None
    assert coordinator.poll_once("slot", port).kind == "BASELINED"
    new = accept(coordinator, port)
    assert new != old
    assert store.claim_next(session)["request_id"] == new


def test_fifo_and_control_cancel_bypass_full_queue(tmp_path):
    store, session, coordinator, port = setup(tmp_path, limit=2)
    coordinator.poll_once("slot", port)
    first = accept(coordinator, port)
    store.claim_next(session)
    store.mark_running(first, {"fixture": True})
    second, third = accept(coordinator, port), accept(coordinator, port)
    port.ready()
    assert coordinator.poll_once("slot", port).code == "QUEUE_FULL"
    assert port.value["dispatch"] == "REJECTED"
    assert port.value["receipt"]["code"] == "QUEUE_FULL"
    port.ready(request={"request_id": None, "action": "CANCEL", "target_request_id": second})
    cancellation = coordinator.poll_once("slot", port)
    assert cancellation.kind == "ACCEPTED"
    assert cancellation.sequence is None
    assert store.get(cancellation.request_id)["status"] == "COMPLETED"
    assert store.get(second)["status"] == "CANCELLED"
    assert store.claim_next(session) is None
    store.finish(first, status="COMPLETED")
    assert store.claim_next(session)["request_id"] == third


def test_restart_suspends_queue_and_retains_unknown_active_lane(tmp_path):
    store, session, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    active, queued = accept(coordinator, port), accept(coordinator, port)
    store.claim_next(session)
    store.mark_running(active, {"fixture": True})
    new_session = store.start_session("runner-1")
    assert store.get(queued)["status"] == "SUSPENDED_RESTART"
    assert store.get(active)["status"] == "INTERRUPTED"
    assert store.claim_next(new_session) is None
    store.reconcile_lane_absent(active, proof={"request_id": active, "process_state": "ABSENT"})
    assert store.claim_next(new_session) is None


def test_atomic_claim_across_independent_connections(tmp_path):
    store, session, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    first, _ = accept(coordinator, port), accept(coordinator, port)
    peers = [IntakeStore(store.database), IntakeStore(store.database)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda peer: peer.claim_next(session), peers))
    assert [r["request_id"] for r in claims if r is not None] == [first]
    assert claims.count(None) == 1


def test_same_explicit_id_across_providers_is_one_request(tmp_path):
    store, session, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    explicit = str(uuid4())
    port.value["request"]["request_id"] = explicit
    assert accept(coordinator, port) == explicit
    other = Port()
    other.value["request"]["request_id"] = explicit
    store.bind("docs", runner_id="runner-1", project_id="project-1", provider="drive",
               coordinates={"document_id": "known", "tab_id": "t.0"}, session_id=session)
    assert coordinator.poll_once("docs", other).kind == "BASELINED"
    other.ready()
    result = coordinator.poll_once("docs", other)
    assert result.request_id == explicit and result.sequence == 1
    assert len(store.list_requests()) == 1
    other.ready()
    other.value["request"]["task"] = "changed"
    assert coordinator.poll_once("docs", other).code == "REQUEST_ID_REUSED"
    assert store.claim_next(session)["request_id"] == explicit


def test_edit_between_read_and_ack_is_preserved_without_execution(tmp_path):
    store, session, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    port.ready()
    port.conflict = True
    result = coordinator.poll_once("slot", port)
    assert result.kind == "INPUT_CHANGED"
    assert port.value["request"]["task"] == "changed-by-author"
    assert store.get(result.request_id)["status"] == "REJECTED"
    assert store.claim_next(session) is None


def test_provider_gap_requires_remote_resubmission(tmp_path):
    store, session, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    port.unavailable = True
    assert coordinator.poll_once("slot", port).kind == "OBSERVATION_GAP"
    port.ready()
    port.unavailable = False
    assert coordinator.poll_once("slot", port).kind == "NEEDS_RESUBMIT"
    assert store.claim_next(session) is None


def test_unknown_cleanup_keeps_lane_blocked(tmp_path):
    store, session, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    first, _ = accept(coordinator, port), accept(coordinator, port)
    store.claim_next(session)
    store.finish(first, status="FAILED", cleanup_verified=False)
    assert store.get(first)["status"] == "NEEDS_RECONCILIATION"
    assert store.claim_next(session) is None


def test_v2_intent_and_sequence_are_immutable(tmp_path):
    store, _, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    request = accept(coordinator, port)
    with store._transaction() as c:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE v2_requests SET digest='changed' WHERE request_id=?", (request,))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("DELETE FROM v2_queue WHERE request_id=?", (request,))
