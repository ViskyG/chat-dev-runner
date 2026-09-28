"""Full-route wiring is available only after real owner-side readiness proofs."""
from __future__ import annotations

from dataclasses import replace
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from runner_app.adapters.drive_poller import FrozenSlotStore
from runner_app.adapters.drive_registry import WorkspaceDriveRegistration
from runner_app.adapters.github import GitHubRepository
from runner_app.domain.commands import LocalPolicy
from runner_app.engine.full_route import (
    ExecutionPorts,
    RouteReadinessError,
    make_full_route_factory,
)
from runner_app.engine.integrations import OwnerRuntimeConfig, RuntimeIntegrations
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger
from runner_app.storage.paths import Paths
from runner_app.workflows.deliver_verify_publish import DeliverVerifyPublishWorkflow


def _case(tmp_path: Path, *, quota: bool = True, rootless: bool = True,
          remote_id: int = 42) -> tuple[
              RuntimeIntegrations, Ledger, Any, Paths, ExecutionPorts,
              WorkspaceDriveRegistration,
          ]:
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    paths.ensure_private()
    ledger = Ledger(Database(paths.database_path))
    ledger.startup()
    fake, config, _, mapping = import_module("test_drive_index").legacy_fixture()
    manifest = import_module("runner_app.adapters.drive_index").LegacyInstallationManifest.from_mapping(
        mapping, config,
    )
    attached = import_module("runner_app.adapters.drive_index").attach_existing(
        fake, config, legacy_manifest=manifest,
    )
    repository = GitHubRepository(
        id=42, node_id="R_42", owner="ViskyG", owner_id=12452296,
        name="toy", full_name="ViskyG/toy",
        html_url="https://github.com/ViskyG/toy",
        clone_url="https://github.com/ViskyG/toy.git",
        ssh_url="git@github.com:ViskyG/toy.git", private=True,
        default_branch="main", description="cdr:v1:" + "a" * 64,
    )
    binding = {
        "provider": "github", "provider_id": remote_id, "node_id": repository.node_id,
        "owner": repository.owner, "name": repository.name,
        "html_url": repository.html_url, "clone_url": repository.clone_url,
        "private": True, "provision_marker": repository.description,
    }

    class Registry:
        database = ledger.database

        def get_project(self, project_id: str) -> dict[str, Any]:
            assert project_id == "toy"
            return {"origin_kind": "new", "origin_reference": None}

        def get_project_remote(self, project_id: str) -> dict[str, Any]:
            assert project_id == "toy"
            return binding

        def get_workspace(self, project_id: str, workspace_id: str) -> dict[str, Any] | None:
            assert project_id == "toy"
            return ({"state": "ACTIVE", "archived": False}
                    if workspace_id in {"primary", "experiment"} else None)

        def get_workspace_profile(self, project_id: str, workspace_id: str
                                  ) -> dict[str, str] | None:
            assert project_id == "toy"
            return ({"template_id": "python-basic", "profile_id": "python-quick"}
                    if workspace_id in {"primary", "experiment"} else None)

    registry = Registry()

    class DriveRegistry:
        registration: WorkspaceDriveRegistration | None = None

        def get(self, project_id: str, workspace_id: str) -> WorkspaceDriveRegistration | None:
            assert project_id == "toy"
            assert self.registration is not None
            return self.registration if workspace_id == self.registration.workspace_id else None

    drive_registry = DriveRegistry()

    class GitHub:
        def get_repository(self, owner: str, name: str, *, expected_repo_id: int | None = None
                           ) -> GitHubRepository:
            assert (owner, name, expected_repo_id) == ("ViskyG", "toy", remote_id)
            return repository

    class Profiles:
        def require(self, profile_id: str) -> object:
            assert profile_id == "python-quick"
            return object()

    runs = SimpleNamespace(
        ledger=ledger, registry=registry,
        evidence=SimpleNamespace(database=ledger.database),
        profiles=Profiles(), broker=SimpleNamespace(probe=lambda: SimpleNamespace(available=rootless)),
    )
    owner = OwnerRuntimeConfig.from_mapping({
        "schema_version": "1.0", "mode": "attach_existing",
        "drive_root_id": config.root_id, "drive_index_id": config.index_id,
        "github_login": "ViskyG", "github_user_id": 12452296,
        "github_identity": "viskyg", "template_profiles": {"python-basic": "python-quick"},
        "import_roots": [], "github_imports": [],
    })
    bindings = RuntimeIntegrations(
        owner, attached, fake, cast(Any, object()), cast(Any, GitHub()),
        lambda: "fake-token-never-used", cast(Any, object()), cast(Any, object()),
        cast(Any, drive_registry), cast(Any, SimpleNamespace(ledger=ledger)),
        LocalPolicy(), {},
    )
    ports = ExecutionPorts(
        cast(Any, runs), lambda _request_id: None,
        lambda _request_id, _profile_id: None,
        lambda _profile_id: quota,
    )
    registration = WorkspaceDriveRegistration(
        attached.index.installation_id, "toy", "primary", "projectFolder123",
        "workspaceFolder123", "commandDocument123", "changesetDocument123",
        "receiptDocument123", "latestResultDocument123", "projectStateDocument123",
        "sourceContextDocument123", "projectCommandDocument123",
        "projectReceiptDocument123",
    )
    drive_registry.registration = registration
    return bindings, ledger, registry, paths, ports, registration


