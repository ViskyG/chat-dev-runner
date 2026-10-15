"""Durable immediate admission and explicit crash reconciliation, without a queue."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from .database import Database

TERMINAL = frozenset({"COMPLETED", "REJECTED", "BLOCKED", "FAULT", "INTERRUPTED", "CANCELLED"})
EFFECT_KINDS = frozenset({"github_repository", "git_ref", "pull_request", "drive_file", "local_workspace"})
_EXECUTOR_UNCERTAIN_CODES = frozenset({"CONTAINER_CLEANUP_FAILED", "CONTAINER_CLEANUP_UNCERTAIN"})
_PHASES = frozenset({"PREPARATION", "TEST"})
_PRE_DISPATCH_ROOT_RECEIPT_STEP = "drive-root-create-receipt"
_PRE_DISPATCH_ROOT_RECEIPT_CODE = "INTERRUPTED_BEFORE_EXECUTION"
_TERMINAL_ROOT_RECEIPT_CODE = "PROVISIONING_PARTIAL"
_TERMINAL_PROJECT_RECEIPT_STEP = "drive-project-terminal-receipt"
_TERMINAL_WORKSPACE_RECEIPT_STEP = "drive-workspace-terminal-receipt"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
_SCOPE = re.compile(r"(?:(?:repo|global):[a-z0-9][a-z0-9-]{0,47}|workspace:[a-z0-9][a-z0-9-]{0,47}/[a-z0-9][a-z0-9-]{0,47})\Z")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _document_sha256(value: Any) -> str:
    """Hash the canonical UTF-8 JSON file bytes used by immutable Drive bundles."""
    return hashlib.sha256((_json(value) + "\n").encode("utf-8")).hexdigest()


class LedgerError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Admission:
    receipt: dict[str, Any]
    execute: bool
    duplicate: bool = False


@dataclass(frozen=True)
class DeliveryPreflight:
    """Durable identities for one admitted delivery of a contained run."""

    delivery_request_id: str
    source_request_id: str
    run_id: str
    project_id: str
    workspace_id: str
    action: str
    source_action: str
    source_status: str
    lock_scope: str
    run_manifest_sha256: str
    input_manifest_sha256: str
    source_operation_manifest_sha256: str | None


@dataclass(frozen=True)
class EffectAdoptionProof:
    """Exact facts returned by a trusted read-only verifier immediately before adoption."""

    source_effect_id: str
    provider_id: str
    observed_intent: Mapping[str, Any]


def _receipt(request_id: str, digest: str, status: str, code: str | None, timestamp: str,
             run_id: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": "1.0", "request_id": request_id, "request_digest": digest,
        "status": status, "code": code,
        "message": "Операция выполняется." if status == "RUNNING" else "Состояние операции сохранено.",
        "observed_at": timestamp, "run_id": run_id,
    }


def _scope_conflict(left: str, right: str) -> bool:
    if left == right:
        return True
    if left.startswith("repo:"):
        return right.startswith("workspace:" + left[5:] + "/")
    if right.startswith("repo:"):
        return left.startswith("workspace:" + right[5:] + "/")
    return False


def _execution_scope(row: sqlite3.Row) -> str:
    if row["action"] == "VERIFY":
        return f"workspace:{row['project_id']}/{row['workspace_id']}"
    if row["action"] == "DELIVER_VERIFY_PUBLISH":
        return f"repo:{row['project_id']}"
    raise LedgerError("CONTAINER_INTENT_NOT_ADMITTED",
                      "Operation does not authorize a contained run")


def _canonical_uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _phase_detail(value: dict[str, Any]) -> str:
    # Schema 6 recorded the only container as TEST without an explicit phase.
    phase = value.get("phase", "TEST")
    if not isinstance(phase, str) or phase not in _PHASES:
        raise ValueError("Unknown container phase")
    return phase


def _container_state(connection: sqlite3.Connection, request_id: str
                     ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Read private append-only intents/proofs; reject duplicates and mismatches."""
    intents: dict[str, tuple[int, dict[str, Any]]] = {}
    proofs: dict[str, tuple[int, dict[str, Any]]] = {}
    for row in connection.execute(
        "SELECT event_id,event_type,detail_json FROM journal WHERE request_id=? "
        "AND event_type IN ('CONTAINER_INTENDED','CONTAINER_CLEANED','CONTAINER_RECONCILED','CONTAINER_ABSENT') "
        "ORDER BY event_id", (request_id,),
    ):
        detail = json.loads(row[2])
        if not isinstance(detail, dict):
            raise TypeError("Container event is malformed")
        phase = _phase_detail(detail)
        bucket = intents if row[1] == "CONTAINER_INTENDED" else proofs
        if phase in bucket:
            raise ValueError("Container phase has duplicate intent or proof")
        bucket[phase] = (row[0], detail)
    for phase, (event_id, proof) in proofs.items():
        intended = intents.get(phase)
        if intended is None or event_id <= intended[0]:
            raise ValueError("Container proof has no earlier intent")
        intent = intended[1]
        if (proof.get("run_id") != intent.get("run_id")
                or proof.get("daemon_id") != intent.get("daemon_id")):
            raise ValueError("Container proof does not match its intent")
        if connection.execute(
            "SELECT event_type FROM journal WHERE event_id=?", (event_id,),
        ).fetchone()[0] == "CONTAINER_ABSENT":
            if proof.get("state") != "ABSENT" or proof.get("container_id") is not None:
                raise ValueError("Absent-container proof is malformed")
        elif (proof.get("state") != "REMOVED"
              or not isinstance(proof.get("container_id"), str)
              or _CONTAINER_ID.fullmatch(proof["container_id"]) is None):
            raise ValueError("Container removal proof does not match its intent")
    return ({phase: detail for phase, (_, detail) in intents.items()},
            {phase: detail for phase, (_, detail) in proofs.items()})


