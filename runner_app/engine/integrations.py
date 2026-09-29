"""Owner-configured EP-05 ports for the existing Drive installation.

An absent private configuration file leaves Runtime without provider handlers.
Loading a configured installation performs exact-ID reads and identity checks;
it never arms the Ledger, polls a mailbox, or writes to either provider.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, cast

from runner_app.adapters.drive_auth import load_authorized_credentials
from runner_app.adapters.drive_docs import DriveError, ReadProvider
from runner_app.adapters.drive_google import GoogleReadProvider
from runner_app.adapters.drive_index import (
    AttachedInstallation,
    ExistingInstallation,
    LegacyInstallationManifest,
    attach_existing,
)
from runner_app.adapters.drive_poller import FrozenSlotStore
from runner_app.adapters.drive_project_receipt import ProjectTerminalReceiptPublisher
from runner_app.adapters.drive_provision import DriveProjectProvisioner, ProjectUploadProvider
from runner_app.adapters.drive_registry import WorkspaceDriveRegistration, WorkspaceDriveRegistry
from runner_app.adapters.drive_root_receipt import RootCreateProjectReceiptPublisher
from runner_app.adapters.drive_transport import isolated_request_builder
from runner_app.adapters.drive_upload import GoogleUploadProvider
from runner_app.adapters.drive_writer import DocsCASWriter, ObservedOutputDoc, RegisteredOutputDoc
from runner_app.adapters.github import GitHubREST, UrllibGitHubTransport
from runner_app.config import DRIVE_INDEX_ID, DRIVE_ROOT_ID
from runner_app.domain.commands import DomainError, LocalPolicy, Provenance, parse_json
from runner_app.engine.context_route import ExportContextRoute
from runner_app.engine.service import Handler
from runner_app.git.github_import import ApprovedGitHubImport, GitHubImportService
from runner_app.git.github_transport import GitHubGitTransport, OwnerTokenFile
from runner_app.git.imports import ImportGrants
from runner_app.git.registry import SourceRegistry
from runner_app.git.service import SourceService
from runner_app.storage.ledger import Ledger
from runner_app.storage.paths import Paths, UnsafePathError, open_private_file
from runner_app.workflows.create_project import (
    CreateProjectWorkflow,
    GitHubBaselineBootstrap,
    GitHubLocalImportBootstrap,
)
from runner_app.workflows.create_workspace import CreateWorkspaceWorkflow
from runner_app.workflows.fetch import FetchWorkflow
from runner_app.workflows.provision_resume_complete import CompleteProvisionRecoveryWorkflow
from runner_app.workflows.provision_resume_partial import PartialProvisionRecoveryWorkflow
from runner_app.workflows.reconcile import ReconcileWorkflow
from runner_app.workflows.reconcile_drive import DriveEffectReader
from runner_app.workflows.reconcile_github import GitHubEffectReader
from runner_app.workflows.reconcile_local import ManagedGitEffectReader
from runner_app.workflows.retarget_drive_state import RetargetDriveState
from runner_app.workflows.retarget_resume import ResumeRetargetWorkflow
from runner_app.workflows.retarget_workspace import RetargetWorkspaceWorkflow

CONFIG_FILENAME = "owner-runtime.json"
_MAX_CONFIG_BYTES = 16_384
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z")
_PROFILE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


class IntegrationConfigError(ValueError):
    """Stable local-configuration failure, never including file or token contents."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OwnerRuntimeConfig:
    github_login: str
    github_user_id: int
    github_identity: str
    template_profiles: Mapping[str, str]
    import_roots: tuple[Path, ...]
    github_imports: Mapping[str, ApprovedGitHubImport]
    execution_profile_dir: Path | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> OwnerRuntimeConfig:
        required = {
            "schema_version", "mode", "drive_root_id", "drive_index_id",
            "github_login", "github_user_id", "github_identity", "template_profiles",
            "import_roots", "github_imports",
        }
        if (not required <= set(value) or set(value) - required - {"execution_profile_dir"}
                or value.get("schema_version") != "1.0"
                or value.get("mode") != "attach_existing"
                or value.get("drive_root_id") != DRIVE_ROOT_ID
                or value.get("drive_index_id") != DRIVE_INDEX_ID):
            raise IntegrationConfigError("RUNTIME_CONFIG_INVALID")
        login, user_id, identity = (
            value["github_login"], value["github_user_id"], value["github_identity"],
        )
        if (not isinstance(login, str) or _LOGIN.fullmatch(login) is None
                or type(user_id) is not int or user_id < 1
                or not isinstance(identity, str) or _SLUG.fullmatch(identity) is None):
            raise IntegrationConfigError("RUNTIME_GITHUB_IDENTITY_INVALID")
        profiles = value["template_profiles"]
        if (not isinstance(profiles, dict) or not profiles or len(profiles) > 32
                or any(not isinstance(key, str) or _SLUG.fullmatch(key) is None
                       or not isinstance(profile, str) or _PROFILE.fullmatch(profile) is None
                       for key, profile in profiles.items())):
            raise IntegrationConfigError("RUNTIME_TEMPLATE_POLICY_INVALID")
        raw_roots = value["import_roots"]
        if (not isinstance(raw_roots, list) or len(raw_roots) > 16
                or any(not isinstance(item, str) or len(item) > 4096
                       or not Path(item).is_absolute() or ".." in Path(item).parts
                       for item in raw_roots)
                or len(set(raw_roots)) != len(raw_roots)):
            raise IntegrationConfigError("RUNTIME_IMPORT_ROOTS_INVALID")
        raw_imports = value["github_imports"]
        if not isinstance(raw_imports, list) or len(raw_imports) > 64:
            raise IntegrationConfigError("RUNTIME_GITHUB_IMPORTS_INVALID")
        approvals: dict[str, ApprovedGitHubImport] = {}
        for item in raw_imports:
            if not isinstance(item, dict) or set(item) != {
                "repository", "github_identity", "authenticated_login",
                "repository_id", "owner_id", "node_id",
            }:
                raise IntegrationConfigError("RUNTIME_GITHUB_IMPORTS_INVALID")
            try:
                approval = ApprovedGitHubImport(**item)
            except (TypeError, ValueError):
                raise IntegrationConfigError("RUNTIME_GITHUB_IMPORTS_INVALID") from None
            if (approval.github_identity != identity
                    or approval.authenticated_login.casefold() != login.casefold()
                    or approval.repository in approvals
                    or any(key.casefold() == approval.repository.casefold()
                           for key in approvals)):
                raise IntegrationConfigError("RUNTIME_GITHUB_IMPORTS_INVALID")
            approvals[approval.repository] = approval
        raw_profile_dir = value.get("execution_profile_dir")
        if raw_profile_dir is not None and (
            not isinstance(raw_profile_dir, str) or not raw_profile_dir
            or len(raw_profile_dir) > 4096 or not Path(raw_profile_dir).is_absolute()
            or ".." in Path(raw_profile_dir).parts
        ):
            raise IntegrationConfigError("RUNTIME_PROFILE_DIR_INVALID")
        return cls(login, user_id, identity, dict(profiles),
                   tuple(Path(item) for item in raw_roots), approvals,
                   Path(raw_profile_dir) if raw_profile_dir is not None else None)


