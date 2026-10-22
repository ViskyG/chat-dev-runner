"""Exact-ID raw Drive reads and bounded transports for locally enrolled handoffs.

No discovery, OAuth consent, export, URL input or resource creation is exposed.
Factories reload owner-private credentials for each operation, so replacing a
token/configuration never leaves a cached credential active indefinitely.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from importlib import import_module
from pathlib import Path
from typing import Any, TypeVar, cast

from runner_app.adapters.drive_auth import _GoogleBackend, load_authorized_credentials
from runner_app.adapters.drive_docs import FILE_FIELDS, DriveError
from runner_app.adapters.drive_google import _bounded_batch, _status
from runner_app.adapters.drive_index import ExistingInstallation, LegacyInstallationManifest
from runner_app.adapters.drive_mailbox_v2 import DocsProviderConfig
from runner_app.config import DRIVE_INDEX_ID, DRIVE_ROOT_ID
from runner_app.handoff.contracts import HandoffError, Limits
from runner_app.storage.paths import Paths

RAW_FILE_FIELDS = (
    "id,name,mimeType,parents,trashed,version,size,sha256Checksum,shortcutDetails,"
    "capabilities(canDownload)"
)
_FILE_ID = re.compile(r"[A-Za-z0-9_-]{8,256}\Z", re.ASCII)
_CHUNK_BYTES = 64 * 1024
_DEFAULT_LIMITS = Limits()
_T = TypeVar("_T")


class _Deadline:
    """A wall-clock bound includes blocking calls, credential refresh and reads.

    Socket timeouts alone do not bound DNS, trickling responses or SDK code.
    The daemon watchdog additionally bounds the caller's wait and closes the
    active transport. A timed-out write is always uncertain, never retried here.
    """

    def __init__(self, timeout_seconds: float, request_timeout_seconds: float,
                 cancelled: Callable[[], bool] = lambda: False) -> None:
        if (isinstance(timeout_seconds, bool) or isinstance(request_timeout_seconds, bool)
                or not math.isfinite(timeout_seconds) or not math.isfinite(request_timeout_seconds)
                or timeout_seconds <= 0 or request_timeout_seconds <= 0):
            raise HandoffError("INVALID_TIMEOUT")
        self.end = time.monotonic() + timeout_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.cancelled = cancelled

    def budget(self) -> float:
        if self.cancelled():
            raise HandoffError("CANCELLED")
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise HandoffError("DOWNLOAD_TIMEOUT")
        return min(remaining, self.request_timeout_seconds)

    def run(self, operation: Callable[[], _T], *, close: Callable[[], Any] | None = None) -> _T:
        until = time.monotonic() + self.budget()
        complete = threading.Event()
        result: list[_T] = []
        errors: list[BaseException] = []

        def invoke() -> None:
            try:
                self.budget()
                result.append(operation())
            except BaseException as error:  # noqa: BLE001 - worker must signal completion
                errors.append(error)
            finally:
                complete.set()

        threading.Thread(target=invoke, name="handoff-provider", daemon=True).start()
        try:
            while not complete.wait(min(0.025, max(0.0, until - time.monotonic()))):
                self.budget()
                if time.monotonic() >= until:
                    raise HandoffError("DOWNLOAD_TIMEOUT")
            self.budget()
            if errors:
                error = errors[0]
                if isinstance(error, HandoffError):
                    raise error
                # Third party errors can contain access tokens, URLs or account details.
                raise HandoffError("PROVIDER_READ_FAILED") from None
            return result[0]
        except HandoffError:
            if close is not None:
                # Closing a malicious/blocked transport must not block the caller.
                def release() -> None:
                    try:
                        close()
                    except Exception:  # noqa: BLE001, S110 - best effort transport shutdown
                        pass
                threading.Thread(target=release, name="handoff-close", daemon=True).start()
            raise


def _known_id(value: str) -> str:
    if not isinstance(value, str) or _FILE_ID.fullmatch(value) is None:
        raise HandoffError("INVALID_DRIVE_FILE_ID")
    return value


class _RequestHttp:
    """A fresh explicitly timed HTTP transport for every SDK request.

    Disable auth response replay as well as SDK retry, especially for Docs CAS
    writes. Credential refresh before the request uses the same timed transport.
    """

    def __init__(self, credentials: Any, deadline: _Deadline) -> None:
        self.credentials, self.deadline = credentials, deadline
        self._active: Any = None

    def request(self, uri: str, method: str = "GET", **kwargs: Any) -> Any:
        http = import_module("httplib2").Http(timeout=self.deadline.budget())
        self._active = http
        authorized = import_module("google_auth_httplib2").AuthorizedHttp(
            self.credentials, http=http, refresh_status_codes=(), max_refresh_attempts=0,
        )
        kwargs["redirections"] = 0
        try:
            self.deadline.budget()
            return authorized.request(uri, method=method, **kwargs)
        finally:
            http.close()

    def close(self) -> None:
        if self._active is not None:
            self._active.close()


class _BoundedCredentialBackend(_GoogleBackend):
    def __init__(self, deadline: _Deadline) -> None:
        self.deadline = deadline

    def refresh(self, credentials: Any) -> None:
        http = import_module("httplib2").Http(timeout=self.deadline.budget())
        try:
            request = import_module("google_auth_httplib2").Request(http)
            credentials.refresh(request)
        finally:
            http.close()


class _BoundedGoogleAPI:
    def __init__(self, credentials: Any, deadline: _Deadline) -> None:
        self.deadline = deadline
        self.http = _RequestHttp(credentials, deadline)
        discovery = import_module("googleapiclient.discovery")
        request_class = import_module("googleapiclient.http").HttpRequest

        def request_builder(_http: Any, *args: Any, **kwargs: Any) -> Any:
            return request_class(self.http, *args, **kwargs)

        # Bundled static documents avoid discovery HTTP during construction.
        self.drive = discovery.build("drive", "v3", http=self.http,
                                     requestBuilder=request_builder,
                                     static_discovery=True, cache_discovery=False)
        self.docs = discovery.build("docs", "v1", http=self.http,
                                    requestBuilder=request_builder,
                                    static_discovery=True, cache_discovery=False)

    def _read(self, factory: Callable[[], Any]) -> Mapping[str, Any]:
        result = self.deadline.run(lambda: factory().execute(num_retries=0), close=self.http.close)
        if not isinstance(result, Mapping):
            raise HandoffError("PROVIDER_RESPONSE_INVALID")
        return result

    def raw_metadata(self, file_id: str) -> Mapping[str, Any]:
        return self._read(lambda: self.drive.files().get(
            fileId=_known_id(file_id), fields=RAW_FILE_FIELDS, supportsAllDrives=True,
        ))

    def get_file(self, file_id: str, *, fields: str = FILE_FIELDS) -> Mapping[str, Any]:
        if fields != FILE_FIELDS:
            raise HandoffError("INVALID_METADATA_FIELDS")
        return self._read(lambda: self.drive.files().get(
            fileId=_known_id(file_id), fields=fields, supportsAllDrives=True,
        ))

    def get_document(self, file_id: str, *, include_tabs_content: bool = True) -> Mapping[str, Any]:
        if include_tabs_content is not True:
            raise HandoffError("DOCS_TAB_MISSING")
        return self._read(lambda: self.docs.documents().get(
            documentId=_known_id(file_id), includeTabsContent=True,
        ))

    def batch_update_document(self, file_id: str, *, requests: Sequence[Mapping[str, Any]],
                              required_revision_id: str) -> Mapping[str, Any]:
        if not isinstance(required_revision_id, str) or not 1 <= len(required_revision_id) <= 1024:
            raise DriveError("STALE_REVISION", "Required revision is missing")
        body = {"requests": _bounded_batch(requests),
                "writeControl": {"requiredRevisionId": required_revision_id}}

        def write() -> Any:
            try:
                return self.docs.documents().batchUpdate(
                    documentId=_known_id(file_id), body=body,
                ).execute(num_retries=0)
            except Exception as error:  # noqa: BLE001 - never expose SDK response contents
                if _status(error) in {400, 403, 404}:
                    raise HandoffError("DOCS_WRITE_REJECTED") from None
                raise HandoffError("PROVIDER_WRITE_UNCERTAIN") from None

        try:
            result = self.deadline.run(write, close=self.http.close)
        except HandoffError as error:
            code = "DOCS_WRITE_REJECTED" if error.code == "DOCS_WRITE_REJECTED" else (
                "PROVIDER_WRITE_UNCERTAIN")
            raise DriveError(code, code) from None
        if not isinstance(result, Mapping) or result.get("documentId") != file_id:
            raise DriveError("PROVIDER_WRITE_UNCERTAIN", "PROVIDER_WRITE_UNCERTAIN")
        return result


class GoogleHandoffBlobProvider:
    """Bounded streaming raw files. Parent/version/hash policy remains in delivery.

    ``metadata`` checks native Google MIME and numeric length itself, before a
    media request can occur. A second metadata read in delivery detects moves or
    version changes. Only exact IDs passed to metadata may subsequently stream.
    """

    def __init__(self, api: Any, session: Any, credentials: Any, *, limits: Limits = _DEFAULT_LIMITS,
                 timeout_seconds: float = 120, request_timeout_seconds: float = 15,
                 cancelled: Callable[[], bool] = lambda: False,
                 deadline: _Deadline | None = None) -> None:
        self.api, self.session, self.credentials = api, session, credentials
        self.limits = limits
        self.deadline = deadline or _Deadline(timeout_seconds, request_timeout_seconds, cancelled)
        self._metadata: dict[str, Mapping[str, Any]] = {}
        self._total = 0

    def metadata(self, file_id: str) -> Mapping[str, Any]:
        _known_id(file_id)
        result = self.deadline.run(lambda: self.api.raw_metadata(file_id))
        mime = result.get("mimeType") if isinstance(result, Mapping) else None
        size = result.get("size") if isinstance(result, Mapping) else None
        version = result.get("version") if isinstance(result, Mapping) else None
        parents = result.get("parents") if isinstance(result, Mapping) else None
        if (not isinstance(result, Mapping) or result.get("id") != file_id
                or result.get("trashed") is not False or not isinstance(mime, str)
                or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", mime)
                or mime.startswith("application/vnd.google-apps.")
                or not isinstance(size, str) or not re.fullmatch(r"0|[1-9][0-9]{0,18}", size)
                or not isinstance(version, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", version)
                or not isinstance(parents, list) or not parents
                or any(not isinstance(parent, str) or _FILE_ID.fullmatch(parent) is None
                       for parent in parents)):
            raise HandoffError("INVALID_DRIVE_METADATA")
        if int(size) > self.limits.file_bytes:
            raise HandoffError("FILE_TOO_LARGE")
        self._metadata[file_id] = dict(result)
        return dict(result)

    def chunks(self, file_id: str) -> Iterator[bytes]:
        _known_id(file_id)
        if file_id not in self._metadata:
            raise HandoffError("METADATA_REQUIRED")
        expected = int(self._metadata[file_id]["size"])
        response: Any = None
        headers: dict[str, str] = {}

        def open_media() -> Any:
            budget = self.deadline.budget()
            auth_request = import_module("google.auth.transport.requests").Request(self.session)

            def refresh_request(*args: Any, **kwargs: Any) -> Any:
                kwargs["timeout"] = self.deadline.budget()
                return auth_request(*args, **kwargs)

            url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
            self.credentials.before_request(refresh_request, "GET", url, headers)
            return self.session.get(url, headers=headers, stream=True,
                                    timeout=(budget, budget), allow_redirects=False)

        try:
            response = self.deadline.run(open_media, close=self.session.close)
            if response.status_code != 200:
                raise HandoffError("PROVIDER_MEDIA_REJECTED")
            # iter_content bounds memory even when Drive ignores a Range header.
            iterator = iter(response.iter_content(chunk_size=_CHUNK_BYTES))
            received = 0

            def next_part() -> bytes | None:
                return next(iterator, None)

            while True:
                chunk = self.deadline.run(next_part, close=response.close)
                if chunk is None:
                    break
                if not isinstance(chunk, bytes) or len(chunk) > _CHUNK_BYTES:
                    raise HandoffError("PROVIDER_RESPONSE_INVALID")
                if not chunk:
                    continue
                received += len(chunk)
                self._total += len(chunk)
                if received > expected or received > self.limits.file_bytes:
                    raise HandoffError("FILE_TOO_LARGE")
                if self._total > self.limits.total_bytes:
                    raise HandoffError("TOTAL_BYTES_EXCEEDED")
                yield chunk
            if received != expected:
                raise HandoffError("DOWNLOAD_SIZE_MISMATCH")
        finally:
            if response is not None:
                response.close()
            self.session.close()


class HandoffDriveFactory:
    """No access on construction; call creates one shared-budget delivery provider."""

    def __init__(self, paths: Paths, *, protected_document_ids: Iterable[str] = ()) -> None:
        self.paths = paths
        self.protected = self._protected_ids(protected_document_ids)

    @staticmethod
    def _protected_ids(values: Iterable[str]) -> frozenset[str]:
        if isinstance(values, (str, bytes)):
            raise HandoffError("INVALID_PROTECTED_DOCS_IDS")
        protected = frozenset(values)
        if any(not isinstance(value, str) or _FILE_ID.fullmatch(value) is None for value in protected):
            raise HandoffError("INVALID_PROTECTED_DOCS_IDS")
        return protected | {DRIVE_ROOT_ID, DRIVE_INDEX_ID}

    def _protect_result(self, config: DocsProviderConfig, document_id: str,
                        protected: frozenset[str]) -> None:
        if config.legacy_manifest_file is not None:
            installation = ExistingInstallation(DRIVE_ROOT_ID, DRIVE_INDEX_ID, "Chat-Dev Runner")
            manifest = LegacyInstallationManifest.from_file(config.legacy_manifest_file, installation)
            protected = protected | {item.file_id for item in manifest.files.values()}
        if document_id in protected:
            raise HandoffError("LEGACY_DOCS_MAILBOX_PROTECTED")

    def _prepare(self, config_path: Path, *, timeout_seconds: float,
                 request_timeout_seconds: float, cancelled: Callable[[], bool],
                 configuration_check: Callable[[DocsProviderConfig], None] | None = None
                 ) -> tuple[Any, _Deadline]:
        deadline = _Deadline(timeout_seconds, request_timeout_seconds, cancelled)
        try:
            config = DocsProviderConfig.from_file(Path(config_path))
            if configuration_check is not None:
                configuration_check(config)
            credentials = deadline.run(lambda: load_authorized_credentials(
                config.credential_paths(self.paths), refresh=True,
                backend=_BoundedCredentialBackend(deadline),
            ))
            return credentials, deadline
        except HandoffError:
            raise
        except Exception:  # noqa: BLE001 - references and OAuth material stay owner-local
            raise HandoffError("HANDOFF_PROVIDER_SETUP_FAILED") from None

    def __call__(self, config_path: Path, *, limits: Limits = _DEFAULT_LIMITS,
                 timeout_seconds: float = 120, request_timeout_seconds: float = 15,
                 cancelled: Callable[[], bool] = lambda: False) -> GoogleHandoffBlobProvider:
        credentials, deadline = self._prepare(
            config_path, timeout_seconds=timeout_seconds,
            request_timeout_seconds=request_timeout_seconds, cancelled=cancelled,
        )
        try:
            api = _BoundedGoogleAPI(credentials, deadline)
            session = import_module("requests").Session()
            # Do not silently inherit proxies or netrc credentials from a service environment.
            session.trust_env = False
            return GoogleHandoffBlobProvider(api, session, credentials, limits=limits,
                                             deadline=deadline)
        except Exception:  # noqa: BLE001 - optional SDK errors are sanitized
            raise HandoffError("HANDOFF_PROVIDER_SETUP_FAILED") from None

    def publisher(self, config_path: Path, *, document_id: str, tab_id: str,
                  timeout_seconds: float = 120, request_timeout_seconds: float = 15,
                  cancelled: Callable[[], bool] = lambda: False,
                  protected_document_ids: Iterable[str] = ()) -> Any:
        from runner_app.adapters.handoff_docs import TerminalHandoffPublisher

        _known_id(document_id)
        protected = self.protected | self._protected_ids(protected_document_ids)
        if document_id in protected:
            raise HandoffError("LEGACY_DOCS_MAILBOX_PROTECTED")
        credentials, deadline = self._prepare(
            config_path, timeout_seconds=timeout_seconds,
            request_timeout_seconds=request_timeout_seconds, cancelled=cancelled,
            configuration_check=lambda config: self._protect_result(config, document_id, protected),
        )
        try:
            api = _BoundedGoogleAPI(credentials, deadline)
            return TerminalHandoffPublisher(cast(Any, api), document_id=document_id, tab_id=tab_id)
        except Exception:  # noqa: BLE001 - optional SDK/config errors are sanitized
            raise HandoffError("HANDOFF_PROVIDER_SETUP_FAILED") from None
