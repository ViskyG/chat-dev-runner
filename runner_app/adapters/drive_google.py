"""Lazy exact-ID Google Drive/Docs transport with one revision-guarded write.

Services must be injected after local OAuth setup. Importing or constructing this
wrapper makes no network request, obtains no credentials and creates no files.
Reads may retry transient failures; a Docs write is never retried here.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from runner_app.adapters.drive_docs import FILE_FIELDS, DriveError

_TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_ID = re.compile(r"[A-Za-z0-9_-]{8,256}\Z")
_RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})


def _status(error: Exception) -> int | None:
    response = getattr(error, "resp", None)
    status = getattr(response, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _reason(error: Exception) -> str | None:
    content = getattr(error, "content", None)
    if not isinstance(content, (str, bytes)) or len(content) > 8192:
        return None
    try:
        body = json.loads(content)
    except (UnicodeError, ValueError):
        return None
    details = body.get("error") if isinstance(body, dict) else None
    entries = details.get("errors") if isinstance(details, dict) else None
    if isinstance(entries, list):
        for item in entries:
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                return str(item["reason"])
    return None


def _retry_after(error: Exception) -> float:
    response = getattr(error, "resp", None)
    if response is None or not hasattr(response, "get"):
        return 0.0
    value = response.get("retry-after")
    if value is None:
        return 0.0
    try:
        return max(0.0, min(2.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _bounded_batch(requests: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Allow only the plain-body delete/insert shape produced by DocsCASWriter."""
    if not isinstance(requests, (list, tuple)) or not 1 <= len(requests) <= 2:
        raise ValueError("One or two plain-text Docs requests are required")
    normalized: list[dict[str, Any]] = []
    tab_id: str | None = None
    for position, request in enumerate(requests):
        if not isinstance(request, Mapping) or len(request) != 1:
            raise ValueError("Unsupported Docs request")
        if "deleteContentRange" in request and position == 0:
            operation = request["deleteContentRange"]
            span = operation.get("range") if isinstance(operation, Mapping) else None
            if (not isinstance(span, Mapping) or set(span) != {"startIndex", "endIndex", "tabId"}
                    or type(span["startIndex"]) is not int or span["startIndex"] != 1
                    or type(span["endIndex"]) is not int
                    or not 1 < span["endIndex"] <= 262_145
                    or not isinstance(span["tabId"], str)
                    or not 0 < len(span["tabId"]) <= 256):
                raise ValueError("Invalid plain-text Docs deletion")
            tab_id = span["tabId"]
            normalized.append({"deleteContentRange": {"range": {
                "startIndex": 1, "endIndex": span["endIndex"], "tabId": tab_id,
            }}})
            continue
        if "insertText" in request and (position == 0 or (
                len(normalized) == 1 and "deleteContentRange" in normalized[0]
        )):
            operation = request["insertText"]
            location = operation.get("location") if isinstance(operation, Mapping) else None
            content = operation.get("text") if isinstance(operation, Mapping) else None
            if (not isinstance(operation, Mapping) or set(operation) != {"location", "text"}
                    or not isinstance(location, Mapping)
                    or set(location) != {"index", "tabId"}
                    or type(location["index"]) is not int or location["index"] != 1
                    or not isinstance(location["tabId"], str)
                    or not 0 < len(location["tabId"]) <= 256
                    or (tab_id is not None and location["tabId"] != tab_id)
                    or not isinstance(content, str) or not content):
                raise ValueError("Invalid plain-text Docs insertion")
            try:
                size = len(content.encode("utf-8", errors="strict"))
            except UnicodeEncodeError as error:
                raise ValueError("Invalid plain-text Docs insertion") from error
            if size > 262_144:
                raise ValueError("Plain-text Docs insertion exceeds local limit")
            tab_id = location["tabId"]
            normalized.append({"insertText": {
                "location": {"index": 1, "tabId": tab_id}, "text": content,
            }})
            continue
        raise ValueError("Unsupported Docs request order")
    return normalized


