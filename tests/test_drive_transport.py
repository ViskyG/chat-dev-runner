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
        self.guard = threading.Lock()


class _FakeHttp:
    instances: ClassVar[list[_FakeHttp]] = []

    def __init__(self) -> None:
        self.instances.append(self)


class _FakeAuthorizedHttp:
    instances: ClassVar[list[_FakeAuthorizedHttp]] = []

    def __init__(self, credentials: _FakeCredentials, *, http: _FakeHttp) -> None:
        self.credentials = credentials
        self.http = http
        self.instances.append(self)

    def request(self, uri: str, *args: Any, **kwargs: Any) -> tuple[httplib2.Response, bytes]:
        del args, kwargs
        with self.credentials.guard:
            self.credentials.active += 1
            self.credentials.maximum_active = max(
                self.credentials.maximum_active, self.credentials.active,
            )
        try:
            time.sleep(0.003)
            self.credentials.calls += 1
            return httplib2.Response({"status": "200"}), uri.encode("ascii")
        finally:
            with self.credentials.guard:
                self.credentials.active -= 1

    def close(self) -> None:
        pass


def test_drive_and_docs_requests_use_independent_http_and_guard_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeHttp.instances.clear()
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
    assert len({id(http) for http in _FakeHttp.instances}) == len(requests)
    assert len(_FakeAuthorizedHttp.instances) == len(requests)
    assert all(item.credentials is credentials for item in _FakeAuthorizedHttp.instances)
    assert credentials.maximum_active == 1
    assert credentials.calls == len(requests)
    assert results == [f"https://example.test/{index}".encode("ascii")
                       for index in range(len(requests))]
    assert all(request.http is not shared_service_http for request in requests)


def test_google_builder_requires_credentials() -> None:
    with pytest.raises(ValueError, match="credentials are required"):
        transport_module.isolated_request_builder(None)
