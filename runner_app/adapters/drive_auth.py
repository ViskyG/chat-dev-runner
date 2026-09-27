"""Owner-local Desktop OAuth boundary for the existing Drive installation.

Inspection never imports Google libraries, opens a browser, refreshes a token, or
contacts Drive. The interactive flow is an explicit local setup action only; it
requests the owner's configured full Drive scope and does not bootstrap or
create any Drive resource. A stored token is not proof that the Installation
Index exists or is readable by the authorized account.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, cast

from runner_app.storage.paths import (
    Paths,
    UnsafePathError,
    _open_directory,
    _open_file_at,
    ensure_private_directory,
)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
CLIENT_FILENAME = "google-oauth-client.json"
TOKEN_FILENAME = "google-drive.json"
_MAX_JSON_BYTES = 65_536
_AUTH_URIS = frozenset({
    "https://accounts.google.com/o/oauth2/auth",
    "https://accounts.google.com/o/oauth2/v2/auth",
})
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_REDIRECT_URIS = frozenset({"http://localhost", "http://127.0.0.1"})
_TOKEN_FIELDS = frozenset({
    "token", "refresh_token", "token_uri", "scopes", "expiry", "rapt_token",
    "universe_domain", "account", "quota_project_id", "trust_boundary",
})


class DriveAuthError(ValueError):
    """Stable code only; provider responses and credential values are never echoed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AuthInspection:
    state: str
    client_configured: bool
    token_stored: bool
    scope: str = DRIVE_SCOPE
    provider_checked: bool = False


class OAuthCredentials(Protocol):
    @property
    def valid(self) -> bool: ...

    @property
    def refresh_token(self) -> str | None: ...

    @property
    def scopes(self) -> Sequence[str] | None: ...

    @property
    def granted_scopes(self) -> Sequence[str] | None: ...

    def to_json(self) -> str: ...


class OAuthBackend(Protocol):
    def from_authorized_info(
        self, info: Mapping[str, Any], scopes: tuple[str, ...],
    ) -> OAuthCredentials: ...

    def run_desktop(
        self, client: Mapping[str, Any], scopes: tuple[str, ...],
    ) -> OAuthCredentials: ...

    def refresh(self, credentials: OAuthCredentials) -> None: ...


class _GoogleBackend:
    """Optional imports and network-capable calls occur only through explicit APIs."""

    def from_authorized_info(
        self, info: Mapping[str, Any], scopes: tuple[str, ...],
    ) -> OAuthCredentials:
        credentials_class = import_module("google.oauth2.credentials").Credentials
        return cast(OAuthCredentials, credentials_class.from_authorized_user_info(
            dict(info), scopes=list(scopes),
        ))

    def run_desktop(
        self, client: Mapping[str, Any], scopes: tuple[str, ...],
    ) -> OAuthCredentials:
        installed_app_flow = import_module("google_auth_oauthlib.flow").InstalledAppFlow
        flow = installed_app_flow.from_client_config(
            {"installed": dict(client)}, scopes=list(scopes),
        )
        return cast(OAuthCredentials, flow.run_local_server(
            host="127.0.0.1", port=0, open_browser=True,
            access_type="offline", prompt="consent", timeout_seconds=900,
            authorization_prompt_message="Complete Google Drive authorization in the opened browser.",
        ))

    def refresh(self, credentials: OAuthCredentials) -> None:
        request_class = import_module("google.auth.transport.requests").Request
        credentials.refresh(request_class())  # type: ignore[attr-defined]


def client_path(paths: Paths) -> Path:
    return paths.config_dir / CLIENT_FILENAME


def token_path(paths: Paths) -> Path:
    return paths.token_dir / TOKEN_FILENAME


