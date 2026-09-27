"""Offline exact-ID Drive upload tests with injected API requests only."""

from __future__ import annotations

import copy
import hashlib
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from runner_app.adapters.drive_index import (
    AttachedInstallation,
    ExistingInstallation,
    IndexedFile,
    InstallationIndex,
)
from runner_app.adapters.drive_upload import (
    FOLDER_MIME,
    LIST_FIELDS,
    METADATA_FIELDS,
    DriveUploadError,
    GoogleUploadProvider,
)

INSTALLATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PARENT = "parentfolderid00001"
KEY = "result:request-001"


class ApiError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__("Bearer sensitive-provider-token")
        self.resp = type("Response", (), {"status": status})()


class Request:
    def __init__(self, call: Callable[[], dict[str, Any]]) -> None:
        self.call = call
        self.count = 0

    def execute(self, *, num_retries: int = 0) -> dict[str, Any]:
        assert num_retries == 0
        self.count += 1
        return self.call()


class FakeService:
    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}
        self.get_calls: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []
        self.create_error_after_write: Exception | None = None
        self.create_error_before_write: Exception | None = None
        self.list_pages: list[dict[str, Any]] | None = None
        self.next_id = 1
        self.entries[PARENT] = self.file(
            PARENT, "Results", FOLDER_MIME, "rootfolderid00001", "results",
            can_add_children=True,
        )

    @staticmethod
    def file(
        file_id: str, name: str, mime_type: str, parent: str, key: str, *,
        can_add_children: bool = False, content: bytes | None = None,
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": file_id, "name": name, "mimeType": mime_type,
            "parents": [parent], "trashed": False, "isAppAuthorized": True,
            "appProperties": {"cdr_installation": INSTALLATION, "cdr_key": key},
            "capabilities": {"canEdit": True, "canAddChildren": can_add_children},
        }
        if content is not None:
            value["size"] = str(len(content))
            value["md5Checksum"] = hashlib.md5(content, usedforsecurity=False).hexdigest()
            value["sha256Checksum"] = hashlib.sha256(content).hexdigest()
            value["appProperties"]["cdr_sha256"] = value["sha256Checksum"]
            value["appProperties"]["cdr_size"] = value["size"]
        return value

    def files(self) -> FakeService:
        return self

    def get(self, **kwargs: Any) -> Request:
        self.get_calls.append(kwargs)
        assert kwargs["fields"] == METADATA_FIELDS
        file_id = kwargs["fileId"]
        return Request(lambda: copy.deepcopy(self.entries[file_id]))

    def list(self, **kwargs: Any) -> Request:
        self.list_calls.append(kwargs)
        assert kwargs["fields"] == LIST_FIELDS
        query = kwargs["q"]
        assert "name" not in query
        match_install = re.search(r"key='cdr_installation' and value='([^']+)'", query)
        match_key = re.search(r"key='cdr_key' and value='([^']+)'", query)
        assert match_install and match_key
        assert " in parents" not in query and match_install[1] == INSTALLATION
        assert match_key[1] == KEY

        def response() -> dict[str, Any]:
            if self.list_pages is not None:
                return copy.deepcopy(self.list_pages.pop(0))
            selected = sorted((entry for entry in self.entries.values()
                               if entry.get("appProperties", {}).get("cdr_installation") == INSTALLATION
                               and entry.get("appProperties", {}).get("cdr_key") == KEY),
                              key=lambda entry: entry["id"])
            offset = int(kwargs["pageToken"] or "0")
            page = selected[offset:offset + kwargs["pageSize"]]
            end = offset + len(page)
            return {"files": copy.deepcopy(page),
                    "nextPageToken": str(end) if end < len(selected) else None}

        return Request(response)

    def create(self, **kwargs: Any) -> Request:
        self.create_calls.append(kwargs)
        assert kwargs["fields"] == METADATA_FIELDS

        def response() -> dict[str, Any]:
            if self.create_error_before_write:
                raise self.create_error_before_write
            body = kwargs["body"]
            file_id = f"createdfileid{self.next_id:05d}"
            self.next_id += 1
            media = kwargs["media_body"]
            content = None if media is None else media["content"]
            entry = self.file(
                file_id, body["name"], body["mimeType"], body["parents"][0],
                body["appProperties"]["cdr_key"],
                can_add_children=body["mimeType"] == FOLDER_MIME,
                content=content,
            )
            entry["appProperties"] = copy.deepcopy(body["appProperties"])
            self.entries[file_id] = entry
            if self.create_error_after_write:
                raise self.create_error_after_write
            return {"id": file_id}

        return Request(response)