def confirmed_create_effects_proof(
    effects: Sequence[Mapping[str, Any]],
) -> tuple[int, str]:
    """Digest exact confirmed non-receipt effects without exposing their values."""
    records: list[dict[str, Any]] = []
    steps: set[str] = set()
    try:
        for item in effects:
            step = item["step_id"]
            if step == _PRE_DISPATCH_ROOT_RECEIPT_STEP:
                continue
            intent = item["intent"]
            if (not isinstance(step, str) or not step or step in steps
                    or not _canonical_uuid(item["effect_id"])
                    or item["state"] != "CONFIRMED"
                    or item["kind"] not in EFFECT_KINDS
                    or not isinstance(item["provider_id"], str)
                    or not item["provider_id"]
                    or not isinstance(intent, Mapping)):
                raise ValueError
            steps.add(step)
            records.append({
                "effect_id": item["effect_id"], "step_id": step,
                "kind": item["kind"], "provider_id": item["provider_id"],
                "intent_sha256": hashlib.sha256(_json(dict(intent)).encode("utf-8")).hexdigest(),
            })
        if not records:
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise LedgerError("TERMINAL_ROOT_EFFECTS_UNSETTLED",
                          "Partial create effects are not exactly confirmed") from None
    records.sort(key=lambda record: record["effect_id"])
    return len(records), hashlib.sha256(_json(records).encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, database: Database, runner_id: str = "local-runner", max_parallel: int = 2,
                 *, single_execution_lane: bool = False):
        if isinstance(max_parallel, bool) or not isinstance(max_parallel, int) or max_parallel < 1:
            raise ValueError("max_parallel must be a positive integer")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", runner_id):
            raise ValueError("Invalid runner identity")
        self.database, self.runner_id, self.max_parallel = database, runner_id, max_parallel
        self.single_execution_lane = single_execution_lane
        database.migrate()
        with self._transaction() as connection:
            timestamp = utcnow()
            connection.execute(
                "INSERT OR IGNORE INTO runner_state VALUES(1,?,?,0,?,?)",
                (runner_id, str(uuid4()), timestamp, timestamp),
            )
            if connection.execute("SELECT runner_id FROM runner_state").fetchone()[0] != runner_id:
                raise LedgerError("WRONG_RUNNER", "Database belongs to another runner")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.database.connect()
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _state(connection: sqlite3.Connection) -> dict[str, Any]:
        row = dict(connection.execute("SELECT * FROM runner_state WHERE singleton=1").fetchone())
        row.pop("singleton")
        row["armed"] = bool(row["armed"])
        return row

    def state(self) -> dict[str, Any]:
        with self._connection() as connection:
            state = self._state(connection)
            state["max_parallel"] = self.max_parallel
            state["reserved_capacity"] = connection.execute(
                "SELECT count(*) FROM operations WHERE capacity_held=1"
            ).fetchone()[0]
            return state

    @staticmethod
    def _append(connection: sqlite3.Connection, request_id: str | None, step_id: str | None,
                event_type: str, detail: dict[str, Any], timestamp: str) -> dict[str, Any]:
        detail_json = _json(detail)
        if len(detail_json.encode("utf-8")) > 65_536:
            raise ValueError("Journal details exceed the local event size limit")
        if step_id is not None and request_id is not None:
            connection.execute(
                "INSERT INTO steps VALUES(?,?,?,?,?) ON CONFLICT(request_id,step_id) "
                "DO UPDATE SET last_event_type=excluded.last_event_type,updated_at=excluded.updated_at",
                (request_id, step_id, event_type, timestamp, timestamp),
            )
        cursor = connection.execute(
            "INSERT INTO journal(request_id,step_id,timestamp,event_type,detail_json) VALUES(?,?,?,?,?)",
            (request_id, step_id, timestamp, event_type, detail_json),
        )
        return {"event_id": cursor.lastrowid, "request_id": request_id, "step_id": step_id,
                "timestamp": timestamp, "type": event_type, "detail": detail}

    def startup(self) -> dict[str, Any]:
        """Called once by the singleton service; no worker or effect is replayed."""
        with self._transaction() as connection:
            timestamp = utcnow()
            for row in connection.execute("SELECT * FROM operations WHERE status='RUNNING'").fetchall():
                receipt = _receipt(row["request_id"], row["digest"], "INTERRUPTED", "NEEDS_RECONCILIATION", timestamp)
                connection.execute(
                    "UPDATE operations SET status='INTERRUPTED',code='NEEDS_RECONCILIATION',"
                    "updated_at=?,receipt_json=?,reconcile_required=1 WHERE request_id=?",
                    (timestamp, _json(receipt), row["request_id"]),
                )
                self._append(connection, row["request_id"], None, "INTERRUPTED",
                             {"code": "NEEDS_RECONCILIATION"}, timestamp)
            for effect in connection.execute("SELECT * FROM effects WHERE state='INTENDED'").fetchall():
                connection.execute("UPDATE effects SET state='UNKNOWN',updated_at=? WHERE effect_id=?",
                                   (timestamp, effect["effect_id"]))
                self._append(connection, effect["request_id"], effect["step_id"], "EFFECT_UNKNOWN",
                             {"effect_id": effect["effect_id"], "code": "NEEDS_RECONCILIATION"}, timestamp)
            connection.execute("UPDATE runner_state SET armed=0,epoch=?,started_at=?,updated_at=? WHERE singleton=1",
                               (str(uuid4()), timestamp, timestamp))
            return self._state(connection)

    def arm(self) -> dict[str, Any]:
        with self._transaction() as connection:
            state = self._state(connection)
            if state["armed"]:
                return state
            retained = connection.execute(
                "SELECT 1 FROM operations WHERE reconcile_required=1 LIMIT 1"
            ).fetchone()
            unknown_effect = connection.execute(
                "SELECT 1 FROM effects WHERE state IN ('INTENDED','UNKNOWN') LIMIT 1"
            ).fetchone()
            uncertain_v2_lane = connection.execute(
                "SELECT 1 FROM execution_lane l JOIN v2_requests r USING(request_id) "
                "WHERE l.protocol='v2' AND r.status IN ('INTERRUPTED','NEEDS_RECONCILIATION') LIMIT 1"
            ).fetchone()
            if retained or unknown_effect or uncertain_v2_lane:
                raise LedgerError("NEEDS_RECONCILIATION", "Reconcile uncertain workers and effects before arming")
            connection.execute("UPDATE runner_state SET armed=1,epoch=?,updated_at=? WHERE singleton=1",
                               (str(uuid4()), utcnow()))
            return self._state(connection)

    def disarm(self) -> dict[str, Any]:
        with self._transaction() as connection:
            connection.execute("UPDATE runner_state SET armed=0,updated_at=? WHERE singleton=1", (utcnow(),))
            return self._state(connection)

    def admit(self, command: dict[str, Any], digest: str, source_id: str, scopes: tuple[str, ...],
              rejection_code: str | None = None, exempt_capacity: bool = False,
              now: str | None = None) -> Admission:
        request_id = command.get("request_id")
        if not isinstance(request_id, str):
            raise LedgerError("INVALID_REQUEST_ID", "A valid UUID is required for a durable receipt")
        try:
            UUID(request_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise LedgerError("INVALID_REQUEST_ID", "A valid UUID is required for a durable receipt") from error
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not source_id:
            raise ValueError("Admission requires a content digest and registered source identity")
        command_json = _json(command)
        if hashlib.sha256(command_json.encode("utf-8")).hexdigest() != digest:
            raise ValueError("Supplied digest does not match the canonical command")
        normalized_scopes = tuple(sorted(set(scopes)))
        if any(not _SCOPE.fullmatch(scope) for scope in normalized_scopes):
            raise ValueError("Invalid locally derived resource scope")
        if (exempt_capacity
                and (command.get("action") not in {
                    "CANCEL_OPERATION", "RECONCILE", "RESUME_RETARGET",
                } or normalized_scopes)):
            raise ValueError("Only targeted recovery or cancellation may bypass capacity")
        with self._transaction() as connection:
            # Observe time inside the admission transaction. A command can expire
            # while waiting for a short SQLite writer lock or dispatch handoff.
            timestamp = now or utcnow()
            previous = connection.execute("SELECT * FROM operations WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                if previous["digest"] == digest and previous["source_id"] == source_id:
                    return Admission(json.loads(previous["receipt_json"]), False, True)
                self._append(connection, request_id, None, "REQUEST_ID_REUSED",
                             {"original_digest": previous["digest"], "observed_digest": digest,
                              "source_changed": previous["source_id"] != source_id}, timestamp)
                return Admission(_receipt(request_id, digest, "REJECTED", "REQUEST_ID_REUSED", timestamp), False)
            if connection.execute("SELECT 1 FROM v2_requests WHERE request_id=?", (request_id,)).fetchone():
                return Admission(_receipt(request_id, digest, "REJECTED", "REQUEST_ID_REUSED", timestamp), False)
            state = self._state(connection)
            code = rejection_code
            if code is None and (command.get("runner_id") != state["runner_id"] or command.get("epoch") != state["epoch"]):
                code = "WRONG_RUNNER_EPOCH"
            if (code is None and not state["armed"]
                    and command.get("action") not in {
                        "CANCEL_OPERATION", "RECONCILE", "RESUME_RETARGET",
                    }):
                code = "UNARMED"
            if code is None:
                expires_at = command.get("expires_at")
                try:
                    if not isinstance(expires_at, str):
                        raise TypeError("Missing expiration")
                    expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                    observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if expiry.tzinfo is None or observed.tzinfo is None:
                        raise ValueError("Timezone required")
                    if expiry <= observed:
                        code = "EXPIRED"
                except (TypeError, ValueError, OverflowError):
                    code = "INVALID_TIME"
            if code is None:
                held_scopes = [row[0] for row in connection.execute("SELECT scope FROM locks")]
                busy = any(_scope_conflict(scope, held) for scope in normalized_scopes for held in held_scopes)
                capacity = connection.execute("SELECT count(*) FROM operations WHERE capacity_held=1").fetchone()[0]
                if (self.single_execution_lane and not exempt_capacity
                        and connection.execute("SELECT 1 FROM execution_lane").fetchone() is not None):
                    code = "BUSY"
                if busy or (not exempt_capacity and capacity >= self.max_parallel):
                    code = "BUSY"
            if code is None and command.get("action") == "RESUME_RETARGET":
                params = command.get("params")
                target_id = params.get("target_request_id") if isinstance(params, dict) else None
                target = connection.execute(
                    "SELECT action,project_id,workspace_id,status,reconcile_required "
                    "FROM operations WHERE request_id=?", (target_id,),
                ).fetchone() if isinstance(target_id, str) else None
                if source_id != "local-owner":
                    code = "SOURCE_SCOPE"
                elif (target is None or target["action"] != "RETARGET_WORKSPACE"
                      or target["project_id"] != command.get("project_id")
                      or target["workspace_id"] != command.get("workspace_id")
                      or target["status"] not in {
                          "BLOCKED", "FAULT", "INTERRUPTED", "CANCELLED",
                      }
                      or not target["reconcile_required"]
                      or connection.execute(
                          "SELECT 1 FROM locks WHERE request_id=? AND scope=?",
                          (target_id, f"repo:{target['project_id']}" if target else ""),
                      ).fetchone() is None):
                    code = "RETARGET_RESUME_NOT_PARTIAL"
                elif connection.execute(
                    "SELECT 1 FROM operations WHERE action='RESUME_RETARGET' "
                    "AND status='RUNNING' AND project_id=? AND workspace_id=? LIMIT 1",
                    (target["project_id"], target["workspace_id"]),
                ).fetchone() is not None:
                    code = "BUSY"
                elif connection.execute(
                    "SELECT 1 FROM journal WHERE request_id=? "
                    "AND event_type='RETARGET_RESUME_CLAIMED' LIMIT 1",
                    (target_id,),
                ).fetchone() is not None:
                    code = "RETARGET_RESUME_CLAIMED"
            report_run_id: str | None = None
            if code is None and command.get("action") == "PUBLISH_REPORT":
                params = command.get("params")
                candidate = params.get("source_run_id") if isinstance(params, dict) else None
                try:
                    if not isinstance(candidate, str) or str(UUID(candidate)) != candidate:
                        raise ValueError
                except (TypeError, ValueError, AttributeError):
                    code = "DELIVERY_RUN_MISMATCH"
                else:
                    source_run = connection.execute(
                        "SELECT r.project_id,r.workspace_id,s.project_id AS source_project_id,"
                        "s.workspace_id AS source_workspace_id,s.action,s.status,"
                        "e.run_id AS contained_run_id "
                        "FROM runs r JOIN operations s ON s.request_id=r.request_id "
                        "LEFT JOIN run_executions e ON e.run_id=r.run_id "
                        "WHERE r.run_id=?", (candidate,),
                    ).fetchone()
                    if source_run is None:
                        code = "SOURCE_RUN_MISSING"
                    elif (source_run["project_id"] != command.get("project_id")
                            or source_run["workspace_id"] != command.get("workspace_id")
                            or source_run["source_project_id"] != source_run["project_id"]
                            or source_run["source_workspace_id"] != source_run["workspace_id"]
                            or source_run["action"] not in {"VERIFY", "DELIVER_VERIFY_PUBLISH"}):
                        code = "DELIVERY_SOURCE_MISMATCH"
                    elif source_run["status"] not in TERMINAL:
                        code = "DELIVERY_RUN_MISMATCH"
                    elif source_run["contained_run_id"] is None:
                        code = "CONTAINED_RUN_MISSING"
                    elif f"workspace:{source_run['project_id']}/{source_run['workspace_id']}" not in normalized_scopes:
                        code = "DELIVERY_LOCK_MISSING"
                    elif connection.execute(
                        "SELECT 1 FROM output_tombstones WHERE run_id=? LIMIT 1",
                        (candidate,),
                    ).fetchone() is not None:
                        code = "SOURCE_EVIDENCE_RETIRED"
                    else:
                        report_run_id = candidate
            status = "REJECTED" if code else "RUNNING"
            receipt = _receipt(request_id, digest, status, code, timestamp)
            # Invalid command fields remain only in frozen JSON. They never become
            # foreign registry authority or unbound SQL values.
            def field(key: str) -> str | None:
                value = command.get(key)
                return value if isinstance(value, str) else None
            connection.execute(
                "INSERT INTO operations(request_id,digest,command_json,source_id,project_id,workspace_id,action,status,"
                "code,received_at,updated_at,receipt_json,capacity_held) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, digest, command_json, source_id, field("project_id"), field("workspace_id"), field("action"),
                 status, code, timestamp, timestamp, _json(receipt), int(code is None and not exempt_capacity)),
            )
            if code is None:
                connection.executemany("INSERT INTO locks VALUES(?,?,?)",
                                       [(scope, request_id, timestamp) for scope in normalized_scopes])
                if self.single_execution_lane and not exempt_capacity:
                    connection.execute("INSERT INTO execution_lane VALUES(1,'v1',?,?)",
                                       (request_id, timestamp))
                if report_run_id is not None:
                    connection.execute(
                        "INSERT INTO run_delivery_claims(delivery_request_id,run_id,created_at) "
                        "VALUES(?,?,?)", (request_id, report_run_id, timestamp),
                    )
            self._append(connection, request_id, None, status, {"code": code}, timestamp)
            return Admission(receipt, code is None)

    def finish(self, request_id: str, status: str, code: str | None = None,
               result: dict[str, Any] | None = None) -> dict[str, Any]:
        if status not in TERMINAL:
            raise LedgerError("INVALID_TRANSITION", "A terminal status is required")
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM operations WHERE request_id=?", (request_id,)).fetchone()
            if row is None or row["status"] != "RUNNING":
                raise LedgerError("INVALID_TRANSITION", "Only a running operation can finish")
            timestamp = utcnow()
            interrupted = status == "INTERRUPTED"
            unresolved = connection.execute(
                "SELECT * FROM effects WHERE request_id=? AND state IN ('INTENDED','UNKNOWN')",
                (request_id,),
            ).fetchall()
            for effect in unresolved:
                if effect["state"] == "INTENDED":
                    connection.execute("UPDATE effects SET state='UNKNOWN',updated_at=? WHERE effect_id=?",
                                       (timestamp, effect["effect_id"]))
                    self._append(connection, request_id, effect["step_id"], "EFFECT_UNKNOWN",
                                 {"effect_id": effect["effect_id"], "code": "NEEDS_RECONCILIATION"}, timestamp)
            # Every durable create intent needs its own exact cleanup proof.
            # A generic handler fault code must not accidentally release a lock
            # after an ambiguous Docker create or a failed cleanup callback.
            try:
                intents, proofs = _container_state(connection, request_id)
                container_uncertain = any(phase not in proofs for phase in intents)
            except (TypeError, ValueError):
                container_uncertain = True
            retained = (interrupted or bool(unresolved) or container_uncertain
                        or code in _EXECUTOR_UNCERTAIN_CODES)
            if unresolved:
                if status == "COMPLETED":
                    status = "FAULT"
                code = "NEEDS_RECONCILIATION"
            elif container_uncertain:
                if status == "COMPLETED":
                    status = "FAULT"
                if code is None:
                    code = "CONTAINER_PROOF_REQUIRED"
            elif interrupted and code is None:
                code = "NEEDS_RECONCILIATION"
            receipt = _receipt(request_id, row["digest"], status, code, timestamp,
                               result.get("run_id") if result else None)
            connection.execute(
                "UPDATE operations SET status=?,code=?,updated_at=?,receipt_json=?,result_json=?,"
                "capacity_held=?,reconcile_required=? WHERE request_id=?",
                (status, code, timestamp, _json(receipt), _json(result) if result is not None else None,
                 row["capacity_held"] if retained else 0, int(retained), request_id),
            )
            if not retained:
                connection.execute("DELETE FROM locks WHERE request_id=?", (request_id,))
                connection.execute("DELETE FROM execution_lane WHERE protocol='v1' AND request_id=?",
                                   (request_id,))
            if (row["action"] == "PUBLISH_REPORT" and status == "COMPLETED"
                    and row["delivery_status"] == "DELIVERED"):
                connection.execute(
                    "INSERT INTO run_delivery_claim_releases(delivery_request_id,released_at) "
                    "SELECT delivery_request_id,? FROM run_delivery_claims "
                    "WHERE delivery_request_id=?", (timestamp, request_id),
                )
            self._append(connection, request_id, None, status, {"code": code}, timestamp)
            return receipt

    def confirm_stopped(self, request_id: str) -> None:
        """Release a terminal reservation only after every container is proven removed."""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status,reconcile_required FROM operations WHERE request_id=?", (request_id,),
            ).fetchone()
            if row is None or row["status"] not in {"INTERRUPTED", "FAULT", "BLOCKED"}:
                raise LedgerError("INVALID_TRANSITION",
                                  "Only uncertain terminal operations need reconciliation")
            if not row["reconcile_required"]:
                return
            effect = connection.execute(
                "SELECT 1 FROM effects WHERE request_id=? AND state IN ('INTENDED','UNKNOWN') LIMIT 1",
                (request_id,),
            ).fetchone()
            if effect:
                raise LedgerError("NEEDS_RECONCILIATION", "External effect is not confirmed")
            try:
                intents, proofs = _container_state(connection, request_id)
                if any(phase not in proofs for phase in intents):
                    raise ValueError("One or more containers have no removal proof")
            except (TypeError, ValueError):
                raise LedgerError("CONTAINER_PROOF_REQUIRED",
                                  "Every owned container must have an exact removal proof") from None
            connection.execute("DELETE FROM locks WHERE request_id=?", (request_id,))
            connection.execute("DELETE FROM execution_lane WHERE protocol='v1' AND request_id=?",
                               (request_id,))
            connection.execute(
                "UPDATE operations SET capacity_held=0,reconcile_required=0,updated_at=? "
                "WHERE request_id=?", (utcnow(), request_id),
            )
            self._append(connection, request_id, None,
                         "WORKERS_CONFIRMED_STOPPED", {}, utcnow())

    @staticmethod
    def _pre_dispatch_root_create(
        connection: sqlite3.Connection, request_id: str, source_id: str,
        digest: str, *, completed: bool = False,
    ) -> tuple[sqlite3.Row, sqlite3.Row | None]:
        """Prove the CREATE handler never passed its durable STARTED fence."""
        row = connection.execute(
            "SELECT * FROM operations WHERE request_id=?", (request_id,),
        ).fetchone()
        expected_code = "NEEDS_RECONCILIATION"
        if (row is None or row["status"] != "INTERRUPTED"
                or row["code"] != expected_code
                or row["reconcile_required"] != (0 if completed else 1)
                or row["action"] != "CREATE_PROJECT" or row["workspace_id"] is not None
                or row["source_id"] != source_id or row["digest"] != digest
                or not isinstance(row["project_id"], str)
                or hashlib.sha256(row["command_json"].encode("utf-8")).hexdigest() != digest):
            raise LedgerError("PRE_DISPATCH_PROOF_FAILED", "Pre-dispatch target differs")
        try:
            command = json.loads(row["command_json"])
            receipt = json.loads(row["receipt_json"])
        except (TypeError, ValueError):
            raise LedgerError("PRE_DISPATCH_PROOF_FAILED", "Frozen operation is invalid") from None
        if (not isinstance(command, dict) or _json(command) != row["command_json"]
                or command.get("request_id") != request_id
                or command.get("action") != "CREATE_PROJECT"
                or command.get("project_id") != row["project_id"]
                or command.get("workspace_id") is not None
                or not isinstance(receipt, dict)
                or receipt.get("request_id") != request_id
                or receipt.get("request_digest") != digest
                or receipt.get("status") != "INTERRUPTED"
                or receipt.get("code") != expected_code):
            raise LedgerError("PRE_DISPATCH_PROOF_FAILED", "Frozen operation differs")
        events = [item[0] for item in connection.execute(
            "SELECT event_type FROM journal WHERE request_id=? ORDER BY event_id", (request_id,),
        )]
        allowed_events = {
            "RUNNING", "INTERRUPTED", "EFFECT_INTENDED", "EFFECT_UNKNOWN",
            "EFFECT_CONFIRMED", "PRE_DISPATCH_ROOT_RECEIPT_CONFIRMED",
        }
        if (events[:2] != ["RUNNING", "INTERRUPTED"]
                or any(event not in allowed_events for event in events)
                or events.count("RUNNING") != 1 or events.count("INTERRUPTED") != 1):
            raise LedgerError("PRE_DISPATCH_PROOF_FAILED", "Dispatch was not proven absent")
        effects = connection.execute(
            "SELECT * FROM effects WHERE request_id=?", (request_id,),
        ).fetchall()
        steps = connection.execute(
            "SELECT step_id FROM steps WHERE request_id=?", (request_id,),
        ).fetchall()
        if (len(effects) > 1 or (effects and (
                effects[0]["step_id"] != _PRE_DISPATCH_ROOT_RECEIPT_STEP
                or effects[0]["kind"] != "drive_file"))
                or [step[0] for step in steps] != (
                    [_PRE_DISPATCH_ROOT_RECEIPT_STEP] if effects else []
                )
                or connection.execute(
                    "SELECT 1 FROM remote_resources WHERE request_id=? LIMIT 1", (request_id,),
                ).fetchone() is not None
                or connection.execute(
                    "SELECT 1 FROM runs WHERE request_id=? LIMIT 1", (request_id,),
                ).fetchone() is not None):
            raise LedgerError("PRE_DISPATCH_PROOF_FAILED", "Other work may have started")
        scopes = {item[0] for item in connection.execute(
            "SELECT scope FROM locks WHERE request_id=?", (request_id,),
        )}
        if completed:
            if row["capacity_held"] != 0 or scopes:
                raise LedgerError("PRE_DISPATCH_PROOF_FAILED", "Recovered reservation remains")
        elif (row["capacity_held"] != 1
              or f"repo:{row['project_id']}" not in scopes
              or "global:drive-installation" not in scopes):
            raise LedgerError("PRE_DISPATCH_PROOF_FAILED", "Admission reservation is absent")
        return row, effects[0] if effects else None

    @staticmethod
    def _terminal_root_create(
        connection: sqlite3.Connection, request_id: str, source_id: str, digest: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row | None, int, str]:
        """Prove a stopped Drive-root create has only confirmed partial effects."""
        row = connection.execute(
            "SELECT * FROM operations WHERE request_id=?", (request_id,),
        ).fetchone()
        if (row is None or row["status"] != "INTERRUPTED"
                or row["code"] != "NEEDS_RECONCILIATION"
                or row["action"] != "CREATE_PROJECT" or row["workspace_id"] is not None
                or row["source_id"] != source_id or row["digest"] != digest
                or row["reconcile_required"] != 0 or row["capacity_held"] != 0
                or hashlib.sha256(row["command_json"].encode("utf-8")).hexdigest() != digest
                or connection.execute(
                    "SELECT 1 FROM locks WHERE request_id=? LIMIT 1", (request_id,),
                ).fetchone() is not None
                or connection.execute(
                    "SELECT 1 FROM provision_resume_claims WHERE target_request_id=? LIMIT 1",
                    (request_id,),
                ).fetchone() is not None):
            raise LedgerError("TERMINAL_ROOT_TARGET_INVALID", "Partial create is not settled")
        try:
            command = json.loads(row["command_json"])
            receipt = json.loads(row["receipt_json"])
        except (TypeError, ValueError):
            raise LedgerError("TERMINAL_ROOT_TARGET_INVALID", "Partial create is invalid") from None
        if (not isinstance(command, dict) or _json(command) != row["command_json"]
                or command.get("request_id") != request_id
                or command.get("action") != "CREATE_PROJECT"
                or command.get("project_id") != row["project_id"]
                or command.get("workspace_id") is not None
                or not isinstance(receipt, dict)
                or receipt.get("request_id") != request_id
                or receipt.get("request_digest") != digest
                or receipt.get("status") != "INTERRUPTED"
                or receipt.get("code") != row["code"]):
            raise LedgerError("TERMINAL_ROOT_TARGET_INVALID", "Partial create identity changed")
        events = [item[0] for item in connection.execute(
            "SELECT event_type FROM journal WHERE request_id=? ORDER BY event_id",
            (request_id,),
        )]
        if (events.count("STARTED") != 1 or events.count("INTERRUPTED") != 1
                or "COMPLETED" in events):
            raise LedgerError("TERMINAL_ROOT_DISPATCH_UNPROVEN", "Started create is unproven")
        rows = connection.execute(
            "SELECT * FROM effects WHERE request_id=? ORDER BY effect_id", (request_id,),
        ).fetchall()
        root = [item for item in rows if item["step_id"] == _PRE_DISPATCH_ROOT_RECEIPT_STEP]
        if len(root) > 1 or (root and root[0]["kind"] != "drive_file"):
            raise LedgerError("TERMINAL_ROOT_RECEIPT_CONFLICT", "Root receipt intent differs")
        confirmed = []
        for item in rows:
            if item["step_id"] == _PRE_DISPATCH_ROOT_RECEIPT_STEP:
                continue
            try:
                intent = json.loads(item["intent_json"])
            except (TypeError, ValueError):
                raise LedgerError("TERMINAL_ROOT_EFFECTS_UNSETTLED",
                                  "Partial effect intent is invalid") from None
            confirmed.append({**dict(item), "intent": intent})
        count, effects_sha256 = confirmed_create_effects_proof(confirmed)
        return row, root[0] if root else None, count, effects_sha256

    def terminal_root_create_effects(
        self, request_id: str, *, source_id: str, digest: str,
    ) -> tuple[int, str]:
        """Read-only settled partial-create proof before any receipt confirmation."""
        with self._connection() as connection:
            _, _, count, effects_sha256 = self._terminal_root_create(
                connection, request_id, source_id, digest,
            )
            return count, effects_sha256

    def begin_terminal_root_receipt(
        self, request_id: str, *, source_id: str, digest: str,
        intent: Mapping[str, Any],
    ) -> str:
        """Journal one partial terminal receipt without replaying project effects."""
        if (not isinstance(intent, Mapping)
                or intent.get("command_doc_id") != source_id
                or intent.get("request_digest") != digest
                or intent.get("status") != "INTERRUPTED"
                or intent.get("code") != _TERMINAL_ROOT_RECEIPT_CODE
                or not isinstance(intent.get("receipt_doc_id"), str)
                or type(intent.get("confirmed_effect_count")) is not int
                or intent["confirmed_effect_count"] < 1
                or not isinstance(intent.get("confirmed_effects_sha256"), str)
                or _SHA256.fullmatch(intent["confirmed_effects_sha256"]) is None
                or not isinstance(intent.get("desired_sha256"), str)
                or _SHA256.fullmatch(intent["desired_sha256"]) is None
                or not isinstance(intent.get("base_revision"), str)
                or not intent["base_revision"]
                or not isinstance(intent.get("frozen_command_revision"), str)
                or not intent["frozen_command_revision"]
                or not isinstance(intent.get("frozen_command_sha256"), str)
                or _SHA256.fullmatch(intent["frozen_command_sha256"]) is None):
            raise LedgerError("TERMINAL_ROOT_INTENT_INVALID", "Partial receipt intent is invalid")
        intent_json = _json(dict(intent))
        with self._transaction() as connection:
            _, previous, count, effects_sha256 = self._terminal_root_create(
                connection, request_id, source_id, digest,
            )
            if (intent["confirmed_effect_count"] != count
                    or intent["confirmed_effects_sha256"] != effects_sha256):
                raise LedgerError("TERMINAL_ROOT_EFFECTS_CHANGED", "Confirmed effects changed")
            if previous is not None:
                if previous["intent_json"] != intent_json:
                    raise LedgerError("TERMINAL_ROOT_INTENT_CHANGED", "Recovery intent changed")
                return str(previous["effect_id"])
            timestamp, effect_id = utcnow(), str(uuid4())
            self._append(connection, request_id, _PRE_DISPATCH_ROOT_RECEIPT_STEP,
                         "EFFECT_INTENDED", {"effect_id": effect_id, "kind": "drive_file"},
                         timestamp)
            connection.execute(
                "INSERT INTO effects(effect_id,request_id,step_id,kind,intent_json,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'INTENDED',?,?)",
                (effect_id, request_id, _PRE_DISPATCH_ROOT_RECEIPT_STEP, "drive_file",
                 intent_json, timestamp, timestamp),
            )
            return effect_id

    def complete_terminal_root_receipt(
        self, request_id: str, *, source_id: str, digest: str,
        receipt_doc_id: str, effect_id: str,
    ) -> None:
        """Acknowledge the exact read-back terminal receipt in the local operation."""
        with self._transaction() as connection:
            _, effect, count, effects_sha256 = self._terminal_root_create(
                connection, request_id, source_id, digest,
            )
            if (effect is None or effect["effect_id"] != effect_id
                    or effect["state"] != "CONFIRMED"
                    or effect["provider_id"] != receipt_doc_id):
                raise LedgerError("TERMINAL_ROOT_RECEIPT_UNCONFIRMED",
                                  "Partial receipt has no exact confirmation")
            try:
                intent = json.loads(effect["intent_json"])
            except (TypeError, ValueError):
                raise LedgerError("TERMINAL_ROOT_RECEIPT_UNCONFIRMED",
                                  "Partial receipt intent is invalid") from None
            if (not isinstance(intent, dict)
                    or intent.get("command_doc_id") != source_id
                    or intent.get("receipt_doc_id") != receipt_doc_id
                    or intent.get("request_digest") != digest
                    or intent.get("status") != "INTERRUPTED"
                    or intent.get("code") != _TERMINAL_ROOT_RECEIPT_CODE
                    or intent.get("confirmed_effect_count") != count
                    or intent.get("confirmed_effects_sha256") != effects_sha256):
                raise LedgerError("TERMINAL_ROOT_RECEIPT_UNCONFIRMED",
                                  "Partial receipt differs from confirmed effects")
            detail = {
                "effect_id": effect_id, "receipt_doc_id": receipt_doc_id,
                "confirmed_effect_count": count,
                "confirmed_effects_sha256": effects_sha256,
            }
            confirmations = connection.execute(
                "SELECT detail_json FROM journal WHERE request_id=? "
                "AND event_type='TERMINAL_ROOT_RECEIPT_CONFIRMED'", (request_id,),
            ).fetchall()
            if confirmations:
                try:
                    recorded = json.loads(confirmations[0][0])
                except (TypeError, ValueError):
                    recorded = None
                if len(confirmations) != 1 or recorded != detail:
                    raise LedgerError("TERMINAL_ROOT_RECEIPT_CONFLICT",
                                      "Partial receipt completion changed")
                return
            self._append(connection, request_id, None, "TERMINAL_ROOT_RECEIPT_CONFIRMED",
                         detail, utcnow())

    def confirmed_terminal_root_receipt(
        self, request_id: str, *, source_id: str, digest: str,
        receipt_doc_id: str,
    ) -> dict[str, Any]:
        """Read only the exact committed partial root receipt proof, never adopt it."""
        with self._connection() as connection:
            _, effect, count, effects_sha256 = self._terminal_root_create(
                connection, request_id, source_id, digest,
            )
            if (effect is None or effect["state"] != "CONFIRMED"
                    or effect["provider_id"] != receipt_doc_id):
                raise LedgerError("TERMINAL_ROOT_RECEIPT_UNCONFIRMED",
                                  "Partial receipt has no exact confirmation")
            try:
                intent = json.loads(effect["intent_json"])
            except (TypeError, ValueError):
                raise LedgerError("TERMINAL_ROOT_RECEIPT_UNCONFIRMED",
                                  "Partial receipt intent is invalid") from None
            if (not isinstance(intent, dict)
                    or intent.get("command_doc_id") != source_id
                    or intent.get("receipt_doc_id") != receipt_doc_id
                    or intent.get("request_digest") != digest
                    or intent.get("status") != "INTERRUPTED"
                    or intent.get("code") != _TERMINAL_ROOT_RECEIPT_CODE
                    or intent.get("confirmed_effect_count") != count
                    or intent.get("confirmed_effects_sha256") != effects_sha256
                    or not isinstance(intent.get("desired_sha256"), str)
                    or _SHA256.fullmatch(intent["desired_sha256"]) is None):
                raise LedgerError("TERMINAL_ROOT_RECEIPT_UNCONFIRMED",
                                  "Partial receipt intent differs from confirmed effects")
            confirmations = connection.execute(
                "SELECT detail_json FROM journal WHERE request_id=? "
                "AND event_type='TERMINAL_ROOT_RECEIPT_CONFIRMED'", (request_id,),
            ).fetchall()
            if len(confirmations) != 1:
                raise LedgerError("TERMINAL_ROOT_RECEIPT_UNCONFIRMED",
                                  "Partial receipt completion is unproven")
            try:
                detail = json.loads(confirmations[0][0])
            except (TypeError, ValueError):
                raise LedgerError("TERMINAL_ROOT_RECEIPT_UNCONFIRMED",
                                  "Partial receipt completion is invalid") from None
            if detail != {
                "effect_id": effect["effect_id"], "receipt_doc_id": receipt_doc_id,
                "confirmed_effect_count": count,
                "confirmed_effects_sha256": effects_sha256,
            }:
                raise LedgerError("TERMINAL_ROOT_RECEIPT_UNCONFIRMED",
                                  "Partial receipt completion changed")
            return intent

    def begin_pre_dispatch_root_receipt(
        self, request_id: str, *, source_id: str, digest: str,
        intent: Mapping[str, Any],
    ) -> str:
        """Journal only the terminal root receipt for a proven pre-handler crash.

        The operation is terminal, so the normal RUNNING-only begin_effect cannot
        be used. A repeated call may return only the exact same prior intent.
        """
        if (not isinstance(intent, Mapping)
                or intent.get("command_doc_id") != source_id
                or intent.get("request_digest") != digest
                or intent.get("status") != "INTERRUPTED"
                or intent.get("code") != _PRE_DISPATCH_ROOT_RECEIPT_CODE
                or not isinstance(intent.get("receipt_doc_id"), str)
                or not isinstance(intent.get("desired_sha256"), str)
                or _SHA256.fullmatch(intent["desired_sha256"]) is None):
            raise LedgerError("PRE_DISPATCH_INTENT_INVALID", "Recovery receipt intent is invalid")
        intent_json = _json(dict(intent))
        with self._transaction() as connection:
            _, previous = self._pre_dispatch_root_create(
                connection, request_id, source_id, digest,
            )
            if previous is not None:
                if previous["intent_json"] != intent_json:
                    raise LedgerError("PRE_DISPATCH_INTENT_CHANGED", "Recovery intent changed")
                return str(previous["effect_id"])
            timestamp, effect_id = utcnow(), str(uuid4())
            self._append(connection, request_id, _PRE_DISPATCH_ROOT_RECEIPT_STEP,
                         "EFFECT_INTENDED", {"effect_id": effect_id, "kind": "drive_file"},
                         timestamp)
            connection.execute(
                "INSERT INTO effects(effect_id,request_id,step_id,kind,intent_json,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'INTENDED',?,?)",
                (effect_id, request_id, _PRE_DISPATCH_ROOT_RECEIPT_STEP, "drive_file",
                 intent_json, timestamp, timestamp),
            )
            return effect_id

    def complete_pre_dispatch_root_receipt(
        self, request_id: str, *, source_id: str, digest: str,
        receipt_doc_id: str, effect_id: str,
    ) -> None:
        """Release retained admission only after the exact root receipt is confirmed."""
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT reconcile_required FROM operations WHERE request_id=?", (request_id,),
            ).fetchone()
            completed = current is not None and current["reconcile_required"] == 0
            _, effect = self._pre_dispatch_root_create(
                connection, request_id, source_id, digest, completed=completed,
            )
            if (effect is None or effect["effect_id"] != effect_id
                    or effect["state"] != "CONFIRMED"
                    or effect["provider_id"] != receipt_doc_id):
                raise LedgerError("PRE_DISPATCH_RECEIPT_UNCONFIRMED", "Recovery receipt is unconfirmed")
            try:
                intent = json.loads(effect["intent_json"])
            except (TypeError, ValueError):
                raise LedgerError("PRE_DISPATCH_RECEIPT_UNCONFIRMED", "Recovery intent is invalid") from None
            if (not isinstance(intent, dict)
                    or intent.get("command_doc_id") != source_id
                    or intent.get("receipt_doc_id") != receipt_doc_id
                    or intent.get("request_digest") != digest
                    or intent.get("status") != "INTERRUPTED"
                    or intent.get("code") != _PRE_DISPATCH_ROOT_RECEIPT_CODE):
                raise LedgerError("PRE_DISPATCH_RECEIPT_UNCONFIRMED", "Recovery intent differs")
            if completed:
                return
            timestamp = utcnow()
            connection.execute("DELETE FROM locks WHERE request_id=?", (request_id,))
            connection.execute(
                "UPDATE operations SET capacity_held=0,reconcile_required=0,updated_at=? "
                "WHERE request_id=?", (timestamp, request_id),
            )
            self._append(connection, request_id, None,
                         "PRE_DISPATCH_ROOT_RECEIPT_CONFIRMED",
                         {"effect_id": effect_id, "receipt_doc_id": receipt_doc_id},
                         timestamp)

    def journal(self, request_id: str, step_id: str | None, event_type: str,
                detail: dict[str, Any]) -> dict[str, Any]:
        # Container proofs need the separate admission and identity checks below.
        if event_type in {"CONTAINER_INTENDED", "CONTAINER_CLEANED", "CONTAINER_RECONCILED",
                          "CONTAINER_ABSENT"}:
            raise LedgerError("RESERVED_EVENT_TYPE",
                              "Container events require the checked local methods")
        # Callers supply sanitized structured facts, never raw command/output text.
        with self._transaction() as connection:
            return self._append(connection, request_id, step_id, event_type, detail, utcnow())

    @staticmethod
    def _locked_execution(connection: sqlite3.Connection, request_id: str,
                          *, running: bool = True) -> sqlite3.Row:
        row = connection.execute(
            "SELECT status,action,project_id,workspace_id,reconcile_required "
            "FROM operations WHERE request_id=?", (request_id,),
        ).fetchone()
        if (row is None or (running and row["status"] != "RUNNING")
                or (not running and row["status"] not in
                    {"RUNNING", "INTERRUPTED", "FAULT", "BLOCKED"})):
            raise LedgerError("CONTAINER_INTENT_NOT_ADMITTED",
                              "Execution is no longer admitted")
        scope = _execution_scope(row)
        if connection.execute(
            "SELECT 1 FROM locks WHERE request_id=? AND scope=?", (request_id, scope),
        ).fetchone() is None:
            raise LedgerError("CONTAINER_INTENT_NOT_ADMITTED",
                              "Execution no longer owns its exact lock")
        return cast(sqlite3.Row, row)

    def reserve_phase_attempt(self, request_id: str, phase: str,
                              run_id: str, snapshot_id: str) -> None:
        """Reserve one immutable attempt per phase before any phase-local side effect."""
        if phase not in _PHASES or not _canonical_uuid(run_id) or not _canonical_uuid(snapshot_id):
            raise LedgerError("PHASE_ATTEMPT_INVALID", "Phase or run identity is invalid")
        with self._transaction() as connection:
            try:
                row = self._locked_execution(connection, request_id)
            except LedgerError:
                raise LedgerError("OPERATION_NOT_RUNNING",
                                  "Run operation is no longer active") from None
            if not self._state(connection)["armed"]:
                raise LedgerError("PHASE_ATTEMPT_NOT_ADMITTED", "Runner is not armed")
            snapshot = connection.execute(
                "SELECT project_id,workspace_id,base_head,snapshot_commit,provenance "
                "FROM source_snapshots WHERE snapshot_id=?", (snapshot_id,),
            ).fetchone()
            effect = connection.execute(
                "SELECT intent_json,state,provider_id FROM effects WHERE request_id=? "
                "AND step_id='source-snapshot' AND kind='git_ref'", (request_id,),
            ).fetchone()
            operation = connection.execute(
                "SELECT command_json FROM operations WHERE request_id=?", (request_id,),
            ).fetchone()
            try:
                source_intent = json.loads(effect["intent_json"]) if effect else None
                command = json.loads(operation["command_json"]) if operation else None
            except (TypeError, ValueError):
                source_intent, command = None, None
            if (snapshot is None or snapshot["project_id"] != row["project_id"]
                    or snapshot["workspace_id"] != row["workspace_id"]
                    or effect is None or effect["state"] != "CONFIRMED"
                    or effect["provider_id"] != snapshot["snapshot_commit"]
                    or not isinstance(source_intent, dict)
                    or not isinstance(command, dict)
                    or source_intent.get("snapshot_id") != snapshot_id
                    or source_intent.get("project_id") != row["project_id"]
                    or source_intent.get("workspace_id") != row["workspace_id"]
                    or source_intent.get("kind") != snapshot["provenance"]
                    or source_intent.get("expected_head") != snapshot["base_head"]
                    or command.get("expected_head") != snapshot["base_head"]):
                raise LedgerError("PHASE_ATTEMPT_NOT_ADMITTED",
                                  "Phase snapshot lacks an exact admitted source effect")
            if phase == "PREPARATION":
                intents, _ = _container_state(connection, request_id)
                if "TEST" in intents:
                    raise LedgerError("PHASE_ATTEMPT_NOT_ADMITTED",
                                      "Preparation cannot begin after test container intent")
            try:
                connection.execute(
                    "INSERT INTO run_attempts(request_id,phase,run_id,snapshot_id,created_at) "
                    "VALUES(?,?,?,?,?)", (request_id, phase, run_id, snapshot_id, utcnow()),
                )
            except sqlite3.IntegrityError:
                raise LedgerError("RUN_ALREADY_ATTEMPTED",
                                  "This phase already has an immutable attempt") from None

    def record_container_intent(self, request_id: str,
                                detail: dict[str, Any], *, phase: str = "TEST") -> None:
        """Atomically fence each Docker create against interruption and lock release."""
        if phase not in _PHASES or not isinstance(detail, dict):
            raise LedgerError("CONTAINER_INTENT_NOT_ADMITTED", "Container phase is invalid")
        fields = dict(detail)
        if fields.get("phase", phase) != phase:
            raise LedgerError("CONTAINER_INTENT_NOT_ADMITTED", "Container phase differs")
        allowed = {
            "phase", "run_id", "daemon_id", "profile_id",
            "profile_revision_sha256", "image", "image_id",
            "argv_sha256", "nonce", "source_path",
        }
        if phase == "TEST":
            allowed.update({"preparation_run_id", "plan_identity_sha256",
                            "prepared_build_tree_sha256", "test_volume_path"})
        if set(fields) - allowed:
            raise LedgerError("CONTAINER_INTENT_NOT_ADMITTED",
                              "Container intent contains unexpected fields")
        fields["phase"] = phase
        source_path = fields.get("source_path")
        # New intents always pin their exact source mount. Legacy schema-6 TEST
        # events may lack it and are handled only by the read/recovery path.
        source = Path(source_path) if isinstance(source_path, str) else None
        if (source is None or not source.is_absolute() or ".." in source.parts
                or str(source) != source_path or len(source_path) > 4096
                or re.fullmatch(r"/[A-Za-z0-9._/-]+", source_path) is None):
            raise LedgerError("CONTAINER_INTENT_NOT_ADMITTED",
                              "Exact source mount identity is required")
        if (not _canonical_uuid(fields.get("run_id"))
                or not isinstance(fields.get("daemon_id"), str)
                or not 1 <= len(fields["daemon_id"]) <= 256
                or not isinstance(fields.get("profile_id"), str)
                or re.fullmatch(r"[a-z][a-z0-9-]{0,47}", fields["profile_id"]) is None
                or not all(isinstance(fields.get(key), str)
                           and _SHA256.fullmatch(fields[key]) is not None
                           for key in ("profile_revision_sha256", "argv_sha256"))
                or not isinstance(fields.get("image"), str)
                or re.search(r"@sha256:[0-9a-f]{64}\Z", fields["image"]) is None
                or not isinstance(fields.get("image_id"), str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", fields["image_id"]) is None
                or not isinstance(fields.get("nonce"), str)
                or re.fullmatch(r"[0-9a-f]{32}", fields["nonce"]) is None):
            raise LedgerError("CONTAINER_INTENT_NOT_ADMITTED", "Container identity is invalid")
        with self._transaction() as connection:
            self._locked_execution(connection, request_id)
            if not self._state(connection)["armed"]:
                raise LedgerError("CONTAINER_INTENT_NOT_ADMITTED",
                                  "Execution is no longer armed")
            attempt = connection.execute(
                "SELECT run_id,snapshot_id FROM run_attempts WHERE request_id=? AND phase=?",
                (request_id, phase),
            ).fetchone()
            try:
                intents, _ = _container_state(connection, request_id)
            except (TypeError, ValueError):
                raise LedgerError("CONTAINER_INTENT_NOT_ADMITTED",
                                  "Durable container history is inconsistent") from None
            if (attempt is None or attempt["run_id"] != fields["run_id"] or phase in intents):
                raise LedgerError("CONTAINER_INTENT_NOT_ADMITTED",
                                  "Contained phase lacks its unique admitted reservation")
            if phase == "PREPARATION" and "TEST" in intents:
                raise LedgerError("CONTAINER_INTENT_NOT_ADMITTED",
                                  "Preparation cannot start after a test intent")
            if phase == "TEST":
                preparation = connection.execute(
                    "SELECT run_id,snapshot_id FROM run_attempts "
                    "WHERE request_id=? AND phase='PREPARATION'", (request_id,),
                ).fetchone()
                if preparation is not None:
                    handoff = connection.execute(
                        "SELECT * FROM preparation_handoffs WHERE request_id=?", (request_id,),
                    ).fetchone()
                    required = {
                        "preparation_run_id": preparation["run_id"],
                        "plan_identity_sha256": handoff["plan_identity_sha256"] if handoff else None,
                        "prepared_build_tree_sha256": handoff["build_tree_sha256"] if handoff else None,
                        "test_volume_path": handoff["test_volume_path"] if handoff else None,
                    }
                    if (handoff is None or handoff["snapshot_id"] != attempt["snapshot_id"]
                            or handoff["test_run_id"] != attempt["run_id"]
                            or any(fields.get(key) != value for key, value in required.items())):
                        raise LedgerError("PREPARATION_HANDOFF_REQUIRED",
                                          "Test container requires the exact completed handoff")
            self._append(connection, request_id, f"container-intent-{phase.lower()}",
                         "CONTAINER_INTENDED", fields, utcnow())

    @staticmethod
    def _proof_fields(proof: object) -> dict[str, Any]:
        if isinstance(proof, Mapping):
            return {key: proof.get(key) for key in
                    ("run_id", "daemon_id", "state", "container_id")}
        return {key: getattr(proof, key, None) for key in
                ("run_id", "daemon_id", "state", "container_id")}

    def _record_container_proof(self, request_id: str, phase: str,
                                proof: object, event_type: str) -> None:
        if phase not in _PHASES:
            raise LedgerError("CONTAINER_PROOF_INVALID", "Container phase is invalid")
        fields = self._proof_fields(proof)
        removal = event_type in {"CONTAINER_CLEANED", "CONTAINER_RECONCILED"}
        if (not _canonical_uuid(fields["run_id"])
                or not isinstance(fields["daemon_id"], str)
                or (removal and (fields["state"] != "REMOVED"
                                 or not isinstance(fields["container_id"], str)
                                 or _CONTAINER_ID.fullmatch(fields["container_id"]) is None))
                or (not removal and (event_type != "CONTAINER_ABSENT"
                                     or fields["state"] != "ABSENT"
                                     or fields["container_id"] is not None))):
            raise LedgerError("CONTAINER_PROOF_INVALID", "Container removal proof is invalid")
        with self._transaction() as connection:
            row = self._locked_execution(connection, request_id, running=False)
            if row["status"] != "RUNNING" and not row["reconcile_required"]:
                raise LedgerError("CONTAINER_PROOF_INVALID",
                                  "Terminal operation does not retain an execution lock")
            try:
                intents, proofs = _container_state(connection, request_id)
            except (TypeError, ValueError):
                raise LedgerError("CONTAINER_PROOF_INVALID",
                                  "Durable container history is inconsistent") from None
            intent = intents.get(phase)
            if (intent is None or phase in proofs
                    or fields["run_id"] != intent.get("run_id")
                    or fields["daemon_id"] != intent.get("daemon_id")):
                raise LedgerError("CONTAINER_PROOF_INVALID",
                                  "Removal proof does not match an unproved intent")
            self._append(connection, request_id, f"container-cleanup-{phase.lower()}",
                         event_type, {"phase": phase, **fields}, utcnow())

    def record_container_cleaned(self, request_id: str, phase: str, proof: object) -> None:
        """Persist exact normal cleanup, including cleanup after a failing phase."""
        self._record_container_proof(request_id, phase, proof, "CONTAINER_CLEANED")

    def record_container_reconciled(self, request_id: str, phase: str, proof: object) -> None:
        """Trusted local recovery only, after removing an exact owned container."""
        self._record_container_proof(request_id, phase, proof, "CONTAINER_RECONCILED")

    def record_container_absent(self, request_id: str, phase: str,
                                     proof: object) -> None:
        """Journal a trusted same-daemon absence proof for one phase."""
        self._record_container_proof(request_id, phase, proof, "CONTAINER_ABSENT")

    def record_preparation_result(self, request_id: str, *, run_id: str,
                                  snapshot_id: str, plan_identity_sha256: str,
                                  preparation_profile_revision_sha256: str,
                                  image: str, outcome: str, exit_code: int | None,
                                  evidence_sha256: str, capture_complete: bool,
                                  cleanup_verified: bool) -> None:
        """Keep bounded phase evidence even when project preparation fails."""
        if (not _canonical_uuid(run_id) or not _canonical_uuid(snapshot_id)
                or not all(isinstance(value, str) and _SHA256.fullmatch(value) is not None
                           for value in (plan_identity_sha256,
                                         preparation_profile_revision_sha256,
                                         evidence_sha256))
                or not isinstance(image, str)
                or re.search(r"@sha256:[0-9a-f]{64}\Z", image) is None
                or outcome not in {"PASSED", "FAILED", "TIMED_OUT", "CANCELLED", "UNKNOWN"}
                or (exit_code is not None and (type(exit_code) is not int
                                               or not -(2**31) <= exit_code < 2**31))
                or type(capture_complete) is not bool or cleanup_verified is not True):
            raise LedgerError("PREPARATION_RESULT_INVALID",
                              "Preparation result identity or evidence is invalid")
        with self._transaction() as connection:
            self._locked_execution(connection, request_id)
            attempt = connection.execute(
                "SELECT run_id,snapshot_id FROM run_attempts "
                "WHERE request_id=? AND phase='PREPARATION'", (request_id,),
            ).fetchone()
            try:
                intents, proofs = _container_state(connection, request_id)
            except (TypeError, ValueError):
                raise LedgerError("PREPARATION_RESULT_INVALID",
                                  "Preparation container history is inconsistent") from None
            intended = intents.get("PREPARATION")
            if (attempt is None or attempt["run_id"] != run_id
                    or attempt["snapshot_id"] != snapshot_id or intended is None
                    or "PREPARATION" not in proofs
                    or intended.get("run_id") != run_id
                    or intended.get("profile_revision_sha256") !=
                    preparation_profile_revision_sha256
                    or intended.get("image") != image):
                raise LedgerError("PREPARATION_RESULT_NOT_ADMITTED",
                                  "Preparation result lacks its exact cleaned attempt")
            try:
                connection.execute(
                    "INSERT INTO preparation_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (request_id, run_id, snapshot_id, plan_identity_sha256,
                     preparation_profile_revision_sha256, image, outcome, exit_code,
                     evidence_sha256, int(capture_complete), 1, utcnow()),
                )
            except sqlite3.IntegrityError:
                raise LedgerError("PREPARATION_RESULT_EXISTS",
                                  "Preparation result is already immutable") from None
            self._append(connection, request_id, "preparation-result",
                         "PREPARATION_RESULT", {
                             "run_id": run_id, "outcome": outcome,
                             "exit_code": exit_code, "capture_complete": capture_complete,
                             "evidence_sha256": evidence_sha256,
                         }, utcnow())

    def record_preparation_handoff(self, request_id: str, *, run_id: str,
                                   snapshot_id: str, test_run_id: str,
                                   plan_identity_sha256: str,
                                   preparation_profile_revision_sha256: str,
                                   image: str, build_tree_sha256: str,
                                   evidence_sha256: str, test_volume_path: str) -> None:
        """Persist a successful, cleaned and staged preparation before test create."""
        volume = Path(test_volume_path) if isinstance(test_volume_path, str) else None
        if (not _canonical_uuid(run_id) or not _canonical_uuid(snapshot_id)
                or not _canonical_uuid(test_run_id)
                or not all(isinstance(value, str) and _SHA256.fullmatch(value) is not None
                           for value in (plan_identity_sha256,
                                         preparation_profile_revision_sha256,
                                         build_tree_sha256, evidence_sha256))
                or not isinstance(image, str)
                or re.search(r"@sha256:[0-9a-f]{64}\Z", image) is None
                or volume is None or not volume.is_absolute() or ".." in volume.parts
                or str(volume) != test_volume_path or volume.name != test_run_id
                or re.fullmatch(r"/[A-Za-z0-9._/-]+", test_volume_path) is None):
            raise LedgerError("PREPARATION_HANDOFF_INVALID", "Prepared handoff identity is invalid")
        with self._transaction() as connection:
            self._locked_execution(connection, request_id)
            preparation = connection.execute(
                "SELECT run_id,snapshot_id FROM run_attempts "
                "WHERE request_id=? AND phase='PREPARATION'", (request_id,),
            ).fetchone()
            test = connection.execute(
                "SELECT run_id,snapshot_id FROM run_attempts "
                "WHERE request_id=? AND phase='TEST'", (request_id,),
            ).fetchone()
            result = connection.execute(
                "SELECT * FROM preparation_results WHERE request_id=?", (request_id,),
            ).fetchone()
            if (preparation is None or preparation["run_id"] != run_id
                    or preparation["snapshot_id"] != snapshot_id
                    or (test is not None and (test["run_id"] != test_run_id
                                             or test["snapshot_id"] != snapshot_id))
                    or result is None or result["run_id"] != run_id
                    or result["snapshot_id"] != snapshot_id
                    or result["plan_identity_sha256"] != plan_identity_sha256
                    or result["profile_revision_sha256"] !=
                    preparation_profile_revision_sha256
                    or result["image"] != image or result["evidence_sha256"] != evidence_sha256
                    or result["outcome"] != "PASSED" or result["exit_code"] != 0
                    or result["capture_complete"] != 1 or result["cleanup_verified"] != 1):
                raise LedgerError("PREPARATION_HANDOFF_NOT_ADMITTED",
                                  "Handoff requires exact successful preparation evidence")
            try:
                connection.execute(
                    "INSERT INTO preparation_handoffs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (request_id, run_id, snapshot_id, test_run_id, plan_identity_sha256,
                     preparation_profile_revision_sha256, image, build_tree_sha256,
                     evidence_sha256, test_volume_path, utcnow()),
                )
            except sqlite3.IntegrityError:
                raise LedgerError("PREPARATION_HANDOFF_EXISTS",
                                  "Preparation handoff is already immutable") from None
            # The private volume path stays in the local table, never the event API.
            self._append(connection, request_id, "preparation-handoff",
                         "PREPARATION_HANDOFF", {
                             "preparation_run_id": run_id, "test_run_id": test_run_id,
                             "plan_identity_sha256": plan_identity_sha256,
                             "build_tree_sha256": build_tree_sha256,
                         }, utcnow())

    def events(self, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        if not isinstance(after, int) or after < 0 or not 1 <= limit <= 1000:
            raise ValueError("Invalid event cursor or page size")
        with self._connection() as connection:
            events = []
            for row in connection.execute(
                "SELECT * FROM journal WHERE event_id>? ORDER BY event_id LIMIT ?", (after, limit),
            ):
                detail = json.loads(row["detail_json"])
                if row["event_type"] == "CONTAINER_INTENDED" and isinstance(detail, dict):
                    # The nonce is needed for trusted local reconciliation but
                    # must not be exposed through the user-facing event stream.
                    detail.pop("nonce", None)
                    detail.pop("test_volume_path", None)
                    detail.pop("source_path", None)
                events.append({
                    "event_id": row["event_id"], "request_id": row["request_id"],
                    "step_id": row["step_id"], "timestamp": row["timestamp"],
                    "type": row["event_type"], "detail": detail,
                })
            return events

    def record_delivery(self, request_id: str, status: str, code: str | None = None) -> None:
        """Persist delivery progress separately from execution and Git publication."""
        if status not in {"PENDING", "PARTIAL", "DELIVERED", "FAILED"}:
            raise LedgerError("DELIVERY_STATUS_INVALID", "Unknown delivery state")
        if code is not None and (not isinstance(code, str) or len(code) > 100):
            raise LedgerError("DELIVERY_STATUS_INVALID", "Delivery reason must be a stable code")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status,delivery_status FROM operations WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None or row["status"] != "RUNNING":
                raise LedgerError("INVALID_TRANSITION", "Only a running operation records delivery")
            previous = row["delivery_status"]
            if previous == "DELIVERED" and status != "DELIVERED":
                raise LedgerError("INVALID_TRANSITION", "Delivered evidence cannot become pending")
            if previous == status:
                return
            timestamp = utcnow()
            connection.execute(
                "UPDATE operations SET delivery_status=?,updated_at=? WHERE request_id=?",
                (status, timestamp, request_id),
            )
            self._append(connection, request_id, "delivery", "DELIVERY_STATUS",
                         {"status": status, "code": code}, timestamp)

    def holds_lock(self, request_id: str, scope: str) -> bool:
        """Read the exact admission reservation required by a finite side-effect plan."""
        if not isinstance(scope, str) or _SCOPE.fullmatch(scope) is None:
            raise ValueError("Invalid locally derived resource scope")
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM locks WHERE request_id=? AND scope=?", (request_id, scope)
            ).fetchone() is not None

    def require_delivery_preflight(self, delivery_request_id: str, run_id: str) -> DeliveryPreflight:
        """Bind a live delivery operation to one registered contained run.

        This is a read-only SQLite snapshot. A PUBLISH_REPORT run claim was
        recorded atomically at admission; the publisher must still declare a
        durable effect before each external write. This check does not authorize
        a replay after a crash.
        """
        for value in (delivery_request_id, run_id):
            try:
                if not isinstance(value, str) or str(UUID(value)) != value:
                    raise ValueError
            except (TypeError, ValueError, AttributeError):
                raise LedgerError("INVALID_DELIVERY_ID", "Canonical request and run IDs are required") from None
        with self._connection() as connection:
            connection.execute("BEGIN")
            try:
                actor = connection.execute(
                    "SELECT request_id,project_id,workspace_id,action,status,command_json "
                    "FROM operations WHERE request_id=?", (delivery_request_id,),
                ).fetchone()
                run = connection.execute(
                    "SELECT run_id,request_id,project_id,workspace_id,manifest_json "
                    "FROM runs WHERE run_id=?", (run_id,),
                ).fetchone()
                if actor is None or actor["status"] != "RUNNING":
                    raise LedgerError("DELIVERY_NOT_RUNNING", "Delivery request is not running")
                if run is None:
                    raise LedgerError("SOURCE_RUN_MISSING", "Contained source run is unavailable")
                source = connection.execute(
                    "SELECT request_id,project_id,workspace_id,action,status,result_json "
                    "FROM operations WHERE request_id=?", (run["request_id"],),
                ).fetchone()
                if (source is None or source["action"] not in {"VERIFY", "DELIVER_VERIFY_PUBLISH"}
                        or not isinstance(run["project_id"], str)
                        or not isinstance(run["workspace_id"], str)
                        or any(row["project_id"] != run["project_id"]
                               or row["workspace_id"] != run["workspace_id"]
                               for row in (actor, source))):
                    raise LedgerError("DELIVERY_SOURCE_MISMATCH", "Delivery scope differs from source run")
                try:
                    command = json.loads(actor["command_json"])
                    manifest = json.loads(run["manifest_json"])
                except (TypeError, ValueError):
                    raise LedgerError("DELIVERY_SOURCE_MISMATCH", "Stored delivery identity is invalid") from None
                if (not isinstance(command, dict) or command.get("request_id") != delivery_request_id
                        or command.get("action") != actor["action"]
                        or command.get("project_id") != run["project_id"]
                        or command.get("workspace_id") != run["workspace_id"]
                        or not isinstance(manifest, dict)
                        or manifest.get("run_id") != run_id
                        or manifest.get("request_id") != run["request_id"]
                        or manifest.get("project_id") != run["project_id"]
                        or manifest.get("workspace_id") != run["workspace_id"]):
                    raise LedgerError("DELIVERY_SOURCE_MISMATCH", "Stored delivery identity is invalid")
                action = actor["action"]
                if action in {"VERIFY", "DELIVER_VERIFY_PUBLISH"}:
                    if delivery_request_id != run["request_id"] or action != source["action"]:
                        raise LedgerError("DELIVERY_RUN_MISMATCH", "Run belongs to another request")
                    scope = (f"workspace:{run['project_id']}/{run['workspace_id']}"
                             if action == "VERIFY" else f"repo:{run['project_id']}")
                elif action == "PUBLISH_REPORT":
                    params = command.get("params")
                    if (not isinstance(params, dict) or params.get("source_run_id") != run_id
                            or source["status"] not in TERMINAL):
                        raise LedgerError("DELIVERY_RUN_MISMATCH", "Report retry does not name a terminal run")
                    scope = f"workspace:{run['project_id']}/{run['workspace_id']}"
                else:
                    raise LedgerError("DELIVERY_ACTION_INVALID", "Action cannot deliver a run")
                if connection.execute(
                    "SELECT 1 FROM output_tombstones WHERE run_id=? LIMIT 1", (run_id,),
                ).fetchone() is not None:
                    raise LedgerError("SOURCE_EVIDENCE_RETIRED", "Run output retention has expired")
                if action == "PUBLISH_REPORT" and connection.execute(
                    "SELECT 1 FROM run_delivery_claims WHERE delivery_request_id=? AND run_id=?",
                    (delivery_request_id, run_id),
                ).fetchone() is None:
                    raise LedgerError("DELIVERY_CLAIM_MISSING", "Report retry has no admission claim")
                source_snapshot = connection.execute(
                    "SELECT s.project_id,s.workspace_id,s.snapshot_commit,s.tree_digest,"
                    "s.manifest_sha256 FROM run_executions e "
                    "JOIN source_snapshots s ON s.snapshot_id=e.snapshot_id "
                    "WHERE e.run_id=?", (run_id,),
                ).fetchone()
                if source_snapshot is None:
                    raise LedgerError("CONTAINED_RUN_MISSING", "Run has no registered contained execution")
                snapshot = manifest.get("snapshot")
                if (not isinstance(snapshot, dict)
                        or source_snapshot["project_id"] != run["project_id"]
                        or source_snapshot["workspace_id"] != run["workspace_id"]
                        or source_snapshot["snapshot_commit"] != snapshot.get("commit")
                        or source_snapshot["tree_digest"] != snapshot.get("tree")
                        or not isinstance(source_snapshot["manifest_sha256"], str)
                        or re.fullmatch(r"[0-9a-f]{64}", source_snapshot["manifest_sha256"]) is None):
                    raise LedgerError("DELIVERY_SOURCE_MISMATCH", "Contained source snapshot differs")
                if connection.execute(
                    "SELECT 1 FROM locks WHERE request_id=? AND scope=?",
                    (delivery_request_id, scope),
                ).fetchone() is None:
                    raise LedgerError("DELIVERY_LOCK_MISSING", "Delivery request lacks its resource lock")
                source_manifest_sha256: str | None = None
                if source["status"] in TERMINAL and source["result_json"] is not None:
                    try:
                        source_result = json.loads(source["result_json"])
                    except (TypeError, ValueError):
                        raise LedgerError(
                            "DELIVERY_SOURCE_MISMATCH", "Stored source result is invalid"
                        ) from None
                    if isinstance(source_result, dict) and "operation_manifest" in source_result:
                        source_manifest = source_result["operation_manifest"]
                        if (not isinstance(source_manifest, dict)
                                or source_manifest.get("request_id") != run["request_id"]
                                or source_manifest.get("project_id") != run["project_id"]
                                or source_manifest.get("workspace_id") != run["workspace_id"]
                                or not isinstance(source_manifest.get("run_ids"), list)
                                or run_id not in source_manifest["run_ids"]):
                            raise LedgerError(
                                "DELIVERY_SOURCE_MISMATCH", "Stored operation manifest is unbound"
                            )
                        source_manifest_sha256 = _document_sha256(source_manifest)
                preflight = DeliveryPreflight(
                    delivery_request_id, run["request_id"], run_id, run["project_id"],
                    run["workspace_id"], action, source["action"], source["status"], scope,
                    _document_sha256(manifest), source_snapshot["manifest_sha256"],
                    source_manifest_sha256,
                )
                connection.execute("COMMIT")
                return preflight
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def adopt_confirmed_effect(self, resume_request_id: str,
                               proof: EffectAdoptionProof) -> str:
        """Atomically copy one positively rechecked CREATE_PROJECT effect to a resume.

        The caller must obtain ``proof`` from an exact-ID, read-only provider or
        managed-source verifier. Equality with the old immutable intent/provider ID
        is checked here; this method does not itself call a cloud provider or replay
        its write. A claim binds one old create to only one resume request.
        """
        if (not isinstance(proof, EffectAdoptionProof)
                or not isinstance(proof.source_effect_id, str)
                or not isinstance(proof.provider_id, str)
                or not 1 <= len(proof.provider_id) <= 4096
                or any(ord(char) < 32 or ord(char) == 127
                       for char in proof.provider_id)
                or not isinstance(proof.observed_intent, Mapping)):
            raise LedgerError("ADOPTION_PROOF_INVALID", "Exact readback proof is required")
        try:
            observed_intent = _json(dict(proof.observed_intent))
            proof_json = _json({
                "source_effect_id": proof.source_effect_id,
                "provider_id": proof.provider_id,
                "intent": json.loads(observed_intent),
            })
        except (TypeError, ValueError, OverflowError):
            raise LedgerError("ADOPTION_PROOF_INVALID", "Exact readback proof is invalid") from None
        if len(proof_json.encode("utf-8")) > 65_536:
            raise LedgerError("ADOPTION_PROOF_INVALID", "Exact readback proof is too large")
        proof_sha256 = hashlib.sha256(proof_json.encode("utf-8")).hexdigest()
        with self._transaction() as connection:
            resume = connection.execute(
                "SELECT request_id,digest,command_json,project_id,workspace_id,action,status "
                "FROM operations WHERE request_id=?", (resume_request_id,),
            ).fetchone()
            if (resume is None or resume["status"] != "RUNNING"
                    or resume["action"] != "RESUME_PROVISION"
                    or resume["workspace_id"] is not None):
                raise LedgerError("ADOPTION_NOT_ADMITTED",
                                  "A running provision resume is required")
            source_effect = connection.execute(
                "SELECT * FROM effects WHERE effect_id=?", (proof.source_effect_id,),
            ).fetchone()
            if source_effect is None:
                raise LedgerError("ADOPTION_SOURCE_MISSING", "Original effect is missing")
            target_id = source_effect["request_id"]
            target = connection.execute(
                "SELECT request_id,digest,command_json,project_id,workspace_id,action,"
                "status,reconcile_required FROM operations WHERE request_id=?",
                (target_id,),
            ).fetchone()
            try:
                resume_command = json.loads(resume["command_json"])
                target_command = json.loads(target["command_json"]) if target else None
                if not isinstance(target_command, dict):
                    raise TypeError("Original command is not an object")
                alias = target_command["params"]["github_identity"]
                valid_commands = (
                    isinstance(resume_command, dict)
                    and isinstance(target_command, dict)
                    and resume_command.get("request_id") == resume_request_id
                    and resume_command.get("action") == "RESUME_PROVISION"
                    and resume_command.get("project_id") == resume["project_id"]
                    and resume_command.get("workspace_id") is None
                    and resume_command.get("expected_head") is None
                    and resume_command.get("params") == {
                        "resume_version": "1.0", "target_request_id": target_id,
                    }
                    and target_command.get("request_id") == target_id
                    and target_command.get("action") == "CREATE_PROJECT"
                    and target_command.get("project_id") == target["project_id"]
                    and target_command.get("workspace_id") is None
                    and isinstance(alias, str)
                    and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", alias) is not None
                    and hashlib.sha256(resume["command_json"].encode("utf-8")).hexdigest()
                        == resume["digest"]
                    and hashlib.sha256(target["command_json"].encode("utf-8")).hexdigest()
                        == target["digest"]
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                valid_commands = False
            if (not valid_commands or target is None
                    or target_id == resume_request_id
                    or target["action"] != "CREATE_PROJECT"
                    or target["status"] not in {"BLOCKED", "FAULT", "INTERRUPTED", "CANCELLED"}
                    or target["workspace_id"] is not None
                    or target["project_id"] != resume["project_id"]):
                raise LedgerError("ADOPTION_TARGET_MISMATCH",
                                  "Resume does not target the original partial create")
            if (target["reconcile_required"]
                    or connection.execute(
                        "SELECT 1 FROM effects WHERE request_id=? "
                        "AND state IN ('INTENDED','UNKNOWN') LIMIT 1", (target_id,),
                    ).fetchone() is not None
                    or connection.execute(
                        "SELECT 1 FROM locks WHERE request_id=? LIMIT 1", (target_id,),
                    ).fetchone() is not None):
                raise LedgerError("ADOPTION_RECONCILIATION_REQUIRED",
                                  "Original create still has uncertain effects or locks")
            required_scopes = {
                f"repo:{resume['project_id']}", f"global:{alias}",
                "global:drive-installation",
            }
            held_scopes = {
                row["scope"] for row in connection.execute(
                    "SELECT scope FROM locks WHERE request_id=?", (resume_request_id,),
                )
            }
            if held_scopes != required_scopes:
                raise LedgerError("ADOPTION_LOCK_MISSING",
                                  "Resume does not hold the exact provision locks")
            if (source_effect["state"] != "CONFIRMED"
                    or source_effect["provider_id"] != proof.provider_id
                    or source_effect["intent_json"] != observed_intent):
                raise LedgerError("ADOPTION_PROOF_MISMATCH",
                                  "Readback differs from the confirmed original effect")
            claim = connection.execute(
                "SELECT resume_request_id FROM provision_resume_claims "
                "WHERE target_request_id=?", (target_id,),
            ).fetchone()
            if claim is not None and claim["resume_request_id"] != resume_request_id:
                raise LedgerError("ADOPTION_TARGET_CLAIMED",
                                  "Another resume already claimed this partial create")
            previous = connection.execute(
                "SELECT * FROM effects WHERE request_id=? AND step_id=? AND kind=?",
                (resume_request_id, source_effect["step_id"], source_effect["kind"]),
            ).fetchone()
            if previous is not None:
                adoption = connection.execute(
                    "SELECT source_effect_id,proof_sha256 FROM effect_adoptions "
                    "WHERE effect_id=?", (previous["effect_id"],),
                ).fetchone()
                if (adoption is None
                        or adoption["source_effect_id"] != proof.source_effect_id
                        or adoption["proof_sha256"] != proof_sha256
                        or previous["intent_json"] != source_effect["intent_json"]
                        or previous["provider_id"] != proof.provider_id
                        or previous["state"] != "CONFIRMED"):
                    raise LedgerError("ADOPTION_EFFECT_CONFLICT",
                                      "Resume effect does not match the original proof")
                return str(previous["effect_id"])
            timestamp, effect_id = utcnow(), str(uuid4())
            if claim is None:
                connection.execute(
                    "INSERT INTO provision_resume_claims "
                    "(target_request_id,resume_request_id,created_at) VALUES(?,?,?)",
                    (target_id, resume_request_id, timestamp),
                )
            self._append(
                connection, resume_request_id, source_effect["step_id"], "EFFECT_ADOPTED",
                {"effect_id": effect_id, "source_effect_id": proof.source_effect_id,
                 "kind": source_effect["kind"], "provider_id": proof.provider_id,
                 "proof_sha256": proof_sha256}, timestamp,
            )
            connection.execute(
                "INSERT INTO effects(effect_id,request_id,step_id,kind,intent_json,state,"
                "provider_id,url,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'CONFIRMED',?,?,?,?)",
                (effect_id, resume_request_id, source_effect["step_id"],
                 source_effect["kind"], source_effect["intent_json"],
                 proof.provider_id, source_effect["url"], timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO effect_adoptions "
                "(effect_id,source_effect_id,proof_json,proof_sha256,verified_at) "
                "VALUES(?,?,?,?,?)",
                (effect_id, proof.source_effect_id, proof_json, proof_sha256, timestamp),
            )
            return effect_id

    def begin_terminal_project_receipt(
        self, request_id: str, *, source_id: str, digest: str,
        intent: Mapping[str, Any], workspace: bool = False,
    ) -> str:
        """Journal an exact scoped Drive receipt after terminal admission.

        A terminal output intent is narrow because generic effects are RUNNING-only.
        Uncertainty persists until the registered Doc is read back.
        """
        step = (_TERMINAL_WORKSPACE_RECEIPT_STEP if workspace
                else _TERMINAL_PROJECT_RECEIPT_STEP)
        scope = "WORKSPACE" if workspace else "PROJECT"
        actions = (
            {"RETARGET_WORKSPACE", "EXPORT_CONTEXT", "PUBLISH_REPORT",
             "DELIVER_VERIFY_PUBLISH"} if workspace else {"CREATE_WORKSPACE", "FETCH"}
        )
        required = {
            "command_doc_id", "receipt_doc_id", "request_digest", "status", "code",
            "desired_sha256", "frozen_command_revision", "frozen_command_sha256",
            "base_revision",
        }
        if workspace:
            required.update({"project_id", "workspace_id"})
        if (not isinstance(intent, Mapping) or set(intent) != required
                or intent.get("command_doc_id") != source_id
                or intent.get("request_digest") != digest
                or not isinstance(intent.get("receipt_doc_id"), str)
                or len(intent["receipt_doc_id"]) < 8
                or not isinstance(intent.get("base_revision"), str)
                or not intent["base_revision"]
                or not isinstance(intent.get("frozen_command_revision"), str)
                or not intent["frozen_command_revision"]
                or not isinstance(intent.get("desired_sha256"), str)
                or _SHA256.fullmatch(intent["desired_sha256"]) is None
                or not isinstance(intent.get("frozen_command_sha256"), str)
                or _SHA256.fullmatch(intent["frozen_command_sha256"]) is None):
            raise LedgerError(f"{scope}_RECEIPT_INTENT_INVALID", "Project receipt intent is invalid")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE request_id=?", (request_id,),
            ).fetchone()
            if (row is None or row["source_id"] != source_id or row["digest"] != digest
                    or row["action"] not in actions
                    or row["project_id"] is None
                    or (workspace and (row["workspace_id"] is None
                                       or intent.get("project_id") != row["project_id"]
                                       or intent.get("workspace_id") != row["workspace_id"]))
                    or row["status"] not in TERMINAL
                    or intent["status"] != row["status"]
                    or intent["code"] != row["code"]):
                raise LedgerError(f"{scope}_RECEIPT_TARGET_INVALID", "Project terminal target differs")
            receipt = json.loads(row["receipt_json"])
            if (receipt.get("request_id") != request_id
                    or receipt.get("request_digest") != digest
                    or receipt.get("status") != row["status"]
                    or receipt.get("code") != row["code"]
                    or _document_sha256(receipt) != intent["desired_sha256"]):
                raise LedgerError(f"{scope}_RECEIPT_TARGET_INVALID", "Committed receipt differs")
            previous = connection.execute(
                "SELECT * FROM effects WHERE request_id=? AND step_id=? AND kind='drive_file'",
                (request_id, step),
            ).fetchall()
            if len(previous) > 1:
                raise LedgerError(f"{scope}_RECEIPT_EFFECT_CONFLICT", "Project receipt effect duplicated")
            if previous:
                old = json.loads(previous[0]["intent_json"])
                if (not isinstance(old, dict)
                        or {key: old.get(key) for key in required} != dict(intent)
                        or type(old.get("prior_reconcile_required")) is not bool):
                    raise LedgerError(f"{scope}_RECEIPT_INTENT_CHANGED", "Project receipt intent changed")
                return str(previous[0]["effect_id"])
            timestamp, effect_id = utcnow(), str(uuid4())
            saved_intent = {**dict(intent),
                            "prior_reconcile_required": bool(row["reconcile_required"])}
            self._append(
                connection, request_id, step,
                "EFFECT_INTENDED", {"effect_id": effect_id, "kind": "drive_file"},
                timestamp,
            )
            connection.execute(
                "INSERT INTO effects(effect_id,request_id,step_id,kind,intent_json,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'INTENDED',?,?)",
                (effect_id, request_id, step, "drive_file",
                 _json(saved_intent), timestamp, timestamp),
            )
            connection.execute(
                "UPDATE operations SET reconcile_required=1,updated_at=? WHERE request_id=?",
                (timestamp, request_id),
            )
            return effect_id

    def complete_terminal_project_receipt(
        self, request_id: str, *, source_id: str, digest: str,
        receipt_doc_id: str, effect_id: str, workspace: bool = False,
    ) -> None:
        """Confirm an exact scoped receipt and release only its own uncertainty."""
        step = (_TERMINAL_WORKSPACE_RECEIPT_STEP if workspace
                else _TERMINAL_PROJECT_RECEIPT_STEP)
        scope = "WORKSPACE" if workspace else "PROJECT"
        actions = (
            {"RETARGET_WORKSPACE", "EXPORT_CONTEXT", "PUBLISH_REPORT",
             "DELIVER_VERIFY_PUBLISH"} if workspace else {"CREATE_WORKSPACE", "FETCH"}
        )
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE request_id=?", (request_id,),
            ).fetchone()
            effect = connection.execute(
                "SELECT * FROM effects WHERE effect_id=? AND request_id=? "
                "AND step_id=? AND kind='drive_file'",
                (effect_id, request_id, step),
            ).fetchone()
            if (row is None or row["source_id"] != source_id or row["digest"] != digest
                    or row["action"] not in actions
                    or (workspace and row["workspace_id"] is None)
                    or row["status"] not in TERMINAL
                    or effect is None or effect["state"] != "CONFIRMED"
                    or effect["provider_id"] != receipt_doc_id):
                raise LedgerError(f"{scope}_RECEIPT_UNCONFIRMED", "Project receipt is unconfirmed")
            intent = json.loads(effect["intent_json"])
            receipt = json.loads(row["receipt_json"])
            if (not isinstance(intent, dict)
                    or intent.get("command_doc_id") != source_id
                    or intent.get("receipt_doc_id") != receipt_doc_id
                    or intent.get("request_digest") != digest
                    or (workspace and (intent.get("project_id") != row["project_id"]
                                       or intent.get("workspace_id") != row["workspace_id"]))
                    or intent.get("status") != row["status"]
                    or intent.get("code") != row["code"]
                    or intent.get("desired_sha256") != _document_sha256(receipt)
                    or type(intent.get("prior_reconcile_required")) is not bool):
                raise LedgerError(f"{scope}_RECEIPT_UNCONFIRMED", "Project receipt intent differs")
            confirmations = connection.execute(
                "SELECT detail_json FROM journal WHERE request_id=? "
                "AND event_type=?",
                (request_id, f"TERMINAL_{scope}_RECEIPT_CONFIRMED"),
            ).fetchall()
            detail = {"effect_id": effect_id, "receipt_doc_id": receipt_doc_id}
            if confirmations:
                try:
                    old_detail = json.loads(confirmations[0][0])
                except (TypeError, ValueError):
                    old_detail = None
                if len(confirmations) != 1 or old_detail != detail:
                    raise LedgerError(f"{scope}_RECEIPT_EFFECT_CONFLICT",
                                      "Project receipt confirmation changed")
                return
            if not intent["prior_reconcile_required"]:
                unsettled = connection.execute(
                    "SELECT 1 FROM effects WHERE request_id=? AND state IN ('INTENDED','UNKNOWN') LIMIT 1",
                    (request_id,),
                ).fetchone()
                if unsettled is not None:
                    raise LedgerError(f"{scope}_RECEIPT_EFFECT_UNSETTLED",
                                      "Project receipt effect remains uncertain")
                connection.execute(
                    "UPDATE operations SET reconcile_required=0,updated_at=? WHERE request_id=?",
                    (utcnow(), request_id),
                )
            self._append(
                connection, request_id, step,
                f"TERMINAL_{scope}_RECEIPT_CONFIRMED", detail, utcnow(),
            )

    def begin_terminal_workspace_receipt(
        self, request_id: str, *, source_id: str, digest: str,
        intent: Mapping[str, Any],
    ) -> str:
        """Journal one exact workspace command receipt after terminal admission."""
        return self.begin_terminal_project_receipt(
            request_id, source_id=source_id, digest=digest, intent=intent, workspace=True,
        )

    def complete_terminal_workspace_receipt(
        self, request_id: str, *, source_id: str, digest: str,
        receipt_doc_id: str, effect_id: str,
    ) -> None:
        """Clear only uncertainty introduced by this workspace receipt write."""
        self.complete_terminal_project_receipt(
            request_id, source_id=source_id, digest=digest,
            receipt_doc_id=receipt_doc_id, effect_id=effect_id, workspace=True,
        )

    def begin_terminal_retarget_state(
        self, target_id: str, *, actor_id: str,
        intents: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, str]:
        """Atomically claim one owner-local continuation and both original Doc intents.

        This accepts only the crash cut before the original retarget declared
        *any* Drive association effect. It never retries an uncertain write.
        The retained original repository lock remains the mutation fence.
        """
        steps = {"drive-retarget-project-state", "drive-retarget-source-context"}
        if set(intents) != steps:
            raise LedgerError("RETARGET_RESUME_INTENT_INVALID", "Both state intents are required")
        with self._transaction() as connection:
            actor = connection.execute(
                "SELECT * FROM operations WHERE request_id=?", (actor_id,),
            ).fetchone()
            target = connection.execute(
                "SELECT * FROM operations WHERE request_id=?", (target_id,),
            ).fetchone()
            if (actor is None or actor["status"] != "RUNNING"
                    or actor["action"] != "RESUME_RETARGET"
                    or actor["source_id"] != "local-owner"
                    or target is None or target["action"] != "RETARGET_WORKSPACE"
                    or target["status"] not in TERMINAL
                    or not target["reconcile_required"]
                    or actor["project_id"] != target["project_id"]
                    or actor["workspace_id"] != target["workspace_id"]
                    or connection.execute(
                        "SELECT 1 FROM locks WHERE request_id=? AND scope=?",
                        (target_id, f"repo:{target['project_id']}"),
                    ).fetchone() is None):
                raise LedgerError("RETARGET_RESUME_SCOPE_INVALID", "Retarget continuation scope differs")
            try:
                actor_command = json.loads(actor["command_json"])
                command = json.loads(target["command_json"])
                valid_commands = (
                    isinstance(actor_command, dict) and isinstance(command, dict)
                    and actor_command.get("params") == {
                        "resume_version": "1.0", "target_request_id": target_id,
                    }
                    and actor_command.get("project_id") == target["project_id"]
                    and actor_command.get("workspace_id") == target["workspace_id"]
                    and actor_command.get("expected_head") is None
                    and command.get("request_id") == target_id
                    and command.get("action") == "RETARGET_WORKSPACE"
                    and command.get("project_id") == target["project_id"]
                    and command.get("workspace_id") == target["workspace_id"]
                    and hashlib.sha256(target["command_json"].encode()).hexdigest()
                    == target["digest"]
                    and hashlib.sha256(actor["command_json"].encode()).hexdigest()
                    == actor["digest"]
                )
            except (TypeError, ValueError):
                valid_commands = False
            if not valid_commands:
                raise LedgerError("RETARGET_RESUME_COMMAND_INVALID", "Frozen commands differ")
            existing = connection.execute(
                "SELECT step_id,kind,intent_json,state,provider_id FROM effects "
                "WHERE request_id=?", (target_id,),
            ).fetchall()
            source = [effect for effect in existing
                      if effect["step_id"] == "retarget-workspace"
                      and effect["kind"] == "local_workspace"]
            if (len(source) != 1 or source[0]["state"] != "CONFIRMED"
                    or source[0]["provider_id"] != command["params"]["target_commit"]
                    or any(effect["step_id"] not in {
                        "retarget-workspace", _TERMINAL_WORKSPACE_RECEIPT_STEP,
                    } or effect["state"] != "CONFIRMED"
                           for effect in existing)):
                raise LedgerError("RETARGET_RESUME_EFFECT_UNSETTLED",
                                  "Original effect is uncertain or Drive intent already exists")
            try:
                source_intent = json.loads(source[0]["intent_json"])
            except (TypeError, ValueError):
                source_intent = None
            if (not isinstance(source_intent, dict)
                    or source_intent.get("project_id") != target["project_id"]
                    or source_intent.get("workspace_id") != target["workspace_id"]
                    or source_intent.get("from") != command.get("expected_head")
                    or source_intent.get("from_branch") != command.get("params", {}).get("expected_branch")
                    or source_intent.get("to") != command["params"]["target_commit"]
                    or source_intent.get("to_branch") != command["params"]["target_branch"]
                    or type(source_intent.get("from_generation")) is not int
                    or source_intent["from_generation"] < 0):
                raise LedgerError("RETARGET_RESUME_SOURCE_INVALID", "Source proof differs")
            if connection.execute(
                "SELECT 1 FROM journal WHERE request_id=? "
                "AND event_type='RETARGET_RESUME_CLAIMED' LIMIT 1",
                (target_id,),
            ).fetchone() is not None:
                raise LedgerError("RETARGET_RESUME_CLAIMED", "Continuation already claimed")
            for step, intent in intents.items():
                expected_key = (
                    f"project-state:{target['project_id']}"
                    if step == "drive-retarget-project-state"
                    else f"source-context:{target['project_id']}/{target['workspace_id']}"
                )
                if (set(intent) != {
                    "file_id", "key", "project_id", "workspace_id",
                    "request_digest", "base_revision", "base_sha256", "desired_sha256",
                } or intent.get("key") != expected_key
                        or intent.get("project_id") != target["project_id"]
                        or intent.get("workspace_id") != target["workspace_id"]
                        or intent.get("request_digest") != target["digest"]
                        or not isinstance(intent.get("file_id"), str)
                        or not intent["file_id"]
                        or not isinstance(intent.get("base_revision"), str)
                        or not intent["base_revision"]
                        or any(not isinstance(intent.get(name), str)
                               or _SHA256.fullmatch(intent[name]) is None
                               for name in ("base_sha256", "desired_sha256"))):
                    raise LedgerError("RETARGET_RESUME_INTENT_INVALID", "Doc intent differs")
            timestamp = utcnow()
            self._append(
                connection, target_id, None, "RETARGET_RESUME_CLAIMED",
                {"actor_id": actor_id, "actor_digest": actor["digest"]}, timestamp,
            )
            effect_ids: dict[str, str] = {}
            for step in sorted(steps):
                effect_id = str(uuid4())
                self._append(
                    connection, target_id, step, "EFFECT_INTENDED",
                    {"effect_id": effect_id, "kind": "drive_file",
                     "resume_actor_id": actor_id}, timestamp,
                )
                connection.execute(
                    "INSERT INTO effects(effect_id,request_id,step_id,kind,intent_json,state,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,'INTENDED',?,?)",
                    (effect_id, target_id, step, "drive_file",
                     _json(dict(intents[step])), timestamp, timestamp),
                )
                effect_ids[step] = effect_id
            return effect_ids

    def release_reconciled_retarget(self, target_id: str, *, actor_id: str) -> None:
        """Release a retarget lock only after exact Git and Drive effects are confirmed.

        A local-owner RECONCILE or exact claimed RESUME_RETARGET may release it.
        A switched checkout with missing or uncertain Drive association stays locked.
        """
        with self._transaction() as connection:
            actor = connection.execute(
                "SELECT * FROM operations WHERE request_id=?", (actor_id,),
            ).fetchone()
            target = connection.execute(
                "SELECT * FROM operations WHERE request_id=?", (target_id,),
            ).fetchone()
            if (actor is None or actor["status"] != "RUNNING"
                    or actor["action"] not in {"RECONCILE", "RESUME_RETARGET"}
                    or actor["source_id"] != "local-owner"
                    or target is None or target["action"] != "RETARGET_WORKSPACE"
                    or target["status"] not in TERMINAL
                    or not target["reconcile_required"]
                    or actor["project_id"] != target["project_id"]
                    or actor["workspace_id"] != target["workspace_id"]):
                raise LedgerError("RETARGET_RECONCILE_SCOPE_INVALID", "Retarget reconciliation scope differs")
            actor_command = json.loads(actor["command_json"])
            if actor_command.get("params", {}).get("target_request_id") != target_id:
                raise LedgerError("RETARGET_RECONCILE_SCOPE_INVALID", "Retarget target differs")
            claims = connection.execute(
                "SELECT detail_json FROM journal WHERE request_id=? "
                "AND event_type='RETARGET_RESUME_CLAIMED'",
                (target_id,),
            ).fetchall()
            try:
                claim = json.loads(claims[0]["detail_json"]) if len(claims) == 1 else None
            except (TypeError, ValueError):
                claim = None
            if actor["action"] == "RESUME_RETARGET":
                if (claim != {"actor_id": actor_id, "actor_digest": actor["digest"]}
                        or actor_command.get("params") != {
                            "resume_version": "1.0", "target_request_id": target_id,
                        }):
                    raise LedgerError("RETARGET_RESUME_CLAIM_INVALID", "Continuation claim differs")
            elif claim is not None:
                claimed_id = claim.get("actor_id") if isinstance(claim, dict) else None
                claimed = connection.execute(
                    "SELECT status FROM operations WHERE request_id=?", (claimed_id,),
                ).fetchone() if isinstance(claimed_id, str) else None
                if claimed is None or claimed["status"] == "RUNNING":
                    raise LedgerError("RETARGET_RESUME_ACTIVE", "Continuation still owns release")
            effects = connection.execute(
                "SELECT step_id,kind,intent_json,state,provider_id FROM effects WHERE request_id=?",
                (target_id,),
            ).fetchall()
            if any(effect["state"] in {"INTENDED", "UNKNOWN"} for effect in effects):
                raise LedgerError("RETARGET_EFFECT_UNSETTLED", "Retarget effect is still uncertain")
            source = [effect for effect in effects
                      if effect["step_id"] == "retarget-workspace"
                      and effect["kind"] == "local_workspace"]
            if len(source) != 1 or source[0]["state"] != "CONFIRMED":
                raise LedgerError("RETARGET_SOURCE_UNCONFIRMED", "Retarget source is not confirmed")
            source_intent = json.loads(source[0]["intent_json"])
            command = json.loads(target["command_json"])
            if (not isinstance(source_intent, dict)
                    or source_intent.get("project_id") != target["project_id"]
                    or source_intent.get("workspace_id") != target["workspace_id"]
                    or source_intent.get("from") != command.get("expected_head")
                    or source_intent.get("from_branch") != command.get("params", {}).get("expected_branch")
                    or source_intent.get("to") != command.get("params", {}).get("target_commit")
                    or source_intent.get("to_branch") != command.get("params", {}).get("target_branch")
                    or type(source_intent.get("from_generation")) is not int
                    or source_intent["from_generation"] < 0):
                raise LedgerError("RETARGET_SOURCE_UNCONFIRMED", "Retarget source intent differs")
            state_steps = {"drive-retarget-project-state", "drive-retarget-source-context"}
            state_effects = [effect for effect in effects if effect["step_id"] in state_steps]
            if source[0]["provider_id"] == "UNCHANGED":
                if state_effects:
                    raise LedgerError("RETARGET_STATE_INCOMPLETE", "Unchanged source has Drive effects")
            elif source[0]["provider_id"] == source_intent["to"]:
                if (len(state_effects) != 2
                        or {effect["step_id"] for effect in state_effects} != state_steps
                        or any(effect["kind"] != "drive_file"
                               or effect["state"] != "CONFIRMED"
                               or not effect["provider_id"]
                               for effect in state_effects)):
                    raise LedgerError("RETARGET_STATE_INCOMPLETE", "Drive association is incomplete")
                for effect in state_effects:
                    intent = json.loads(effect["intent_json"])
                    expected_key = (
                        f"project-state:{target['project_id']}"
                        if effect["step_id"] == "drive-retarget-project-state"
                        else f"source-context:{target['project_id']}/{target['workspace_id']}"
                    )
                    if (not isinstance(intent, dict)
                            or intent.get("file_id") != effect["provider_id"]
                            or intent.get("key") != expected_key
                            or intent.get("project_id") != target["project_id"]
                            or intent.get("workspace_id") != target["workspace_id"]
                            or intent.get("request_digest") != target["digest"]
                            or not isinstance(intent.get("desired_sha256"), str)
                            or _SHA256.fullmatch(intent["desired_sha256"]) is None):
                        raise LedgerError("RETARGET_STATE_INCOMPLETE", "Drive association intent differs")
            else:
                raise LedgerError("RETARGET_SOURCE_UNCONFIRMED", "Retarget source proof differs")
            connection.execute("DELETE FROM locks WHERE request_id=?", (target_id,))
            connection.execute(
                "UPDATE operations SET capacity_held=0,reconcile_required=0,updated_at=? "
                "WHERE request_id=?", (utcnow(), target_id),
            )
            self._append(
                connection, target_id, "retarget-workspace",
                ("RETARGET_RESUMED_RELEASED" if actor["action"] == "RESUME_RETARGET"
                 else "RETARGET_RECONCILED_RELEASED"),
                {"actor_id": actor_id}, utcnow(),
            )

    def begin_effect(self, request_id: str, step_id: str, kind: str, intent: dict[str, Any]) -> str:
        if kind not in EFFECT_KINDS or not step_id:
            raise ValueError("Unknown effect kind or missing step identity")
        intent_json = _json(intent)
        with self._transaction() as connection:
            row = connection.execute("SELECT status FROM operations WHERE request_id=?", (request_id,)).fetchone()
            if row is None or row["status"] != "RUNNING":
                raise LedgerError("INVALID_TRANSITION", "Only a running operation can declare a new effect")
            previous = connection.execute("SELECT * FROM effects WHERE request_id=? AND step_id=? AND kind=?",
                                          (request_id, step_id, kind)).fetchone()
            if previous:
                if previous["intent_json"] != intent_json:
                    raise LedgerError("EFFECT_INTENT_CHANGED", "An effect intent is immutable")
                return str(previous["effect_id"])
            timestamp, effect_id = utcnow(), str(uuid4())
            self._append(connection, request_id, step_id, "EFFECT_INTENDED", {"effect_id": effect_id, "kind": kind}, timestamp)
            connection.execute(
                "INSERT INTO effects(effect_id,request_id,step_id,kind,intent_json,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'INTENDED',?,?)",
                (effect_id, request_id, step_id, kind, intent_json, timestamp, timestamp),
            )
            return effect_id

    def confirm_effect(self, effect_id: str, provider_id: str) -> None:
        if not isinstance(provider_id, str) or not provider_id:
            raise ValueError("A provider-returned resource identity is required")
        with self._transaction() as connection:
            effect = connection.execute("SELECT * FROM effects WHERE effect_id=?", (effect_id,)).fetchone()
            if effect is None:
                raise LedgerError("UNKNOWN_EFFECT", "No durable effect intent exists")
            if effect["state"] == "CONFIRMED":
                if effect["provider_id"] != provider_id:
                    raise LedgerError("EFFECT_ID_CHANGED", "Confirmed provider identity is immutable")
                return
            timestamp = utcnow()
            connection.execute("UPDATE effects SET state='CONFIRMED',provider_id=?,updated_at=? WHERE effect_id=?",
                               (provider_id, timestamp, effect_id))
            self._append(connection, effect["request_id"], effect["step_id"], "EFFECT_CONFIRMED",
                         {"effect_id": effect_id, "provider_id": provider_id}, timestamp)

    @staticmethod
    def _operation(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for name in ("command", "receipt", "result"):
            value = result.pop(name + "_json")
            result[name] = json.loads(value) if value is not None else None
        result["effects"] = []
        for effect in connection.execute(
            "SELECT e.*,a.source_effect_id,a.proof_sha256,a.verified_at "
            "FROM effects e LEFT JOIN effect_adoptions a ON a.effect_id=e.effect_id "
            "WHERE e.request_id=? ORDER BY e.created_at,e.effect_id", (row["request_id"],)
        ):
            item = dict(effect)
            source_effect_id = item.pop("source_effect_id")
            proof_sha256 = item.pop("proof_sha256")
            verified_at = item.pop("verified_at")
            item["intent"] = json.loads(item.pop("intent_json"))
            if source_effect_id is not None:
                item["adoption"] = {
                    "source_effect_id": source_effect_id,
                    "proof_sha256": proof_sha256,
                    "verified_at": verified_at,
                }
            result["effects"].append(item)
        result["steps"] = [dict(step) for step in connection.execute(
            "SELECT * FROM steps WHERE request_id=? ORDER BY created_at,step_id", (row["request_id"],)
        )]
        return result

    def get_preparation_result(self, request_id: str) -> dict[str, str] | None:
        """Return only review-safe identity and evidence for a durable phase result."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT run_id,outcome,evidence_sha256 FROM preparation_results "
                "WHERE request_id=?", (request_id,),
            ).fetchone()
            return ({"run_id": row["run_id"], "outcome": row["outcome"],
                     "evidence_sha256": row["evidence_sha256"]} if row else None)

    def get(self, request_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM operations WHERE request_id=?", (request_id,)).fetchone()
            return self._operation(connection, row) if row else None

    def list_operations(self, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise ValueError("Operation page size must be between 1 and 200")
        try:
            after = int(cursor) if cursor is not None else 0
        except (TypeError, ValueError) as error:
            raise ValueError("Invalid operation cursor") from error
        if after < 0:
            raise ValueError("Invalid operation cursor")
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM operations WHERE sequence>? ORDER BY sequence LIMIT ?", (after, limit + 1)).fetchall()
            return {"items": [self._operation(connection, row) for row in rows[:limit]],
                    "next_cursor": str(rows[limit - 1]["sequence"]) if len(rows) > limit else None}

    def project_fetch_snapshot(self, project_id: str) -> dict[str, Any]:
        """Durable per-project FETCH freshness, never inferred from ls-remote."""
        with self._connection() as connection:
            latest = connection.execute(
                "SELECT request_id,status,code,updated_at FROM operations "
                "WHERE project_id=? AND action='FETCH' ORDER BY sequence DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            confirmed = connection.execute(
                "SELECT o.request_id,o.result_json,e.intent_json,e.provider_id,"
                "e.updated_at AS confirmed_at FROM operations o "
                "JOIN effects e ON e.request_id=o.request_id "
                "WHERE o.project_id=? AND o.action='FETCH' AND o.status='COMPLETED' "
                "AND o.reconcile_required=0 AND e.step_id='git-fetch' "
                "AND e.kind='local_workspace' AND e.state='CONFIRMED' "
                "ORDER BY o.sequence DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        success_at: str | None = None
        observation: dict[str, Any] | None = None
        if confirmed is not None:
            result = json.loads(confirmed["result_json"]) if confirmed["result_json"] else None
            intent = json.loads(confirmed["intent_json"])
            fetch = result.get("fetch") if isinstance(result, dict) else None
            if (isinstance(fetch, dict) and isinstance(intent, dict)
                    and fetch.get("kind") == "TRACKING_REFS_FETCH"
                    and fetch.get("project_id") == project_id
                    and fetch.get("repository_id") == intent.get("repository_id")
                    and fetch.get("clone_url") == intent.get("clone_url")
                    and fetch.get("refspec") == intent.get("refspec")
                    and fetch.get("remote_tracking_refs_sha256") == confirmed["provider_id"]):
                success_at = confirmed["confirmed_at"]
                observation = {**fetch, "fetched_at": success_at,
                               "request_id": confirmed["request_id"]}
        if latest is None:
            return {"status": "UNKNOWN", "last_checked_at": None,
                    "last_success_at": None, "code": None, "observation": None}
        if (confirmed is not None and success_at is not None
                and latest["request_id"] == confirmed["request_id"]):
            return {"status": "OK", "last_checked_at": success_at,
                    "last_success_at": success_at, "code": None,
                    "observation": observation}
        if latest["status"] == "RUNNING":
            return {"status": "UNKNOWN", "last_checked_at": None,
                    "last_success_at": success_at, "code": None,
                    "observation": observation}
        code = latest["code"]
        if not isinstance(code, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) is None:
            code = "FETCH_NOT_COMPLETED"
        return {"status": "ERROR", "last_checked_at": latest["updated_at"],
                "last_success_at": success_at, "code": code,
                "observation": observation}

    def completed_provision_project_ids(self) -> frozenset[str]:
        """Projects with a terminal successful CREATE/RESUME proof, not partial state."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT project_id FROM operations "
                "WHERE action IN ('CREATE_PROJECT','RESUME_PROVISION') "
                "AND status='COMPLETED' AND reconcile_required=0 AND project_id IS NOT NULL"
            ).fetchall()
        return frozenset(str(row[0]) for row in rows)

    def completed_workspace_pairs(self) -> frozenset[tuple[str, str]]:
        """Only areas whose CREATE_WORKSPACE route reached terminal success."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT project_id,workspace_id FROM operations "
                "WHERE action='CREATE_WORKSPACE' AND status='COMPLETED' "
                "AND reconcile_required=0 AND project_id IS NOT NULL "
                "AND workspace_id IS NOT NULL"
            ).fetchall()
        return frozenset((str(row[0]), str(row[1])) for row in rows)

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [self._metadata(row) for row in connection.execute("SELECT * FROM projects ORDER BY project_id")]

    @staticmethod
    def _metadata(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        return item

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
            return self._metadata(row) if row else None

    def list_workspaces(self, project_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [self._metadata(row) for row in connection.execute(
                "SELECT * FROM workspaces WHERE project_id=? ORDER BY workspace_id", (project_id,)
            )]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT manifest_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def list_outputs(self, run_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT manifest_json FROM outputs WHERE run_id=? ORDER BY output_id", (run_id,)
            )]