def test_full_route_factory_assembles_exact_ports_without_write(tmp_path: Path) -> None:
    bindings, ledger, registry, paths, ports, registration = _case(tmp_path)
    slots = FrozenSlotStore(tmp_path / "slots")
    factory = make_full_route_factory(bindings, ledger, cast(Any, registry), paths, ports)
    route = factory(registration, "python-quick", slots)
    assert isinstance(route, DeliverVerifyPublishWorkflow)
    assert route.frozen_slots is slots
    assert route.publication.repository.full_name == "ViskyG/toy"
    assert route.receipt_key == "workspace-receipt:toy/primary"
    assert route.latest_result_key == "latest-result:toy/primary"
    assert cast(Any, route.delivery).writer.registration(route.receipt_key).file_id == registration.receipt_doc_id
    assert cast(Any, route.delivery).writer.registration(route.latest_result_key).file_id == (
        registration.latest_result_doc_id
    )
    assert all(method in {"files.get", "documents.get"}
               for method, _ in cast(Any, bindings.provider).calls)


def test_full_route_factory_binds_secondary_workspace_outputs_without_write(tmp_path: Path) -> None:
    bindings, ledger, registry, paths, ports, primary = _case(tmp_path)
    secondary = replace(
        primary, workspace_id="experiment", workspace_folder_id="secondFolder123",
        command_doc_id="secondCommand123", changeset_doc_id="secondChangeset123",
        receipt_doc_id="secondReceipt123", latest_result_doc_id="secondLatest123",
        context_doc_id="secondContext123",
    )
    cast(Any, bindings.drive_registry).registration = secondary
    slots = FrozenSlotStore(tmp_path / "slots")
    route = make_full_route_factory(bindings, ledger, cast(Any, registry), paths, ports)(
        secondary, "python-quick", slots,
    )
    assert route.receipt_key == "workspace-receipt:toy/experiment"
    assert route.latest_result_key == "latest-result:toy/experiment"
    writer = cast(Any, route.delivery).writer
    assert writer.registration(route.receipt_key).file_id == secondary.receipt_doc_id
    assert writer.registration(route.latest_result_key).file_id == secondary.latest_result_doc_id
    assert writer.registration(route.receipt_key).parent_id == secondary.workspace_folder_id
    assert all(method in {"files.get", "documents.get"}
               for method, _ in cast(Any, bindings.provider).calls)


def test_full_route_factory_refuses_swapped_secondary_drive_outputs(tmp_path: Path) -> None:
    bindings, ledger, registry, paths, ports, primary = _case(tmp_path)
    secondary = replace(
        primary, workspace_id="experiment", workspace_folder_id="secondFolder123",
        command_doc_id="secondCommand123", changeset_doc_id="secondChangeset123",
        receipt_doc_id="secondReceipt123", latest_result_doc_id="secondLatest123",
        context_doc_id="secondContext123",
    )
    cast(Any, bindings.drive_registry).registration = secondary
    swapped = replace(secondary, receipt_doc_id="foreignReceipt123")
    factory = make_full_route_factory(bindings, ledger, cast(Any, registry), paths, ports)
    with pytest.raises(RouteReadinessError) as error:
        factory(swapped, "python-quick", FrozenSlotStore(tmp_path / "slots"))
    assert error.value.code == "WORKSPACE_DRIVE_IDENTITY_CHANGED"


@pytest.mark.parametrize(
    ("quota", "rootless", "remote_id", "code"),
    [
        (False, True, 42, "DISK_QUOTA_UNAVAILABLE"),
        (True, False, 42, "ROOTLESS_RUNTIME_UNAVAILABLE"),
        (True, True, 41, "PROJECT_REMOTE_CHANGED"),
    ],
)
def test_full_route_factory_refuses_unready_or_changed_identity(
    tmp_path: Path, quota: bool, rootless: bool, remote_id: int, code: str,
) -> None:
    bindings, ledger, registry, paths, ports, registration = _case(
        tmp_path, quota=quota, rootless=rootless, remote_id=remote_id,
    )
    factory = make_full_route_factory(bindings, ledger, cast(Any, registry), paths, ports)
    with pytest.raises(RouteReadinessError) as error:
        factory(registration, "python-quick", FrozenSlotStore(tmp_path / "slots"))
    assert error.value.code == code