def provider(
    service: FakeService, *, page_size: int = 100, max_pages: int = 10,
    attached: AttachedInstallation | None = None,
):
    return GoogleUploadProvider(
        service, media_factory=lambda content, media_type: {
            "content": content, "media_type": media_type,
        }, page_size=page_size, max_pages=max_pages, attached=attached,
    )


def legacy_results_attachment(service: FakeService) -> AttachedInstallation:
    """Model an exact-ID attachment after the old folder was verified."""
    results = service.entries[PARENT]
    results["name"] = "02 Results"
    results["isAppAuthorized"] = False
    results.pop("appProperties")
    root_id = "rootfolderid00001"
    index_id = "indexdocid00000001"
    indexed = {
        "root": IndexedFile("root", root_id, "Chat-Dev Runner", FOLDER_MIME, (), {}),
        "results": IndexedFile("results", PARENT, "02 Results", FOLDER_MIME,
                               (root_id,), {}),
        "installation-index": IndexedFile(
            "installation-index", index_id, "INSTALLATION INDEX",
            "application/vnd.google-apps.document", (root_id,), {},
        ),
    }
    return AttachedInstallation(
        ExistingInstallation(root_id, index_id, "Chat-Dev Runner"),
        InstallationIndex(INSTALLATION, indexed, "owner_manifest"),
        "index-rev", "0" * 64, {"results": copy.deepcopy(results)},
    )


def private_file(path: Path, content: bytes) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_bytes(content)
    os.chmod(path, 0o600)
    return path


def test_create_folder_is_one_exact_parent_write_with_app_identity() -> None:
    service = FakeService()
    adapter = provider(service)
    assert service.get_calls == [] and service.create_calls == []
    created = adapter.create_folder(PARENT, INSTALLATION, KEY, "request-001")
    assert created["parents"] == [PARENT]
    assert created["mimeType"] == FOLDER_MIME
    assert created["appProperties"] == {"cdr_installation": INSTALLATION, "cdr_key": KEY}
    assert len(service.create_calls) == 1
    assert service.create_calls[0]["media_body"] is None
    assert service.create_calls[0]["body"]["parents"] == [PARENT]
    assert [call["fileId"] for call in service.get_calls] == [PARENT, created["id"]]


def test_upload_uses_verified_private_bytes_and_remote_checksums(tmp_path: Path) -> None:
    content = b"sanitized result\n"
    source = private_file(tmp_path / "bundle" / "report.md", content)
    sha = hashlib.sha256(content).hexdigest()
    service = FakeService()
    uploaded = provider(service).upload_file(
        PARENT, INSTALLATION, KEY, "report.md", source, "text/markdown; charset=utf-8",
        len(content), sha,
    )
    assert uploaded["mimeType"] == "text/markdown"
    assert uploaded["appProperties"]["cdr_sha256"] == sha
    assert uploaded["appProperties"]["cdr_size"] == str(len(content))
    assert uploaded["size"] == str(len(content))
    assert uploaded["md5Checksum"] == hashlib.md5(content, usedforsecurity=False).hexdigest()
    assert service.create_calls[0]["media_body"] == {
        "content": content, "media_type": "text/markdown; charset=utf-8",
    }
    assert service.create_calls[0]["body"]["parents"] == [PARENT]


def test_source_change_symlink_and_wide_permissions_fail_before_write(tmp_path: Path) -> None:
    content = b"approved bytes"
    source = private_file(tmp_path / "bundle" / "report.md", content)
    service = FakeService()
    adapter = provider(service)
    sha = hashlib.sha256(content).hexdigest()
    source.write_bytes(b"changed bytes!")
    with pytest.raises(DriveUploadError) as error:
        adapter.upload_file(PARENT, INSTALLATION, KEY, "report.md", source, "text/plain",
                            len(content), sha)
    assert error.value.code == "UPLOAD_SOURCE_CHANGED"
    source.write_bytes(content)
    os.chmod(source, 0o644)
    with pytest.raises(DriveUploadError) as error:
        adapter.upload_file(PARENT, INSTALLATION, KEY, "report.md", source, "text/plain",
                            len(content), sha)
    assert error.value.code == "UPLOAD_SOURCE_UNSAFE"
    os.chmod(source, 0o600)
    link = source.parent / "link.md"
    link.symlink_to(source)
    with pytest.raises(DriveUploadError) as error:
        adapter.upload_file(PARENT, INSTALLATION, KEY, "report.md", link, "text/plain",
                            len(content), sha)
    assert error.value.code == "UPLOAD_SOURCE_UNSAFE"
    assert service.create_calls == []


