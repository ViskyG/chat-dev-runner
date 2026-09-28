"""EP-05 project Docs provisioning under a pre-attached installation, using fake Drive."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from runner_app.adapters.drive_docs import DocumentSnapshot
from runner_app.adapters.drive_index import (
    AttachedInstallation,
    ExistingInstallation,
    IndexedFile,
    InstallationIndex,
)
from runner_app.adapters.drive_provision import DriveProjectProvisioner, DriveProvisionError
from runner_app.adapters.drive_registry import DriveRegistryError, WorkspaceDriveRegistry
from runner_app.adapters.drive_upload import (
    DOCUMENT_MIME,
    FOLDER_MIME,
    DriveUploadError,
    GoogleUploadProvider,
)
from runner_app.adapters.drive_writer import ObservedOutputDoc, RegisteredOutputDoc, WriteResult
from runner_app.domain.commands import freeze
from runner_app.git.registry import SourceRegistry
from runner_app.git.service import SourceService
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger

_INSTALLATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_ROOT = "rootfolderid0000001"
_INDEX = "indexdocument000001"
_PROJECTS = "projectrootfolder0001"


class Request:
    def __init__(self, call: Any) -> None:
        self.call = call

    def execute(self, *, num_retries: int = 0) -> dict[str, Any]:
        assert num_retries == 0
        return self.call()


class FakeDriveAPI:
    def __init__(self, *, legacy: bool = False) -> None:
        self.files_by_id: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self.lost_ack_key: str | None = None
        self.inject_project_command_name_collision = False
        self.legacy = legacy
        self.files_by_id[_PROJECTS] = {
            "id": _PROJECTS, "name": "01 Projects", "mimeType": FOLDER_MIME,
            "parents": [_ROOT], "trashed": False, "isAppAuthorized": not legacy,
            "capabilities": {"canEdit": True, "canAddChildren": True},
        }
        if not legacy:
            self.files_by_id[_PROJECTS]["appProperties"] = {
                "cdr_installation": _INSTALLATION, "cdr_key": "projects",
            }

    def files(self) -> FakeDriveAPI:
        return self

    def get(self, *, fileId: str, fields: str) -> Request:
        assert "appProperties" in fields
        return Request(lambda: copy.deepcopy(self.files_by_id[fileId]))

    def list(self, *, q: str, fields: str, pageSize: int,
             pageToken: str | None) -> Request:
        assert "files(" in fields and pageSize > 0
        expected = dict(re.findall(r"key='(cdr_[a-z]+)' and value='([^']+)'", q))

        def response() -> dict[str, Any]:
            matches = [item for item in self.files_by_id.values()
                       if all(item.get("appProperties", {}).get(key) == value
                              for key, value in expected.items())]
            start = int(pageToken or "0")
            selected = matches[start:start + pageSize]
            following = start + len(selected)
            return {"files": copy.deepcopy(selected),
                    "nextPageToken": str(following) if following < len(matches) else None}

        return Request(response)

    def create(self, *, body: dict[str, Any], media_body: Any,
               fields: str) -> Request:
        assert media_body is None and "appProperties" in fields

        def response() -> dict[str, Any]:
            file_id = f"createdfileid{len(self.created) + 1:05d}"
            metadata = {
                **copy.deepcopy(body), "id": file_id, "trashed": False,
                "isAppAuthorized": True,
                "capabilities": {"canEdit": True,
                                 "canAddChildren": body["mimeType"] == FOLDER_MIME},
            }
            self.files_by_id[file_id] = metadata
            self.created.append(metadata)
            if (self.inject_project_command_name_collision
                    and body["appProperties"]["cdr_key"] == "project:fixture"):
                self.files_by_id["unrelatedcommand0001"] = {
                    "id": "unrelatedcommand0001", "name": "PROJECT COMMAND",
                    "mimeType": DOCUMENT_MIME, "parents": [file_id],
                    "trashed": False, "isAppAuthorized": True,
                    "appProperties": {"cdr_installation": _INSTALLATION,
                                      "cdr_key": "unrelated"},
                    "capabilities": {"canEdit": True, "canAddChildren": False},
                }
            if body["appProperties"]["cdr_key"] == self.lost_ack_key:
                self.lost_ack_key = None
                raise RuntimeError("sensitive fake provider exception")
            return {"id": file_id}

        return Request(response)


class RegistryRead:
    def __init__(self, api: FakeDriveAPI) -> None:
        self.api = api

    def get_file(self, file_id: str, *, fields: str) -> dict[str, Any]:
        assert "appProperties" in fields
        return copy.deepcopy(self.api.files_by_id[file_id])


def installation(api: FakeDriveAPI) -> AttachedInstallation:
    files = {
        "root": IndexedFile("root", _ROOT, "Chat-Dev Runner", FOLDER_MIME, (), {}),
        "installation-index": IndexedFile(
            "installation-index", _INDEX, "INSTALLATION INDEX", DOCUMENT_MIME, (_ROOT,), {},
        ),
        "projects": IndexedFile("projects", _PROJECTS, "01 Projects", FOLDER_MIME, (_ROOT,), {}),
    }
    return AttachedInstallation(
        ExistingInstallation(_ROOT, _INDEX, "Chat-Dev Runner"),
        InstallationIndex(_INSTALLATION, files,
                          "owner_manifest" if api.legacy else "app_properties"),
        "revision-1", "a" * 64,
        {"projects": copy.deepcopy(api.files_by_id[_PROJECTS])},
    )


def admit(ledger: Ledger, project_id: str, name: str) -> str:
    state = ledger.state()
    now = datetime.now(timezone.utc)
    command = {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": now.isoformat(), "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "CREATE_PROJECT", "project_id": project_id, "workspace_id": None,
        "expected_head": None, "intent": "Provision fake Drive project",
        "params": {"name": name, "source": {"kind": "new"},
                   "template_id": "python-basic", "github_identity": "owner",
                   "visibility": "private"},
    }
    assert ledger.admit(
        command, freeze(command).digest, "local-owner",
        (f"repo:{project_id}", "global:owner", "global:drive-installation"),
    ).execute
    return command["request_id"]


def setup(tmp_path: Path, *, legacy: bool = False
          ) -> tuple[Ledger, FakeDriveAPI, DriveProjectProvisioner, WorkspaceDriveRegistry]:
    ledger = Ledger(Database(tmp_path / "state" / "ledger.sqlite"))
    ledger.startup()
    ledger.arm()
    api = FakeDriveAPI(legacy=legacy)
    attached = installation(api)
    upload = GoogleUploadProvider(api, attached=attached)
    registry = WorkspaceDriveRegistry(tmp_path / "registrations", attached,
                                      RegistryRead(api))
    return ledger, api, DriveProjectProvisioner(ledger, attached, upload, registry), registry


def test_provisions_ten_scoped_resources_and_reobserves_exact_ids(tmp_path: Path) -> None:
    ledger, api, provisioner, registry = setup(tmp_path)
    request_id = admit(ledger, "fixture", "Fixture")
    record = provisioner.provision(request_id, "fixture", "primary", "Fixture")
    assert len(api.created) == 10
    assert record == registry.get("fixture", "primary")
    assert all(item["appProperties"]["cdr_project"] == "fixture"
               for item in api.created)
    assert all(item["appProperties"].get("cdr_workspace") == "primary"
               for item in api.created
               if item["appProperties"]["cdr_key"] not in {
                   "project:fixture", "project-state", "project-command", "project-receipt",
               })
    assert record.project_state_doc_id == api.created[1]["id"]
    assert record.project_command_doc_id == api.created[2]["id"]
    assert record.project_receipt_doc_id == api.created[3]["id"]
    assert record.context_doc_id == api.created[5]["id"]
    assert record.project_mailbox().command_id == record.project_command_doc_id
    assert record.project_mailbox().workspace_id is None
    assert record.project_receipt_output().file_id == record.project_receipt_doc_id
    assert api.created[0]["parents"] == [_PROJECTS]
    assert [item["mimeType"] for item in api.created] == [
        FOLDER_MIME, DOCUMENT_MIME, DOCUMENT_MIME, DOCUMENT_MIME,
        FOLDER_MIME, DOCUMENT_MIME, DOCUMENT_MIME, DOCUMENT_MIME,
        DOCUMENT_MIME, DOCUMENT_MIME,
    ]
    assert provisioner.provision(request_id, "fixture", "primary", "Fixture") == record
    assert len(api.created) == 10
    assert len(ledger.get(request_id)["effects"]) == 10
    assert all(item["state"] == "CONFIRMED"
               for item in ledger.get(request_id)["effects"])
    ledger.finish(request_id, "COMPLETED")



def test_project_command_same_name_collision_is_not_adopted(tmp_path: Path) -> None:
    ledger, api, provisioner, registry = setup(tmp_path)
    api.inject_project_command_name_collision = True
    request_id = admit(ledger, "fixture", "Fixture")
    record = provisioner.provision(request_id, "fixture", "primary", "Fixture")
    unrelated = api.files_by_id["unrelatedcommand0001"]
    assert unrelated["name"] == "PROJECT COMMAND"
    assert unrelated["parents"] == [record.project_folder_id]
    assert unrelated["appProperties"]["cdr_key"] == "unrelated"
    assert record.project_command_doc_id != unrelated["id"]
    assert api.files_by_id[record.project_command_doc_id]["appProperties"] == {
        "cdr_installation": _INSTALLATION,
        "cdr_key": "project-command",
        "cdr_project": "fixture",
    }
    assert registry.get("fixture", "primary") == record


def test_lost_project_receipt_ack_reconciles_exact_key_without_duplicate(
    tmp_path: Path,
) -> None:
    ledger, api, provisioner, registry = setup(tmp_path)
    request_id = admit(ledger, "fixture", "Fixture")
    api.lost_ack_key = "project-receipt"
    with pytest.raises(DriveUploadError) as error:
        provisioner.provision(request_id, "fixture", "primary", "Fixture")
    assert error.value.code == "UPLOAD_WRITE_UNCERTAIN"
    assert len(api.created) == 4
    pending = next(item for item in ledger.get(request_id)["effects"]
                   if item["step_id"] == "drive-project-receipt-doc")
    assert pending["state"] == "INTENDED"
    record = provisioner.provision(request_id, "fixture", "primary", "Fixture")
    assert registry.get("fixture", "primary") == record
    assert len(api.created) == 10
    assert len([item for item in api.created
                if item["appProperties"]["cdr_key"] == "project-receipt"]) == 1
    assert all(item["state"] == "CONFIRMED"
               for item in ledger.get(request_id)["effects"])


def test_project_document_workspace_scope_drift_is_refused(tmp_path: Path) -> None:
    ledger, api, provisioner, registry = setup(tmp_path)
    request_id = admit(ledger, "fixture", "Fixture")
    record = provisioner.provision(request_id, "fixture", "primary", "Fixture")
    api.files_by_id[record.project_command_doc_id]["appProperties"]["cdr_workspace"] = "primary"
    with pytest.raises(DriveRegistryError) as error:
        registry.get("fixture", "primary")
    assert error.value.code == "REGISTRATION_SCOPE_CHANGED"

def test_lost_ack_reconciles_by_scoped_key_without_second_create(tmp_path: Path) -> None:
    ledger, api, provisioner, registry = setup(tmp_path, legacy=True)
    request_id = admit(ledger, "fixture", "Fixture")
    api.lost_ack_key = "workspace-receipt"
    with pytest.raises(DriveUploadError) as error:
        provisioner.provision(request_id, "fixture", "primary", "Fixture")
    assert error.value.code == "UPLOAD_WRITE_UNCERTAIN"
    assert len(api.created) == 9
    pending = next(item for item in ledger.get(request_id)["effects"]
                   if item["step_id"] == "drive-receipt-doc")
    assert pending["state"] == "INTENDED"
    record = provisioner.provision(request_id, "fixture", "primary", "Fixture")
    assert record == registry.get("fixture", "primary")
    assert len(api.created) == 10
    assert len({item["id"] for item in api.created}) == 10
    assert all(item["state"] == "CONFIRMED"
               for item in ledger.get(request_id)["effects"])


def test_unobserved_uncertain_create_never_retries_and_name_is_not_ownership(
    tmp_path: Path,
) -> None:
    ledger, api, provisioner, _ = setup(tmp_path)
    # A same-name folder without our scoped key is not adopted.
    api.files_by_id["unrelatedfolder0001"] = {
        "id": "unrelatedfolder0001", "name": "fixture", "mimeType": FOLDER_MIME,
        "parents": [_PROJECTS], "trashed": False, "isAppAuthorized": True,
        "appProperties": {"cdr_installation": _INSTALLATION,
                          "cdr_key": "unrelated"},
        "capabilities": {"canEdit": True, "canAddChildren": True},
    }
    request_id = admit(ledger, "fixture", "Fixture")
    record = provisioner.provision(request_id, "fixture", "primary", "Fixture")
    assert record.project_folder_id != "unrelatedfolder0001"
    ledger.finish(request_id, "COMPLETED")

    second_id = admit(ledger, "second", "Second")
    api.lost_ack_key = "workspace-receipt"
    with pytest.raises(DriveUploadError):
        provisioner.provision(second_id, "second", "primary", "Second")
    api.files_by_id = {
        key: value for key, value in api.files_by_id.items()
        if value.get("appProperties", {}).get("cdr_project") != "second"
        or value.get("appProperties", {}).get("cdr_key") != "workspace-receipt"
    }
    before = len(api.created)
    with pytest.raises(DriveProvisionError) as error:
        provisioner.provision(second_id, "second", "primary", "Second")
    assert error.value.code == "PROVISION_EFFECT_UNCERTAIN"
    assert len(api.created) == before


class FakeStateWriter:
    def __init__(self, record: Any) -> None:
        assert record.project_state_doc_id and record.context_doc_id
        self.docs = {item.key: item for item in record.state_docs()}
        self.contents = {key: "\n" for key in self.docs}
        self.revisions = {key: 1 for key in self.docs}
        self.writes: list[str] = []
        self.lose_ack_key: str | None = None

    def registration(self, key: str) -> RegisteredOutputDoc:
        return self.docs[key]

    def read(self, key: str) -> ObservedOutputDoc:
        value = self.contents[key]
        snapshot = DocumentSnapshot(
            self.docs[key].file_id, f"rev-{self.revisions[key]}", value, "tab-one",
            hashlib.sha256(value.encode()).hexdigest(),
        )
        return ObservedOutputDoc(key, snapshot, 1, len(value) + 1)

    def replace(self, key: str, text: str, *, expected: ObservedOutputDoc) -> WriteResult:
        current = self.read(key)
        assert current.snapshot.revision_id == expected.snapshot.revision_id
        assert current.snapshot.sha256 == expected.snapshot.sha256
        self.contents[key] = text
        self.revisions[key] += 1
        self.writes.append(key)
        state = "UNCERTAIN" if self.lose_ack_key == key else "CONFIRMED"
        self.lose_ack_key = None
        return WriteResult(
            state, key, self.docs[key].file_id, expected.snapshot.revision_id,
            hashlib.sha256(text.encode()).hexdigest(),
            resulting_revision=f"rev-{self.revisions[key]}" if state == "CONFIRMED" else None,
        )


def _local_source_binding(ledger: Ledger, tmp_path: Path, request_id: str) -> SourceRegistry:
    source_registry = SourceRegistry(ledger.database, tmp_path / "managed")
    service = SourceService(ledger, source_registry)
    service.create_new_source(request_id)
    service.create_initial_workspace_source(request_id)
    source_registry.bind_workspace_profile(
        "fixture", "primary", template_id="python-basic", profile_id="python-quick",
    )
    return source_registry


def test_project_state_and_source_context_use_real_registered_ids_and_binding(
    tmp_path: Path,
) -> None:
    ledger, api, provisioner, registry = setup(tmp_path)
    request_id = admit(ledger, "fixture", "Fixture")
    source_registry = _local_source_binding(ledger, tmp_path, request_id)
    record = provisioner.provision(request_id, "fixture", "primary", "Fixture")
    writer = FakeStateWriter(record)
    result = provisioner.populate(
        request_id, record, source_registry=source_registry, writer=writer,
        repository_id=12345, repository_url="https://github.com/Owner/fixture",
    )
    state_text = writer.contents["project-state:fixture"]
    state = json.loads(state_text.split("\n", 1)[1])
    workspace = source_registry.get_workspace("fixture", "primary")
    assert state["repository"] == {
        "id": 12345, "url": "https://github.com/Owner/fixture",
        "full_name": "Owner/fixture",
    }
    assert state["workspaces"][0]["head"] == workspace["head"]
    assert state["workspaces"][0]["profile_id"] == "python-quick"
    assert state["workspaces"][0]["context_doc_id"] == record.context_doc_id
    assert state["workspaces"][0]["command_doc_id"] == record.command_doc_id
    assert state["project_state_doc_id"] == record.project_state_doc_id
    assert state["project_command_doc_id"] == record.project_command_doc_id
    assert state["project_receipt_doc_id"] == record.project_receipt_doc_id
    context = writer.contents["source-context:fixture/primary"]
    assert workspace["head"] in context and "No source export has been requested yet" in context
    assert result["project_state_sha256"] == hashlib.sha256(state_text.encode()).hexdigest()
    assert provisioner.populate(
        request_id, record, source_registry=source_registry, writer=writer,
        repository_id=12345, repository_url="https://github.com/Owner/fixture",
    ) == result
    assert len(writer.writes) == 2
    assert len([item for item in ledger.get(request_id)["effects"]
                if item["kind"] == "drive_file"]) == 12
    assert registry.get("fixture", "primary") == record
    assert len(api.created) == 10


def test_lost_ack_on_project_index_content_reconciles_without_rewrite(tmp_path: Path) -> None:
    ledger, _, provisioner, _ = setup(tmp_path)
    request_id = admit(ledger, "fixture", "Fixture")
    source_registry = _local_source_binding(ledger, tmp_path, request_id)
    record = provisioner.provision(request_id, "fixture", "primary", "Fixture")
    writer = FakeStateWriter(record)
    writer.lose_ack_key = "project-state:fixture"
    with pytest.raises(DriveProvisionError) as error:
        provisioner.populate(
            request_id, record, source_registry=source_registry, writer=writer,
            repository_id=12345, repository_url="https://github.com/Owner/fixture",
        )
    assert error.value.code == "PROVISION_DOC_UNCERTAIN"
    pending = next(item for item in ledger.get(request_id)["effects"]
                   if item["step_id"] == "drive-project-state-content")
    assert pending["state"] == "INTENDED"
    provisioner.populate(
        request_id, record, source_registry=source_registry, writer=writer,
        repository_id=12345, repository_url="https://github.com/Owner/fixture",
    )
    assert writer.writes == ["project-state:fixture", "source-context:fixture/primary"]
    assert all(item["state"] == "CONFIRMED" for item in ledger.get(request_id)["effects"])


def test_populate_refuses_nonempty_unowned_project_state(tmp_path: Path) -> None:
    ledger, _, provisioner, _ = setup(tmp_path)
    request_id = admit(ledger, "fixture", "Fixture")
    source_registry = _local_source_binding(ledger, tmp_path, request_id)
    record = provisioner.provision(request_id, "fixture", "primary", "Fixture")
    writer = FakeStateWriter(record)
    writer.contents["project-state:fixture"] = "Owner content\n"
    with pytest.raises(DriveProvisionError) as error:
        provisioner.populate(
            request_id, record, source_registry=source_registry, writer=writer,
            repository_id=12345, repository_url="https://github.com/Owner/fixture",
        )
    assert error.value.code == "PROVISION_DOC_NOT_EMPTY"
    assert writer.contents["project-state:fixture"] == "Owner content\n"
    assert writer.writes == []


@pytest.mark.parametrize("repository_url", [
    "https://github.com/Owner/fixture\n",
    "https://github.com/Owner/fixture?token=hidden",
    "https://evil.example/Owner/fixture",
])
def test_populate_rejects_untrusted_repository_url_without_doc_write(
    tmp_path: Path, repository_url: str,
) -> None:
    ledger, _, provisioner, _ = setup(tmp_path)
    request_id = admit(ledger, "fixture", "Fixture")
    source_registry = _local_source_binding(ledger, tmp_path, request_id)
    record = provisioner.provision(request_id, "fixture", "primary", "Fixture")
    writer = FakeStateWriter(record)
    with pytest.raises(DriveProvisionError) as error:
        provisioner.populate(
            request_id, record, source_registry=source_registry, writer=writer,
            repository_id=12345, repository_url=repository_url,
        )
    assert error.value.code == "PROVISION_REPOSITORY_INVALID"
    assert writer.writes == []


def test_reconciled_workspace_folder_prefix_continues_under_resume_without_duplicate(
    tmp_path: Path,
) -> None:
    from runner_app.storage.ledger import EffectAdoptionProof
    from runner_app.workflows.reconcile_drive import DriveEffectReader

    ledger, api, provisioner, registry = setup(tmp_path)
    original_id = admit(ledger, "fixture", "Fixture")
    source_registry = _local_source_binding(ledger, tmp_path, original_id)
    api.lost_ack_key = "workspace:fixture/primary"
    with pytest.raises(DriveUploadError) as error:
        provisioner.provision(original_id, "fixture", "primary", "Fixture")
    assert error.value.code == "UPLOAD_WRITE_UNCERTAIN"
    assert len(api.created) == 5
    original_ids = {item["id"] for item in api.created}
    ledger.finish(original_id, "INTERRUPTED", code="NEEDS_RECONCILIATION")
    old = ledger.get(original_id)
    assert old is not None and len(old["effects"]) == 7
    reader = DriveEffectReader(provisioner.attached, provisioner.upload,
                               cast(Any, object()))
    unknown = next(effect for effect in old["effects"]
                   if effect["step_id"] == "drive-workspace-folder")
    proof = reader(old, unknown)
    assert proof is not None
    ledger.confirm_effect(unknown["effect_id"], proof.provider_id)
    ledger.confirm_stopped(original_id)
    old = ledger.get(original_id)
    assert old is not None and not old["reconcile_required"]
    assert all(effect["state"] == "CONFIRMED" for effect in old["effects"])

    state = ledger.state()
    now = datetime.now(timezone.utc)
    resume = {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "RESUME_PROVISION", "project_id": "fixture",
        "workspace_id": None, "expected_head": None,
        "params": {"resume_version": "1.0", "target_request_id": original_id},
        "intent": "Continue fake reconciled project",
    }
    assert ledger.admit(
        resume, freeze(resume).digest, "local-owner",
        ("repo:fixture", "global:owner", "global:drive-installation"),
    ).execute
    for effect in old["effects"]:
        if effect["kind"] != "drive_file":
            continue
        readback = reader(old, {**effect, "state": "UNKNOWN"})
        assert readback is not None
        ledger.adopt_confirmed_effect(
            resume["request_id"], EffectAdoptionProof(
                effect["effect_id"], readback.provider_id, readback.observed_intent,
            ),
        )
    record = provisioner.provision(
        resume["request_id"], "fixture", "primary", "Fixture",
        resume_target_id=original_id,
    )
    assert record == registry.get("fixture", "primary")
    assert len(api.created) == 10
    assert original_ids == {item["id"] for item in api.created[:5]}
    assert len({item["id"] for item in api.created}) == 10
    effects = ledger.get(resume["request_id"])["effects"]
    assert len(effects) == 10
    assert len([item for item in effects if "adoption" in item]) == 5
    assert all(item["state"] == "CONFIRMED" for item in effects)
    writer = FakeStateWriter(record)
    content = provisioner.populate(
        resume["request_id"], record, source_registry=source_registry,
        writer=writer, repository_id=12345,
        repository_url="https://github.com/Owner/fixture",
        resume_target_id=original_id,
    )
    state_text = writer.contents["project-state:fixture"]
    project_state = json.loads(state_text.split("\n", 1)[1])
    assert project_state["created_at"] == old["received_at"]
    assert old["received_at"] in writer.contents["source-context:fixture/primary"]
    assert set(content) == {"project_state_sha256", "context_sha256"}
    assert len(ledger.get(resume["request_id"])["effects"]) == 12
