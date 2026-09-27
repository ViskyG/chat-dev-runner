"""Trusted, explicit recovery of uncertain rootless execution phases.

This is neither an Engine action nor an automatic retry. It never executes
project code. Every intended phase needs exact removal evidence on the
original rootless daemon before the operation lock can be released.
"""
from __future__ import annotations

from uuid import UUID

from runner_app.storage.ledger import Ledger, LedgerError, _container_state

from .broker import ContainerIntent, ContainerReconciliation, RootlessDockerBroker


class ReconciliationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def reconcile_run_attempt(ledger: Ledger, broker: RootlessDockerBroker,
                          request_id: str) -> ContainerReconciliation:
    """Reconcile every unproved phase intent, then release its reservation."""
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
        attempts = {
            row[0]: row[1] for row in connection.execute(
                "SELECT phase,run_id FROM run_attempts WHERE request_id=?", (request_id,),
            )
        }
        intents, proofs = _container_state(connection, request_id)
    except (TypeError, ValueError):
        raise ReconciliationError("CONTAINER_INTENT_INVALID",
                                  "Durable container history is malformed") from None
    finally:
        connection.close()
    if not intents:
        raise ReconciliationError("CONTAINER_INTENT_MISSING",
                                  "No durable pre-create intent is available")
    if any(attempts.get(phase) != detail.get("run_id") for phase, detail in intents.items()):
        raise ReconciliationError("CONTAINER_INTENT_INVALID",
                                  "Durable intent differs from a reserved phase attempt")
    params = operation["command"].get("params")
    if (not isinstance(params, dict) or
            ("TEST" in intents and params.get("profile_id") != intents["TEST"].get("profile_id"))):
        raise ReconciliationError("CONTAINER_INTENT_INVALID",
                                  "Test intent differs from the admitted profile")

    last: ContainerReconciliation | None = None
    for phase in ("PREPARATION", "TEST"):
        detail = intents.get(phase)
        if detail is None:
            continue
        prior = proofs.get(phase)
        if prior is not None:
            last = ContainerReconciliation(prior["run_id"], prior["daemon_id"],
                                           "REMOVED", prior["container_id"])
            continue
        try:
            fields = {
                key: detail[key] for key in (
                    "run_id", "daemon_id", "profile_id", "profile_revision_sha256",
                    "image", "image_id", "argv_sha256", "nonce",
                )
            }
            if "source_path" in detail:
                fields["source_path"] = detail["source_path"]
            intent = ContainerIntent(**fields)
        except (KeyError, TypeError, ValueError):
            raise ReconciliationError("CONTAINER_INTENT_INVALID",
                                      "Durable container intent is malformed") from None
        proof = broker.reconcile_container(intent)
        if (proof.run_id != intent.run_id or proof.daemon_id != intent.daemon_id
                or proof.state != "REMOVED" or proof.container_id is None):
            raise ReconciliationError("CONTAINER_PROOF_INVALID",
                                      "Recovery proof differs from the durable intent")
        try:
            ledger.record_container_reconciled(request_id, phase, proof)
        except LedgerError:
            raise ReconciliationError("CONTAINER_PROOF_INVALID",
                                      "Recovery proof could not be recorded") from None
        last = proof
    if last is None:
        raise ReconciliationError("CONTAINER_INTENT_MISSING",
                                  "No durable pre-create intent is available")
    ledger.confirm_stopped(request_id)
    return last