def test_lost_upload_ack_has_one_write_and_digest_reconciliation(tmp_path: Path) -> None:
    content = b"frozen artifact\n"
    source = private_file(tmp_path / "bundle" / "report.md", content)
    sha = hashlib.sha256(content).hexdigest()
    md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
    service = FakeService()
    service.create_error_after_write = OSError("Bearer sensitive-provider-token")
    adapter = provider(service)
    with pytest.raises(DriveUploadError) as error:
        adapter.upload_file(PARENT, INSTALLATION, KEY, "report.md", source,
                            "text/plain", len(content), sha)
    assert error.value.code == "UPLOAD_WRITE_UNCERTAIN"
    assert error.value.effect_uncertain is True
    assert "sensitive-provider-token" not in str(error.value)
    assert len(service.create_calls) == 1
    found = adapter.find_by_key(
        PARENT, INSTALLATION, KEY, expected_mime_type="text/plain",
        expected_name="report.md", expected_sha256=sha,
        expected_size_bytes=len(content), expected_md5=md5,
    )
    assert found is not None and found["appProperties"]["cdr_sha256"] == sha
    assert len(service.create_calls) == 1


def test_reconciliation_refuses_same_title_wrong_key_and_duplicate_key() -> None:
    service = FakeService()
    service.entries["wrongkeyfile00001"] = service.file(
        "wrongkeyfile00001", "same-title", "text/plain", PARENT, "other-key",
    )
    adapter = provider(service)
    assert adapter.find_by_key(PARENT, INSTALLATION, KEY) is None
    service.entries["matchkeyfile00001"] = service.file(
        "matchkeyfile00001", "same-title", "text/plain", PARENT, KEY,
    )
    found = adapter.find_by_key(PARENT, INSTALLATION, KEY, expected_mime_type="text/plain")
    assert found is not None and found["id"] == "matchkeyfile00001"
    service.entries["matchkeyfile00002"] = service.file(
        "matchkeyfile00002", "different-title", "text/plain", PARENT, KEY,
    )
    with pytest.raises(DriveUploadError) as error:
        adapter.find_by_key(PARENT, INSTALLATION, KEY)
    assert error.value.code == "UPLOAD_DUPLICATE_KEY"
    assert service.create_calls == []


def test_reconciliation_rechecks_exact_id_and_parent_after_listing() -> None:
    service = FakeService()
    moved = service.file("movedfileid00001", "report", "text/plain", "otherfolderid00001", KEY)
    service.entries[moved["id"]] = moved
    with pytest.raises(DriveUploadError) as error:
        provider(service).find_by_key(PARENT, INSTALLATION, KEY)
    assert error.value.code == "UPLOAD_FILE_MOVED"
    assert service.get_calls[-1]["fileId"] == moved["id"]


def test_trashed_app_owned_key_is_refused_instead_of_recreated() -> None:
    service = FakeService()
    entry = service.file("trashedfileid00001", "report.md", "text/plain", PARENT, KEY)
    entry["trashed"] = True
    service.entries[entry["id"]] = entry
    with pytest.raises(DriveUploadError) as error:
        provider(service).find_by_key(PARENT, INSTALLATION, KEY)
    assert error.value.code == "UPLOAD_FILE_IDENTITY_MISMATCH"
    assert service.create_calls == []


def test_missing_remote_checksum_cannot_confirm_self_declared_hash(tmp_path: Path) -> None:
    content = b"checked"
    sha = hashlib.sha256(content).hexdigest()
    service = FakeService()
    entry = service.file("binaryfileid00001", "report.md", "text/plain", PARENT, KEY,
                         content=content)
    entry.pop("sha256Checksum")
    entry.pop("md5Checksum")
    service.entries[entry["id"]] = entry
    with pytest.raises(DriveUploadError) as error:
        provider(service).find_by_key(
            PARENT, INSTALLATION, KEY, expected_sha256=sha,
            expected_size_bytes=len(content),
        )
    assert error.value.code == "UPLOAD_CHECKSUM_UNVERIFIED"


def test_listing_is_bounded_and_rejects_repeated_cursor() -> None:
    service = FakeService()
    service.list_pages = [
        {"files": [], "nextPageToken": "same"},
        {"files": [], "nextPageToken": "same"},
    ]
    with pytest.raises(DriveUploadError) as error:
        provider(service, page_size=1, max_pages=3).find_by_key(PARENT, INSTALLATION, KEY)
    assert error.value.code == "UPLOAD_LIST_INVALID"
    assert len(service.list_calls) == 2


