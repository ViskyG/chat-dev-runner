"""Request-local Google HTTP transports for the multi-threaded live runner.

Drive and Docs services share immutable discovery metadata, but the Google API
client's httplib2 transport is not thread-safe. A service request therefore
receives a fresh AuthorizedHttp/Http pair. Only access to shared mutable Desktop
OAuth credentials is serialized; network requests use their independent HTTP
transports concurrently.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from importlib import import_module
from typing import Any


class _LockedCredentials:
    """Serialize token application/refresh without locking provider I/O."""

    def __init__(self, credentials: Any, lock: threading.RLock) -> None:
        self._credentials = credentials
        self._lock = lock

    def before_request(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return self._credentials.before_request(*args, **kwargs)

    def refresh(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return self._credentials.refresh(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._credentials, name)


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
    guarded_credentials = _LockedCredentials(credentials, credential_lock)

    def build_request(service_http: Any, *args: Any, **kwargs: Any) -> Any:
        del service_http
        # A stalled provider request must not hold service shutdown indefinitely.
        transport = authorized_http(guarded_credentials, http=http_factory(timeout=20))
        return request_type(transport, *args, **kwargs)

    return build_request
