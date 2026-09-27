"""Expiring one-time local bootstrap and per-session CSRF credentials.

Secrets are never placed in URLs or logs. The bootstrap is handed to the owner through
an owner-only local file; browser code exchanges it once using a same-origin JSON POST.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass


class AuthenticationError(ValueError):
    pass


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Session:
    csrf: str
    expires_at: float


class Sessions:
    cookie_name = "cdr_session"

    def __init__(self, *, ttl: int = 28_800, bootstrap_ttl: int = 300) -> None:
        self.ttl = ttl
        self.bootstrap_ttl = bootstrap_ttl
        self._bootstrap: tuple[str, float] | None = None
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def issue_bootstrap(self) -> str:
        secret = secrets.token_urlsafe(32)
        with self._lock:
            self._bootstrap = (_digest(secret), time.monotonic() + self.bootstrap_ttl)
        return secret

    def exchange(self, secret: str) -> tuple[str, Session]:
        with self._lock:
            now = time.monotonic()
            self._sessions = {key: value for key, value in self._sessions.items()
                              if value.expires_at > now}
            expected = self._bootstrap
            if (expected is None or expected[1] <= now
                    or not secrets.compare_digest(expected[0], _digest(secret))):
                raise AuthenticationError("INVALID_BOOTSTRAP")
            if len(self._sessions) >= 16:
                raise AuthenticationError("SESSION_LIMIT")
            self._bootstrap = None
            token = secrets.token_urlsafe(32)
            session = Session(secrets.token_urlsafe(32), now + self.ttl)
            self._sessions[_digest(token)] = session
            return token, session

    def authenticate(self, token: str | None, csrf: str | None = None,
                     *, mutation: bool = False) -> Session:
        if not token or len(token) > 128:
            raise AuthenticationError("SESSION_REQUIRED")
        with self._lock:
            session = self._sessions.get(_digest(token))
            if session is None or session.expires_at <= time.monotonic():
                raise AuthenticationError("SESSION_EXPIRED")
            if mutation and (not csrf or not secrets.compare_digest(session.csrf, csrf)):
                raise AuthenticationError("INVALID_CSRF")
            return session