def test_parent_must_be_registered_app_owned_writable_folder() -> None:
    service = FakeService()
    service.entries[PARENT]["capabilities"]["canAddChildren"] = False
    with pytest.raises(DriveUploadError) as error:
        provider(service).create_folder(PARENT, INSTALLATION, KEY, "request-001")
    assert error.value.code == "UPLOAD_PARENT_NOT_WRITABLE"
    assert service.create_calls == []
    service.entries[PARENT]["capabilities"]["canAddChildren"] = True
    service.entries[PARENT]["appProperties"]["cdr_installation"] = "other-installation"
    with pytest.raises(DriveUploadError) as error:
        provider(service).create_folder(PARENT, INSTALLATION, KEY, "request-001")
    assert error.value.code == "UPLOAD_APP_IDENTITY_MISMATCH"
    assert service.create_calls == []


def test_provider_write_error_is_sanitized_and_never_retried() -> None:
    service = FakeService()
    service.create_error_before_write = ApiError(503)
    with pytest.raises(DriveUploadError) as error:
        provider(service).create_folder(PARENT, INSTALLATION, KEY, "request-001")
    assert error.value.code == "UPLOAD_WRITE_UNCERTAIN"
    assert error.value.effect_uncertain is True
    assert "sensitive-provider-token" not in str(error.value)
    assert len(service.create_calls) == 1

    service = FakeService()
    service.create_error_before_write = ApiError(403)
    with pytest.raises(DriveUploadError) as error:
        provider(service).create_folder(PARENT, INSTALLATION, KEY, "request-001")
    assert error.value.code == "UPLOAD_PROVIDER_WRITE_REJECTED"
    assert error.value.effect_uncertain is False
    assert len(service.create_calls) == 1


def test_invalid_key_is_rejected_before_query_or_write() -> None:
    service = FakeService()
    adapter = provider(service)
    with pytest.raises(DriveUploadError) as error:
        adapter.find_by_key(PARENT, INSTALLATION, "bad' or name='anything")
    assert error.value.code == "UPLOAD_IDENTITY_INVALID"
    assert service.get_calls == [] and service.list_calls == []
    with pytest.raises(DriveUploadError) as too_long:
        adapter.find_by_key(PARENT, INSTALLATION, "x" * 118)
    assert too_long.value.code == "UPLOAD_IDENTITY_INVALID"
    with pytest.raises(DriveUploadError) as malformed_name:
        adapter.create_folder(PARENT, INSTALLATION, KEY, "bad\ud800")
    assert malformed_name.value.code == "UPLOAD_NAME_INVALID"
    assert service.create_calls == []


def test_get_file_checks_digest_parent_type_and_key() -> None:
    content = b"content"
    sha = hashlib.sha256(content).hexdigest()
    service = FakeService()
    entry = service.file("exactfileid00001", "report.md", "text/plain", PARENT, KEY,
                         content=content)
    service.entries[entry["id"]] = entry
    adapter = provider(service)
    assert adapter.get_file(
        entry["id"], expected_parent_id=PARENT, expected_installation_id=INSTALLATION,
        expected_key=KEY, expected_mime_type="text/plain", expected_sha256=sha,
        expected_size_bytes=len(content),
    )["id"] == entry["id"]
    entry["appProperties"]["cdr_sha256"] = "0" * 64
    with pytest.raises(DriveUploadError) as error:
        adapter.get_file(entry["id"], expected_sha256=sha)
    assert error.value.code == "UPLOAD_CONTENT_DIGEST_MISMATCH"



def test_legacy_indexed_results_accepts_exact_parent_and_creates_strict_descendant() -> None:
    service = FakeService()
    attached = legacy_results_attachment(service)
    adapter = provider(service, attached=attached)
    assert adapter.get_file(
        PARENT, expected_parent_id=attached.config.root_id,
        expected_installation_id=INSTALLATION, expected_key="results",
        expected_mime_type=FOLDER_MIME, expected_name="02 Results",
    )["id"] == PARENT
    assert adapter.find_by_key(PARENT, INSTALLATION, KEY) is None
    created = adapter.create_folder(PARENT, INSTALLATION, KEY, "request-001")
    assert created["parents"] == [PARENT]
    assert created["appProperties"] == {
        "cdr_installation": INSTALLATION, "cdr_key": KEY,
    }
    assert adapter.find_by_key(PARENT, INSTALLATION, KEY)["id"] == created["id"]
    service.entries[created["id"]]["isAppAuthorized"] = False
    with pytest.raises(DriveUploadError) as error:
        adapter.get_file(
            created["id"], expected_parent_id=PARENT,
            expected_installation_id=INSTALLATION, expected_key=KEY,
        )
    assert error.value.code == "UPLOAD_PER_FILE_GRANT_MISSING"


