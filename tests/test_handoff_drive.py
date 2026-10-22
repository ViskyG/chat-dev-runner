from __future__ import annotations

import copy
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httplib2
import pytest

import runner_app.adapters.handoff_drive as module
from runner_app.adapters.handoff_drive import (
    RAW_FILE_FIELDS,
    GoogleHandoffBlobProvider,
    HandoffDriveFactory,
    _BoundedGoogleAPI,
    _Deadline,
)
from runner_app.handoff.contracts import HandoffError, Limits
from runner_app.storage.paths import Paths

FILE = "raw-file-id-123"
PARENT = "allowed-folder-123"


def metadata(size: int = 4) -> dict[str, Any]:
    return {"id": FILE, "name": "design.txt", "mimeType": "text/plain", "parents": [PARENT],
            "trashed": False, "version": "3", "size": str(size),
            "capabilities": {"canDownload": True}}


class FakeAPI:
    def __init__(self, value: dict[str, Any] | None = None) -> None:
        self.value = value or metadata()
        self.calls: list[str] = []

    def raw_metadata(self, file_id: str) -> dict[str, Any]:
        self.calls.append(file_id)
        return copy.deepcopy(self.value)


class FakeResponse:
    status_code = 200

    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.chunks = [b"de", b"si"] if chunks is None else chunks
        self.closed = False

    def iter_content(self, *, chunk_size: int) -> Any:
        assert chunk_size == 65_536
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, response: FakeResponse | None = None) -> None:
        self.response = response or FakeResponse()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.response

    def close(self) -> None:
        self.closed = True


class FakeCredentials:
    def before_request(self, request: Any, method: str, url: str,
                       headers: dict[str, str]) -> None:
        headers["Authorization"] = "Bearer private-value"


def provider(*, api: Any = None, response: FakeResponse | None = None,
             **kwargs: Any) -> GoogleHandoffBlobProvider:
    return GoogleHandoffBlobProvider(api or FakeAPI(), FakeSession(response), FakeCredentials(),
                                     **kwargs)


def test_exact_id_raw_stream_and_metadata_recheck() -> None:
    api = FakeAPI()
    raw = provider(api=api)
    before = raw.metadata(FILE)
    assert b"".join(raw.chunks(FILE)) == b"desi"
    api.value["version"] = "4"
    after = raw.metadata(FILE)
    assert before["version"] == "3" and after["version"] == "4"
    assert api.calls == [FILE, FILE]
    session = raw.session
    assert session.calls[0][0] == f"https://www.googleapis.com/drive/v3/files/{FILE}?alt=media"
    assert session.calls[0][1]["stream"] is True
    assert session.calls[0][1]["allow_redirects"] is False
    assert all(0 < timeout <= 15 for timeout in session.calls[0][1]["timeout"])
    assert session.response.closed and session.closed


@pytest.mark.parametrize("change", [
    {"id": "different-file-id"}, {"mimeType": "application/vnd.google-apps.document"},
    {"mimeType": "application/vnd.google-apps.shortcut"}, {"mimeType": "bad mime"},
    {"size": None}, {"size": "-1"}, {"size": "01"}, {"size": True},
    {"version": ""}, {"parents": []}, {"trashed": True},
])
def test_invalid_metadata_never_opens_media(change: dict[str, Any]) -> None:
    api = FakeAPI({**metadata(), **change})
    raw = provider(api=api)
    with pytest.raises(HandoffError, match="INVALID_DRIVE_METADATA"):
        raw.metadata(FILE)
    assert raw.session.calls == []


@pytest.mark.parametrize("file_id", ["https://evil.example/file", "../file-id", "short", "x?alt=media"])
def test_reject_url_and_non_id_before_provider(file_id: str) -> None:
    api = FakeAPI()
    raw = provider(api=api)
    with pytest.raises(HandoffError, match="INVALID_DRIVE_FILE_ID"):
        raw.metadata(file_id)
    assert not api.calls


def test_metadata_required_and_file_total_partial_caps() -> None:
    raw = provider()
    with pytest.raises(HandoffError, match="METADATA_REQUIRED"):
        list(raw.chunks(FILE))
    raw = provider(limits=replace(Limits(), file_bytes=3))
    with pytest.raises(HandoffError, match="FILE_TOO_LARGE"):
        raw.metadata(FILE)
    raw = provider(response=FakeResponse([b"longer"]))
    raw.metadata(FILE)
    with pytest.raises(HandoffError, match="FILE_TOO_LARGE"):
        list(raw.chunks(FILE))
    raw = provider(response=FakeResponse([b"de"]))
    raw.metadata(FILE)
    with pytest.raises(HandoffError, match="DOWNLOAD_SIZE_MISMATCH"):
        list(raw.chunks(FILE))
    raw = provider(limits=replace(Limits(), total_bytes=3))
    raw.metadata(FILE)
    with pytest.raises(HandoffError, match="TOTAL_BYTES_EXCEEDED"):
        list(raw.chunks(FILE))


