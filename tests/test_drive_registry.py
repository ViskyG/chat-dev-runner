"""Offline exact-ID workspace registrations; no Google writes or bootstrap."""
from __future__ import annotations

import copy
import stat
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event
from typing import Any
from uuid import uuid4

import pytest

from runner_app.adapters.drive_index import (
    AttachedInstallation,
    ExistingInstallation,
    IndexedFile,
    InstallationIndex,
)
from runner_app.adapters.drive_registry import (
    DriveRegistryError,
    WorkspaceDriveRegistration,
    WorkspaceDriveRegistry,
)
from runner_app.adapters.drive_writer import RegisteredOutputDoc

_FOLDER = "application/vnd.google-apps.folder"
_DOC = "application/vnd.google-apps.document"
_PROJECTS_ID = "projectrootfolder0001"
_PROJECT_ID = "fixture"
_WORKSPACE_ID = "area"


class FakeReadProvider:
    def __init__(self) -> None:
        self.metadata: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    def get_file(self, file_id: str, *, fields: str) -> dict[str, Any]:
        assert "appProperties" in fields and "capabilities" in fields
        self.calls.append(file_id)
        return copy.deepcopy(self.metadata[file_id])


def _entry(file_id: str, parent: str, name: str, mime: str, app_key: str,
           installation: str, *, project: str = _PROJECT_ID,
           workspace: str | None = _WORKSPACE_ID) -> dict[str, Any]:
    props = {
        "cdr_installation": installation, "cdr_key": app_key, "cdr_project": project,
    }
    if workspace is not None:
        props["cdr_workspace"] = workspace
    return {
        "id": file_id, "name": name, "mimeType": mime, "parents": [parent],
        "trashed": False, "isAppAuthorized": True, "appProperties": props,
        "capabilities": {"canEdit": mime == _DOC, "canAddChildren": mime == _FOLDER},
    }


def fixture(tmp_path: Path, *, legacy: bool = False) -> tuple[
    WorkspaceDriveRegistry, WorkspaceDriveRegistration, FakeReadProvider, Path,
]:
    installation = str(uuid4())
    record = WorkspaceDriveRegistration(
        installation, _PROJECT_ID, _WORKSPACE_ID, "projectfolder000001",
        "workspacefolder0001", "commanddocument0001", "changesetdocument01",
        "receiptdocument0001", "latestdocument00001",
    )
    provider = FakeReadProvider()
    provider.metadata[_PROJECTS_ID] = _entry(
        _PROJECTS_ID, "rootfolderid0000001", "01 Projects", _FOLDER,
        "projects", installation, workspace=None,
    )
    if legacy:
        provider.metadata[_PROJECTS_ID]["isAppAuthorized"] = False
        provider.metadata[_PROJECTS_ID].pop("appProperties")
    provider.metadata[record.project_folder_id] = _entry(
        record.project_folder_id, _PROJECTS_ID, _PROJECT_ID, _FOLDER,
        "project:fixture", installation, workspace=None,
    )
    provider.metadata[record.workspace_folder_id] = _entry(
        record.workspace_folder_id, record.project_folder_id, _WORKSPACE_ID,
        _FOLDER, "workspace:fixture/area", installation,
    )
    for file_id, name, key in (
        (record.command_doc_id, "COMMAND", "workspace-command"),
        (record.changeset_doc_id, "CHANGESET", "workspace-changeset"),
        (record.receipt_doc_id, "RECEIPT", "workspace-receipt"),
        (record.latest_result_doc_id, "LATEST RESULT", "latest-result"),
    ):
        provider.metadata[file_id] = _entry(
            file_id, record.workspace_folder_id, name, _DOC, key, installation,
        )
    attached = AttachedInstallation(
        ExistingInstallation("rootfolderid0000001", "indexdocument000001", "Chat-Dev Runner"),
        InstallationIndex(installation, {
            "projects": IndexedFile("projects", _PROJECTS_ID, "01 Projects", _FOLDER,
                                    ("rootfolderid0000001",), {}),
        }, "owner_manifest" if legacy else "app_properties"),
        "index-revision", "index-hash", {},
    )
    directory = tmp_path / "private-registrations"
    return WorkspaceDriveRegistry(directory, attached, provider), record, provider, directory


def _code(call: Any, expected: str) -> None:
    with pytest.raises(DriveRegistryError) as error:
        call()
    assert error.value.code == expected