@pytest.mark.parametrize("change,code", [
    (lambda entry: entry.update(parents=["otherrootfolder0001"]), "UPLOAD_FILE_MOVED"),
    (lambda entry: entry.update(name="Other Results"), "UPLOAD_FILE_NAME_CHANGED"),
    (lambda entry: entry.update(mimeType="text/plain"), "UPLOAD_FILE_TYPE_MISMATCH"),
    (lambda entry: entry.update(trashed=True), "UPLOAD_FILE_IDENTITY_MISMATCH"),
    (lambda entry: entry["capabilities"].update(canAddChildren=False),
     "UPLOAD_PARENT_NOT_WRITABLE"),
    (lambda entry: entry.update(appProperties={"cdr_installation": INSTALLATION}),
     "UPLOAD_APP_IDENTITY_MISMATCH"),
    (lambda entry: entry.update(appProperties={"cdr_installation": "other",
                                               "cdr_key": "results"}),
     "UPLOAD_APP_IDENTITY_MISMATCH"),
    (lambda entry: entry.update(appProperties={"cdr_installation": INSTALLATION,
                                               "cdr_key": "other"}),
     "UPLOAD_APP_IDENTITY_MISMATCH"),
    (lambda entry: entry.update(appProperties={"cdr_workspace": "other"}),
     "UPLOAD_APP_IDENTITY_MISMATCH"),
    (lambda entry: entry.update(appProperties={"cdr_installation": INSTALLATION,
                                               "cdr_key": "results", 7: "malformed"}),
     "UPLOAD_APP_IDENTITY_MISSING"),
    (lambda entry: entry.update(appProperties=["malformed"]),
     "UPLOAD_APP_IDENTITY_MISSING"),
])
def test_legacy_results_drift_or_conflicting_identity_blocks_write(
    change: Callable[[dict[str, Any]], None], code: str,
) -> None:
    service = FakeService()
    attached = legacy_results_attachment(service)
    change(service.entries[PARENT])
    with pytest.raises(DriveUploadError) as error:
        provider(service, attached=attached).create_folder(
            PARENT, INSTALLATION, KEY, "request-001",
        )
    assert error.value.code == code
    assert service.create_calls == []


def test_legacy_results_accepts_complete_matching_visible_identity() -> None:
    service = FakeService()
    attached = legacy_results_attachment(service)
    service.entries[PARENT]["appProperties"] = {
        "cdr_installation": INSTALLATION, "cdr_key": "results",
    }
    created = provider(service, attached=attached).create_folder(
        PARENT, INSTALLATION, KEY, "request-001",
    )
    assert created["parents"] == [PARENT]


def test_legacy_results_exception_needs_verified_attachment_and_exact_call() -> None:
    service = FakeService()
    attached = legacy_results_attachment(service)
    with pytest.raises(DriveUploadError) as no_attachment:
        provider(service).create_folder(PARENT, INSTALLATION, KEY, "request-001")
    assert no_attachment.value.code == "UPLOAD_PER_FILE_GRANT_MISSING"
    with pytest.raises(DriveUploadError) as no_exact_contract:
        provider(service, attached=attached).get_file(
            PARENT, expected_installation_id=INSTALLATION,
        )
    assert no_exact_contract.value.code == "UPLOAD_PER_FILE_GRANT_MISSING"
    unverified = AttachedInstallation(
        attached.config, attached.index, attached.index_revision,
        attached.index_text_sha256, {},
    )
    with pytest.raises(DriveUploadError) as no_verified_metadata:
        provider(service, attached=unverified).create_folder(
            PARENT, INSTALLATION, KEY, "request-001",
        )
    assert no_verified_metadata.value.code == "UPLOAD_PER_FILE_GRANT_MISSING"
    unindexed = "unindexedfolder00001"
    service.entries[unindexed] = service.file(
        unindexed, "other", FOLDER_MIME, attached.config.root_id, "other",
        can_add_children=True,
    )
    service.entries[unindexed]["isAppAuthorized"] = False
    service.entries[unindexed].pop("appProperties")
    with pytest.raises(DriveUploadError) as wrong_parent:
        provider(service, attached=attached).create_folder(
            unindexed, INSTALLATION, KEY, "request-001",
        )
    assert wrong_parent.value.code == "UPLOAD_PER_FILE_GRANT_MISSING"
    assert service.create_calls == []
