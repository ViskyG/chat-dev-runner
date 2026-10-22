"""Separate exact-tab terminal handoff receipts; command/ACK bodies are untouched.

The enrolled tab contains one bounded JSON document. Each request ID advances
only through verified recovery; delivered payloads are immutable. Concurrent
records survive revision-guarded replacement. No uncertain write is replayed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from runner_app.adapters.drive_docs import FILE_FIELDS, DriveError, extract_document
from runner_app.adapters.drive_mailbox_v2 import DocsMailboxError, DocsMailboxPort, DriveDocsAPI
from runner_app.adapters.drive_writer import _plain_body_span, _validate_new_text
from runner_app.domain.contracts_v2 import parse_contract_json
from runner_app.handoff.contracts import HandoffError

_PROTOCOL = "cdr-handoff-results/1"
_DEFINITE_REJECTIONS = frozenset({
    "STALE_REVISION", "STALE_REVISION_OR_INVALID_BATCH", "DOCS_WRITE_REJECTED",
    "PER_FILE_GRANT_MISSING", "OAUTH_SCOPE_INSUFFICIENT", "FILE_UNAVAILABLE",
    "PER_FILE_ACCESS_MISSING",
})
_SECRET_KEYS = frozenset({"access_token", "refresh_token", "client_secret", "authorization",
                          "credentials", "token_file", "oauth_client_file"})


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise HandoffError("INVALID_DELIVERY_RECEIPT") from None


def _public_json(value: Any, *, depth: int = 0) -> None:
    if depth > 24:
        raise HandoffError("INVALID_DELIVERY_RECEIPT")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or key.casefold() in _SECRET_KEYS:
                raise HandoffError("PRIVATE_DELIVERY_RECEIPT")
            _public_json(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _public_json(item, depth=depth + 1)
    elif isinstance(value, str):
        # Remote results use relative inbox paths; absolute owner paths belong
        # only in the authenticated local UI and the locally delivered prompt.
        if (value.startswith(("/", "\\", "file:")) or re.match(r"^[A-Za-z]:[\\/]", value)
                or re.search(r"(?:^|\s)(?:/home/|/Users/|/tmp/|[A-Za-z]:\\)", value)):
            raise HandoffError("PRIVATE_DELIVERY_RECEIPT")
    elif value is not None and type(value) not in (bool, int, float):
        raise HandoffError("INVALID_DELIVERY_RECEIPT")


@dataclass(frozen=True)
class _ResultTab:
    revision: str
    payload: bytes
    results: Mapping[str, Any]
    start: int
    end: int


class TerminalHandoffPublisher:
    """A locally enrolled result Doc/tab; the caller protects command/v1 IDs.

    ``publish`` returns CONFIRMED, UNCONFIRMED or FAILED separately from the
    filesystem delivery result. UNKNOWN is never interpreted as failed local
    delivery. A retry always starts with an exact read, and issues at most one
    CAS write from that revision. A verified recovery may upgrade an unsuccessful
    terminal state to DELIVERED for the identical request and manifest; delivered
    entries remain immutable.
    """

    def __init__(self, provider: DriveDocsAPI, *, document_id: str, tab_id: str,
                 max_bytes: int = 262_144) -> None:
        # Reuse the mailbox's exact-ID/tab validation and complete nested-tab
        # selector, without invoking its JSON command or ACK interpretation.
        self._selector = DocsMailboxPort(provider, document_id=document_id, tab_id=tab_id,
                                         max_bytes=max_bytes)
        self.provider = provider
        self.document_id, self.tab_id, self.max_bytes = document_id, tab_id, max_bytes

    def _read(self) -> _ResultTab:
        metadata = self.provider.get_file(self.document_id, fields=FILE_FIELDS)
        capabilities = metadata.get("capabilities") if isinstance(metadata, Mapping) else None
        if (not isinstance(metadata, Mapping) or metadata.get("id") != self.document_id
                or metadata.get("trashed") is not False
                or metadata.get("mimeType") != "application/vnd.google-apps.document"
                or metadata.get("isAppAuthorized") is not True
                or not isinstance(capabilities, Mapping) or capabilities.get("canEdit") is not True):
            raise HandoffError("DOCS_RESULT_IDENTITY_CHANGED")
        document = self.provider.get_document(self.document_id, include_tabs_content=True)
        if not isinstance(document, Mapping) or document.get("documentId") != self.document_id:
            raise HandoffError("DOCS_RESULT_IDENTITY_CHANGED")
        revision = document.get("revisionId")
        if not isinstance(revision, str) or not 1 <= len(revision) <= 1024:
            raise HandoffError("DOCS_REVISION_MISSING")
        selected = self._selector._tab(document)
        projected = {"documentId": self.document_id, "revisionId": revision,
                     "tabs": [{**selected, "childTabs": []}]}
        snapshot = extract_document(projected, self.document_id, max_bytes=self.max_bytes + 1)
        start, end = _plain_body_span(projected, snapshot)
        payload = snapshot.raw_bytes[:-1]
        if len(payload) > self.max_bytes:
            raise HandoffError("DOCS_RESULT_TOO_LARGE")
        value = parse_contract_json(payload) if payload else {}
        if value == {}:
            results: Mapping[str, Any] = {}
        elif (set(value) == {"protocol", "results"} and value["protocol"] == _PROTOCOL
              and isinstance(value["results"], dict)):
            results = value["results"]
            for key, item in results.items():
                if (not isinstance(key, str) or not isinstance(item, dict)
                        or item.get("request_id") != key):
                    raise HandoffError("DOCS_RESULT_FORMAT_INVALID")
        else:
            # This includes a cdr-mailbox/2 command or ACK in a mistakenly
            # enrolled result tab; it can never be replaced by this publisher.
            raise HandoffError("DOCS_RESULT_FORMAT_INVALID")
        return _ResultTab(revision, payload, results, start, end)

    def _result(self, status: str, code: str, request_id: str, *, digest: str | None = None
                ) -> Mapping[str, Any]:
        result: dict[str, Any] = {"status": status, "code": code, "request_id": request_id,
                                  "document_id": self.document_id, "tab_id": self.tab_id}
        if digest is not None:
            result["receipt_sha256"] = digest
        return result

    @staticmethod
    def _recovered(previous: Mapping[str, Any], desired: Mapping[str, Any]) -> bool:
        """Only the locally verified, same-request recovery can advance state."""
        if (previous.get("status") not in {"INTERRUPTED", "FAILED", "SUSPENDED_RESTART"}
                or desired.get("status") != "DELIVERED"):
            return False
        for key in ("request_id", "request_digest", "project_id", "runner_id"):
            value = previous.get(key)
            if not isinstance(value, str) or not value or desired.get(key) != value:
                return False
        old, new = previous.get("handoff"), desired.get("handoff")
        if (not isinstance(old, Mapping) or not isinstance(new, Mapping)
                or old.get("local_delivery_status") == "DELIVERED"
                or new.get("local_delivery_status") != "DELIVERED"):
            return False
        manifest = old.get("manifest_sha256")
        if (not isinstance(manifest, str) or re.fullmatch(r"[0-9a-f]{64}", manifest) is None
                or new.get("manifest_sha256") != manifest):
            return False
        return all(isinstance(old.get(key), str) and old.get(key)
                   and new.get(key) == old.get(key)
                   for key in ("handoff_id", "project_id", "target_id"))

    def publish(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        request_id = payload.get("request_id") if isinstance(payload, Mapping) else None
        try:
            if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 256
                    or any(ord(char) < 32 for char in request_id)):
                raise HandoffError("INVALID_DELIVERY_RECEIPT")
            _public_json(payload)
            encoded = _canonical(dict(payload))
            if len(encoded) > self.max_bytes:
                raise HandoffError("DOCS_RESULT_TOO_LARGE")
            normalized = parse_contract_json(encoded)
            current = self._read()
            existing = current.results.get(request_id)
            if existing is not None:
                if _canonical(existing) == encoded:
                    return self._result("CONFIRMED", "DELIVERY_RESULT_CONFIRMED", request_id,
                                        digest=hashlib.sha256(encoded).hexdigest())
                if not isinstance(existing, Mapping) or not self._recovered(existing, normalized):
                    return self._result("FAILED", "DELIVERY_RESULT_CONFLICT", request_id)
            results = dict(current.results)
            results[request_id] = normalized
            desired = _canonical({"protocol": _PROTOCOL, "results": results})
            if len(desired) > self.max_bytes:
                raise HandoffError("DOCS_RESULT_TOO_LARGE")
            text = desired.decode("utf-8")
            _validate_new_text(text + "\n", max_bytes=self.max_bytes + 1)
        except (HandoffError, DocsMailboxError, DriveError) as error:
            return self._result("FAILED", error.code, request_id if isinstance(request_id, str) else "")
        except Exception:  # noqa: BLE001 - no provider/account details in remote status
            return self._result("FAILED", "DOCS_RESULT_READ_FAILED",
                                request_id if isinstance(request_id, str) else "")

        requests: list[Mapping[str, Any]] = []
        if current.end - 1 > current.start:
            requests.append({"deleteContentRange": {"range": {
                "startIndex": current.start, "endIndex": current.end - 1, "tabId": self.tab_id,
            }}})
        requests.append({"insertText": {
            "location": {"index": current.start, "tabId": self.tab_id}, "text": text,
        }})
        rejected = False
        try:
            self.provider.batch_update_document(
                self.document_id, requests=requests, required_revision_id=current.revision,
            )
        except DriveError as error:
            rejected = error.code in _DEFINITE_REJECTIONS
        except Exception:  # noqa: BLE001, S110 - a lost response may follow an applied write
            pass
        try:
            after = self._read()
            actual = after.results.get(request_id)
            # Other concurrent records may be present; prove this exact entry.
            if actual is not None and _canonical(actual) == encoded:
                return self._result("CONFIRMED", "DELIVERY_RESULT_CONFIRMED", request_id,
                                    digest=hashlib.sha256(encoded).hexdigest())
            if rejected:
                return self._result("FAILED", "DOCS_RESULT_REVISION_CONFLICT", request_id)
        except Exception:  # noqa: BLE001, S110 - preserve unknown write separately from local delivery
            pass
        return self._result("UNCONFIRMED", "DELIVERY_RESULT_UNCONFIRMED", request_id)
