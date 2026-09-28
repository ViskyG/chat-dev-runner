"""Fake network contract for the live Google requestBuilder."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import httplib2  # type: ignore[import-untyped]
import pytest

import runner_app.adapters.drive_transport as transport_module


class _FakeCredentials:
    def __init__(self) -> None:
        self.active = 0
        self.maximum_active = 0
        self.calls = 0
        self.refreshes = 0
        self.guard = threading.Lock()

    def _enter(self) -> None:
        with self.guard:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        time.sleep(0.003)

    def _leave(self) -> None:
        with self.guard:
            self.active -= 1

    def before_request(self, request: Any, method: str, uri: str,
                       headers: dict[str, str]) -> None:
        del request, method, uri
        self._enter()
        try:
            self.calls += 1
            headers["Authorization"] = "Bearer fake"
        finally:
            self._leave()

    def refresh(self, request: Any) -> None:
        del request
        self._enter()
        try:
            self.refreshes += 1
        finally:
            self._leave()


class _FakeHttp:
    instances: ClassVar[list[_FakeHttp]] = []
    active: ClassVar[int] = 0
    maximum_active: ClassVar[int] = 0
    guard: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout
        self.instances.append(self)

    def request(self, uri: str, *args: Any,
                **kwargs: Any) -> tuple[httplib2.Response, bytes]:
        del args
        assert kwargs["headers"]["Authorization"] == "Bearer fake"
        with self.guard:
            type(self).active += 1
            type(self).maximum_active = max(type(self).maximum_active,
                                            type(self).active)
        try:
            time.sleep(0.03)
            return httplib2.Response({"status": "200"}), uri.encode("ascii")
        finally:
            with self.guard:
                type(self).active -= 1

    def close(self) -> None:
        pass


class _FakeAuthorizedHttp:
    instances: ClassVar[list[_FakeAuthorizedHttp]] = []

    def __init__(self, credentials: Any, *, http: _FakeHttp) -> None:
        self.credentials = credentials
        self.http = http
        self.instances.append(self)

    def request(self, uri: str, *args: Any,
                **kwargs: Any) -> tuple[httplib2.Response, bytes]:
        headers = dict(kwargs.get("headers") or {})
        self.credentials.before_request(self.http, kwargs.get("method", "GET"),
                                        uri, headers)
        kwargs["headers"] = headers
        return self.http.request(uri, *args, **kwargs)

    def close(self) -> None:
        self.http.close()


def test_drive_and_docs_requests_overlap_http_but_guard_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeHttp.instances.clear()
    _FakeHttp.active = 0
    _FakeHttp.maximum_active = 0
    _FakeAuthorizedHttp.instances.clear()
    real_import = import_module

    def fake_import(name: str) -> Any:
        if name == "httplib2":
            return SimpleNamespace(Http=_FakeHttp)
        if name == "google_auth_httplib2":
            return SimpleNamespace(AuthorizedHttp=_FakeAuthorizedHttp)
        return real_import(name)

    monkeypatch.setattr(transport_module, "import_module", fake_import)
    credentials = _FakeCredentials()
    builder = transport_module.isolated_request_builder(credentials)
    shared_service_http = object()
    requests = [
        builder(shared_service_http, lambda _response, body: body,
                f"https://example.test/{index}",
                method="POST" if index % 2 else "GET")
        for index in range(12)
    ]
    barrier = threading.Barrier(len(requests))

    def execute(request: Any) -> bytes:
        barrier.wait(timeout=5)
        return cast(bytes, request.execute(num_retries=0))

    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        results = list(pool.map(execute, requests))

    assert len(_FakeHttp.instances) == len(requests)
    assert all(http.timeout == 20 for http in _FakeHttp.instances)
    assert len({id(http) for http in _FakeHttp.instances}) == len(requests)
    assert len(_FakeAuthorizedHttp.instances) == len(requests)
    assert all(item.credentials._credentials is credentials
               for item in _FakeAuthorizedHttp.instances)
    assert credentials.maximum_active == 1
    assert credentials.calls == len(requests)
    assert _FakeHttp.maximum_active > 1
    assert results == [f"https://example.test/{index}".encode("ascii")
                       for index in range(len(requests))]
    assert all(request.http is not shared_service_http for request in requests)


def test_refresh_and_header_application_share_one_credential_lock() -> None:
    credentials = _FakeCredentials()
    guarded = transport_module._LockedCredentials(credentials, threading.RLock())
    barrier = threading.Barrier(8)

    def call(index: int) -> None:
        barrier.wait(timeout=5)
        if index % 2:
            guarded.refresh(None)
        else:
            guarded.before_request(None, "GET", "https://example.test",
                                   {})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(call, range(8)))

    assert credentials.maximum_active == 1
    assert credentials.calls == 4
    assert credentials.refreshes == 4


def test_google_builder_requires_credentials() -> None:
    with pytest.raises(ValueError, match="credentials are required"):
        transport_module.isolated_request_builder(None)
