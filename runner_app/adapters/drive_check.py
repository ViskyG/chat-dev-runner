"""Explicit, read-only live attachment probe for the existing Drive installation.

The ordinary CLI remains offline. Google services are imported only after
``runner drive-check --live`` is requested; no Drive resources are created,
mailboxes polled, or runner state armed.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

from runner_app.adapters.drive_auth import load_authorized_credentials
from runner_app.adapters.drive_docs import DriveError, ReadProvider
from runner_app.adapters.drive_google import GoogleReadProvider
from runner_app.adapters.drive_index import (
    ExistingInstallation,
    LegacyInstallationManifest,
    attach_existing,
)
from runner_app.config import DRIVE_INDEX_ID, DRIVE_ROOT_ID
from runner_app.storage.paths import Paths

_LEGACY_MANIFEST = "installation-legacy.json"


def _selected_installation(config_path: Path | None) -> ExistingInstallation:
    if config_path is not None:
        return ExistingInstallation.from_file(config_path)
    # These source-approved IDs are packaged with the runner. A wheel need not
    # contain the editable checkout's config/ directory.
    return ExistingInstallation.from_mapping({
        "schema_version": "1.0", "mode": "attach_existing",
        "bootstrap_allowed": False, "root_folder_name": "Chat-Dev Runner",
        "root_folder_id": DRIVE_ROOT_ID,
        "installation_index_document_id": DRIVE_INDEX_ID,
    })


def check_existing_installation(
    provider: ReadProvider, paths: Paths, *, config_path: Path | None = None,
) -> dict[str, object]:
    """Validate approved IDs and the private legacy map without writing to Drive."""
    config = _selected_installation(config_path)
    manifest = LegacyInstallationManifest.from_file(
        paths.config_dir / _LEGACY_MANIFEST, config,
    )
    attached = attach_existing(provider, config, legacy_manifest=manifest)
    return {
        "status": "ATTACHED",
        "mode": "LIVE_READ_ONLY",
        "identity_mode": attached.index.identity_mode,
        "indexed_roles": len(attached.index.files),
        "installation_index_text_sha256": attached.index_text_sha256,
        "cloud_writes": False,
        "armed": False,
    }


def _build_provider(credentials: Any) -> GoogleReadProvider:
    try:
        build = import_module("googleapiclient.discovery").build
        drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        docs = build("docs", "v1", credentials=credentials, cache_discovery=False)
    except Exception:  # noqa: BLE001 - third-party exceptions may contain OAuth material
        raise DriveError("GOOGLE_SERVICE_UNAVAILABLE", "Google services are unavailable") from None
    return GoogleReadProvider(drive, docs)


def live_drive_check(
    paths: Paths | None = None, *, config_path: Path | None = None,
) -> dict[str, object]:
    """Use stored full-scope OAuth for one exact-ID read-only probe.

    An expired token may be refreshed in the private token store. No remote
    Drive or Docs write method is invoked.
    """
    selected_paths = paths if paths is not None else Paths.discover()
    credentials = load_authorized_credentials(selected_paths, refresh=True)
    provider = _build_provider(credentials)
    return check_existing_installation(provider, selected_paths, config_path=config_path)
