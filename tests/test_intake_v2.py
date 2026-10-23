from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from runner_app.engine.intake_v2 import AckResult, IntakeCoordinator, Observation
from runner_app.storage.database import _MIGRATIONS, SCHEMA_VERSION, Database
from runner_app.storage.intake_v2 import IntakeError, IntakeStore


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
        assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
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


def recovery_fixture(tmp_path):
    store, old_session, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    active, queued = accept(coordinator, port), accept(coordinator, port)
    store.claim_next(old_session)
    store.mark_running(active, {"source": "original frozen identity"})
    session = store.start_session("runner-1")
    return store, old_session, session, active, queued


def test_recovery_candidates_require_prior_ended_session_and_retained_lane(tmp_path):
    store, old, session, active, queued = recovery_fixture(tmp_path)
    assert [row["request_id"] for row in store.recovery_candidates("runner-1", session)] == [active]
    assert store.get(queued)["status"] == "SUSPENDED_RESTART"
    with pytest.raises(IntakeError, match="SESSION_ENDED"):
        store.recovery_candidates("runner-1", old)
    other = store.start_session("runner-2")
    assert store.recovery_candidates("runner-2", other) == []
    with pytest.raises(IntakeError, match="WRONG_RUNNER"):
        store.recovery_candidates("runner-2", session)
    store.reconcile_lane_absent(active, proof={"request_id": active, "process_state": "ABSENT"})
    assert store.get(active)["status"] == "INTERRUPTED"
    assert store.recovery_candidates("runner-1", session) == []
    with pytest.raises(IntakeError, match="RECOVERY_NOT_APPLICABLE"):
        store.finish_recovery(active, session, status="INTERRUPTED", cleanup_verified=True)


@pytest.mark.parametrize("terminal", ["INTERRUPTED", "COMPLETED", "CANCELLED"])
def test_verified_recovery_preserves_journal_and_original_admission_then_releases_lane(tmp_path, terminal):
    store, old, session, active, queued = recovery_fixture(tmp_path)
    history = store.journal(active)
    result = {"execution": {"state": terminal}, "cleanup_verified": True}
    store.finish_recovery(active, session, status=terminal, code="RECOVERED", result=result,
                          cleanup_verified=True)
    row = store.get(active)
    assert row["session_id"] == old
    assert (row["status"], row["code"], row["result"]) == (terminal, "RECOVERED", result)
    assert store.journal(active) == history + [
        ("RECOVERY", {"prior_session_id": old, "session_id": session,
                      "prior_status": "INTERRUPTED", "cleanup_verified": True}),
        (terminal, {"code": "RECOVERED"}),
    ]
    assert store.recovery_candidates("runner-1", session) == []
    with store._connection() as connection:
        assert connection.execute("SELECT * FROM execution_lane").fetchall() == []
    assert store.get(queued)["status"] == "SUSPENDED_RESTART"
    assert store.claim_next(session) is None


def test_uncertain_recovery_keeps_lane_and_evidence_for_later_reconciliation(tmp_path):
    store, old, session, active, queued = recovery_fixture(tmp_path)
    history = store.journal(active)
    store.finish_recovery(active, session, status="COMPLETED", result={"evidence": "uncertain"},
                          cleanup_verified=False)
    row = store.get(active)
    assert row["status"] == "NEEDS_RECONCILIATION"
    assert row["code"] == "CLEANUP_PROOF_REQUIRED"
    assert row["session_id"] == old
    assert row["result"] == {"evidence": "uncertain"}
    assert store.journal(active)[:len(history)] == history
    assert [row["request_id"] for row in store.recovery_candidates("runner-1", session)] == [active]
    assert store.claim_next(session) is None
    assert store.get(queued)["status"] == "SUSPENDED_RESTART"