def load_owner_runtime_config(paths: Paths) -> OwnerRuntimeConfig | None:
    """Read one 0600 owner file; missing means no live handler registration."""
    path = paths.config_dir / CONFIG_FILENAME
    if not os.path.lexists(path):
        return None
    try:
        descriptor = open_private_file(path)
        try:
            raw = os.read(descriptor, _MAX_CONFIG_BYTES + 1)
            if len(raw) > _MAX_CONFIG_BYTES or os.read(descriptor, 1):
                raise IntegrationConfigError("RUNTIME_CONFIG_INVALID")
        finally:
            os.close(descriptor)
        value = parse_json(raw, max_bytes=_MAX_CONFIG_BYTES)
    except (OSError, UnsafePathError, DomainError):
        raise IntegrationConfigError("RUNTIME_CONFIG_INVALID") from None
    return OwnerRuntimeConfig.from_mapping(value)


@dataclass(frozen=True)
class ProviderConnections:
    """Injected fake providers for tests or real adapters after explicit setup."""

    reader: ReadProvider
    upload_factory: Callable[[AttachedInstallation], ProjectUploadProvider]
    github: GitHubREST
    token_provider: Callable[[], str]


def _real_connections(paths: Paths, config: OwnerRuntimeConfig) -> ProviderConnections:
    token_provider = OwnerTokenFile(paths.config_dir / "github-token")
    credentials = load_authorized_credentials(paths, refresh=True)
    try:
        build = import_module("googleapiclient.discovery").build
        request_builder = isolated_request_builder(credentials)
        drive = build("drive", "v3", credentials=credentials,
                      requestBuilder=request_builder, cache_discovery=False)
        docs = build("docs", "v1", credentials=credentials,
                     requestBuilder=request_builder, cache_discovery=False)
    except Exception:  # noqa: BLE001 -- Google exceptions can contain credential material
        raise DriveError("GOOGLE_SERVICE_UNAVAILABLE", "Google services are unavailable") from None
    reader = GoogleReadProvider(drive, docs)
    github = GitHubREST(
        UrllibGitHubTransport(token_provider), expected_login=config.github_login,
        expected_user_id=config.github_user_id,
    )
    return ProviderConnections(
        reader, lambda attached: cast(ProjectUploadProvider, GoogleUploadProvider(drive, attached=attached)),
        github, token_provider,
    )


