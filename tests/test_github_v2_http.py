from __future__ import annotations

import io
import json
import urllib.error
from collections import OrderedDict
from email.message import Message
from typing import Any

import pytest

import runner_app.adapters.github_mailbox_v2 as module
from runner_app.adapters.github_mailbox_v2 import BoundedGitHubV2HTTP, GitHubV2Error


class Reply(io.BytesIO):
    def __init__(self, status: int, data: Any, headers: dict[str, str] | None = None):
        super().__init__(json.dumps(data).encode() if data is not None else b"")
        self.status = status
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setattr(BoundedGitHubV2HTTP, "_cooldowns", OrderedDict())
    replies, calls, tokens = [], [], ["fixture-token"]
    transport = BoundedGitHubV2HTTP(lambda: tokens[-1])

    def open_request(request, **_kwargs):
        calls.append(request)
        reply = replies.pop(0)
        if reply.status >= 300:
            raise urllib.error.HTTPError(request.full_url, reply.status, "fixture", reply.headers, reply)
        return reply

    monkeypatch.setattr(transport._opener, "open", open_request)
    return transport, replies, calls, tokens


@pytest.mark.parametrize("path", ["/repos/owner/project", "/repos/owner/project/git/ref/heads/tested"])
def test_authenticated_conditional_read_requires_actual_304_and_keeps_independent_body(http, path):
    transport, replies, calls, _ = http
    replies.extend([Reply(200, {"id": 42}, {"ETag": '"first"'}),
                    Reply(304, None, {"X-RateLimit-Remaining": "4999"}),
                    Reply(200, {"id": 43}, {"ETag": '"changed"'})])
    first = transport.request("GET", path)
    first.data["id"] = 999
    second = transport.request("GET", path)
    assert second.status == 200 and second.data == {"id": 42}
    assert second.headers["x-ratelimit-remaining"] == "4999"
    assert calls[0].get_header("If-none-match") is None
    assert calls[1].get_header("If-none-match") == '"first"'
    assert calls[1].get_header("Authorization") == "Bearer fixture-token"
    assert transport.request("GET", path).data == {"id": 43}
    assert len(calls) == 3


def test_cached_response_never_crosses_token_or_authorization_failure(http):
    transport, replies, calls, tokens = http
    path = "/repos/owner/project"
    replies.extend([Reply(200, {"id": 42}, {"ETag": '"old-auth"'}), Reply(304, None)])
    transport.request("GET", path)
    tokens.append("new-fixture-token")
    with pytest.raises(GitHubV2Error, match="GITHUB_CONDITIONAL_CACHE_MISS"):
        transport.request("GET", path)
    assert calls[-1].get_header("If-none-match") is None
    tokens.append("")
    with pytest.raises(GitHubV2Error, match="GITHUB_AUTH_UNAVAILABLE"):
        transport.request("GET", path)
    assert len(calls) == 2


def test_errors_never_fall_back_to_cached_response(http):
    transport, replies, calls, _ = http
    path = "/repos/owner/project"
    replies.extend([Reply(200, {"id": 42}, {"ETag": '"first"'}), Reply(503, {}),
                    Reply(401, {}), Reply(304, None)])
    transport.request("GET", path)
    assert transport.request("GET", path).status == 503
    assert transport.request("GET", path).status == 401
    with pytest.raises(GitHubV2Error, match="GITHUB_CONDITIONAL_CACHE_MISS"):
        transport.request("GET", path)
    assert calls[-1].get_header("If-none-match") is None


@pytest.mark.parametrize("method,path,params,body", [
    ("GET", "/repos/owner/project/git/trees/abc", None, None),
    ("GET", "/repos/owner/project", {"page": "1"}, None),
    ("PATCH", "/repos/owner/project/git/refs/heads/tested", None, {"force": False}),
])
def test_only_exact_identity_and_ref_gets_are_cached(http, method, path, params, body):
    transport, replies, calls, _ = http
    replies.extend(Reply(200, {"id": 42}, {"ETag": '"first"'}) for _ in range(2))
    for _ in range(2):
        transport.request(method, path, params=params, body=body)
    assert len(calls) == 2
    assert all(call.get_header("If-none-match") is None for call in calls)
    assert not transport._conditional


def test_conditional_cache_has_entry_and_byte_limits(http, monkeypatch):
    transport, replies, calls, _ = http
    monkeypatch.setattr(module, "_CONDITIONAL_CACHE_ENTRIES", 2)
    monkeypatch.setattr(module, "_CONDITIONAL_CACHE_BYTES", 500)
    replies.extend(Reply(200, {"id": "a" * 100}, {"ETag": '"v"'}) for _ in range(5))
    for name in ("a", "b", "c", "a"):
        transport.request("GET", f"/repos/owner/{name}")
    assert len(transport._conditional) <= 2 and transport._conditional_bytes <= 500
    assert calls[-1].get_header("If-none-match") is None
    replies[0] = Reply(200, {"id": "x" * 1000}, {"ETag": '"too-large"'})
    transport.request("GET", "/repos/owner/large")
    assert all(key[1] != "/repos/owner/large" for key in transport._conditional)


def test_rate_limit_cooldown_is_shared_by_auth_and_honors_reset_and_retry_after(http, monkeypatch):
    transport, replies, calls, tokens = http
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module.time, "time", lambda: 1000.0)
    replies.extend([Reply(429, {}, {"Retry-After": "3", "X-RateLimit-Remaining": "0",
                                   "X-RateLimit-Reset": "1010"}), Reply(200, {})])
    assert transport.request("GET", "/repos/owner/project").status == 429
    peer = BoundedGitHubV2HTTP(lambda: tokens[-1])
    monkeypatch.setattr(peer._opener, "open", transport._opener.open)
    now[0] = 105
    with pytest.raises(GitHubV2Error, match="GITHUB_RATE_LIMITED"):
        peer.request("GET", "/repos/owner/other")
    assert len(calls) == 1
    now[0] = 111
    assert peer.request("GET", "/repos/owner/other").status == 200


def test_secondary_limit_has_exponential_cooldown_without_retrying_write(http, monkeypatch):
    transport, replies, calls, _ = http
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    replies.extend([Reply(403, {"message": "You have exceeded a secondary rate limit."}),
                    Reply(429, {})])
    path = "/repos/owner/project/git/refs/heads/tested"
    assert transport.request("PATCH", path, body={"force": False}).status == 403
    now[0] = 159
    with pytest.raises(GitHubV2Error, match="GITHUB_RATE_LIMITED"):
        transport.request("PATCH", path, body={"force": False})
    now[0] = 161
    assert transport.request("PATCH", path, body={"force": False}).status == 429
    now[0] = 280
    with pytest.raises(GitHubV2Error, match="GITHUB_RATE_LIMITED"):
        transport.request("PATCH", path, body={"force": False})
    assert len(calls) == 2


def test_permission_denied_does_not_cool_down_other_repositories(http, monkeypatch):
    transport, replies, calls, tokens = http
    replies.extend([Reply(403, {"message": "Resource not accessible by personal access token"},
                          {"X-RateLimit-Remaining": "4999"}), Reply(200, {"id": 7})])
    assert transport.request("GET", "/repos/owner/forbidden").status == 403
    peer = BoundedGitHubV2HTTP(lambda: tokens[-1])
    monkeypatch.setattr(peer._opener, "open", transport._opener.open)
    assert peer.request("GET", "/repos/owner/allowed").status == 200
    assert len(calls) == 2