@pytest.mark.parametrize("guard", ["stale", "wrong_runner", "same_session", "manual", "queued"])
def test_recovery_transition_refuses_stale_or_unowned_authority_without_history_changes(tmp_path, guard):
    store, old, session, active, queued = recovery_fixture(tmp_path)
    target = active
    if guard == "stale":
        store.start_session("runner-1")
    elif guard == "wrong_runner":
        session = store.start_session("runner-2")
    elif guard == "same_session":
        # An uncertain execution in the current session is not restart recovery.
        with store._transaction() as connection:
            connection.execute("UPDATE v2_sessions SET ended_at=? WHERE session_id=?", ("ended", session))
            connection.execute("UPDATE v2_sessions SET ended_at=NULL WHERE session_id=?", (old,))
        session = old
    elif guard == "manual":
        store.reconcile_lane_absent(active, proof={"request_id": active, "process_state": "ABSENT"})
    else:
        target = queued
    before = store.get(target)
    history = store.journal(target)
    with pytest.raises(IntakeError):
        store.finish_recovery(target, session, status="COMPLETED", cleanup_verified=True)
    assert store.get(target) == before
    assert store.journal(target) == history


def test_two_recovery_completions_cannot_release_or_rewrite_twice(tmp_path):
    store, _, session, active, _ = recovery_fixture(tmp_path)
    peers = [IntakeStore(store.database), IntakeStore(store.database)]

    def finish(peer):
        try:
            peer.finish_recovery(active, session, status="INTERRUPTED", cleanup_verified=True)
            return "finished"
        except IntakeError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(finish, peers)) == ["RECOVERY_NOT_APPLICABLE", "finished"]
    assert sum(event == "RECOVERY" for event, _ in store.journal(active)) == 1


def test_restart_notifications_compare_terminal_and_successful_delivery_event_order(tmp_path):
    store, _, session, active, queued = recovery_fixture(tmp_path)
    assert {row["request_id"] for row in store.restart_notifications("runner-1", session)} == {
        active, queued,
    }
    store.record_event(queued, "REMOTE_STATE_LOCAL_ONLY", {"code": "GITHUB_DELIVERY_NOT_CONFIGURED"})
    store.record_event(active, "STATE_DELIVERY", {"status": "UNKNOWN"})
    assert [row["request_id"] for row in store.restart_notifications("runner-1", session)] == [active]
    store.record_event(active, "STATE_DELIVERY", {"status": "DELIVERED"})
    assert store.restart_notifications("runner-1", session) == []
    # Even the same status now has newer cleanup proof and needs one new delivery.
    store.finish_recovery(active, session, status="INTERRUPTED", cleanup_verified=True)
    assert [row["request_id"] for row in store.restart_notifications("runner-1", session)] == [active]
    store.record_event(active, "STATE_DELIVERY", {"status": "DELIVERED"})
    assert store.restart_notifications("runner-1", session) == []


def test_restart_notifications_include_completed_crash_before_publication(tmp_path):
    store, old, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    delivered, interrupted_delivery = accept(coordinator, port), accept(coordinator, port)
    store.claim_next(old)
    store.finish(delivered, status="COMPLETED")
    store.record_event(delivered, "STATE_DELIVERY", {"status": "DELIVERED"})
    store.claim_next(old)
    store.record_event(interrupted_delivery, "STATE_DELIVERY", {"status": "DELIVERED"})
    store.finish(interrupted_delivery, status="COMPLETED")
    assert store.restart_notifications("runner-1", old) == []
    new = store.start_session("runner-1")
    assert [row["request_id"] for row in store.restart_notifications("runner-1", new)] == [
        interrupted_delivery,
    ]


def test_restart_notifications_are_bounded_and_session_scoped(tmp_path):
    store, old, session, _, _ = recovery_fixture(tmp_path)
    history = store.list_requests()
    assert len(store.restart_notifications("runner-1", session, limit=1)) == 1
    assert len(store.restart_notifications("runner-1", session, limit=2)) == 2
    with pytest.raises(ValueError):
        store.restart_notifications("runner-1", session, limit=0)
    with pytest.raises(IntakeError, match="SESSION_ENDED"):
        store.restart_notifications("runner-1", old)
    with pytest.raises(IntakeError, match="WRONG_RUNNER"):
        store.restart_notifications("runner-2", session)
    other = store.start_session("runner-2")
    assert store.restart_notifications("runner-2", other) == []
    assert store.list_requests() == history