class _ReconcileDocuments:
    """Resolve only registered output Docs by exact project/workspace ID."""

    def __init__(self, provider: ReadProvider, attached: AttachedInstallation,
                 registry: WorkspaceDriveRegistry) -> None:
        self.provider = provider
        self.attached = attached
        self.registry = registry

    def _writer(self, key: str) -> DocsCASWriter:
        if key == "root-receipt":
            return DocsCASWriter(cast(Any, self.provider), self.attached)
        project_id: str | None = None
        workspace_id = "primary"
        if key.startswith("project-state:"):
            project_id = key.removeprefix("project-state:")
        elif key.startswith("source-context:"):
            pair = key.removeprefix("source-context:").split("/")
            if len(pair) == 2:
                project_id, workspace_id = pair
        if (project_id is None or _SLUG.fullmatch(project_id) is None
                or _SLUG.fullmatch(workspace_id) is None):
            raise DriveError("OUTPUT_NOT_REGISTERED", "Output Doc is not registered")
        registration = self.registry.get(project_id, workspace_id)
        return DocsCASWriter(cast(Any, self.provider), self.attached,
                             extra_docs=registration.state_docs())

    def registration(self, key: str) -> RegisteredOutputDoc:
        return self._writer(key).registration(key)

    def read(self, key: str) -> ObservedOutputDoc:
        return self._writer(key).read(key)


@dataclass(frozen=True)
class RuntimeIntegrations:
    config: OwnerRuntimeConfig
    attached: AttachedInstallation
    provider: ReadProvider
    upload: ProjectUploadProvider
    github: GitHubREST
    token_provider: Callable[[], str]
    root_receipt: RootCreateProjectReceiptPublisher
    project_receipt: ProjectTerminalReceiptPublisher
    drive_registry: WorkspaceDriveRegistry
    source: SourceService
    policy: LocalPolicy
    handlers: Mapping[str, Handler]