class GoogleReadProvider:
    def __init__(
        self,
        drive_service: Any,
        docs_service: Any,
        *,
        attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if attempts < 1 or attempts > 5:
            raise ValueError("Drive read attempts must be between 1 and 5")
        self._drive = drive_service
        self._docs = docs_service
        self._attempts = attempts
        self._sleep = sleep
        self._jitter = jitter

    def _read(self, request_factory: Callable[[], Any], *, kind: str) -> Mapping[str, Any]:
        for attempt in range(self._attempts):
            try:
                result = request_factory().execute()
            except Exception as error:  # noqa: BLE001 - sanitize all third-party API errors
                status = _status(error)
                reason = _reason(error)
                transient = (
                    status in _TRANSIENT_STATUSES
                    or (status == 403 and reason in _RATE_LIMIT_REASONS)
                    or (status is None and isinstance(error, (OSError, TimeoutError)))
                )
                if transient and attempt + 1 < self._attempts:
                    delay = max(
                        min(2.0, 0.2 * (2 ** attempt)
                            + max(0.0, min(1.0, self._jitter())) * 0.1),
                        _retry_after(error),
                    )
                    self._sleep(delay)
                    continue
                if transient:
                    raise DriveError(
                        "PROVIDER_READ_UNAVAILABLE", f"Google {kind} read was throttled or unavailable"
                    ) from None
                if status == 403:
                    code = (
                        "PER_FILE_GRANT_MISSING" if reason == "appNotAuthorizedToFile"
                        else "OAUTH_SCOPE_INSUFFICIENT" if reason in (
                            "insufficientPermissions", "insufficientScopes"
                        ) else "PER_FILE_ACCESS_MISSING"
                    )
                    raise DriveError(code, "OAuth app cannot access the known file") from None
                if status == 404:
                    raise DriveError("FILE_UNAVAILABLE", "Known file is unavailable") from None
                raise DriveError(
                    "PROVIDER_READ_FAILED", f"Google {kind} read failed without a usable response"
                ) from None
            if not isinstance(result, dict):
                raise DriveError("PROVIDER_RESPONSE_INVALID", "Google read returned invalid data")
            return result
        raise AssertionError("Bounded retry loop did not return or raise")

    def get_file(self, file_id: str, *, fields: str = FILE_FIELDS) -> Mapping[str, Any]:
        if not isinstance(file_id, str) or not _ID.fullmatch(file_id) or fields != FILE_FIELDS:
            raise ValueError("Exact known file ID and approved metadata field mask are required")
        return self._read(
            lambda: self._drive.files().get(fileId=file_id, fields=fields), kind="Drive file",
        )

    def get_document(
        self, file_id: str, *, include_tabs_content: bool = True,
    ) -> Mapping[str, Any]:
        if (not isinstance(file_id, str) or not _ID.fullmatch(file_id)
                or include_tabs_content is not True):
            raise ValueError("Exact known Doc ID and complete tab content are required")
        return self._read(
            lambda: self._docs.documents().get(
                documentId=file_id, includeTabsContent=True,
            ),
            kind="native Doc",
        )

    def batch_update_document(
        self, file_id: str, *, requests: Sequence[Mapping[str, Any]],
        required_revision_id: str,
    ) -> Mapping[str, Any]:
        """Issue exactly one Docs CAS write; never replay an ambiguous response."""
        if (not isinstance(file_id, str) or not _ID.fullmatch(file_id)
                or not isinstance(required_revision_id, str)
                or not 0 < len(required_revision_id) <= 1024):
            raise ValueError("Exact Doc ID and required revision are mandatory")
        body = {
            "requests": _bounded_batch(requests),
            "writeControl": {"requiredRevisionId": required_revision_id},
        }
        try:
            response = self._docs.documents().batchUpdate(
                documentId=file_id, body=body,
            ).execute()
        except Exception as error:  # noqa: BLE001 - never expose third-party error detail
            status = _status(error)
            reason = _reason(error)
            if status == 400:
                raise DriveError(
                    "STALE_REVISION_OR_INVALID_BATCH",
                    "Google Docs rejected the required revision or update request",
                ) from None
            if status == 403 and reason == "appNotAuthorizedToFile":
                raise DriveError(
                    "PER_FILE_GRANT_MISSING", "OAuth app cannot write the known Doc",
                ) from None
            if status == 403 and reason in ("insufficientPermissions", "insufficientScopes"):
                raise DriveError(
                    "OAUTH_SCOPE_INSUFFICIENT", "OAuth app cannot write the known Doc",
                ) from None
            if status == 404:
                raise DriveError("FILE_UNAVAILABLE", "Known Doc is unavailable") from None
            raise DriveError(
                "PROVIDER_WRITE_UNCERTAIN",
                "Google Docs write may have been applied; reconcile before another attempt",
            ) from None
        if not isinstance(response, dict) or response.get("documentId") != file_id:
            raise DriveError(
                "PROVIDER_WRITE_RESPONSE_INVALID", "Google Docs write response is invalid",
            )
        control = response.get("writeControl")
        if (not isinstance(control, dict)
                or not isinstance(control.get("requiredRevisionId"), str)
                or not control["requiredRevisionId"]):
            raise DriveError(
                "PROVIDER_WRITE_RESPONSE_INVALID", "Google Docs write revision is missing",
            )
        return response
