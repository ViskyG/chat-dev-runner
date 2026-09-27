"""Offline-only tests: owner-supplied IDs, never a real account or bootstrap apply."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from runner_app.adapters.drive_docs import DriveError
from runner_app.adapters.drive_fake import FakeDrive
from runner_app.adapters.drive_index import (
    ExistingInstallation,
    attach_existing,
    list_children_bounded,
    parse_installation_index,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "drive-seed/bootstrap-manifest.json").read_text())
CONFIG = ROOT / "config/installation.existing.json"
DOC = "application/vnd.google-apps.document"
FOLDER = "application/vnd.google-apps.folder"


def installed_fixture() -> tuple[FakeDrive, ExistingInstallation, dict]:
    config = ExistingInstallation.from_file(CONFIG)
    provider = FakeDrive()
    installation_id = str(uuid4())
    IDs = {
        entry["key"]: config.root_id if entry["key"] == "root" else
        config.index_id if entry["key"] == "installation-index" else
        "fake-" + hashlib.sha256(entry["key"].encode()).hexdigest()[:24]
        for entry in MANIFEST["entries"]
    }
    records = {}
    for entry in MANIFEST["entries"]:
        key = entry["key"]
        kind = entry["kind"]
        mime = (
            FOLDER if kind == "folder" else
            DOC if kind == "document" else entry["mime_type"]
        )
        parent = entry.get("parent")
        metadata = {
            "id": IDs[key], "name": entry["name"], "mimeType": mime,
            "parents": [IDs[parent]] if parent else ["my-drive-root"],
            "trashed": False, "isAppAuthorized": True,
            "capabilities": {"canEdit": True, "canAddChildren": True},
            "appProperties": {"cdr_installation": installation_id, "cdr_key": key},
            "version": "1",
        }
        records[key] = copy.deepcopy(metadata)
        provider.put_file(metadata)
        if kind == "document" and key != "installation-index":
            text = (ROOT / entry["source"]).read_text() if "source" in entry else ""
            provider.put_document(IDs[key], text)
    index_text = (
        "CHAT-DEV RUNNER / INSTALLATION INDEX\n\n"
        "Runner is NOT armed. No READY commands were created.\n\n"
        + json.dumps({"installation_id": installation_id, "documents": records}, indent=2)
    )
    provider.put_document(config.index_id, index_text, revision="index-rev-1")
    return provider, config, records


def test_attach_existing_validates_all_indexed_ids_without_title_search() -> None:
    provider, config, records = installed_fixture()
    attached = attach_existing(provider, config)
    assert set(attached.index.files) == set(records)
    assert attached.index.files["root"].file_id == config.root_id
    assert attached.index.files["installation-index"].file_id == config.index_id
    assert attached.root_mailbox().command_id == records["command"]["id"]
    assert attached.root_mailbox().receipt_id == records["receipt"]["id"]
    assert attached.index_revision == "index-rev-1"
    assert all(call[0] in {"files.get", "documents.get"} for call in provider.calls)
    assert sum(call[0] == "files.list" for call in provider.calls) == 0


def test_owner_ids_and_installation_mode_cannot_be_replaced() -> None:
    _, config, _ = installed_fixture()
    value = json.loads(CONFIG.read_text())
    value["root_folder_id"] = "another-root"
    with pytest.raises(DriveError) as error:
        ExistingInstallation.from_mapping(value)
    assert error.value.code == "INSTALLATION_ID_MISMATCH"
    value["root_folder_id"] = config.root_id
    value["bootstrap_allowed"] = True
    with pytest.raises(DriveError) as error:
        ExistingInstallation.from_mapping(value)
    assert error.value.code == "INSTALLATION_CONFIG_INVALID"


def test_missing_per_file_grant_does_not_fall_back_to_folder_access() -> None:
    provider, config, records = installed_fixture()
    command_id = records["command"]["id"]
    provider.denied_files.add(command_id)
    with pytest.raises(DriveError) as error:
        attach_existing(provider, config)
    assert error.value.code == "PER_FILE_ACCESS_MISSING"
    assert ("files.list", records["control"]["id"]) not in provider.calls

    provider.denied_files.clear()
    provider.files[command_id]["isAppAuthorized"] = False
    with pytest.raises(DriveError) as error:
        attach_existing(provider, config)
    assert error.value.code == "PER_FILE_GRANT_MISSING"


@pytest.mark.parametrize("key,capability,expected", [
    ("receipt", "canEdit", "WRITE_CAPABILITY_MISSING"),
    ("installation-index", "canEdit", "WRITE_CAPABILITY_MISSING"),
    ("projects", "canAddChildren", "CHILD_CREATE_CAPABILITY_MISSING"),
    ("results", "canAddChildren", "CHILD_CREATE_CAPABILITY_MISSING"),
])
def test_attach_checks_needed_write_and_child_create_capabilities(
    key: str, capability: str, expected: str,
) -> None:
    provider, config, records = installed_fixture()
    provider.files[records[key]["id"]]["capabilities"][capability] = False
    with pytest.raises(DriveError) as error:
        attach_existing(provider, config)
    assert error.value.code == expected


def test_moved_file_is_configuration_fault_not_name_lookup() -> None:
    provider, config, records = installed_fixture()
    provider.files[records["command"]["id"]]["parents"] = [records["templates"]["id"]]
    with pytest.raises(DriveError) as error:
        attach_existing(provider, config)
    assert error.value.code == "FILE_MOVED"
    assert not any(call[0] == "files.list" for call in provider.calls)


def test_index_rejects_duplicate_ids_and_missing_required_mapping() -> None:
    provider, config, records = installed_fixture()
    index = provider.documents[config.index_id]
    content = index["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0]["textRun"]
    value = json.loads(content["content"].split("\n\n", 2)[-1])
    value["documents"]["receipt"]["id"] = value["documents"]["command"]["id"]
    with pytest.raises(DriveError) as error:
        parse_installation_index(
            "CHAT-DEV RUNNER / INSTALLATION INDEX\n\n" + json.dumps(value), config
        )
    assert error.value.code == "DUPLICATE_INDEX_ID"
    del value["documents"]["state"]
    with pytest.raises(DriveError) as error:
        parse_installation_index(
            "CHAT-DEV RUNNER / INSTALLATION INDEX\n\n" + json.dumps(value), config
        )
    assert error.value.code == "INDEX_LAYOUT_INCOMPLETE"
    assert records["root"]["id"] == config.root_id


def test_index_rejects_wrong_self_id_and_multitab_document() -> None:
    provider, config, _ = installed_fixture()
    doc = provider.documents[config.index_id]
    text = doc["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0]["textRun"]["content"]
    value = json.loads(text.split("\n\n", 2)[-1])
    value["documents"]["installation-index"]["id"] = "fake-different-index-id"
    with pytest.raises(DriveError) as error:
        parse_installation_index(
            "CHAT-DEV RUNNER / INSTALLATION INDEX\n\n" + json.dumps(value), config
        )
    assert error.value.code == "INSTALLATION_ID_MISMATCH"
    doc["tabs"].append(copy.deepcopy(doc["tabs"][0]))
    with pytest.raises(DriveError) as error:
        attach_existing(provider, config)
    assert error.value.code == "UNSUPPORTED_TABS"


def test_duplicate_titles_do_not_override_exact_registered_id() -> None:
    provider, config, records = installed_fixture()
    extra = copy.deepcopy(records["command"])
    extra["id"] = "fake-duplicate-command-id"
    provider.put_file(extra)
    attached = attach_existing(provider, config)
    assert attached.root_mailbox().command_id == records["command"]["id"]
    listed = list_children_bounded(provider, records["control"]["id"], page_size=2)
    same_title = [item for item in listed if item["name"] == "COMMAND"]
    assert {item["id"] for item in same_title} == {
        records["command"]["id"], extra["id"],
    }


def test_paginated_listing_has_hard_cap_and_detects_repeated_cursor() -> None:
    provider, _, records = installed_fixture()
    parent = records["contracts"]["id"]
    listed = list_children_bounded(provider, parent, page_size=2)
    assert len(listed) >= 7
    with pytest.raises(DriveError) as error:
        list_children_bounded(provider, parent, page_size=1, max_pages=1)
    assert error.value.code == "PAGINATION_PAGE_LIMIT"

    class Repeating:
        def list_children(self, _parent, *, page_token=None, page_size=100):
            return {"files": [], "nextPageToken": "same"}

    with pytest.raises(DriveError) as error:
        list_children_bounded(Repeating(), parent, max_pages=3)
    assert error.value.code == "PAGINATION_INVALID"


def test_missing_private_app_properties_blocks_attachment_even_with_visible_index() -> None:
    provider, config, records = installed_fixture()
    provider.files[records["command"]["id"]].pop("appProperties")
    with pytest.raises(DriveError) as error:
        attach_existing(provider, config)
    assert error.value.code == "APP_IDENTITY_UNVERIFIED"


def test_wrong_private_app_properties_block_impersonated_index_key() -> None:
    provider, config, records = installed_fixture()
    provider.files[records["command"]["id"]]["appProperties"]["cdr_key"] = "receipt"
    with pytest.raises(DriveError) as error:
        attach_existing(provider, config)
    assert error.value.code == "APP_IDENTITY_MISMATCH"


def test_index_edit_during_metadata_scan_aborts_attachment() -> None:
    provider, config, _ = installed_fixture()

    def mutate(fake: FakeDrive, count: int) -> None:
        if count == 2:
            previous = fake.documents[config.index_id]["tabs"][0]["documentTab"]["body"][
                "content"
            ][0]["paragraph"]["elements"][0]["textRun"]["content"]
            fake.put_document(config.index_id, previous + " ", revision="index-rev-2")

    provider.on_document_read[config.index_id] = mutate
    with pytest.raises(DriveError) as error:
        attach_existing(provider, config)
    assert error.value.code == "INPUT_CHANGED"


def test_file_get_requests_app_property_and_capability_fields() -> None:
    from runner_app.adapters.drive_docs import FILE_FIELDS

    assert "isAppAuthorized" in FILE_FIELDS
    assert "appProperties" in FILE_FIELDS
    assert "canEdit" in FILE_FIELDS and "canAddChildren" in FILE_FIELDS
    assert "version" in FILE_FIELDS and "parents" in FILE_FIELDS
    assert "headRevisionId" not in FILE_FIELDS