def build_runtime_integrations(
    paths: Paths, ledger: Ledger, registry: SourceRegistry,
    import_grants: ImportGrants, config: OwnerRuntimeConfig, *,
    providers: ProviderConnections | None = None,
) -> RuntimeIntegrations:
    """Attach and prove identities before making any handler available to Engine."""
    connections = providers if providers is not None else _real_connections(paths, config)
    selected = ExistingInstallation.from_mapping({
        "schema_version": "1.0", "mode": "attach_existing", "bootstrap_allowed": False,
        "root_folder_name": "Chat-Dev Runner", "root_folder_id": DRIVE_ROOT_ID,
        "installation_index_document_id": DRIVE_INDEX_ID,
    })
    manifest = LegacyInstallationManifest.from_file(
        paths.config_dir / "installation-legacy.json", selected,
    )
    attached = attach_existing(connections.reader, selected, legacy_manifest=manifest)
    identity = connections.github.authenticated_identity()
    if (identity.login.casefold() != config.github_login.casefold()
            or identity.id != config.github_user_id):
        raise IntegrationConfigError("RUNTIME_GITHUB_IDENTITY_CHANGED")
    upload = connections.upload_factory(attached)
    drive_registry = WorkspaceDriveRegistry(
        paths.state_dir / "drive-registrations", attached, connections.reader,
    )
    project_receipt = ProjectTerminalReceiptPublisher(
        ledger, attached, connections.reader, drive_registry,
    )
    source = SourceService(ledger, registry, import_grants=import_grants)
    provisioner = DriveProjectProvisioner(ledger, attached, upload, drive_registry)

    def populate(request_id: str, registration: Any, sources: SourceRegistry,
                 repository: Any) -> Mapping[str, str]:
        writer = DocsCASWriter(cast(Any, connections.reader), attached,
                               extra_docs=registration.state_docs())
        return provisioner.populate(
            request_id, registration, source_registry=sources, writer=writer,
            repository_id=repository.id, repository_url=repository.html_url,
        )

    def populate_workspace(request_id: str, registration: WorkspaceDriveRegistration,
                           sources: SourceRegistry) -> Mapping[str, str]:
        writer = DocsCASWriter(cast(Any, connections.reader), attached,
                               extra_docs=registration.state_docs())
        return provisioner.populate_workspace(
            request_id, registration, source_registry=sources, writer=writer,
        )

    root_writer = DocsCASWriter(cast(Any, connections.reader), attached)
    root_receipt = RootCreateProjectReceiptPublisher(
        ledger, attached, connections.reader, root_writer,
        trusted_github_owner=config.github_login,
    )
    github_import = GitHubImportService(
        ledger, registry, connections.github,
        lambda repository: GitHubGitTransport(repository, connections.token_provider),
        config.github_imports,
    )
    fetch = FetchWorkflow(ledger, registry, connections.github, connections.token_provider)
    retarget_state = RetargetDriveState(
        ledger, attached, connections.reader, drive_registry,
    )
    retarget = RetargetWorkspaceWorkflow(ledger, source, retarget_state)
    resume_retarget = ResumeRetargetWorkflow(ledger, registry, retarget_state)
    create_workspace = CreateWorkspaceWorkflow(
        ledger, source, connections.github, provisioner, populate_workspace,
        template_profiles=config.template_profiles,
    )
    create = CreateProjectWorkflow(
        ledger, source, connections.github, provisioner,
        GitHubBaselineBootstrap(registry, connections.github, connections.token_provider),
        populate, installation_id=attached.index.installation_id,
        github_owner=config.github_login, github_identity=config.github_identity,
        template_profiles=config.template_profiles, github_import=github_import,
        local_import_bootstrap=GitHubLocalImportBootstrap(
            registry, connections.github, connections.token_provider,
        ),
        root_receipt=root_receipt,
    )
    github_reader = GitHubEffectReader(
        connections.github, registry, installation_id=attached.index.installation_id,
        github_owner=config.github_login,
    )
    drive_reader = DriveEffectReader(
        attached, cast(Any, upload),
        _ReconcileDocuments(connections.reader, attached, drive_registry),
    )
    owner_ids = frozenset({"local-owner"})
    reconcile = ReconcileWorkflow(ledger, owner_source_ids=owner_ids, ports={
        "github_repository": github_reader.repository,
        "git_ref": github_reader.ref,
        "pull_request": github_reader.pull_request,
        "drive_file": drive_reader,
        "local_workspace": ManagedGitEffectReader(registry),
    })
    complete_resume = CompleteProvisionRecoveryWorkflow(
        ledger, registry, connections.github, drive_reader, drive_registry,
        installation_id=attached.index.installation_id,
        projects_folder_id=attached.index.files["projects"].file_id,
        github_owner=config.github_login, github_identity=config.github_identity,
        template_profiles=config.template_profiles, owner_source_ids=owner_ids,
        root_command_id=root_receipt.command_id,
    )
    root_slots = FrozenSlotStore(paths.state_dir / "drive-frozen-root")
    resume = PartialProvisionRecoveryWorkflow(
        complete_resume, provisioner,
        lambda registration: DocsCASWriter(
            cast(Any, connections.reader), attached,
            extra_docs=registration.state_docs(),
        ),
        root_proof=lambda request_id: root_receipt.confirmed_partial(
            request_id, root_slots,
        ),
        root_receipt_id=root_receipt.receipt_id,
    )
    policy = LocalPolicy(
        mailboxes={root_receipt.command_id: Provenance(
            "drive", root_receipt.command_id,
        )},
        approved_templates=frozenset(config.template_profiles),
        github_identities=frozenset({config.github_identity}),
        approved_github_repositories=frozenset(config.github_imports),
    )
    return RuntimeIntegrations(
        config, attached, connections.reader, upload, connections.github,
        connections.token_provider, root_receipt, project_receipt,
        drive_registry, source, policy,
        {"CREATE_PROJECT": create, "CREATE_WORKSPACE": create_workspace,
         "RECONCILE": reconcile, "RESUME_PROVISION": resume, "FETCH": fetch,
         "RETARGET_WORKSPACE": retarget, "RESUME_RETARGET": resume_retarget,
         "EXPORT_CONTEXT": ExportContextRoute(ledger, registry)},
    )
