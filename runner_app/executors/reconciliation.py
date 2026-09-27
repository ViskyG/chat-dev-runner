"""Trusted, explicit recovery of one uncertain rootless execution attempt.

This module is not an Engine action or an automatic startup retry. The owner
must invoke it locally after inspecting the retained operation. It never runs
project code and cannot release a ledger lock on an unverified Docker lookup.
"""
from __future__ import annotations

import json
from uuid import UUID

from runner_app.storage.ledger import Ledger

from .broker import ContainerIntent, ContainerReconciliation, RootlessDockerBroker


class ReconciliationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def reconcile_run_attempt(ledger: Ledger, broker: RootlessDockerBroker,
                          request_id: str) -> ContainerReconciliation:
    """Reconcile an exact durable intent, then release its operation reservation."""
    try:
        if str(UUID(request_id)) != request_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ReconciliationError("INVALID_REQUEST_ID", "Canonical request UUID required") from None
    operation = ledger.get(request_id)
    if (operation is None or operation["status"] not in {"INTERRUPTED", "FAULT", "BLOCKED"}
            or operation["reconcile_required"] != 1
            or operation["command"].get("action") not in {"VERIFY", "DELIVER_VERIFY_PUBLISH"}):
        raise ReconciliationError("RECONCILIATION_NOT_REQUIRED",
                                  "Only an uncertain terminal execution can be reconciled")
    connection = ledger.database.connect()
    try:
        attempt = connection.execute(
            "SELECT run_id FROM run_attempts WHERE request_id=?", (request_id,),
        ).fetchone()
        events = connection.execute(
            "SELECT detail_json FROM journal WHERE request_id=? "
            "AND event_type='CONTAINER_INTENDED' ORDER BY event_id LIMIT 2", (request_id,),
        ).fetchall()
    finally:
        connection.close()
    if attempt is None or len(events) != 1:
        raise ReconciliationError("CONTAINER_INTENT_MISSING",
                                  "Exactly one durable pre-create intent is required")
    try:
        detail = json.loads(events[0][0])
        if not isinstance(detail, dict):
            raise TypeError
        intent = ContainerIntent(**detail)
    except (TypeError, ValueError):
        raise ReconciliationError("CONTAINER_INTENT_INVALID",
                                  "Durable container intent is malformed") from None
    params = operation["command"].get("params")
    if (intent.run_id != attempt[0] or not isinstance(params, dict)
            or params.get("profile_id") != intent.profile_id):
        raise ReconciliationError("CONTAINER_INTENT_INVALID",
                                  "Durable intent differs from the admitted run")
    proof = broker.reconcile_container(intent)
    if (proof.run_id != intent.run_id or proof.daemon_id != intent.daemon_id
            or proof.state != "REMOVED" or proof.container_id is None):
        raise ReconciliationError("CONTAINER_PROOF_INVALID",
                                  "Recovery proof differs from the durable intent")
    ledger.journal(request_id, "container-reconciliation", "CONTAINER_RECONCILED", {
        "run_id": proof.run_id, "daemon_id": proof.daemon_id,
        "state": proof.state, "container_id": proof.container_id,
    })
    ledger.confirm_stopped(request_id)
    return proof