def test_registration_is_private_idempotent_and_reloadable(tmp_path: Path) -> None:
    store, record, provider, directory = fixture(tmp_path)
    assert store.register(record) == record
    assert store.register(record) == record
    assert store.get(_PROJECT_ID, _WORKSPACE_ID) == record
    assert store.get(_PROJECT_ID, _WORKSPACE_ID).mailbox().command_id == record.command_doc_id
    private_files = tuple(directory.iterdir())
    assert len(private_files) == 2
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in private_files)
    assert all(path.name.endswith(".json") for path in private_files)
    assert provider.calls and set(provider.calls) == {
        _PROJECTS_ID, record.project_folder_id, record.workspace_folder_id,
        record.command_doc_id, record.changeset_doc_id,
        record.receipt_doc_id, record.latest_result_doc_id,
    }


def test_registration_reads_independent_ids_in_bounded_parallel(tmp_path: Path) -> None:
    store, record, provider, _ = fixture(tmp_path)
    original_get_file = provider.get_file
    two_children_started = Barrier(2)

    def overlapping_get_file(file_id: str, *, fields: str) -> dict[str, Any]:
        if file_id in (record.project_folder_id, record.workspace_folder_id):
            two_children_started.wait(timeout=2)
        return original_get_file(file_id, fields=fields)

    provider.get_file = overlapping_get_file  # type: ignore[method-assign]
    assert store.register(record) == record
    assert provider.calls[0] == _PROJECTS_ID


def test_parallel_registration_keeps_first_expected_error(tmp_path: Path) -> None:
    store, record, provider, directory = fixture(tmp_path)
    provider.metadata[record.project_folder_id]["parents"] = ["wrongparent000001"]
    provider.metadata[record.workspace_folder_id]["appProperties"]["cdr_workspace"] = "wrong"
    original_get_file = provider.get_file
    later_read_finished = Event()

    def out_of_order_get_file(file_id: str, *, fields: str) -> dict[str, Any]:
        if file_id == record.project_folder_id:
            assert later_read_finished.wait(timeout=2)
        result = original_get_file(file_id, fields=fields)
        if file_id == record.workspace_folder_id:
            later_read_finished.set()
        return result

    provider.get_file = out_of_order_get_file  # type: ignore[method-assign]
    _code(lambda: store.register(record), "REGISTRATION_IDENTITY_CHANGED")
    assert list(directory.iterdir()) == []


def test_output_pair_must_match_exact_workspace_ids(tmp_path: Path) -> None:
    store, record, _, _ = fixture(tmp_path)
    store.register(record)
    receipt, latest = record.output_docs(
        receipt_key="workspace-receipt:fixture/area",
        latest_result_key="workspace-result:fixture/area",
    )
    assert store.require_output_pair(_PROJECT_ID, _WORKSPACE_ID, receipt, latest) == record
    other = RegisteredOutputDoc("other", "LATEST_RESULT", "otherdocument000001",
                                record.workspace_folder_id, "latest-result")
    _code(lambda: store.require_output_pair(_PROJECT_ID, _WORKSPACE_ID, receipt, other),
          "OUTPUT_SCOPE_MISMATCH")
    _code(lambda: store.require_output_pair("another", _WORKSPACE_ID, receipt, latest),
          "REGISTRATION_UNAVAILABLE")


def test_remote_move_scope_drift_or_missing_grant_fail_closed(tmp_path: Path) -> None:
    store, record, provider, directory = fixture(tmp_path)
    provider.metadata[record.receipt_doc_id]["parents"] = ["anotherfolder000001"]
    _code(lambda: store.register(record), "REGISTRATION_IDENTITY_CHANGED")
    assert list(directory.iterdir()) == []
    provider.metadata[record.receipt_doc_id]["parents"] = [record.workspace_folder_id]
    store.register(record)
    provider.metadata[record.latest_result_doc_id]["appProperties"]["cdr_workspace"] = "other"
    _code(lambda: store.get(_PROJECT_ID, _WORKSPACE_ID), "REGISTRATION_SCOPE_CHANGED")
    provider.metadata[record.latest_result_doc_id]["appProperties"]["cdr_workspace"] = "area"
    provider.metadata[record.latest_result_doc_id]["isAppAuthorized"] = False
    _code(lambda: store.get(_PROJECT_ID, _WORKSPACE_ID), "REGISTRATION_IDENTITY_CHANGED")


def test_different_registration_never_replaces_existing_record(tmp_path: Path) -> None:
    store, record, provider, directory = fixture(tmp_path)
    store.register(record)
    original = {path.name: path.read_bytes() for path in directory.iterdir()}
    changed = replace(record, latest_result_doc_id="newlatestdocument001")
    provider.metadata[changed.latest_result_doc_id] = _entry(
        changed.latest_result_doc_id, record.workspace_folder_id, "LATEST RESULT",
        _DOC, "latest-result", record.installation_id,
    )
    _code(lambda: store.register(changed), "REGISTRATION_CONFLICT")
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == original
    assert store.get(_PROJECT_ID, _WORKSPACE_ID) == record