@pytest.mark.parametrize("transition", [
    "CANCELLED_BEFORE_START", "ACK_PROOF_LOST", "INTAKE_REVOKED", "ACK_FAILED", "INPUT_CHANGED",
])
def test_restart_notifications_include_existing_terminal_transition_aliases(tmp_path, transition):
    store, _, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    if transition == "CANCELLED_BEFORE_START":
        target = accept(coordinator, port)
        assert store.cancel(target) == "CANCELLED"
    else:
        port.ready()
        port.lost = True
        target = coordinator.poll_once("slot", port).request_id
        candidate = store.pending_candidate("slot")
        if transition in {"ACK_PROOF_LOST", "INTAKE_REVOKED"}:
            store.ack_proof_lost(candidate["candidate_id"], code=transition)
        else:
            store.ack_failed(candidate["candidate_id"], code=transition)
    new = store.start_session("runner-1")
    assert [row["request_id"] for row in store.restart_notifications("runner-1", new)] == [target]
    store.record_event(target, "STATE_DELIVERY", {"status": "DELIVERED"})
    store.record_event(target, "STATE_PUBLISH_INTENT", {"unrelated": "later publication attempt"})
    store.record_event(target, "STATE_DELIVERY", {"status": "UNKNOWN"})
    assert store.restart_notifications("runner-1", new) == []
    newer = store.start_session("runner-1")
    assert store.restart_notifications("runner-1", newer) == []


def test_restart_notifications_include_redelivery_after_previous_terminal_publication(tmp_path):
    store, old, coordinator, port = setup(tmp_path)
    coordinator.poll_once("slot", port)
    target = accept(coordinator, port)
    store.claim_next(old)
    digest = "a" * 64
    store.finish(target, status="COMPLETED", result={"bundle_sha256": digest})
    store.record_event(target, "STATE_DELIVERY", {"status": "DELIVERED"})
    port.value["request"] = {"request_id": None, "action": "REDELIVER", "target_request_id": target}
    actor = accept(coordinator, port)
    store.claim_next(old)
    store.record_redelivery(target, actor_id=actor,
                            delivery={"bundle_sha256": digest, "status": "DELIVERED"})
    store.finish(actor, status="COMPLETED")
    store.record_event(actor, "STATE_DELIVERY", {"status": "DELIVERED"})
    new = store.start_session("runner-1")
    assert [row["request_id"] for row in store.restart_notifications("runner-1", new)] == [target]
    store.record_event(target, "STATE_DELIVERY", {"status": "DELIVERED"})
    assert store.restart_notifications("runner-1", new) == []


def test_restart_notifications_include_handoff_recovery_and_respect_confirmed_receipt(tmp_path):
    store, old, coordinator, port = setup(tmp_path)
    port.value["request"] = {"request_id": None, "action": "DELIVER_HANDOFF", "handoff": {
        "protocol": "cdr-handoff/1", "handoff_id": str(uuid4()), "project_id": "project-1",
        "target_id": "inbox", "purpose": "roadmap", "title": "Recovery fixture",
        "base_commit": None, "entrypoint": {"artifact_id": "notes", "path": "notes.txt"},
        "artifacts": [{"artifact_id": "notes", "drive_file_id": "fixture-file", "drive_version": "1",
                       "filename": "notes.zip", "size_bytes": 1, "sha256": "a" * 64,
                       "media_type": "application/zip", "unpack": "zip"}],
    }}
    coordinator.poll_once("slot", port)
    target = accept(coordinator, port)
    store.claim_next(old)
    new = store.start_session("runner-1")
    store.record_event(target, "STATE_DELIVERY", {"status": "DELIVERED"})
    assert store.restart_notifications("runner-1", new) == []
    store.confirm_handoff_recovery(target, {"handoff": {"local_delivery_status": "DELIVERED"}})
    assert [row["request_id"] for row in store.restart_notifications("runner-1", new)] == [target]
    store.record_handoff_publication(target, {"status": "UNCONFIRMED"})
    assert [row["request_id"] for row in store.restart_notifications("runner-1", new)] == [target]
    store.record_handoff_publication(target, {"status": "CONFIRMED"})
    assert store.restart_notifications("runner-1", new) == []
    # Receipt metadata is omitted from the state view and cannot invalidate its own delivery.
    store.record_handoff_publication(target, {"status": "UNCONFIRMED"})
    newer = store.start_session("runner-1")
    assert store.restart_notifications("runner-1", newer) == []


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