def _read_private_json(path: Path, *, missing_code: str, invalid_code: str) -> dict[str, Any]:
    if not path.is_absolute() or ".." in path.parts:
        raise DriveAuthError("AUTH_PATH_UNSAFE")
    # A missing app directory is ordinary not-configured state. lexists keeps a
    # dangling symlink from masquerading as a missing directory.
    if not os.path.lexists(path.parent):
        raise DriveAuthError(missing_code)
    # Open the verified parent first: a missing child is ordinary setup state,
    # while an unsafe parent, symlink or non-private child must fail closed.
    try:
        directory = _open_directory(path.parent, create=False)
        try:
            descriptor = _open_file_at(directory, path.name, create=False)
        finally:
            os.close(directory)
    except FileNotFoundError:
        raise DriveAuthError(missing_code) from None
    except (UnsafePathError, OSError):
        raise DriveAuthError("AUTH_PATH_UNSAFE") from None
    try:
        payload = bytearray()
        while len(payload) <= _MAX_JSON_BYTES:
            chunk = os.read(descriptor, min(8192, _MAX_JSON_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > _MAX_JSON_BYTES:
            raise DriveAuthError(invalid_code)
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise DriveAuthError(invalid_code) from None
        if not isinstance(value, dict):
            raise DriveAuthError(invalid_code)
        return value
    finally:
        os.close(descriptor)


def _load_client(paths: Paths) -> dict[str, Any]:
    raw = _read_private_json(
        client_path(paths), missing_code="DESKTOP_CLIENT_MISSING",
        invalid_code="DESKTOP_CLIENT_INVALID",
    )
    if set(raw) != {"installed"} or not isinstance(raw.get("installed"), dict):
        raise DriveAuthError("DESKTOP_CLIENT_INVALID")
    client: dict[str, Any] = raw["installed"]
    client_id = client.get("client_id")
    secret = client.get("client_secret")
    redirects = client.get("redirect_uris")
    if (not isinstance(client_id, str) or not client_id.endswith(".apps.googleusercontent.com")
            or len(client_id) > 512 or not isinstance(secret, str) or not secret
            or client.get("auth_uri") not in _AUTH_URIS
            or client.get("token_uri") != _TOKEN_URI
            or not isinstance(redirects, list) or not redirects
            or any(not isinstance(uri, str) or uri not in _REDIRECT_URIS
                   for uri in redirects)):
        raise DriveAuthError("DESKTOP_CLIENT_INVALID")
    return client


def _validate_token(info: Mapping[str, Any]) -> None:
    # Persist only token material. Client fields remain in the owner-private
    # Desktop OAuth JSON and are inserted into Google Credentials in memory.
    if set(info) - _TOKEN_FIELDS:
        raise DriveAuthError("TOKEN_INVALID")
    if info.get("scopes") != [DRIVE_SCOPE]:
        raise DriveAuthError("TOKEN_SCOPE_INVALID")
    if info.get("token_uri") != _TOKEN_URI:
        raise DriveAuthError("TOKEN_INVALID")
    refresh = info.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise DriveAuthError("TOKEN_REFRESH_MISSING")


def _load_token(paths: Paths) -> dict[str, Any]:
    info = _read_private_json(
        token_path(paths), missing_code="AUTHORIZATION_REQUIRED",
        invalid_code="TOKEN_INVALID",
    )
    _validate_token(info)
    return info


def inspect_local_auth(paths: Paths) -> AuthInspection:
    """Offline local-file check; TOKEN_STORED_UNVERIFIED is not a Drive grant probe."""
    try:
        _load_client(paths)
    except DriveAuthError as error:
        return AuthInspection(error.code, False, False)
    try:
        _load_token(paths)
    except DriveAuthError as error:
        return AuthInspection(error.code, True, False)
    return AuthInspection("TOKEN_STORED_UNVERIFIED", True, True)


def _serialized_credentials(
    credentials: OAuthCredentials, client: Mapping[str, Any],
) -> dict[str, Any]:
    if not credentials.valid:
        raise DriveAuthError("OAUTH_CREDENTIALS_INVALID")
    try:
        scopes = credentials.scopes
        granted = credentials.granted_scopes
        if scopes is not None and set(scopes) != {DRIVE_SCOPE}:
            raise DriveAuthError("TOKEN_SCOPE_INVALID")
        if granted is not None and set(granted) != {DRIVE_SCOPE}:
            raise DriveAuthError("TOKEN_SCOPE_INVALID")
        info = json.loads(credentials.to_json())
    except DriveAuthError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise DriveAuthError("OAUTH_CREDENTIALS_INVALID") from None
    if not isinstance(info, dict):
        raise DriveAuthError("OAUTH_CREDENTIALS_INVALID")
    if (info.get("client_id") != client["client_id"]
            or info.get("client_secret") != client["client_secret"]):
        raise DriveAuthError("TOKEN_CLIENT_MISMATCH")
    token_info = {key: value for key, value in info.items()
                  if key not in {"client_id", "client_secret"}}
    _validate_token(token_info)
    return token_info


def _write_token(paths: Paths, info: Mapping[str, Any]) -> None:
    path = token_path(paths)
    encoded = (json.dumps(info, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > _MAX_JSON_BYTES:
        raise DriveAuthError("TOKEN_INVALID")
    try:
        ensure_private_directory(path.parent)
        directory = _open_directory(path.parent, create=False)
    except (UnsafePathError, OSError):
        raise DriveAuthError("AUTH_PATH_UNSAFE") from None
    temporary = f".google-drive-{secrets.token_hex(12)}"
    try:
        try:
            previous = _open_file_at(directory, path.name, create=False)
        except FileNotFoundError:
            pass
        else:
            os.close(previous)
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=directory,
        )
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        readable_directory = os.open(
            ".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=directory,
        )
        try:
            os.fsync(readable_directory)
        finally:
            os.close(readable_directory)
    except (UnsafePathError, OSError):
        raise DriveAuthError("AUTH_PATH_UNSAFE") from None
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def authorize_interactive(
    paths: Paths, *, backend: OAuthBackend | None = None,
) -> OAuthCredentials:
    """Explicit local browser consent only; creates no Drive files or workspace."""
    client = _load_client(paths)
    chosen = backend if backend is not None else _GoogleBackend()
    try:
        credentials = chosen.run_desktop(client, (DRIVE_SCOPE,))
    except Exception:  # noqa: BLE001 - third-party errors may contain credential material
        raise DriveAuthError("OAUTH_INTERACTIVE_FAILED") from None
    info = _serialized_credentials(credentials, client)
    _write_token(paths, info)
    return credentials


def load_authorized_credentials(
    paths: Paths, *, refresh: bool = False, backend: OAuthBackend | None = None,
) -> OAuthCredentials:
    """Use a stored token explicitly; refreshing may call Google when requested."""
    client = _load_client(paths)
    info = _load_token(paths)
    chosen = backend if backend is not None else _GoogleBackend()
    # Google's authorized-user constructor needs the Desktop client's fields.
    # Merge them only in memory; never place them in the token file.
    authorized_info = {**info, "client_id": client["client_id"],
                       "client_secret": client["client_secret"]}
    try:
        credentials = chosen.from_authorized_info(authorized_info, (DRIVE_SCOPE,))
    except Exception:  # noqa: BLE001 - third-party errors may contain credential material
        raise DriveAuthError("OAUTH_CREDENTIALS_INVALID") from None
    if not credentials.valid:
        if not refresh:
            raise DriveAuthError("TOKEN_REFRESH_REQUIRED")
        if not credentials.refresh_token:
            raise DriveAuthError("TOKEN_REFRESH_MISSING")
        try:
            chosen.refresh(credentials)
        except Exception:  # noqa: BLE001 - third-party errors may contain credential material
            raise DriveAuthError("OAUTH_REFRESH_FAILED") from None
        _write_token(paths, _serialized_credentials(credentials, client))
    else:
        _serialized_credentials(credentials, client)
    return credentials
