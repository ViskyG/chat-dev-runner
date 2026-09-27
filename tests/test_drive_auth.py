"""Offline Desktop OAuth boundary tests; no Google modules or account access."""

from __future__ import annotations

import builtins
import json
import os
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from runner_app.adapters.drive_auth import (
    DRIVE_SCOPE,
    DriveAuthError,
    OAuthCredentials,
    _GoogleBackend,
    authorize_interactive,
    client_path,
    inspect_local_auth,
    load_authorized_credentials,
    token_path,
)
from runner_app.storage.paths import Paths


def paths_at(root: Path) -> Paths:
    return Paths(root / "config", root / "state", root / "cache")


def client_data() -> dict[str, Any]:
    return {"installed": {
        "client_id": "local-test.apps.googleusercontent.com",
        "client_secret": "fixture-secret-keep-private",
        "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }}


def token_data(*, scopes: list[str] | None = None) -> dict[str, Any]:
    return {
        "token": "short-lived-fixture-token",
        "refresh_token": "fixture-refresh-keep-private",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": [DRIVE_SCOPE] if scopes is None else scopes,
    }


def credential_data(*, scopes: list[str] | None = None) -> dict[str, Any]:
    installed = client_data()["installed"]
    return {**token_data(scopes=scopes),
            "client_id": installed["client_id"],
            "client_secret": installed["client_secret"]}


def write_private(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_text(json.dumps(value), encoding="utf-8")
    os.chmod(path, 0o600)


class FakeCredentials:
    def __init__(self, *, valid: bool = True, info: dict[str, Any] | None = None) -> None:
        self.valid = valid
        self.info = credential_data() if info is None else info
        self.refresh_token = self.info.get("refresh_token")
        self.scopes = self.info.get("scopes")
        self.granted_scopes = [DRIVE_SCOPE]

    def to_json(self) -> str:
        return json.dumps(self.info)


class FakeBackend:
    def __init__(self, credentials: FakeCredentials | None = None) -> None:
        self.credentials = credentials or FakeCredentials()
        self.actions: list[str] = []

    def from_authorized_info(
        self, info: Mapping[str, Any], scopes: tuple[str, ...],
    ) -> FakeCredentials:
        assert scopes == (DRIVE_SCOPE,)
        assert info["client_id"] == client_data()["installed"]["client_id"]
        assert info["client_secret"] == client_data()["installed"]["client_secret"]
        self.actions.append("load")
        self.credentials.info = dict(info)
        return self.credentials

    def run_desktop(
        self, client: Mapping[str, Any], scopes: tuple[str, ...],
    ) -> FakeCredentials:
        assert client["client_id"] == client_data()["installed"]["client_id"]
        assert scopes == (DRIVE_SCOPE,)
        self.actions.append("interactive")
        return self.credentials

    def refresh(self, credentials: OAuthCredentials) -> None:
        self.actions.append("refresh")
        fake = cast(FakeCredentials, credentials)
        fake.valid = True
        fake.info["token"] = "refreshed-token"



def test_client_filename_matches_local_setup(tmp_path: Path) -> None:
    assert client_path(paths_at(tmp_path)).name == "google-oauth-client.json"


def test_google_backend_opens_browser_without_printing_authorization_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class LocalFlow:
        def run_local_server(self, **kwargs: Any) -> FakeCredentials:
            seen.update(kwargs)
            return FakeCredentials()

    class FakeInstalledAppFlow:
        @staticmethod
        def from_client_config(config: dict[str, Any], scopes: list[str]) -> LocalFlow:
            assert config == client_data()
            assert scopes == [DRIVE_SCOPE]
            return LocalFlow()

    package = ModuleType("google_auth_oauthlib")
    package.__dict__["__path__"] = []
    flow_module = ModuleType("google_auth_oauthlib.flow")
    flow_module.__dict__["InstalledAppFlow"] = FakeInstalledAppFlow
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib", package)
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", flow_module)

    _GoogleBackend().run_desktop(client_data()["installed"], (DRIVE_SCOPE,))
    assert seen["open_browser"] is True
    assert seen["host"] == "127.0.0.1"
    assert seen["access_type"] == "offline"
    assert seen["prompt"] == "consent"
    assert seen["timeout_seconds"] == 900
    prompt_message = seen["authorization_prompt_message"]
    assert prompt_message == "Complete Google Drive authorization in the opened browser."
    assert "{url}" not in prompt_message
    assert "apps.googleusercontent.com" not in prompt_message


def test_module_import_has_no_google_import_or_network() -> None:
    script = """import builtins
import socket
original_import = builtins.__import__
def block_import(name, *args, **kwargs):
    if name.startswith(('google', 'google_auth_oauthlib', 'googleapiclient')):
        raise AssertionError('Google dependency imported at module load')
    return original_import(name, *args, **kwargs)
def block_network(*args, **kwargs):
    raise AssertionError('Network attempted at module load')
builtins.__import__ = block_import
socket.socket.connect = block_network
socket.socket.connect_ex = block_network
socket.create_connection = block_network
import runner_app.adapters.drive_auth
"""
    subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
        check=True, capture_output=True, text=True,
    )


def test_missing_local_config_and_import_are_inert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth_paths = paths_at(tmp_path)
    original_import = builtins.__import__

    def forbid_google(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith(("google", "google_auth_oauthlib", "googleapiclient")):
            raise AssertionError("Google import during offline inspection")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", forbid_google)
    assert inspect_local_auth(auth_paths).state == "DESKTOP_CLIENT_MISSING"
    assert not auth_paths.config_dir.exists()
    assert not auth_paths.token_dir.exists()


def test_client_file_must_be_private_regular_and_nofollow(tmp_path: Path) -> None:
    auth_paths = paths_at(tmp_path)
    target = tmp_path / "outside.json"
    write_private(target, client_data())
    auth_paths.config_dir.mkdir(mode=0o700)
    client_path(auth_paths).symlink_to(target)
    assert inspect_local_auth(auth_paths).state == "AUTH_PATH_UNSAFE"
    client_path(auth_paths).unlink()
    write_private(client_path(auth_paths), client_data())
    os.chmod(client_path(auth_paths), 0o644)
    assert inspect_local_auth(auth_paths).state == "AUTH_PATH_UNSAFE"
    os.chmod(client_path(auth_paths), 0o600)
    os.link(client_path(auth_paths), tmp_path / "second-link")
    assert inspect_local_auth(auth_paths).state == "AUTH_PATH_UNSAFE"


@pytest.mark.parametrize("change", [
    {"auth_uri": "https://attacker.invalid/authorize"},
    {"token_uri": "https://attacker.invalid/token"},
    {"redirect_uris": ["http://attacker.invalid"]},
])
def test_client_rejects_untrusted_oauth_endpoint(
    tmp_path: Path, change: dict[str, Any],
) -> None:
    auth_paths = paths_at(tmp_path)
    value = client_data()
    value["installed"].update(change)
    write_private(client_path(auth_paths), value)
    assert inspect_local_auth(auth_paths).state == "DESKTOP_CLIENT_INVALID"


def test_web_oauth_client_rejected(tmp_path: Path) -> None:
    auth_paths = paths_at(tmp_path)
    write_private(client_path(auth_paths), {"web": client_data()["installed"]})
    assert inspect_local_auth(auth_paths).state == "DESKTOP_CLIENT_INVALID"


def test_missing_token_in_existing_private_directory_requires_authorization(tmp_path: Path) -> None:
    auth_paths = paths_at(tmp_path)
    write_private(client_path(auth_paths), client_data())
    auth_paths.token_dir.mkdir(mode=0o700)
    assert inspect_local_auth(auth_paths).state == "AUTHORIZATION_REQUIRED"
    with pytest.raises(DriveAuthError) as missing:
        load_authorized_credentials(auth_paths, backend=FakeBackend())
    assert missing.value.code == "AUTHORIZATION_REQUIRED"

    outside = tmp_path / "outside-token.json"
    write_private(outside, token_data())
    token_path(auth_paths).symlink_to(outside)
    assert inspect_local_auth(auth_paths).state == "AUTH_PATH_UNSAFE"


def test_stored_token_permissions_are_checked_without_loading_backend(tmp_path: Path) -> None:
    auth_paths = paths_at(tmp_path)
    write_private(client_path(auth_paths), client_data())
    write_private(token_path(auth_paths), token_data())
    os.chmod(token_path(auth_paths), 0o644)
    assert inspect_local_auth(auth_paths).state == "AUTH_PATH_UNSAFE"
    os.chmod(token_path(auth_paths), 0o600)
    os.link(token_path(auth_paths), tmp_path / "token-hardlink")
    assert inspect_local_auth(auth_paths).state == "AUTH_PATH_UNSAFE"


def test_stored_scope_is_exact_and_client_fields_are_rejected(tmp_path: Path) -> None:
    auth_paths = paths_at(tmp_path)
    write_private(client_path(auth_paths), client_data())
    assert inspect_local_auth(auth_paths).state == "AUTHORIZATION_REQUIRED"
    assert not auth_paths.token_dir.exists()
    old_scope = "https://www.googleapis.com/auth/drive.file"
    write_private(token_path(auth_paths), token_data(scopes=[old_scope]))
    assert inspect_local_auth(auth_paths).state == "TOKEN_SCOPE_INVALID"
    backend = FakeBackend()
    with pytest.raises(DriveAuthError) as old_token_error:
        load_authorized_credentials(auth_paths, backend=backend)
    assert old_token_error.value.code == "TOKEN_SCOPE_INVALID"
    assert backend.actions == []
    write_private(token_path(auth_paths), token_data(scopes=[DRIVE_SCOPE, old_scope]))
    assert inspect_local_auth(auth_paths).state == "TOKEN_SCOPE_INVALID"
    invalid = credential_data()
    write_private(token_path(auth_paths), invalid)
    assert inspect_local_auth(auth_paths).state == "TOKEN_INVALID"
    write_private(token_path(auth_paths), token_data())
    status = inspect_local_auth(auth_paths)
    assert status.state == "TOKEN_STORED_UNVERIFIED"
    assert status.provider_checked is False


def test_explicit_interactive_flow_writes_only_private_local_token(tmp_path: Path) -> None:
    auth_paths = paths_at(tmp_path)
    write_private(client_path(auth_paths), client_data())
    backend = FakeBackend()
    assert backend.actions == []
    assert inspect_local_auth(auth_paths).state == "AUTHORIZATION_REQUIRED"
    result = authorize_interactive(auth_paths, backend=backend)
    assert result is backend.credentials
    assert backend.actions == ["interactive"]
    assert inspect_local_auth(auth_paths).state == "TOKEN_STORED_UNVERIFIED"
    assert stat.S_IMODE(token_path(auth_paths).stat().st_mode) == 0o600
    assert stat.S_IMODE(auth_paths.token_dir.stat().st_mode) == 0o700
    assert list(auth_paths.token_dir.iterdir()) == [token_path(auth_paths)]
    stored = json.loads(token_path(auth_paths).read_text())
    assert stored == token_data()
    assert "client_id" not in stored and "client_secret" not in stored


def test_interactive_refuses_existing_unsafe_token_without_replacement(tmp_path: Path) -> None:
    auth_paths = paths_at(tmp_path)
    write_private(client_path(auth_paths), client_data())
    auth_paths.token_dir.mkdir(mode=0o700)
    outside = tmp_path / "outside-token.json"
    write_private(outside, token_data())
    token_path(auth_paths).symlink_to(outside)
    with pytest.raises(DriveAuthError) as error:
        authorize_interactive(auth_paths, backend=FakeBackend())
    assert error.value.code == "AUTH_PATH_UNSAFE"
    assert token_path(auth_paths).is_symlink()
    assert json.loads(outside.read_text()) == token_data()


def test_extra_interactive_grant_cannot_be_persisted(tmp_path: Path) -> None:
    auth_paths = paths_at(tmp_path)
    write_private(client_path(auth_paths), client_data())
    credentials = FakeCredentials()
    credentials.granted_scopes = [DRIVE_SCOPE, "https://www.googleapis.com/auth/drive.file"]
    with pytest.raises(DriveAuthError) as error:
        authorize_interactive(auth_paths, backend=FakeBackend(credentials))
    assert error.value.code == "TOKEN_SCOPE_INVALID"
    assert not auth_paths.token_dir.exists()


def test_interactive_rejects_mismatched_desktop_client_before_writing_token(
    tmp_path: Path,
) -> None:
    auth_paths = paths_at(tmp_path)
    write_private(client_path(auth_paths), client_data())
    info = credential_data()
    info["client_id"] = "other.apps.googleusercontent.com"
    with pytest.raises(DriveAuthError) as error:
        authorize_interactive(auth_paths, backend=FakeBackend(FakeCredentials(info=info)))
    assert error.value.code == "TOKEN_CLIENT_MISMATCH"
    assert not auth_paths.token_dir.exists()


def test_stored_token_never_refreshes_implicitly(tmp_path: Path) -> None:
    auth_paths = paths_at(tmp_path)
    write_private(client_path(auth_paths), client_data())
    write_private(token_path(auth_paths), token_data())
    backend = FakeBackend(FakeCredentials(valid=False))
    with pytest.raises(DriveAuthError) as error:
        load_authorized_credentials(auth_paths, backend=backend)
    assert error.value.code == "TOKEN_REFRESH_REQUIRED"
    assert backend.actions == ["load"]
    assert json.loads(token_path(auth_paths).read_text())["token"] == "short-lived-fixture-token"
    credentials = load_authorized_credentials(auth_paths, refresh=True, backend=backend)
    assert credentials.valid is True
    assert backend.actions == ["load", "load", "refresh"]
    refreshed = json.loads(token_path(auth_paths).read_text())
    assert refreshed["token"] == "refreshed-token"
    assert "client_id" not in refreshed and "client_secret" not in refreshed
    assert stat.S_IMODE(token_path(auth_paths).stat().st_mode) == 0o600


def test_error_never_contains_client_or_token_values(tmp_path: Path) -> None:
    auth_paths = paths_at(tmp_path)
    write_private(client_path(auth_paths), client_data())
    value = token_data()
    value["scopes"] = ["https://www.googleapis.com/auth/drive.file"]
    write_private(token_path(auth_paths), value)
    with pytest.raises(DriveAuthError) as error:
        load_authorized_credentials(auth_paths, backend=FakeBackend())
    assert error.value.code == "TOKEN_SCOPE_INVALID"
    assert "fixture-secret" not in str(error.value)
    assert "fixture-refresh" not in str(error.value)
