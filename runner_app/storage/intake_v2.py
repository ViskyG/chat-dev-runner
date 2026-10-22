"""Durable reserve/ACK/FIFO state; provider I/O never occurs in transactions."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from runner_app.storage.database import Database

if TYPE_CHECKING:
    from runner_app.domain.mailbox_v2 import FrozenMailbox

TERMINAL_V2 = frozenset({
    "COMPLETED", "FAILED", "CANCELLED", "REJECTED", "SUSPENDED_RESTART",
    "INTERRUPTED", "NEEDS_RECONCILIATION",
})
CONTROLS = frozenset({"CANCEL", "STATUS"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


class IntakeError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Reservation:
    candidate_id: str
    request_id: str
    duplicate: bool


@dataclass(frozen=True)
class AckProof:
    revision: str
    payload_sha256: str
    provider_identity: str


class IntakeStore:
    def __init__(self, database: Database, *, queue_limit: int = 100) -> None:
        if type(queue_limit) is not int or not 1 <= queue_limit <= 10_000:
            raise ValueError("Invalid queue limit")
        self.database = database
        self.queue_limit = queue_limit
        database.migrate()
        with self._transaction() as c:
            c.execute("INSERT OR IGNORE INTO v2_installation VALUES(1,?)", (str(uuid4()),))

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        c = self.database.connect()
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    @staticmethod
    def _event(c: sqlite3.Connection, request_id: str | None, event: str,
               detail: Mapping[str, Any] | None = None) -> None:
        c.execute("INSERT INTO v2_journal(request_id,event,detail_json,created_at) VALUES(?,?,?,?)",
                  (request_id, event, _json(detail or {}), _now()))

    @property
    def installation_id(self) -> str:
        with self._connection() as c:
            return str(c.execute("SELECT installation_id FROM v2_installation").fetchone()[0])

    def start_session(self, runner_id: str) -> str:
        session_id, now = str(uuid4()), _now()
        with self._transaction() as c:
            previous = [r[0] for r in c.execute(
                "SELECT session_id FROM v2_sessions WHERE runner_id=? AND ended_at IS NULL",
                (runner_id,),
            )]
            for old in previous:
                c.execute("UPDATE v2_sessions SET ended_at=? WHERE session_id=?", (now, old))
                pending = c.execute(
                    "SELECT request_id FROM v2_requests WHERE session_id=? "
                    "AND status IN ('ACK_PENDING','QUEUED','CONTROL','CONTROL_RUNNING')", (old,),
                ).fetchall()
                for row in pending:
                    c.execute("UPDATE v2_requests SET status='SUSPENDED_RESTART',"
                              "code='SESSION_ENDED',updated_at=? WHERE request_id=?", (now, row[0]))
                    self._event(c, row[0], "SUSPENDED_RESTART")
                active = c.execute(
                    "SELECT request_id FROM v2_requests WHERE session_id=? "
                    "AND status IN ('STARTING','RUNNING')", (old,),
                ).fetchall()
                for row in active:
                    c.execute("UPDATE v2_requests SET status='INTERRUPTED',"
                              "code='PROCESS_PROOF_REQUIRED',updated_at=? WHERE request_id=?",
                              (now, row[0]))
                    self._event(c, row[0], "INTERRUPTED")
            c.execute("INSERT INTO v2_sessions(session_id,runner_id,started_at) VALUES(?,?,?)",
                      (session_id, runner_id, now))
            c.execute("UPDATE v2_mailboxes SET continuous=0,updated_at=? WHERE runner_id=?",
                      (now, runner_id))
            self._event(c, None, "SESSION_STARTED", {"session_id": session_id})
        return session_id

    def end_session(self, session_id: str) -> None:
        with self._transaction() as c:
            c.execute("UPDATE v2_sessions SET ended_at=COALESCE(ended_at,?) WHERE session_id=?",
                      (_now(), session_id))
            rows = c.execute("SELECT request_id,status FROM v2_requests WHERE session_id=? "
                             "AND status IN ('ACK_PENDING','QUEUED','STARTING','RUNNING','CONTROL','CONTROL_RUNNING')",
                             (session_id,)).fetchall()
            for row in rows:
                state = "INTERRUPTED" if row["status"] in {"STARTING", "RUNNING"} else "SUSPENDED_RESTART"
                c.execute("UPDATE v2_requests SET status=?,code='SESSION_ENDED',updated_at=? WHERE request_id=?",
                          (state, _now(), row["request_id"]))
                self._event(c, row["request_id"], state)
            c.execute("UPDATE v2_mailboxes SET continuous=0 WHERE session_id=?", (session_id,))

    def record_event(self, request_id: str, event: str, detail: Mapping[str, Any]) -> None:
        with self._transaction() as c:
            if c.execute("SELECT 1 FROM v2_requests WHERE request_id=?", (request_id,)).fetchone() is None:
                raise IntakeError("REQUEST_NOT_FOUND")
            self._event(c, request_id, event, detail)

    @staticmethod
    def _live_session(c: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = c.execute("SELECT * FROM v2_sessions WHERE session_id=? AND ended_at IS NULL",
                        (session_id,)).fetchone()
        if row is None:
            raise IntakeError("SESSION_ENDED")
        return cast(sqlite3.Row, row)

    def bind(self, binding_id: str, *, runner_id: str, project_id: str,
             provider: str, coordinates: Mapping[str, Any], session_id: str) -> None:
        identity = (runner_id, project_id, provider, _json(coordinates))
        with self._transaction() as c:
            session = self._live_session(c, session_id)
            if session["runner_id"] != runner_id:
                raise IntakeError("WRONG_RUNNER")
            old = c.execute("SELECT * FROM v2_mailboxes WHERE binding_id=?",
                            (binding_id,)).fetchone()
            if old is not None and tuple(old[k] for k in (
                    "runner_id", "project_id", "provider", "coordinates_json")) != identity:
                raise IntakeError("MAILBOX_IDENTITY_CHANGED")
            if old is None:
                c.execute("INSERT INTO v2_mailboxes(binding_id,runner_id,project_id,provider,"
                          "coordinates_json,session_id,updated_at) VALUES(?,?,?,?,?,?,?)",
                          (binding_id, *identity, session_id, _now()))
            elif old["session_id"] != session_id:
                c.execute("UPDATE v2_mailboxes SET session_id=?,continuous=0,updated_at=? "
                          "WHERE binding_id=?", (session_id, _now(), binding_id))

    def mailbox(self, binding_id: str) -> dict[str, Any]:
        with self._connection() as c:
            row = c.execute("SELECT * FROM v2_mailboxes WHERE binding_id=?",
                            (binding_id,)).fetchone()
            if row is None:
                raise IntakeError("MAILBOX_NOT_ENROLLED")
            return dict(row)

    def establish_baseline(self, binding_id: str, *, session_id: str,
                           slot_token: str, revision: str) -> None:
        if not revision:
            raise IntakeError("ACK_PROOF_REQUIRED")
        with self._transaction() as c:
            self._live_session(c, session_id)
            changed = c.execute(
                "UPDATE v2_mailboxes SET slot_token=?,observed_revision=?,continuous=1,"
                "updated_at=? WHERE binding_id=? AND session_id=?",
                (slot_token, revision, _now(), binding_id, session_id),
            ).rowcount
            if changed != 1:
                raise IntakeError("MAILBOX_NOT_ENROLLED")

    def observation_gap(self, binding_id: str, *, session_id: str | None = None) -> None:
        with self._transaction() as c:
            c.execute("UPDATE v2_mailboxes SET continuous=0,updated_at=? WHERE binding_id=? "
                      "AND (? IS NULL OR session_id=?)", (_now(), binding_id, session_id, session_id))

    def record_diagnostic(self, binding_id: str, code: str, observed_sha256: str) -> None:
        with self._transaction() as c:
            old = c.execute("SELECT intake_code,diagnostic_sha256 FROM v2_mailboxes WHERE binding_id=?",
                            (binding_id,)).fetchone()
            if old is not None and tuple(old) != (code, observed_sha256):
                c.execute("UPDATE v2_mailboxes SET intake_code=?,diagnostic_sha256=?,updated_at=? "
                          "WHERE binding_id=?", (code, observed_sha256, _now(), binding_id))
                self._event(c, None, "INTAKE_DIAGNOSTIC", {"binding_id": binding_id, "code": code,
                                                       "observed_sha256": observed_sha256})

    def reserve(self, binding_id: str, frozen: FrozenMailbox, *, revision: str,
                observed_sha256: str, ack_payload: bytes) -> Reservation:
        if not frozen.ready or not revision:
            raise IntakeError("NOT_READY")
        env = frozen.envelope
        ack = json.loads(ack_payload)
        receipt = ack.get("receipt") or {}
        if (ack.get("dispatch") != "ACCEPTED" or ack.get("slot_token") == env["slot_token"]
                or receipt.get("request_id") != frozen.request_id
                or receipt.get("payload_sha256") != frozen.digest
                or receipt.get("accepted_slot_token") != env["slot_token"]
                or receipt.get("status") != "ACCEPTED"
                or receipt.get("queue_sequence") is not None):
            raise IntakeError("ACK_INTENT_MISMATCH")
        with self._transaction() as c:
            mailbox = c.execute("SELECT * FROM v2_mailboxes WHERE binding_id=?",
                                (binding_id,)).fetchone()
            if mailbox is None or not mailbox["continuous"]:
                raise IntakeError("NEEDS_BASELINE")
            self._live_session(c, mailbox["session_id"])
            if (mailbox["slot_token"] != env["slot_token"]
                    or mailbox["runner_id"] != env["runner_id"]
                    or mailbox["project_id"] != env["project_id"]):
                raise IntakeError("STALE_SLOT")
            existing = c.execute("SELECT * FROM v2_requests WHERE request_id=?",
                                 (frozen.request_id,)).fetchone()
            if existing is not None and existing["digest"] != frozen.digest:
                raise IntakeError("REQUEST_ID_REUSED")
            candidate = c.execute("SELECT * FROM v2_candidates WHERE binding_id=? AND slot_token=?",
                                  (binding_id, env["slot_token"])).fetchone()
            if candidate is not None:
                if (candidate["request_id"] != frozen.request_id
                        or candidate["observed_sha256"] != observed_sha256):
                    raise IntakeError("INPUT_CHANGED")
                return Reservation(candidate["candidate_id"], frozen.request_id, existing is not None)
            if existing is not None and existing["status"] == "ACK_PENDING":
                raise IntakeError("ORIGINAL_ACK_PENDING")
            if existing is None:
                if c.execute("SELECT 1 FROM operations WHERE request_id=?",
                             (frozen.request_id,)).fetchone() is not None:
                    raise IntakeError("REQUEST_ID_REUSED")
                action = env["request"]["action"]
                if action not in CONTROLS:
                    pending = c.execute("SELECT count(*) FROM v2_requests "
                                        "WHERE status IN ('ACK_PENDING','QUEUED') "
                                        "AND action NOT IN ('CANCEL','STATUS')").fetchone()[0]
                    if pending >= self.queue_limit:
                        raise IntakeError("QUEUE_FULL")
                now = _now()
                c.execute("INSERT INTO v2_requests(request_id,digest,payload,runner_id,project_id,"
                          "action,session_id,status,created_at,updated_at) "
                          "VALUES(?,?,?,?,?,?,?,'ACK_PENDING',?,?)",
                          (frozen.request_id, frozen.digest, frozen.canonical_payload,
                           env["runner_id"], env["project_id"], action, mailbox["session_id"], now, now))
                self._event(c, frozen.request_id, "RESERVED")
            candidate_id, now = str(uuid4()), _now()
            c.execute("INSERT INTO v2_candidates(candidate_id,binding_id,session_id,slot_token,"
                      "request_id,observed_revision,observed_sha256,ack_payload,ack_sha256,"
                      "state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'ACK_PENDING',?,?)",
                      (candidate_id, binding_id, mailbox["session_id"], env["slot_token"],
                       frozen.request_id, revision, observed_sha256, ack_payload,
                       hashlib.sha256(ack_payload).hexdigest(), now, now))
            return Reservation(candidate_id, frozen.request_id, existing is not None)

    def candidate(self, candidate_id: str) -> dict[str, Any]:
        with self._connection() as c:
            row = c.execute("SELECT * FROM v2_candidates WHERE candidate_id=?",
                            (candidate_id,)).fetchone()
            if row is None:
                raise IntakeError("CANDIDATE_MISSING")
            return dict(row)

    def pending_candidate(self, binding_id: str) -> dict[str, Any] | None:
        with self._connection() as c:
            row = c.execute("SELECT c.* FROM v2_candidates c JOIN v2_sessions s USING(session_id) "
                            "JOIN v2_requests r USING(request_id) "
                            "WHERE c.binding_id=? AND s.ended_at IS NULL "
                            "AND r.status='ACK_PENDING' "
                            "AND c.state IN ('ACK_PENDING','ACK_UNKNOWN') "
                            "ORDER BY c.created_at LIMIT 1", (binding_id,)).fetchone()
            return dict(row) if row is not None else None

    def ack_failed(self, candidate_id: str, *, code: str, uncertain: bool = False) -> None:
        state = "ACK_UNKNOWN" if uncertain else "INPUT_CHANGED" if code == "INPUT_CHANGED" else "ACK_FAILED"
        with self._transaction() as c:
            row = c.execute("SELECT * FROM v2_candidates WHERE candidate_id=?",
                            (candidate_id,)).fetchone()
            if row is None or row["state"] == "ACKED":
                raise IntakeError("CANDIDATE_NOT_PENDING")
            c.execute("UPDATE v2_candidates SET state=?,updated_at=? WHERE candidate_id=?",
                      (state, _now(), candidate_id))
            if not uncertain:
                c.execute("UPDATE v2_requests SET status='REJECTED',code=?,updated_at=? "
                          "WHERE request_id=? AND status='ACK_PENDING'",
                          (code, _now(), row["request_id"]))
            self._event(c, row["request_id"], state, {"code": code})

    def finalize_ack(self, candidate_id: str, proof: AckProof) -> int | None:
        with self._transaction() as c:
            row = c.execute("SELECT * FROM v2_candidates WHERE candidate_id=?",
                            (candidate_id,)).fetchone()
            if row is None or row["state"] not in {"ACK_PENDING", "ACK_UNKNOWN", "ACKED"}:
                raise IntakeError("CANDIDATE_NOT_PENDING")
            if (not proof.revision or proof.provider_identity != row["binding_id"]
                    or proof.payload_sha256 != row["ack_sha256"]):
                raise IntakeError("ACK_PROOF_MISMATCH")
            self._live_session(c, row["session_id"])
            if row["state"] != "ACKED":
                c.execute("UPDATE v2_candidates SET state='ACKED',proof_json=?,updated_at=? "
                          "WHERE candidate_id=?", (_json({"revision": proof.revision,
                          "payload_sha256": proof.payload_sha256,
                          "provider_identity": proof.provider_identity}), _now(), candidate_id))
                token = json.loads(row["ack_payload"])["slot_token"]
                c.execute("UPDATE v2_mailboxes SET slot_token=?,observed_revision=?,updated_at=? "
                          "WHERE binding_id=? AND session_id=? AND slot_token=?",
                          (token, proof.revision, _now(), row["binding_id"], row["session_id"],
                           row["slot_token"]))
            request = c.execute("SELECT * FROM v2_requests WHERE request_id=?",
                                (row["request_id"],)).fetchone()
            if request["status"] == "ACK_PENDING":
                state = "CONTROL" if request["action"] in CONTROLS else "QUEUED"
                if state == "QUEUED":
                    c.execute("INSERT INTO v2_queue(request_id,session_id,finalized_at) VALUES(?,?,?)",
                              (request["request_id"], row["session_id"], _now()))
                c.execute("UPDATE v2_requests SET status=?,updated_at=? WHERE request_id=?",
                          (state, _now(), request["request_id"]))
                self._event(c, request["request_id"], state)
            queue = c.execute("SELECT sequence FROM v2_queue WHERE request_id=?",
                              (request["request_id"],)).fetchone()
            return int(queue[0]) if queue else None

    def ack_proof_lost(self, candidate_id: str, *, code: str = "ACK_PROOF_LOST") -> None:
        """Keep ambiguous acceptance visible while permitting a fresh, baselined slot."""
        if code not in {"ACK_PROOF_LOST", "INTAKE_REVOKED"}:
            raise ValueError("Invalid acknowledgement reconciliation code")
        with self._transaction() as c:
            row = c.execute("SELECT * FROM v2_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            if row is None or row["state"] not in {"ACK_PENDING", "ACK_UNKNOWN"}:
                raise IntakeError("CANDIDATE_NOT_PENDING")
            c.execute("UPDATE v2_candidates SET state='ACK_UNKNOWN',updated_at=? WHERE candidate_id=?",
                      (_now(), candidate_id))
            c.execute("UPDATE v2_requests SET status='NEEDS_RECONCILIATION',"
                      "code=?,updated_at=? WHERE request_id=? AND status='ACK_PENDING'",
                      (code, _now(), row["request_id"]))
            c.execute("UPDATE v2_mailboxes SET continuous=0 WHERE binding_id=? AND session_id=?",
                      (row["binding_id"], row["session_id"]))
            self._event(c, row["request_id"], code)

    def claim_control(self, request_id: str) -> bool:
        with self._transaction() as c:
            return c.execute("UPDATE v2_requests SET status='CONTROL_RUNNING',updated_at=? "
                             "WHERE request_id=? AND status='CONTROL'", (_now(), request_id)).rowcount == 1

    @staticmethod
    def _view(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        result = value.pop("result_json")
        value["result"] = json.loads(result) if result else None
        return value

    def get(self, request_id: str) -> dict[str, Any] | None:
        with self._connection() as c:
            row = c.execute("SELECT r.*,q.sequence FROM v2_requests r LEFT JOIN v2_queue q "
                            "USING(request_id) WHERE r.request_id=?", (request_id,)).fetchone()
            return self._view(row) if row else None

    def request_origins(self, request_id: str) -> list[dict[str, Any]]:
        with self._connection() as c:
            return [dict(row) for row in c.execute(
                "SELECT DISTINCT m.* FROM v2_mailboxes m JOIN v2_candidates c "
                "USING(binding_id) WHERE c.request_id=? AND c.state='ACKED'", (request_id,))]

    def execution_started(self, request_id: str) -> bool:
        with self._connection() as c:
            return c.execute("SELECT 1 FROM v2_journal WHERE request_id=? AND event='RUNNING' "
                             "LIMIT 1", (request_id,)).fetchone() is not None

    def record_handoff_publication(self, request_id: str, publication: Mapping[str, Any]) -> None:
        """Receipt delivery is independent of local delivery and terminal time."""
        with self._transaction() as c:
            row = c.execute("SELECT result_json FROM v2_requests WHERE request_id=?",
                            (request_id,)).fetchone()
            if row is None:
                raise IntakeError("REQUEST_NOT_FOUND")
            result = json.loads(row[0]) if row[0] else {}
            result["receipt_publication"] = dict(publication)
            c.execute("UPDATE v2_requests SET result_json=? WHERE request_id=?",
                      (_json(result), request_id))
            self._event(c, request_id, "HANDOFF_RECEIPT_DELIVERY", dict(publication))

    def confirm_handoff_recovery(self, request_id: str, result: Mapping[str, Any]) -> None:
        """Record a verified existing local commit; never schedule or execute it."""
        with self._transaction() as c:
            row = c.execute("SELECT * FROM v2_requests WHERE request_id=?", (request_id,)).fetchone()
            if (row is None or row["action"] != "DELIVER_HANDOFF"
                    or row["status"] not in {"INTERRUPTED", "SUSPENDED_RESTART", "FAILED"}):
                raise IntakeError("HANDOFF_RECOVERY_NOT_APPLICABLE")
            c.execute("UPDATE v2_requests SET status='COMPLETED',code=NULL,result_json=?,updated_at=? "
                      "WHERE request_id=?", (_json(dict(result)), _now(), request_id))
            c.execute("DELETE FROM execution_lane WHERE protocol='v2' AND request_id=?", (request_id,))
            self._event(c, request_id, "HANDOFF_RECOVERED", dict(result))

    def record_redelivery(self, request_id: str, *, actor_id: str,
                          delivery: Mapping[str, Any]) -> None:
        """Update only the current delivery view; frozen result evidence stays exact."""
        with self._transaction() as c:
            target = c.execute("SELECT * FROM v2_requests WHERE request_id=?", (request_id,)).fetchone()
            actor = c.execute("SELECT * FROM v2_requests WHERE request_id=?", (actor_id,)).fetchone()
            if (target is None or actor is None or actor["status"] != "STARTING"
                    or target["runner_id"] != actor["runner_id"]
                    or target["project_id"] != actor["project_id"] or not target["result_json"]):
                raise IntakeError("RESULT_SCOPE_MISMATCH")
            intent = json.loads(actor["payload"])["request"]
            result = json.loads(target["result_json"])
            if (intent["action"] != "REDELIVER" or intent["target_request_id"] != request_id
                    or delivery.get("bundle_sha256") != result.get("bundle_sha256")):
                raise IntakeError("RESULT_IDENTITY_MISMATCH")
            result["delivery"] = dict(delivery)
            c.execute("UPDATE v2_requests SET result_json=?,updated_at=? WHERE request_id=?",
                      (_json(result), _now(), request_id))
            self._event(c, request_id, "DELIVERY_UPDATED", {"actor_request_id": actor_id,
                                                          "delivery": dict(delivery)})

    def list_requests(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid request limit")
        with self._connection() as c:
            return [self._view(row) for row in c.execute(
                "SELECT r.*,q.sequence FROM v2_requests r LEFT JOIN v2_queue q USING(request_id) "
                "ORDER BY r.created_at DESC LIMIT ?", (limit,),
            )]

    def claim_next(self, session_id: str) -> dict[str, Any] | None:
        request_id: str | None = None
        with self._transaction() as c:
            self._live_session(c, session_id)
            if (c.execute("SELECT 1 FROM execution_lane").fetchone() is not None
                    or c.execute("SELECT 1 FROM operations WHERE status='RUNNING' "
                                 "AND capacity_held=1 LIMIT 1").fetchone() is not None):
                return None
            row = c.execute("SELECT r.request_id FROM v2_queue q JOIN v2_requests r USING(request_id) "
                            "WHERE q.session_id=? AND r.status='QUEUED' ORDER BY q.sequence LIMIT 1",
                            (session_id,)).fetchone()
            if row is None:
                return None
            request_id = row[0]
            c.execute("INSERT INTO execution_lane VALUES(1,'v2',?,?)", (request_id, _now()))
            c.execute("UPDATE v2_requests SET status='STARTING',updated_at=? WHERE request_id=?",
                      (_now(), request_id))
            self._event(c, request_id, "STARTING")
        return self.get(request_id)

    def mark_running(self, request_id: str, provenance: Mapping[str, Any]) -> None:
        with self._transaction() as c:
            if c.execute("SELECT 1 FROM execution_lane WHERE protocol='v2' AND request_id=?",
                         (request_id,)).fetchone() is None:
                raise IntakeError("EXECUTION_LANE_MISSING")
            if c.execute("UPDATE v2_requests SET status='RUNNING',updated_at=? "
                         "WHERE request_id=? AND status='STARTING'", (_now(), request_id)).rowcount != 1:
                raise IntakeError("INVALID_TRANSITION")
            self._event(c, request_id, "RUNNING", provenance)

    def finish(self, request_id: str, *, status: str, code: str | None = None,
               result: Mapping[str, Any] | None = None, cleanup_verified: bool = True) -> None:
        if status not in TERMINAL_V2:
            raise IntakeError("INVALID_TERMINAL_STATUS")
        with self._transaction() as c:
            row = c.execute("SELECT status FROM v2_requests WHERE request_id=?",
                            (request_id,)).fetchone()
            if row is None or row[0] not in {"STARTING", "RUNNING", "CONTROL_RUNNING"}:
                raise IntakeError("INVALID_TRANSITION")
            if not cleanup_verified:
                status, code = "NEEDS_RECONCILIATION", code or "CLEANUP_PROOF_REQUIRED"
            c.execute("UPDATE v2_requests SET status=?,code=?,result_json=?,updated_at=? "
                      "WHERE request_id=?", (status, code, _json(result) if result is not None else None,
                                           _now(), request_id))
            if cleanup_verified:
                c.execute("DELETE FROM execution_lane WHERE protocol='v2' AND request_id=?", (request_id,))
            self._event(c, request_id, status, {"code": code})

    def cancel(self, request_id: str) -> str:
        with self._transaction() as c:
            row = c.execute("SELECT status FROM v2_requests WHERE request_id=?",
                            (request_id,)).fetchone()
            if row is None:
                raise IntakeError("REQUEST_NOT_FOUND")
            state = str(row[0])
            if state in {"ACK_PENDING", "QUEUED"}:
                c.execute("UPDATE v2_requests SET status='CANCELLED',code='CANCELLED_BEFORE_START',"
                          "cancel_requested=1,updated_at=? WHERE request_id=?", (_now(), request_id))
                self._event(c, request_id, "CANCELLED_BEFORE_START")
                return "CANCELLED"
            if state in {"STARTING", "RUNNING"}:
                c.execute("UPDATE v2_requests SET cancel_requested=1,updated_at=? WHERE request_id=?",
                          (_now(), request_id))
                self._event(c, request_id, "CANCEL_REQUESTED")
                return "CANCELLING"
            return state

    def reconcile_lane_absent(self, request_id: str, *, proof: Mapping[str, Any]) -> None:
        if proof.get("process_state") != "ABSENT" or proof.get("request_id") != request_id:
            raise IntakeError("PROCESS_PROOF_REQUIRED")
        with self._transaction() as c:
            row = c.execute("SELECT status FROM v2_requests WHERE request_id=?", (request_id,)).fetchone()
            if row is None or row[0] not in {"INTERRUPTED", "NEEDS_RECONCILIATION"}:
                raise IntakeError("INVALID_TRANSITION")
            self._event(c, request_id, "PROCESS_ABSENT", proof)
            c.execute("DELETE FROM execution_lane WHERE protocol='v2' AND request_id=?", (request_id,))
