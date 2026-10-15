"""Lazy, reusable Docs channel setup without changing legacy mailbox slots."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from runner_app.adapters.drive_auth import load_authorized_credentials
from runner_app.adapters.drive_check import _build_provider
from runner_app.adapters.drive_index import AttachedInstallation
from runner_app.adapters.drive_mailbox_v2 import (
    DocsMailboxError,
    DocsMailboxPort,
    DocsProviderConfig,
    DriveDocsAPI,
)
from runner_app.config import DRIVE_INDEX_ID, DRIVE_ROOT_ID
from runner_app.storage.paths import Paths


class _MailboxConfig(Protocol):
    """Structural surface of config_v2.MailboxConfig; no runtime config import."""

    @property
    def provider(self) -> str: ...

    @property
    def coordinates(self) -> Mapping[str, Any]: ...


Fingerprint = tuple[tuple[int, ...] | None, ...]


@dataclass(frozen=True)
class _Prepared:
    config: DocsProviderConfig
    fingerprint: Fingerprint
    provider: DriveDocsAPI
    attached: AttachedInstallation
    protected: frozenset[str]


def _fingerprint(path: Path, config: DocsProviderConfig) -> Fingerprint:
    result: list[tuple[int, ...] | None] = []
    for reference in (path, config.oauth_client_file, config.token_file, config.legacy_manifest_file):
        if reference is None:
            result.append(None)
        else:
            info = reference.stat(follow_symlinks=False)
            result.append((info.st_dev, info.st_ino, info.st_mode, info.st_size,
                           info.st_mtime_ns, info.st_ctime_ns, info.st_nlink))
    return tuple(result)


class DocsPortFactory:
    """Attach once per unchanged owner config; construct only enrolled v2 ports.

    Construction performs no filesystem, credential or provider access. Explicit
    calls load existing credentials with refresh=True, create existing services,
    and validate the approved installation read-only. No setup path writes a Doc.
    Pass known v1 registry document IDs through protected_document_ids; every
    attached installation role is always protected independently of that list.
    """

    def __init__(self, paths: Paths, *, protected_document_ids: Iterable[str] = ()) -> None:
        if isinstance(protected_document_ids, (str, bytes)):
            raise DocsMailboxError("INVALID_PROTECTED_DOCS_IDS")
        protected = frozenset(protected_document_ids)
        if any(not isinstance(value, str) or not value for value in protected):
            raise DocsMailboxError("INVALID_PROTECTED_DOCS_IDS")
        self.paths = paths
        self._protected = protected | {DRIVE_ROOT_ID, DRIVE_INDEX_ID}
        self._cache: dict[Path, _Prepared] = {}
        self._lock = threading.RLock()

    def _prepare(self, path: Path, config: DocsProviderConfig, before: Fingerprint) -> _Prepared:
        try:
            credentials = load_authorized_credentials(config.credential_paths(self.paths), refresh=True)
            # Refresh may replace the private token. Other selected config inputs
            # must remain unchanged while credentials are being constructed.
            loaded = _fingerprint(path, config)
            if any(before[index] != loaded[index] for index in (0, 1, 3)):
                raise DocsMailboxError("DOCS_CONFIG_CHANGED_DURING_SETUP")
            provider = _build_provider(credentials)
            attached = config.attach(provider)
            after_config = DocsProviderConfig.from_file(path)
            after = _fingerprint(path, after_config)
            if after_config != config or after != loaded:
                raise DocsMailboxError("DOCS_CONFIG_CHANGED_DURING_SETUP")
            protected = self._protected | {item.file_id for item in attached.index.files.values()}
            return _Prepared(config, after, provider, attached, frozenset(protected))
        except DocsMailboxError:
            raise
        except Exception:  # noqa: BLE001 - OAuth/provider errors may contain credential material
            raise DocsMailboxError("DOCS_CHANNEL_SETUP_FAILED") from None

    def __call__(self, config_path: Path, mailbox: _MailboxConfig) -> DocsMailboxPort:
        coordinates = mailbox.coordinates
        if (mailbox.provider != "drive" or not isinstance(coordinates, Mapping)
                or set(coordinates) != {"document_id", "tab_id"}
                or not isinstance(coordinates["document_id"], str)
                or not isinstance(coordinates["tab_id"], str)):
            raise DocsMailboxError("INVALID_DOCS_MAILBOX_BINDING")
        document_id, tab_id = coordinates["document_id"], coordinates["tab_id"]
        if document_id in self._protected:
            raise DocsMailboxError("LEGACY_DOCS_MAILBOX_PROTECTED")
        path = Path(config_path)
        if not path.is_absolute() or ".." in path.parts:
            raise DocsMailboxError("DOCS_CONFIG_INVALID")
        with self._lock:
            config = DocsProviderConfig.from_file(path)
            current = _fingerprint(path, config)
            prepared = self._cache.get(path)
            if prepared is None or prepared.config != config or prepared.fingerprint != current:
                self._cache.pop(path, None)
                prepared = self._prepare(path, config, current)
                self._cache[path] = prepared
            if document_id in prepared.protected:
                raise DocsMailboxError("LEGACY_DOCS_MAILBOX_PROTECTED")
            port = DocsMailboxPort(prepared.provider, document_id=document_id, tab_id=tab_id)
            port.read_current()  # Exact target/tab preflight; provider setup remains cached.
            return port
