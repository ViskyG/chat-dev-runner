"""Offline-only tests: owner-supplied IDs, never a real account or bootstrap apply."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from runner_app.adapters.drive_docs import DriveError, RegisteredMailbox, read_ready_snapshot
from runner_app.adapters.drive_fake import FakeDrive
from runner_app.adapters.drive_index import (
    ExistingInstallation,
    LegacyInstallationManifest,
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


LEGACY_ROLES = {
    "root", "control", "projects", "results", "contracts", "templates",
    "start-here", "chat-protocol", "command", "receipt", "state", "recipes",
    "installation-index",
}


def legacy_fixture() -> tuple[FakeDrive, ExistingInstallation, dict, dict]:
    provider, config, records = installed_fixture()
    installation_id = records["root"]["appProperties"]["cdr_installation"]
    text = (
        "CHAT-DEV RUNNER — INSTALLATION INDEX\n"
        "This installed workspace has a human-readable guide, without a JSON file map.\n"
        + "".join(f"{key}: {records[key]['id']}\n" for key in sorted(LEGACY_ROLES))
    )
    provider.put_document(config.index_id, text, revision="legacy-index-rev-1")
    provider.files[records["start-here"]["id"]]["name"] = "START HERE — Chat-Dev Runner"
    documents = {}
    for key in LEGACY_ROLES:
        metadata = provider.files[records[key]["id"]]
        documents[key] = {
            "id": metadata["id"], "name": metadata["name"],
            "mimeType": metadata["mimeType"], "parents": list(metadata["parents"]),
        }
        metadata.pop("appProperties", None)
        metadata["isAppAuthorized"] = False
    mapping = {
        "schema_version": "1.0", "mode": "owner_verified_legacy",
        "root_folder_id": config.root_id,
        "installation_index_document_id": config.index_id,
        "installation_index_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "installation_id": installation_id, "documents": documents,
    }
    return provider, config, records, mapping


def test_owner_manifest_attaches_existing_human_index_without_title_routing() -> None:
    provider, config, records, mapping = legacy_fixture()
    manifest = LegacyInstallationManifest.from_mapping(mapping, config)
    attached = attach_existing(provider, config, legacy_manifest=manifest)
    assert attached.index.identity_mode == "owner_manifest"
    assert set(attached.index.files) == LEGACY_ROLES
    assert attached.root_mailbox().command_id == records["command"]["id"]
    assert attached.root_mailbox().legacy_attachment is attached
    assert attached.index_text_sha256 == mapping["installation_index_text_sha256"]
    assert all(call[0] in {"files.get", "documents.get"} for call in provider.calls)
    with pytest.raises(DriveError) as error:
        attach_existing(provider, config)
    assert error.value.code == "INDEX_HEADER_INVALID"


def test_owner_manifest_pins_index_text_and_rechecks_revision() -> None:
    provider, config, _, mapping = legacy_fixture()
    mapping["installation_index_text_sha256"] = "0" * 64
    with pytest.raises(DriveError) as error:
        attach_existing(
            provider, config, legacy_manifest=LegacyInstallationManifest.from_mapping(mapping, config),
        )
    assert error.value.code == "INDEX_TEXT_MISMATCH"
    provider, config, _, mapping = legacy_fixture()
    manifest = LegacyInstallationManifest.from_mapping(mapping, config)

    def mutate(fake: FakeDrive, count: int) -> None:
        if count == 2:
            fake.put_document(config.index_id, "Changed after first read\n", revision="legacy-index-rev-2")

    provider.on_document_read[config.index_id] = mutate
    with pytest.raises(DriveError) as error:
        attach_existing(provider, config, legacy_manifest=manifest)
    assert error.value.code == "INPUT_CHANGED"


@pytest.mark.parametrize("change,expected", [
    ("moved", "FILE_MOVED"),
    ("trashed", "FILE_UNAVAILABLE"),
    ("renamed", "FILE_METADATA_MISMATCH"),
    ("no_edit", "WRITE_CAPABILITY_MISSING"),
    ("conflicting_properties", "APP_IDENTITY_MISMATCH"),
    ("partial_properties", "APP_IDENTITY_MISMATCH"),
])
def test_owner_manifest_rejects_drift_and_visible_identity_conflicts(
    change: str, expected: str,
) -> None:
    provider, config, records, mapping = legacy_fixture()
    target = provider.files[records["command"]["id"]]
    if change == "moved":
        target["parents"] = [records["projects"]["id"]]
    elif change == "trashed":
        target["trashed"] = True
    elif change == "renamed":
        target["name"] = "OTHER COMMAND"
    elif change == "no_edit":
        target["capabilities"]["canEdit"] = False
    elif change == "partial_properties":
        target["appProperties"] = {"cdr_key": "command"}
    else:
        target["appProperties"] = {"cdr_key": "receipt"}
    with pytest.raises(DriveError) as error:
        attach_existing(
            provider, config, legacy_manifest=LegacyInstallationManifest.from_mapping(mapping, config),
        )
    assert error.value.code == expected


@pytest.mark.parametrize("change,expected", [
    ("missing_role", "LEGACY_MANIFEST_INCOMPLETE"),
    ("duplicate_id", "DUPLICATE_INDEX_ID"),
    ("wrong_parent", "INDEX_PARENT_MISMATCH"),
    ("wrong_root", "INSTALLATION_ID_MISMATCH"),
    ("unknown_role", "LEGACY_MANIFEST_INVALID"),
    ("optional_seed_role", "LEGACY_MANIFEST_INVALID"),
])
def test_owner_manifest_requires_complete_exact_id_topology(
    change: str, expected: str,
) -> None:
    _, config, _, mapping = legacy_fixture()
    if change == "missing_role":
        del mapping["documents"]["command"]
    elif change == "duplicate_id":
        mapping["documents"]["receipt"]["id"] = mapping["documents"]["command"]["id"]
    elif change == "wrong_parent":
        mapping["documents"]["command"]["parents"] = [mapping["documents"]["projects"]["id"]]
    elif change == "wrong_root":
        mapping["root_folder_id"] = "some-other-root-id"
    elif change == "optional_seed_role":
        mapping["documents"]["output-formats"] = {
            "id": "extra-output-formats-doc-123", "name": "OUTPUT FORMATS",
            "mimeType": DOC, "parents": [mapping["documents"]["contracts"]["id"]],
        }
    else:
        mapping["documents"]["untrusted-command"] = mapping["documents"]["command"]
    with pytest.raises(DriveError) as error:
        LegacyInstallationManifest.from_mapping(mapping, config)
    assert error.value.code == expected


def test_owner_manifest_file_requires_private_regular_file(tmp_path: Path) -> None:
    _, config, _, mapping = legacy_fixture()
    private = tmp_path / "owner-private"
    private.mkdir(mode=0o700)
    path = private / "installation.json"
    path.write_text(json.dumps(mapping))
    path.chmod(0o600)
    assert LegacyInstallationManifest.from_file(path, config).root_id == config.root_id
    path.chmod(0o644)
    with pytest.raises(DriveError) as error:
        LegacyInstallationManifest.from_file(path, config)
    assert error.value.code == "LEGACY_MANIFEST_INVALID"
    path.chmod(0o600)
    link = private / "link.json"
    link.symlink_to(path)
    with pytest.raises(DriveError) as error:
        LegacyInstallationManifest.from_file(link, config)
    assert error.value.code == "LEGACY_MANIFEST_INVALID"
    ancestor_link = tmp_path / "alias"
    ancestor_link.symlink_to(private, target_is_directory=True)
    with pytest.raises(DriveError) as error:
        LegacyInstallationManifest.from_file(ancestor_link / path.name, config)
    assert error.value.code == "LEGACY_MANIFEST_INVALID"


def test_directly_constructed_incomplete_owner_manifest_cannot_bypass_validation() -> None:
    provider, config, _, mapping = legacy_fixture()
    bypass = LegacyInstallationManifest(
        config.root_id, config.index_id, mapping["installation_index_text_sha256"],
        mapping["installation_id"], {},
    )
    with pytest.raises(DriveError) as error:
        attach_existing(provider, config, legacy_manifest=bypass)
    assert error.value.code == "LEGACY_MANIFEST_INCOMPLETE"


def test_owner_manifest_id_must_appear_as_complete_token_in_pinned_index() -> None:
    provider, config, records, mapping = legacy_fixture()
    text = "CHAT-DEV RUNNER — INSTALLATION INDEX\n"
    for key in sorted(LEGACY_ROLES):
        file_id = records[key]["id"]
        text += f"{key}: {file_id + '-suffix' if key == 'command' else file_id}\n"
    provider.put_document(config.index_id, text, revision="legacy-index-rev-2")
    mapping["installation_index_text_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    with pytest.raises(DriveError) as error:
        attach_existing(
            provider, config, legacy_manifest=LegacyInstallationManifest.from_mapping(mapping, config),
        )
    assert error.value.code == "INDEX_ID_NOT_REFERENCED"


def _root_command() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": "local-runner", "epoch": str(uuid4()),
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "CREATE_PROJECT", "project_id": "project", "workspace_id": None,
        "expected_head": None,
        "params": {"name": "project", "source": {"kind": "new"},
                   "template_id": "python-basic", "github_identity": "github-owner",
                   "visibility": "private"},
        "intent": "One explicit fake-backed root command",
    }


def test_legacy_root_mailbox_reads_only_through_verified_attachment() -> None:
    provider, config, records, mapping = legacy_fixture()
    command_id = records["command"]["id"]
    provider.put_document(command_id, json.dumps(_root_command()), revision="command-rev-1")
    attached = attach_existing(
        provider, config, legacy_manifest=LegacyInstallationManifest.from_mapping(mapping, config),
    )
    frozen = read_ready_snapshot(provider, attached.root_mailbox())
    assert frozen is not None
    assert frozen.command.command["action"] == "CREATE_PROJECT"
    assert frozen.command_doc_id == command_id
    strict_mailbox = RegisteredMailbox(
        command_id, records["control"]["id"], receipt_id=records["receipt"]["id"],
    )
    with pytest.raises(DriveError) as error:
        read_ready_snapshot(provider, strict_mailbox)
    assert error.value.code == "PER_FILE_GRANT_MISSING"
    forged_mailbox = RegisteredMailbox(
        "arbitrary-command-doc-123", records["control"]["id"],
        receipt_id=records["receipt"]["id"], legacy_attachment=attached,
    )
    with pytest.raises(DriveError) as error:
        read_ready_snapshot(provider, forged_mailbox)
    assert error.value.code == "INVALID_MAILBOX"
    assert ("files.get", "arbitrary-command-doc-123") not in provider.calls


@pytest.mark.parametrize("change,expected", [
    ("moved", "FILE_MOVED"),
    ("trashed", "FILE_UNAVAILABLE"),
    ("type", "FILE_TYPE_MISMATCH"),
    ("renamed", "FILE_METADATA_MISMATCH"),
    ("conflicting_properties", "APP_IDENTITY_MISMATCH"),
    ("partial_properties", "APP_IDENTITY_MISMATCH"),
    ("unknown_grant", "PER_FILE_GRANT_UNKNOWN"),
    ("no_edit", "WRITE_CAPABILITY_MISSING"),
])
def test_attached_legacy_root_command_rejects_remote_identity_drift(
    change: str, expected: str,
) -> None:
    provider, config, records, mapping = legacy_fixture()
    command_id = records["command"]["id"]
    attached = attach_existing(
        provider, config, legacy_manifest=LegacyInstallationManifest.from_mapping(mapping, config),
    )
    provider.calls.clear()
    metadata = provider.files[command_id]
    if change == "moved":
        metadata["parents"] = [records["projects"]["id"]]
    elif change == "trashed":
        metadata["trashed"] = True
    elif change == "type":
        metadata["mimeType"] = "text/plain"
    elif change == "renamed":
        metadata["name"] = "OTHER"
    elif change == "conflicting_properties":
        metadata["appProperties"] = {"cdr_key": "receipt"}
    elif change == "partial_properties":
        metadata["appProperties"] = {"cdr_key": "command"}
    elif change == "unknown_grant":
        metadata.pop("isAppAuthorized")
    else:
        metadata["capabilities"]["canEdit"] = False
    with pytest.raises(DriveError) as error:
        read_ready_snapshot(provider, attached.root_mailbox())
    assert error.value.code == expected
    assert ("documents.get", command_id) not in provider.calls


def test_legacy_attachment_cannot_authorize_workspace_mailbox() -> None:
    provider, config, _, mapping = legacy_fixture()
    attached = attach_existing(
        provider, config, legacy_manifest=LegacyInstallationManifest.from_mapping(mapping, config),
    )
    with pytest.raises(DriveError) as error:
        RegisteredMailbox(
            "workspace-command-doc-123", "workspace-folder-123",
            receipt_id="workspace-receipt-doc-123", changeset_id="changeset-doc-123",
            project_id="project", workspace_id="workspace", legacy_attachment=attached,
        )
    assert error.value.code == "INVALID_MAILBOX"


def test_legacy_index_edit_during_ready_freeze_blocks_admission() -> None:
    provider, config, records, mapping = legacy_fixture()
    command_id = records["command"]["id"]
    provider.put_document(command_id, json.dumps(_root_command()), revision="command-rev-1")
    attached = attach_existing(
        provider, config, legacy_manifest=LegacyInstallationManifest.from_mapping(mapping, config),
    )

    def edit_index(fake: FakeDrive, count: int) -> None:
        if count == 1:
            fake.put_document(config.index_id, "changed Index\n", revision="index-rev-2")

    provider.on_document_read[command_id] = edit_index
    with pytest.raises(DriveError) as error:
        read_ready_snapshot(provider, attached.root_mailbox())
    assert error.value.code == "INDEX_CHANGED"
