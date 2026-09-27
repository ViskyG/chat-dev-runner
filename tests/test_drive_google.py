"""Google API wrapper tests use injected fake services; no OAuth or network."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from runner_app.adapters.drive_docs import FILE_FIELDS, DriveError
from runner_app.adapters.drive_google import GoogleReadProvider

FILE_ID = "1x2KIBZCZfHs2w-s_Ah3yMW80elgG4KseTe7OZq7L_hA"


class ApiError(Exception):
    def __init__(self, status: int, reason: str | None = None):
        super().__init__("sensitive-token-should-never-appear")
        self.resp = SimpleNamespace(status=status)
        self.content = json.dumps({
            "error": {"errors": [{"reason": reason}] if reason else []}
        }).encode()


class Request:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = 0

    def execute(self):
        self.calls += 1
        value = self.outcomes.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class Resource:
    def __init__(self, request):
        self.request = request
        self.kwargs = []
        self.batch_kwargs = []

    def batchUpdate(self, **kwargs):
        self.batch_kwargs.append(kwargs)
        return self.request

    def get(self, **kwargs):
        self.kwargs.append(kwargs)
        return self.request


class Service:
    def __init__(self, resource, name):
        self.resource = resource
        self.name = name
        self.calls = 0

    def files(self):
        assert self.name == "files"
        self.calls += 1
        return self.resource

    def documents(self):
        assert self.name == "documents"
        self.calls += 1
        return self.resource


def provider(file_outcomes, doc_outcomes=None, *, attempts=3):
    files = Resource(Request(file_outcomes))
    docs = Resource(Request(doc_outcomes or [{"documentId": FILE_ID, "revisionId": "r"}]))
    sleeps = []
    adapter = GoogleReadProvider(
        Service(files, "files"), Service(docs, "documents"),
        attempts=attempts, sleep=sleeps.append, jitter=lambda: 0,
    )
    return adapter, files, docs, sleeps


def test_construction_is_inert_and_reads_exact_ids_with_required_fields() -> None:
    adapter, files, docs, _ = provider([{"id": FILE_ID}])
    assert files.kwargs == [] and docs.kwargs == [] and docs.batch_kwargs == []
    assert adapter.get_file(FILE_ID) == {"id": FILE_ID}
    assert files.kwargs == [{"fileId": FILE_ID, "fields": FILE_FIELDS}]
    assert adapter.get_document(FILE_ID)["revisionId"] == "r"
    assert docs.kwargs == [{"documentId": FILE_ID, "includeTabsContent": True}]
    with pytest.raises(ValueError):
        adapter.get_file("bad")
    with pytest.raises(ValueError):
        adapter.get_document(FILE_ID, include_tabs_content=False)


def test_403_rate_limit_retries_but_grant_failure_does_not() -> None:
    adapter, files, _, sleeps = provider([
        ApiError(403, "userRateLimitExceeded"), {"id": FILE_ID},
    ])
    assert adapter.get_file(FILE_ID)["id"] == FILE_ID
    assert files.request.calls == 2 and len(sleeps) == 1

    adapter, files, _, sleeps = provider([ApiError(403, "appNotAuthorizedToFile")])
    with pytest.raises(DriveError) as error:
        adapter.get_file(FILE_ID)
    assert error.value.code == "PER_FILE_GRANT_MISSING"
    assert "sensitive-token" not in str(error.value)
    assert files.request.calls == 1 and sleeps == []


def test_scope_and_transient_failures_have_sanitized_bounded_diagnostics() -> None:
    adapter, _, _, _ = provider([ApiError(403, "insufficientPermissions")])
    with pytest.raises(DriveError) as error:
        adapter.get_file(FILE_ID)
    assert error.value.code == "OAUTH_SCOPE_INSUFFICIENT"

    adapter, files, _, sleeps = provider([ApiError(429), ApiError(503)], attempts=2)
    with pytest.raises(DriveError) as error:
        adapter.get_file(FILE_ID)
    assert error.value.code == "PROVIDER_READ_UNAVAILABLE"
    assert files.request.calls == 2 and len(sleeps) == 1

    adapter, _, _, _ = provider([ApiError(404)])
    with pytest.raises(DriveError) as error:
        adapter.get_file(FILE_ID)
    assert error.value.code == "FILE_UNAVAILABLE"


WRITE_REQUESTS = [
    {"deleteContentRange": {"range": {
        "startIndex": 1, "endIndex": 4, "tabId": "tab-one",
    }}},
    {"insertText": {
        "location": {"index": 1, "tabId": "tab-one"}, "text": "new",
    }},
]


def test_single_cas_write_uses_required_revision_and_exact_doc_id() -> None:
    reply = {"documentId": FILE_ID, "writeControl": {"requiredRevisionId": "new-rev"}}
    adapter, files, docs, sleeps = provider([{"id": FILE_ID}], [reply], attempts=5)
    assert docs.batch_kwargs == []
    assert adapter.batch_update_document(
        FILE_ID, requests=WRITE_REQUESTS, required_revision_id="old-rev",
    ) == reply
    assert docs.batch_kwargs == [{
        "documentId": FILE_ID,
        "body": {
            "requests": WRITE_REQUESTS,
            "writeControl": {"requiredRevisionId": "old-rev"},
        },
    }]
    assert docs.request.calls == 1 and files.request.calls == 0 and sleeps == []


def test_cas_write_validates_id_revision_shape_and_tab_before_service_call() -> None:
    adapter, _, docs, _ = provider([{"id": FILE_ID}])
    for file_id, requests, revision in [
        ("bad", WRITE_REQUESTS, "old-rev"),
        (FILE_ID, WRITE_REQUESTS, ""),
        (FILE_ID, [], "old-rev"),
        (FILE_ID, [WRITE_REQUESTS[1], WRITE_REQUESTS[1]], "old-rev"),
        (FILE_ID, [{"replaceAllText": {"text": "unsafe"}}], "old-rev"),
        (FILE_ID, [WRITE_REQUESTS[0], {"insertText": {
            "location": {"index": 1, "tabId": "another"}, "text": "wrong-tab",
        }}], "old-rev"),
    ]:
        with pytest.raises(ValueError):
            adapter.batch_update_document(
                file_id, requests=requests, required_revision_id=revision,
            )
    assert docs.batch_kwargs == []


def test_stale_revision_or_invalid_batch_is_sanitized_and_never_retried() -> None:
    adapter, _, docs, sleeps = provider([{"id": FILE_ID}], [ApiError(400)], attempts=5)
    with pytest.raises(DriveError) as error:
        adapter.batch_update_document(
            FILE_ID, requests=WRITE_REQUESTS, required_revision_id="stale-rev",
        )
    assert error.value.code == "STALE_REVISION_OR_INVALID_BATCH"
    assert "sensitive-token" not in str(error.value)
    assert docs.request.calls == 1 and sleeps == []


@pytest.mark.parametrize("failure", [ApiError(429), ApiError(503), OSError("lost ack")])
def test_ambiguous_write_error_is_not_retried(failure: Exception) -> None:
    adapter, _, docs, sleeps = provider([{"id": FILE_ID}], [failure], attempts=5)
    with pytest.raises(DriveError) as error:
        adapter.batch_update_document(
            FILE_ID, requests=WRITE_REQUESTS, required_revision_id="old-rev",
        )
    assert error.value.code == "PROVIDER_WRITE_UNCERTAIN"
    assert "sensitive-token" not in str(error.value)
    assert docs.request.calls == 1 and sleeps == []


@pytest.mark.parametrize("status,reason,code", [
    (403, "appNotAuthorizedToFile", "PER_FILE_GRANT_MISSING"),
    (403, "insufficientPermissions", "OAUTH_SCOPE_INSUFFICIENT"),
    (404, None, "FILE_UNAVAILABLE"),
])
def test_write_auth_and_missing_file_are_sanitized(
    status: int, reason: str | None, code: str,
) -> None:
    adapter, _, docs, _ = provider([{"id": FILE_ID}], [ApiError(status, reason)])
    with pytest.raises(DriveError) as error:
        adapter.batch_update_document(
            FILE_ID, requests=WRITE_REQUESTS, required_revision_id="old-rev",
        )
    assert error.value.code == code
    assert docs.request.calls == 1


@pytest.mark.parametrize("reply", [
    {},
    {"documentId": "different-document", "writeControl": {"requiredRevisionId": "r2"}},
    {"documentId": FILE_ID, "writeControl": {}},
])
def test_write_response_must_confirm_exact_document_and_new_revision(reply: dict) -> None:
    adapter, _, docs, _ = provider([{"id": FILE_ID}], [reply])
    with pytest.raises(DriveError) as error:
        adapter.batch_update_document(
            FILE_ID, requests=WRITE_REQUESTS, required_revision_id="old-rev",
        )
    assert error.value.code == "PROVIDER_WRITE_RESPONSE_INVALID"
    assert docs.request.calls == 1
