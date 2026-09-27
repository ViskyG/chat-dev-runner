"""Drive RECONCILE reads exact scoped files and document hashes without writes."""
from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

from test_drive_provision import (
    FakeStateWriter,
    admit,
    installation,
    setup,
)
from test_drive_root_receipt import _resources
from test_drive_root_receipt import _setup as root_setup

from runner_app.adapters.drive_upload import GoogleUploadProvider
from runner_app.workflows.reconcile_drive import DriveEffectReader


def _uncertain(effect: dict) -> dict:
    return {**effect, "state": "INTENDED", "provider_id": None}


def test_folder_lost_ack_uses_existing_installation_key_and_exact_scope(tmp_path: Path) -> None:
    ledger, api, provisioner, _ = setup(tmp_path)
    request_id = admit(ledger, "fixture", "Fixture")
    record = provisioner.provision(request_id, "fixture", "primary", "Fixture")
    target = ledger.get(request_id)
    assert target is not None
    target = {**target, "status": "BLOCKED"}
    attached = installation(api)
    upload = GoogleUploadProvider(api, attached=attached)
    reader = DriveEffectReader(attached, upload, FakeStateWriter(record))
    effect = _uncertain(next(
        item for item in target["effects"] if item["step_id"] == "drive-project-folder"
    ))
    before = len(api.created)
    proof = reader(target, effect)
    assert proof is not None and proof.provider_id == record.project_folder_id
    assert proof.observed_intent == effect["intent"]
    assert len(api.created) == before

    api.files_by_id[record.project_folder_id]["appProperties"]["cdr_key"] = "other-project"
    assert reader(target, effect) is None
    assert len(api.created) == before


def test_document_lost_ack_needs_registered_id_and_exact_content_hash(
    tmp_path: Path,
) -> None:
    ledger, api, provisioner, _ = setup(tmp_path)
    request_id = admit(ledger, "fixture", "Fixture")
    record = provisioner.provision(request_id, "fixture", "primary", "Fixture")
    writer = FakeStateWriter(record)
    registered = record.state_docs()[0]
    text = "Project context for fixture\n"
    writer.contents[registered.key] = text
    desired = hashlib.sha256(text.encode()).hexdigest()
    effect = {
        "effect_id": str(uuid4()), "step_id": "drive-project-state-content",
        "kind": "drive_file", "state": "INTENDED", "provider_id": None,
        "intent": {"file_id": registered.file_id, "key": registered.key,
                   "base_revision": "rev-1", "desired_sha256": desired},
    }
    target = {"status": "BLOCKED", "project_id": "fixture",
              "workspace_id": None, "action": "CREATE_PROJECT"}
    attached = installation(api)
    reader = DriveEffectReader(
        attached, GoogleUploadProvider(api, attached=attached), writer,
    )
    proof = reader(target, effect)
    assert proof is not None and proof.provider_id == registered.file_id
    assert proof.observed_intent == effect["intent"]
    assert writer.writes == []
    writer.contents[registered.key] = "Human changed this Doc\n"
    assert reader(target, effect) is None


def test_unrelated_or_missing_drive_resource_is_not_confirmed(tmp_path: Path) -> None:
    ledger, api, provisioner, _ = setup(tmp_path)
    request_id = admit(ledger, "fixture", "Fixture")
    record = provisioner.provision(request_id, "fixture", "primary", "Fixture")
    attached = installation(api)
    reader = DriveEffectReader(
        attached, GoogleUploadProvider(api, attached=attached), FakeStateWriter(record),
    )
    target = {"status": "BLOCKED", "project_id": "fixture",
              "workspace_id": None, "action": "CREATE_PROJECT"}
    effect = {
        "effect_id": str(uuid4()), "step_id": "drive-project-folder",
        "kind": "drive_file", "state": "INTENDED", "provider_id": None,
        "intent": {"parent_id": record.project_folder_id,
                   "installation_id": attached.index.installation_id,
                   "key": "different-project", "name": "Different project",
                   "mime_type": "application/vnd.google-apps.folder",
                   "project_id": "fixture", "workspace_id": None},
    }
    assert reader(target, effect) is None
    assert len(api.created) == 10


def test_root_receipt_lost_ack_reads_exact_attached_document(tmp_path: Path) -> None:
    ledger, api, publisher, command = root_setup(tmp_path)
    assert publisher.publish(command["request_id"], resources=_resources()) == publisher.receipt_id
    target = ledger.get(command["request_id"])
    assert target is not None
    target = {**target, "status": "BLOCKED"}
    effect = _uncertain(next(
        item for item in target["effects"]
        if item["step_id"] == "drive-root-create-receipt"
    ))

    class UnusedUpload:
        def get_file(self, *_args: object, **_kwargs: object) -> dict:
            raise AssertionError("Root receipt proof must use the registered Doc")

        def find_by_key(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("Root receipt proof must use the registered Doc")

    reader = DriveEffectReader(publisher.attached, UnusedUpload(), publisher.writer)
    proof = reader(target, effect)
    assert proof is not None and proof.provider_id == publisher.receipt_id
    assert proof.observed_intent == effect["intent"]
    assert len(api.batch_ids) == 1
    api.set_document(publisher.receipt_id, "Human changed the receipt\n")
    assert reader(target, effect) is None
    assert len(api.batch_ids) == 1
