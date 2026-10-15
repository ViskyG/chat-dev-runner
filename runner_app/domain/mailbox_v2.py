"""Immutable v2 mailbox intent capture; no admission, authority or execution side effects."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID, uuid4, uuid5

from bridge_reference.protocol import Rejected, validate_branch
from runner_app.domain.commands import DomainError
from runner_app.domain.contracts_v2 import (
    canonical_bytes,
    parse_contract_json,
    require_uuid,
    validate_schema,
)

ReceiptStatus = Literal["ACCEPTED", "REJECTED", "NEEDS_RESUBMIT", "ACK_UNKNOWN"]
DispatchStatus = Literal["ACCEPTED", "REJECTED", "NEEDS_RESUBMIT"]


@dataclass(frozen=True)
class FrozenMailbox:
    """Byte snapshots are authoritative; every envelope view is detached and mutable."""

    canonical_envelope: bytes
    canonical_payload: bytes
    request_id: str
    digest: str

    @property
    def envelope(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.canonical_envelope))

    @property
    def ready(self) -> bool:
        return bool(self.envelope["dispatch"] == "READY")


def validate_mailbox_shape(envelope: dict[str, Any]) -> None:
    """Schema plus UUID/ref/script invariants, without trusting any remote authority."""
    validate_schema(envelope, "v2-mailbox")
    require_uuid(envelope["slot_token"])
    request = envelope["request"]
    for value in (request.get("request_id"), request.get("target_request_id")):
        if value is not None:
            require_uuid(value)
    receipt = envelope["receipt"]
    if receipt is not None:
        require_uuid(receipt["request_id"])
        require_uuid(receipt["accepted_slot_token"])
    if "source" in request:
        source = request["source"]
        try:
            validate_branch(source["branch"])
        except Rejected:
            raise DomainError("INVALID_BRANCH", "Source must name a valid Git branch.") from None
        if source["kind"] == "github_branch" and "workspace_id" in source:
            raise DomainError("INVALID_SCHEMA", "A GitHub source cannot select a local workspace.")
    if "shell" in request and "\x00" in request["shell"]["script"]:
        raise DomainError("INVALID_SCHEMA", "Shell script cannot contain NUL characters.")


def parse_mailbox(raw: bytes, *, installation_id: str, mailbox_id: str) -> FrozenMailbox:
    """Capture a valid slot, including DRAFT; execution must additionally require READY.

    The trusted installation UUID is the UUIDv5 namespace. A compact JSON tuple of
    enrolled binding ID and consumed token is the UUID name, avoiding delimiters
    whose presence in a binding ID could produce an ambiguous identity.
    """
    installation = UUID(require_uuid(installation_id))
    if (not isinstance(mailbox_id, str) or not mailbox_id or len(mailbox_id) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in mailbox_id)):
        raise DomainError("INVALID_SOURCE", "Mailbox must have a stable enrolled binding ID.")
    try:
        mailbox_id.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise DomainError("INVALID_SOURCE", "Mailbox binding ID must be valid Unicode.") from None
    envelope = parse_contract_json(raw)
    validate_mailbox_shape(envelope)
    name = json.dumps([mailbox_id, envelope["slot_token"]], ensure_ascii=False,
                      separators=(",", ":"))
    request_id = envelope["request"]["request_id"] or str(uuid5(installation, name))
    payload = canonical_bytes({"runner_id": envelope["runner_id"],
                               "project_id": envelope["project_id"],
                               "request": envelope["request"]})
    return FrozenMailbox(canonical_bytes(envelope), payload, request_id,
                         hashlib.sha256(payload).hexdigest())


def require_ready(mailbox: FrozenMailbox) -> None:
    """The explicit action gate; parsing a DRAFT or a prior ACK never authorizes work."""
    if not mailbox.ready:
        raise DomainError("NOT_READY", "Only a READY mailbox can initiate an action.")


def build_receipt(mailbox: FrozenMailbox, *, status: ReceiptStatus,
                  code: str | None = None, queue_sequence: int | None = None) -> dict[str, Any]:
    if status not in {"ACCEPTED", "REJECTED", "NEEDS_RESUBMIT", "ACK_UNKNOWN"}:
        raise DomainError("INVALID_SCHEMA", "Unknown mailbox receipt status.")
    if queue_sequence is not None and (type(queue_sequence) is not int or queue_sequence < 1):
        raise DomainError("INVALID_SCHEMA", "Queue sequence must be a positive integer.")
    if code is not None and not isinstance(code, str):
        raise DomainError("INVALID_SCHEMA", "Receipt code must be a string or null.")
    return {"request_id": mailbox.request_id,
            "accepted_slot_token": mailbox.envelope["slot_token"],
            "payload_sha256": mailbox.digest, "status": status,
            "queue_sequence": queue_sequence, "code": code}


def acknowledge_mailbox(mailbox: FrozenMailbox, *, status: DispatchStatus = "ACCEPTED",
                        code: str | None = None, slot_token: str | None = None) -> bytes:
    """Construct ACK intent only; caller must persist and conditionally write it.

    Initial acceptance always has a null queue sequence. Durable finalization owns
    ordering; the reusable slot is never enriched here after acceptance.
    """
    if status not in {"ACCEPTED", "REJECTED", "NEEDS_RESUBMIT"}:
        raise DomainError("INVALID_SCHEMA", "Unknown mailbox acknowledgement status.")
    if status == "ACCEPTED":
        require_ready(mailbox)
    envelope = mailbox.envelope
    token = require_uuid(slot_token) if slot_token is not None else str(uuid4())
    if token == envelope["slot_token"]:
        raise DomainError("INVALID_SCHEMA", "Acknowledgement must rotate the consumed slot token.")
    envelope["dispatch"] = status
    envelope["receipt"] = build_receipt(mailbox, status=status, code=code)
    envelope["slot_token"] = token
    validate_mailbox_shape(envelope)
    return canonical_bytes(envelope)