def test_corrupt_private_record_is_refused_without_remote_guess(tmp_path: Path) -> None:
    store, record, provider, directory = fixture(tmp_path)
    store.register(record)
    private_file = next(path for path in directory.iterdir()
                        if not path.name.startswith("project-"))
    private_file.chmod(0o644)
    before = len(provider.calls)
    _code(lambda: store.get(_PROJECT_ID, _WORKSPACE_ID), "REGISTRATION_INVALID")
    assert len(provider.calls) == before


def test_registration_cannot_reuse_installed_project_root(tmp_path: Path) -> None:
    store, record, provider, directory = fixture(tmp_path)
    conflict = replace(record, project_folder_id=_PROJECTS_ID)
    _code(lambda: store.register(conflict), "REGISTRATION_ID_CONFLICT")
    assert provider.calls == [_PROJECTS_ID]
    assert list(directory.iterdir()) == []


def test_moved_indexed_projects_root_is_refused_before_child_lookup(tmp_path: Path) -> None:
    store, record, provider, directory = fixture(tmp_path)
    provider.metadata[_PROJECTS_ID]["parents"] = ["anotherrootfolder0001"]
    _code(lambda: store.register(record), "PROJECT_ROOT_CHANGED")
    assert provider.calls == [_PROJECTS_ID]
    assert list(directory.iterdir()) == []
    provider.metadata[_PROJECTS_ID]["parents"] = ["rootfolderid0000001"]
    store.register(record)
    provider.metadata[_PROJECTS_ID]["appProperties"]["cdr_key"] = "other"
    _code(lambda: store.get(_PROJECT_ID, _WORKSPACE_ID), "PROJECT_ROOT_CHANGED")


def test_same_project_cannot_register_two_different_project_folders(tmp_path: Path) -> None:
    store, record, provider, directory = fixture(tmp_path)
    store.register(record)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    other = replace(
        record, workspace_id="other", project_folder_id="otherprojectfolder01",
        workspace_folder_id="otherworkspacefolder01", command_doc_id="othercommanddoc0001",
        changeset_doc_id="otherchangesetdoc01", receipt_doc_id="otherreceiptdoc0001",
        latest_result_doc_id="otherlatestdoc00001",
    )
    provider.metadata[other.project_folder_id] = _entry(
        other.project_folder_id, _PROJECTS_ID, _PROJECT_ID, _FOLDER,
        "project:fixture", record.installation_id, workspace=None,
    )
    provider.metadata[other.workspace_folder_id] = _entry(
        other.workspace_folder_id, other.project_folder_id, "other", _FOLDER,
        "workspace:fixture/other", record.installation_id, workspace="other",
    )
    for file_id, name, key in (
        (other.command_doc_id, "COMMAND", "workspace-command"),
        (other.changeset_doc_id, "CHANGESET", "workspace-changeset"),
        (other.receipt_doc_id, "RECEIPT", "workspace-receipt"),
        (other.latest_result_doc_id, "LATEST RESULT", "latest-result"),
    ):
        provider.metadata[file_id] = _entry(
            file_id, other.workspace_folder_id, name, _DOC, key,
            record.installation_id, workspace="other",
        )
    _code(lambda: store.register(other), "PROJECT_REGISTRATION_CONFLICT")
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    assert store.get(_PROJECT_ID, _WORKSPACE_ID) == record


def test_legacy_projects_parent_allows_current_app_child_registration(tmp_path: Path) -> None:
    store, record, provider, _ = fixture(tmp_path, legacy=True)
    assert store.register(record) == record
    assert store.get(_PROJECT_ID, _WORKSPACE_ID) == record
    provider.metadata[record.command_doc_id]["isAppAuthorized"] = False
    _code(lambda: store.get(_PROJECT_ID, _WORKSPACE_ID), "REGISTRATION_IDENTITY_CHANGED")


def test_legacy_projects_parent_rejects_movement_and_visible_property_conflict(
    tmp_path: Path,
) -> None:
    store, record, provider, directory = fixture(tmp_path, legacy=True)
    projects = provider.metadata[_PROJECTS_ID]
    projects["parents"] = ["other-root-folder-123"]
    _code(lambda: store.register(record), "PROJECT_ROOT_CHANGED")
    assert list(directory.iterdir()) == []
    projects["parents"] = ["rootfolderid0000001"]
    projects["appProperties"] = {"cdr_key": "results"}
    _code(lambda: store.register(record), "PROJECT_ROOT_CHANGED")
    projects["appProperties"] = {"cdr_key": "projects"}
    _code(lambda: store.register(record), "PROJECT_ROOT_CHANGED")
    assert list(directory.iterdir()) == []
