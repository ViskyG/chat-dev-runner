"""Request-local Google HTTP transports for the multi-threaded live runner.

Drive and Docs services share immutable discovery metadata, but the Google API
client's httplib2 transport is not thread-safe. A service request therefore
receives a fresh AuthorizedHttp/Http pair. One short owner-side lock serializes
requests that share mutable Desktop OAuth credentials, including refresh.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from importlib import import_module
from typing import Any


class _CredentialGuardedHttp:
    """Protect one shared OAuth credential across independent HTTP transports."""

    def __init__(self, authorized: Any, lock: threading.RLock) -> None:
        self._authorized = authorized
        self._lock = lock

    def request(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return self._authorized.request(*args, **kwargs)

    def close(self) -> None:
        with self._lock:
            self._authorized.close()


def isolated_request_builder(credentials: Any) -> Callable[..., Any]:
    """Return a googleapiclient requestBuilder for both Drive and Docs.

    The ignored service_http is the default shared transport constructed by
    discovery.build(). No returned request uses it. The scope remains the
    caller's already-authorized credential scope.
    """
    if credentials is None:
        raise ValueError("Google credentials are required")
    http_factory = import_module("httplib2").Http
    authorized_http = import_module("google_auth_httplib2").AuthorizedHttp
    request_type = import_module("googleapiclient.http").HttpRequest
    credential_lock = threading.RLock()

    def build_request(service_http: Any, *args: Any, **kwargs: Any) -> Any:
        del service_http
        # A stalled provider request must not hold service shutdown indefinitely.
        transport = authorized_http(credentials, http=http_factory(timeout=20))
        return request_type(_CredentialGuardedHttp(transport, credential_lock),
                            *args, **kwargs)

    return build_request
