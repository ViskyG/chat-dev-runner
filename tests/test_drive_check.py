"""Fake-backed live attachment entrypoint; these tests never contact Google."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from runner_app import cli
from runner_app.adapters import drive_check
from runner_app.adapters.drive_docs import DriveError
from runner_app.adapters.drive_fake import FakeDrive
from runner_app.config import DRIVE_INDEX_ID, DRIVE_ROOT_ID
from runner_app.storage.paths import Paths

ROOT = Path(__file__).resolve().parents[1]
SEED = json.loads((ROOT / "drive-seed/bootstrap-manifest.json").read_text())
LEGACY_ROLES = {
    "root", "control", "projects", "results", "contracts", "templates",
    "start-here", "chat-protocol", "command", "receipt", "state", "recipes",
    "installation-index",
}


def fake_legacy_installation(tmp_path: Path) -> tuple[FakeDrive, Paths, str]:
    provider = FakeDrive()
    paths = Paths(tmp_path / "config" / "chat-dev-runner", tmp_path / "data", tmp_path / "cache")
    paths.config_dir.mkdir(parents=True, mode=0o700)
    ids = {
        entry["key"]: (
            DRIVE_ROOT_ID if entry["key"] == "root" else
            DRIVE_INDEX_ID if entry["key"] == "installation-index" else
            "fake-" + hashlib.sha256(entry["key"].encode()).hexdigest()[:24]
        )
        for entry in SEED["entries"] if entry["key"] in LEGACY_ROLES
    }
    documents = {}
    for entry in SEED["entries"]:
        key = entry["key"]
        if key not in LEGACY_ROLES:
            continue
        mime = ("application/vnd.google-apps.folder" if entry["kind"] == "folder" else
                "application/vnd.google-apps.document")
        metadata = {
            "id": ids[key], "name": entry["name"], "mimeType": mime,
            "parents": [ids[entry["parent"]]] if entry.get("parent") else ["my-drive-root"],
            "trashed": False, "isAppAuthorized": False,
            "capabilities": {"canEdit": True, "canAddChildren": True},
            "version": "1",
        }
        provider.put_file(metadata)
        documents[key] = {
            "id": metadata["id"], "name": metadata["name"],
            "mimeType": metadata["mimeType"], "parents": metadata["parents"],
        }
    text = "CHAT-DEV RUNNER — INSTALLATION INDEX\n" + "".join(
        f"{key}: {ids[key]}\n" for key in sorted(LEGACY_ROLES)
    )
    provider.put_document(DRIVE_INDEX_ID, text, revision="legacy-index-rev-1")
    digest = hashlib.sha256(text.encode()).hexdigest()
    manifest = {
        "schema_version": "1.0", "mode": "owner_verified_legacy",
        "root_folder_id": DRIVE_ROOT_ID,
        "installation_index_document_id": DRIVE_INDEX_ID,
        "installation_index_text_sha256": digest,
        "installation_id": str(uuid4()), "documents": documents,
    }
    path = paths.config_dir / "installation-legacy.json"
    path.write_text(json.dumps(manifest))
    path.chmod(0o600)
    return provider, paths, digest


def test_fake_backed_check_reads_exact_existing_ids_only(tmp_path: Path) -> None:
    provider, paths, digest = fake_legacy_installation(tmp_path)
    result = drive_check.check_existing_installation(provider, paths)
    assert result == {
        "status": "ATTACHED", "mode": "LIVE_READ_ONLY",
        "identity_mode": "owner_manifest", "indexed_roles": len(LEGACY_ROLES),
        "installation_index_text_sha256": digest,
        "cloud_writes": False, "armed": False,
    }
    expected_ids = {
        value["id"] for value in json.loads(
            (paths.config_dir / "installation-legacy.json").read_text()
        )["documents"].values()
    }
    assert {file_id for method, file_id in provider.calls if method == "files.get"} == expected_ids
    assert {method for method, _ in provider.calls} == {"files.get", "documents.get"}
    assert {file_id for method, file_id in provider.calls if method == "documents.get"} == {
        DRIVE_INDEX_ID,
    }



def test_default_selection_never_needs_checkout_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, paths, digest = fake_legacy_installation(tmp_path)

    def forbidden_file_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("An installed wheel must not require config/ from the source tree")

    monkeypatch.setattr(drive_check.ExistingInstallation, "from_file", forbidden_file_read)
    result = drive_check.check_existing_installation(provider, paths)
    assert result["installation_index_text_sha256"] == digest


def test_explicit_config_cannot_select_another_installation(tmp_path: Path) -> None:
    provider, paths, _ = fake_legacy_installation(tmp_path)
    config = tmp_path / "another-installation.json"
    config.write_text(json.dumps({
        "schema_version": "1.0", "mode": "attach_existing",
        "bootstrap_allowed": False, "root_folder_name": "Chat-Dev Runner",
        "root_folder_id": "another-root", "installation_index_document_id": DRIVE_INDEX_ID,
    }))
    with pytest.raises(DriveError) as error:
        drive_check.check_existing_installation(provider, paths, config_path=config)
    assert error.value.code == "INSTALLATION_ID_MISMATCH"

def test_missing_or_changed_owner_manifest_fails_closed(tmp_path: Path) -> None:
    provider, paths, _ = fake_legacy_installation(tmp_path)
    manifest = paths.config_dir / "installation-legacy.json"
    manifest.chmod(0o644)
    with pytest.raises(DriveError) as error:
        drive_check.check_existing_installation(provider, paths)
    assert error.value.code == "LEGACY_MANIFEST_INVALID"
    manifest.chmod(0o600)
    provider.put_document(DRIVE_INDEX_ID, "Changed Index\n", revision="rev-2")
    with pytest.raises(DriveError) as error:
        drive_check.check_existing_installation(provider, paths)
    assert error.value.code == "INDEX_TEXT_MISMATCH"


def test_live_entrypoint_uses_stored_oauth_and_fake_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, paths, digest = fake_legacy_installation(tmp_path)
    credential = object()
    observations: list[object] = []

    def fake_auth(selected: Paths, *, refresh: bool) -> object:
        observations.extend((selected, refresh))
        return credential

    def fake_builder(value: object) -> FakeDrive:
        observations.append(value)
        return provider

    monkeypatch.setattr(drive_check, "load_authorized_credentials", fake_auth)
    monkeypatch.setattr(drive_check, "_build_provider", fake_builder)
    result = drive_check.live_drive_check(paths)
    assert observations == [paths, True, credential]
    assert result["installation_index_text_sha256"] == digest
    assert result["cloud_writes"] is False


def test_cli_live_check_is_explicit_and_errors_are_sanitized(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        cli.parser().parse_args(["drive-check"])
    assert error.value.code == 2
    capsys.readouterr()
    monkeypatch.setattr(drive_check, "live_drive_check", lambda: {
        "status": "ATTACHED", "mode": "LIVE_READ_ONLY", "indexed_roles": 13,
    })
    assert cli.main(["drive-check", "--live"]) == 0
    assert json.loads(capsys.readouterr().out)["indexed_roles"] == 13

    def fail() -> dict[str, object]:
        raise DriveError("INDEX_TEXT_MISMATCH", "secret provider detail")

    monkeypatch.setattr(drive_check, "live_drive_check", fail)
    assert cli.main(["drive-check", "--live"]) == 2
    printed = capsys.readouterr().out
    assert json.loads(printed) == {"error": "INDEX_TEXT_MISMATCH"}
    assert "secret" not in printed
