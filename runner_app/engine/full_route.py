"""Assemble a workspace full route only from proven owner-side capabilities.

The production Runtime may register mailboxes without this optional port.
When supplied, each project is bound to its exact local source, private GitHub
repository, Drive registration, profile, rootless runtime and quota readiness
before DELIVER_VERIFY_PUBLISH is exposed or its COMMAND document is polled.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from runner_app.adapters.drive_poller import FrozenSlotStore
from runner_app.adapters.drive_publish import DriveDeliveryPublisher
from runner_app.adapters.drive_registry import WorkspaceDriveRegistration
from runner_app.adapters.drive_writer import DocsCASWriter
from runner_app.executors.run_service import RunService, TrustedPreparation
from runner_app.executors.verify_handler import (
    PreprovisionedVolumeProvider,
    TrustedPreparationProvider,
)
from runner_app.git.github_transport import GitHubGitTransport
from runner_app.git.registry import SourceRegistry
from runner_app.storage.ledger import Ledger
from runner_app.storage.paths import Paths
from runner_app.workflows.deliver_verify_publish import DeliverVerifyPublishWorkflow
from runner_app.workflows.publish_run import PublishRunWorkflow, _matches_project_remote

if TYPE_CHECKING:
    from runner_app.engine.integrations import RuntimeIntegrations


class RouteReadinessError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ExecutionPorts:
    """Trusted local ports; a command never supplies any of these callables."""

    runs: RunService
    volume_for_run: PreprovisionedVolumeProvider
    preparation_for_run: TrustedPreparationProvider
    quota_ready: Callable[[str], bool]


def make_full_route_factory(
    integrations: RuntimeIntegrations, ledger: Ledger, registry: SourceRegistry,
    paths: Paths, ports: ExecutionPorts,
) -> Callable[[WorkspaceDriveRegistration, str, FrozenSlotStore], DeliverVerifyPublishWorkflow]:
    """Return a per-workspace builder; construction performs no provider write."""
    if (ports.runs.ledger.database.path != ledger.database.path
            or ports.runs.registry.database.path != registry.database.path
            or ports.runs.evidence.database.path != ledger.database.path):
        raise RouteReadinessError("EXECUTION_LEDGER_MISMATCH")

    def build(registration: WorkspaceDriveRegistration, profile_id: str,
              slots: FrozenSlotStore) -> DeliverVerifyPublishWorkflow:
        project_id, workspace_id = registration.project_id, registration.workspace_id
        if (registration.installation_id != integrations.attached.index.installation_id
                or workspace_id != "primary"):
            raise RouteReadinessError("WORKSPACE_DRIVE_IDENTITY_CHANGED")
        ports.runs.profiles.require(profile_id)
        try:
            ready = ports.quota_ready(profile_id)
        except Exception:  # noqa: BLE001 -- helper/provider details stay local
            raise RouteReadinessError("DISK_QUOTA_UNAVAILABLE") from None
        if ready is not True:
            raise RouteReadinessError("DISK_QUOTA_UNAVAILABLE")
        probe = getattr(ports.runs.broker, "probe", None)
        if not callable(probe):
            raise RouteReadinessError("ROOTLESS_RUNTIME_UNAVAILABLE")
        try:
            observed = probe()
        except Exception:  # noqa: BLE001 -- broker details are not command output
            raise RouteReadinessError("ROOTLESS_RUNTIME_UNAVAILABLE") from None
        if getattr(observed, "available", None) is not True:
            raise RouteReadinessError("ROOTLESS_RUNTIME_UNAVAILABLE")
        project = registry.get_project(project_id)
        binding = registry.get_project_remote(project_id)
        if project is None or binding is None:
            raise RouteReadinessError("PROJECT_REMOTE_UNAVAILABLE")
        owner, name, provider_id = (
            binding.get("owner"), binding.get("name"), binding.get("provider_id"),
        )
        if (not isinstance(owner, str) or not isinstance(name, str)
                or type(provider_id) is not int or provider_id < 1):
            raise RouteReadinessError("PROJECT_REMOTE_INVALID")
        repository = integrations.github.get_repository(
            owner, name, expected_repo_id=provider_id,
        )
        if not _matches_project_remote(project, binding, repository):
            raise RouteReadinessError("PROJECT_REMOTE_CHANGED")
        receipt_key = f"workspace-receipt:{project_id}/{workspace_id}"
        latest_key = f"latest-result:{project_id}/{workspace_id}"
        writer = DocsCASWriter(
            cast(Any, integrations.provider), integrations.attached,
            extra_docs=registration.output_docs(
                receipt_key=receipt_key, latest_result_key=latest_key,
            ),
        )
        publication = PublishRunWorkflow(
            ledger, registry, integrations.github, repository,
            git_transport=GitHubGitTransport(repository, integrations.token_provider),
        )
        delivery = DriveDeliveryPublisher(
            ledger, cast(Any, integrations.upload), integrations.attached,
            writer, integrations.drive_registry,
        )

        def required_preparation(request_id: str, selected_profile_id: str
                                 ) -> TrustedPreparation | None:
            if selected_profile_id != profile_id:
                raise RouteReadinessError("PROFILE_BINDING_CHANGED")
            preparation = ports.preparation_for_run(request_id, selected_profile_id)
            if preparation is None:
                raise RouteReadinessError("PREPARATION_NOT_CONFIGURED")
            return preparation

        return DeliverVerifyPublishWorkflow(
            integrations.source, ports.runs, slots, publication, delivery,
            volume_for_run=ports.volume_for_run,
            preparation_for_run=cast(TrustedPreparationProvider, required_preparation),
            bundle_parent=paths.state_dir / "delivery-bundles",
            receipt_key=receipt_key, latest_result_key=latest_key,
        )

    return build