def test_redirect_and_stream_read_failure_are_bounded_and_sanitized() -> None:
    response = FakeResponse()
    response.status_code = 302
    raw = provider(response=response)
    raw.metadata(FILE)
    with pytest.raises(HandoffError, match="PROVIDER_MEDIA_REJECTED"):
        list(raw.chunks(FILE))
    assert response.closed

    class Broken(FakeResponse):
        def iter_content(self, *, chunk_size: int) -> Any:
            yield b"de"
            raise OSError("Bearer private-value /home/owner/private")

    raw = provider(response=Broken())
    raw.metadata(FILE)
    with pytest.raises(HandoffError) as error:
        list(raw.chunks(FILE))
    assert str(error.value) == "PROVIDER_READ_FAILED"


def test_blocked_metadata_call_obeys_request_timeout() -> None:
    release = threading.Event()

    class Blocked(FakeAPI):
        def raw_metadata(self, file_id: str) -> dict[str, Any]:
            release.wait(5)
            return metadata()

    raw = provider(api=Blocked(), timeout_seconds=2, request_timeout_seconds=0.05)
    started = time.monotonic()
    try:
        with pytest.raises(HandoffError, match="DOWNLOAD_TIMEOUT"):
            raw.metadata(FILE)
    finally:
        release.set()
    assert time.monotonic() - started < 0.75


def test_blocked_stream_call_obeys_shared_deadline_and_cancel() -> None:
    for cancel in (False, True):
        release, cancelled = threading.Event(), threading.Event()

        class Blocked(FakeResponse):
            def iter_content(self, *, chunk_size: int) -> Any:
                release.wait(5)  # noqa: B023 - this iteration releases its worker before continuing
                yield b"desi"

        response = Blocked()
        raw = provider(response=response, timeout_seconds=0.07 if not cancel else 2,
                       request_timeout_seconds=1, cancelled=cancelled.is_set)
        raw.metadata(FILE)
        if cancel:
            timer = threading.Timer(0.04, cancelled.set)
            timer.start()
        started = time.monotonic()
        try:
            with pytest.raises(HandoffError, match="CANCELLED" if cancel else "DOWNLOAD_TIMEOUT"):
                list(raw.chunks(FILE))
        finally:
            release.set()
            if cancel:
                timer.join()
        assert time.monotonic() - started < 0.75
        assert response.closed


def test_calls_share_one_overall_budget() -> None:
    deadline = _Deadline(0.1, 1)
    deadline.run(lambda: time.sleep(0.06))
    with pytest.raises(HandoffError, match="DOWNLOAD_TIMEOUT"):
        deadline.run(lambda: time.sleep(0.08))


def test_real_sdk_uses_exact_field_mask_static_discovery_and_http_timeouts(monkeypatch: Any) -> None:
    actual_import = module.import_module
    calls: list[tuple[str, str, dict[str, Any]]] = []
    timeouts: list[float] = []

    class Http:
        def __init__(self, *, timeout: float) -> None:
            timeouts.append(timeout)

        def close(self) -> None:
            pass

    class Authorized:
        def __init__(self, credentials: Any, *, http: Http, refresh_status_codes: tuple[Any, ...],
                     max_refresh_attempts: int) -> None:
            assert refresh_status_codes == () and max_refresh_attempts == 0

        def request(self, uri: str, method: str = "GET", **kwargs: Any) -> Any:
            calls.append((uri, method, kwargs))
            return httplib2.Response({"status": "200", "content-type": "application/json"}), (
                json.dumps(metadata()).encode())

    def imported(name: str) -> Any:
        if name == "httplib2":
            return SimpleNamespace(Http=Http)
        if name == "google_auth_httplib2":
            return SimpleNamespace(AuthorizedHttp=Authorized)
        return actual_import(name)

    monkeypatch.setattr(module, "import_module", imported)
    api = _BoundedGoogleAPI(FakeCredentials(), _Deadline(4, 0.75))
    assert calls == [] and timeouts == []  # No discovery traffic on construction.
    assert api.raw_metadata(FILE)["id"] == FILE
    assert len(calls) == 1 and len(timeouts) == 1
    assert 0 < timeouts[0] <= 0.75
    assert "/drive/v3/files/raw-file-id-123?" in calls[0][0]
    assert "alt=media" not in calls[0][0]
    assert "fields=" in calls[0][0]
    assert calls[0][2]["redirections"] == 0
    assert "capabilities(canDownload)" in RAW_FILE_FIELDS


def test_factory_construction_is_offline_and_credentials_reload(monkeypatch: Any, tmp_path: Path) -> None:
    calls: list[str] = []
    selected = SimpleNamespace(credential_paths=lambda paths: paths)

    def config(path: Path) -> Any:
        calls.append("config")
        return selected

    def load(paths: Paths, **kwargs: Any) -> FakeCredentials:
        calls.append("credentials")
        assert kwargs["refresh"] is True
        assert isinstance(kwargs["backend"], module._BoundedCredentialBackend)
        return FakeCredentials()

    monkeypatch.setattr(module.DocsProviderConfig, "from_file", config)
    monkeypatch.setattr(module, "load_authorized_credentials", load)
    monkeypatch.setattr(module, "_BoundedGoogleAPI", lambda *args: FakeAPI())
    factory = HandoffDriveFactory(Paths.discover({"HOME": str(tmp_path)}))
    assert calls == []
    first = factory(tmp_path / "docs.json")
    second = factory(tmp_path / "docs.json")
    assert first is not second
    assert calls == ["config", "credentials", "config", "credentials"]
