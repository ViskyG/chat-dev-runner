"""One-shot new-project orchestration with durable identities at every effect.

The handler is inert until an owner configures the provider ports and registers
it with Engine. All ports receive only values from an already admitted command
and owner configuration; no request can supply a local path or a credential.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from runner_app.adapters.drive_registry import WorkspaceDriveRegistration
from runner_app.adapters.drive_root_receipt import RootReceiptError
from runner_app.adapters.github import (
    GitHubError,
    GitHubRepository,
    RepositoryResult,
    make_provision_marker,
)
from runner_app.domain.commands import FrozenCommand, validate_shape
from runner_app.engine.service import Context, OperationResult
from runner_app.git.argv import GitError, run_git, validate_branch
from runner_app.git.github_import import GitHubImportResult
from runner_app.git.github_transport import GitHubGitTransport, RepositoryInspector
from runner_app.git.publish import PublicationError
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.git.service import SourceService
from runner_app.storage.ledger import Ledger


class RepositoryProvisioner(Protocol):
    def create_or_attach_private(self, owner: str, name: str, provision_marker: str, *,
                                 expected_repo_id: int | None = None) -> RepositoryResult: ...


class DriveProjectPort(Protocol):
    def provision(self, request_id: str, project_id: str, workspace_id: str,
                  project_name: str) -> WorkspaceDriveRegistration: ...


class RootReceiptPort(Protocol):
    command_id: str
    receipt_id: str

    def publish(self, request_id: str, *, status: str = "COMPLETED",
                code: str | None = None,
                resources: Mapping[str, Any] | None = None) -> str | None: ...


class GitHubImportPort(Protocol):
    def import_source(self, request_id: str) -> GitHubImportResult: ...


BaselinePort = Callable[[str, str, GitHubRepository], str]
LocalImportBaselinePort = Callable[[str, str, str, GitHubRepository], str]
PopulatePort = Callable[[str, WorkspaceDriveRegistration, SourceRegistry, GitHubRepository],
                        Mapping[str, str]]


class GitHubBaselineBootstrap:
    """Owner-configured live baseline port; construction performs no network I/O."""

    def __init__(self, registry: SourceRegistry, inspector: RepositoryInspector,
                 token_provider: Callable[[], str]) -> None:
        self.registry = registry
        self.inspector = inspector
        self.token_provider = token_provider

    def __call__(self, project_id: str, expected_commit: str,
                 repository: GitHubRepository) -> str:
        project = self.registry.get_project(project_id)
        binding = self.registry.get_project_remote(project_id)
        marker = repository.description
        if (project is None or binding is None or marker is None
                or binding.get("provider_id") != repository.id
                or binding.get("node_id") != repository.node_id
                or binding.get("clone_url") != repository.clone_url
                or binding.get("provision_marker") != marker):
            raise GitError("GITHUB_REPOSITORY_UNVERIFIED",
                           "Local project and GitHub repository identity differ")
        transport = GitHubGitTransport(repository, self.token_provider)
        result = transport.bootstrap_main(
            Path(project["repo_path"]), expected_commit,
            inspector=self.inspector, provision_marker=marker,
        )
        if result.repository_id != repository.id or result.commit != expected_commit:
            raise GitError("GITHUB_BASELINE_PUSH_UNKNOWN",
                           "GitHub baseline push proof differs")
        return result.commit


class GitHubLocalImportBootstrap:
    """Push a verified managed local import to a newly created private target."""

    def __init__(self, registry: SourceRegistry, inspector: RepositoryInspector,
                 token_provider: Callable[[], str]) -> None:
        self.registry = registry
        self.inspector = inspector
        self.token_provider = token_provider

    def __call__(self, project_id: str, expected_commit: str,
                 source_branch: str, repository: GitHubRepository) -> str:
        project = self.registry.get_project(project_id)
        binding = self.registry.get_project_remote(project_id)
        if (project is None or project["origin_kind"] != "local"
                or binding is None or binding.get("origin_kind") != "local"
                or binding.get("origin_reference") != project["origin_reference"]
                or binding.get("provider_id") != repository.id
                or binding.get("owner_id") != repository.owner_id
                or binding.get("node_id") != repository.node_id
                or binding.get("clone_url") != repository.clone_url
                or binding.get("source_branch") != source_branch
                or binding.get("source_head") != expected_commit
                or binding.get("provision_marker") != repository.description):
            raise GitError("GITHUB_REPOSITORY_UNVERIFIED",
                           "Local import and target repository identity differ")
        marker = repository.description
        if marker is None:
            raise GitError("GITHUB_REPOSITORY_UNVERIFIED",
                           "Local import target has no provision marker")
        transport = GitHubGitTransport(repository, self.token_provider)
        result = transport.bootstrap_imported_main(
            Path(project["repo_path"]), expected_commit,
            registry=self.registry, project_id=project_id,
            expected_source_branch=source_branch,
            inspector=self.inspector, provision_marker=marker,
        )
        if result.repository_id != repository.id or result.commit != expected_commit:
            raise GitError("GITHUB_IMPORT_PUSH_UNKNOWN",
                           "Imported baseline push proof differs")
        return result.commit


class CreateProjectWorkflow:
    """CREATE_PROJECT(new), using only configured and identity-checking ports.

    No fallback to an unrelated same-name repository exists. An unknown effect
    stays in the Ledger for explicit reconciliation, including when a provider
    loses its response. Cancellation stops the next effect, never rolls back
    resources already confirmed by their provider.
    """

    def __init__(self, ledger: Ledger, source: SourceService,
                 github: RepositoryProvisioner, drive: DriveProjectPort,
                 bootstrap: BaselinePort, populate: PopulatePort, *, installation_id: str,
                 github_owner: str, template_profiles: Mapping[str, str],
                 github_identity: str | None = None,
                 github_import: GitHubImportPort | None = None,
                 local_import_bootstrap: LocalImportBaselinePort | None = None,
                 root_receipt: RootReceiptPort | None = None,
                 root_command_id: str | None = None) -> None:
        if ledger.database.path != source.ledger.database.path:
            raise ValueError("Workflow and source must use one admission database")
        if not installation_id or not github_owner or not template_profiles:
            raise ValueError("Owner-approved installation, GitHub owner and profiles are required")
        identity_alias = github_identity if github_identity is not None else github_owner
        if (re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", identity_alias) is None
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", github_owner) is None):
            raise ValueError("Approved GitHub alias and owner are required")
        if (root_receipt is not None
                and (not root_receipt.command_id or not root_receipt.receipt_id
                     or (root_command_id is not None
                         and root_command_id != root_receipt.command_id))):
            raise ValueError("Root receipt port does not match the configured command")
        self.root_command_id = (
            root_receipt.command_id if root_receipt is not None else root_command_id
        )
        self.root_receipt = root_receipt
        self.ledger = ledger
        self.source = source
        self.github = github
        self.github_import = github_import
        self.local_import_bootstrap = local_import_bootstrap
        self.drive = drive
        self.bootstrap = bootstrap
        self.populate = populate
        self.installation_id = installation_id
        self.github_owner = github_owner
        self.github_identity = identity_alias
        self.template_profiles = dict(template_profiles)

    def _configure_origin(self, request_id: str, repo: Path,
                          repository: GitHubRepository) -> None:
        """Attach only the verified GitHub URL to the managed bare repository."""
        if run_git(repo, ["remote"]).strip():
            raise GitError("REMOTE_NOT_ALLOWED",
                           "Managed repository has an unexpected remote")
        self.ledger.journal(
            request_id, "origin-binding", "INTENDED",
            {"remote": "origin", "repository_id": repository.id},
        )
        run_git(repo, ["remote", "add", "origin", repository.clone_url])
        if run_git(repo, ["remote", "get-url", "origin"]).strip() != repository.clone_url:
            raise GitError("REMOTE_NOT_ALLOWED", "Configured GitHub origin differs")
        self.ledger.journal(
            request_id, "origin-binding", "CONFIRMED",
            {"remote": "origin", "repository_id": repository.id},
        )

    def __call__(self, frozen: FrozenCommand, context: Context) -> OperationResult:
        command = frozen.command
        validate_shape(command)
        if command["action"] != "CREATE_PROJECT":
            return OperationResult("BLOCKED", "WRONG_WORKFLOW_ACTION")
        request_id, project_id = command["request_id"], command["project_id"]
        if context.ledger.database.path != self.ledger.database.path:
            return OperationResult("FAULT", "WORKFLOW_LEDGER_MISMATCH")
        operation = self.ledger.get(request_id)
        if (operation is None or operation["status"] != "RUNNING"
                or operation["command"] != command
                or operation["digest"] != frozen.digest):
            return OperationResult("BLOCKED", "OPERATION_NOT_RUNNING")
        drive_origin = (
            self.root_command_id is not None
            and operation["source_id"] == self.root_command_id
        )
        if drive_origin and self.root_receipt is None:
            return OperationResult("BLOCKED", "ROOT_RECEIPT_NOT_CONFIGURED")
        source = command["params"]["source"]
        if source["kind"] == "github":
            if self.github_import is None:
                return OperationResult("BLOCKED", "GITHUB_IMPORT_NOT_CONFIGURED")
        elif source["kind"] == "local":
            if self.source.import_grants is None or self.local_import_bootstrap is None:
                return OperationResult("BLOCKED", "LOCAL_IMPORT_NOT_CONFIGURED")
        elif source["kind"] != "new":
            return OperationResult("BLOCKED", "SOURCE_KIND_UNSUPPORTED")
        if command["params"]["github_identity"] != self.github_identity:
            return OperationResult("BLOCKED", "GITHUB_IDENTITY_MISMATCH")
        template_id = command["params"]["template_id"]
        profile_id = self.template_profiles.get(template_id)
        if profile_id is None:
            return OperationResult("BLOCKED", "TEMPLATE_BINDING_MISSING")

        # Engine reserves the project, GitHub identity and attached installation.
        # Do not let a directly invoked handler bypass admission reservations.
        scopes = {f"repo:{project_id}", f"global:{self.github_identity}",
                  "global:drive-installation"}
        if not all(self.ledger.holds_lock(request_id, scope) for scope in scopes):
            return OperationResult("BLOCKED", "PROVISION_LOCK_MISSING")

        state: dict[str, object] = {
            "project_id": project_id,
            "workspace_id": "primary",
            "template_id": template_id,
            "profile_id": profile_id,
            "github_for_chat": "MANUAL_REQUIRED",
        }

        def cancelled() -> OperationResult | None:
            if context.cancel.is_set():
                return OperationResult("CANCELLED", "CANCEL_REQUESTED", dict(state))
            return None

        try:
            stopped = cancelled()
            if stopped is not None:
                return stopped
            if source["kind"] in {"new", "local"}:
                if source["kind"] == "local":
                    assert self.source.import_grants is not None
                    # Revalidate the owner-issued handle before any GitHub write.
                    # SourceService pins it again while copying committed refs.
                    self.source.import_grants.resolve(source["import_handle"])
                marker = make_provision_marker(self.installation_id, project_id, request_id)
                effect = self.ledger.begin_effect(
                    request_id, "github-repository", "github_repository",
                    {"owner": self.github_owner, "name": project_id, "private": True,
                     "marker": marker},
                )
                created = self.github.create_or_attach_private(
                    self.github_owner, project_id, marker,
                )
                repository = created.repository
                if (repository.full_name.casefold() !=
                        f"{self.github_owner}/{project_id}".casefold()
                        or repository.private is not True):
                    raise GitHubError("GITHUB_IDENTITY_MISMATCH",
                                      "Provider returned another repository")
                self.ledger.confirm_effect(effect, str(repository.id))
                github_state: dict[str, object] = {
                    "repository_id": repository.id,
                    "owner": repository.owner,
                    "name": repository.name,
                    "node_id": repository.node_id,
                    "full_name": repository.full_name,
                    "html_url": repository.html_url,
                    "clone_url": repository.clone_url,
                    "private": True,
                    "disposition": created.disposition,
                }
                state["github"] = github_state
                stopped = cancelled()
                if stopped is not None:
                    return stopped

                if source["kind"] == "new":
                    project = self.source.create_new_source(request_id)
                    source_branch = "main"
                else:
                    project = self.source.import_local_source(request_id)
                    source_branch = validate_branch(
                        run_git(Path(project["repo_path"]), [
                            "symbolic-ref", "--quiet", "--short", "HEAD",
                        ]).strip(),
                    )
                    if (project["origin_kind"] != "local"
                            or project["origin_reference"] != source["import_handle"]):
                        raise SourceError("IMPORT_SOURCE_CHANGED",
                                          "Managed local import identity differs")
                    github_state["source_branch"] = source_branch
                repo = Path(project["repo_path"])
                if repo != self.source.registry.managed_root / project_id / "repo.git":
                    raise SourceError("OUTSIDE_MANAGED_ROOT",
                                      "Managed project repository path differs")
                head = run_git(
                    repo, ["rev-parse", "--verify",
                           f"refs/heads/{source_branch}^{{commit}}"],
                ).strip()
                local_state = {"repo_path": str(repo), "baseline_commit": head}
                state["local"] = local_state
                if source["kind"] == "new":
                    remote_binding = self.source.registry.bind_project_remote(
                        project_id, provider_id=repository.id, node_id=repository.node_id,
                        owner=repository.owner, name=repository.name,
                        html_url=repository.html_url, clone_url=repository.clone_url,
                        provision_marker=marker,
                    )
                else:
                    remote_binding = self.source.registry.bind_local_import_project_remote(
                        project_id, origin_reference=source["import_handle"],
                        provider_id=repository.id, owner_id=repository.owner_id,
                        node_id=repository.node_id, owner=repository.owner,
                        name=repository.name, html_url=repository.html_url,
                        clone_url=repository.clone_url, provision_marker=marker,
                        source_branch=source_branch, source_head=head,
                        private=repository.private,
                    )
                self.ledger.journal(
                    request_id, "remote-binding", "BOUND",
                    {"provider_id": remote_binding["provider_id"]},
                )
                self._configure_origin(request_id, repo, repository)
                local_state["origin_url"] = repository.clone_url
                stopped = cancelled()
                if stopped is not None:
                    return stopped

                effect = self.ledger.begin_effect(
                    request_id, "baseline-main", "git_ref",
                    {"repository_id": repository.id, "ref": "refs/heads/main",
                     "commit": head},
                )
                if source["kind"] == "new":
                    pushed = self.bootstrap(project_id, head, repository)
                else:
                    assert self.local_import_bootstrap is not None
                    pushed = self.local_import_bootstrap(
                        project_id, head, source_branch, repository,
                    )
                if pushed != head:
                    raise GitError("GIT_REMOTE_MISMATCH",
                                   "Remote baseline differs from local commit")
                self.ledger.confirm_effect(effect, head)
                baseline_state = {"ref": "refs/heads/main", "commit": head}
                if source["kind"] == "local":
                    baseline_state["source_ref"] = f"refs/heads/{source_branch}"
                state["baseline"] = baseline_state
            else:
                assert self.github_import is not None
                imported = self.github_import.import_source(request_id)
                project, repository, head = (
                    dict(imported.project), imported.repository, imported.default_head,
                )
                repo = Path(project["repo_path"])
                registered = self.source.registry.get_project(project_id)
                expected_repo = self.source.registry.managed_root / project_id / "repo.git"
                branch = repository.default_branch
                if (repo != expected_repo or registered is None
                        or registered["repo_path"] != str(expected_repo)
                        or registered["origin_kind"] != "github"
                        or registered["origin_reference"] != source["repository"]
                        or project.get("origin_kind") != "github"
                        or project.get("origin_reference") != source["repository"]
                        or repository.full_name != source["repository"]
                        or repository.private is not True or branch is None
                        or run_git(repo, ["symbolic-ref", "--quiet", "HEAD"]).strip()
                            != f"refs/heads/{branch}"
                        or run_git(repo, [
                            "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}",
                        ]).strip() != head):
                    raise SourceError("GITHUB_IMPORT_IDENTITY_CHANGED",
                                      "Imported project and default HEAD differ")
                state["github"] = {
                    "repository_id": repository.id,
                    "owner": repository.owner,
                    "name": repository.name,
                    "owner_id": repository.owner_id,
                    "node_id": repository.node_id,
                    "full_name": repository.full_name,
                    "html_url": repository.html_url,
                    "clone_url": repository.clone_url,
                    "private": True,
                    "disposition": "imported",
                    "default_branch": branch,
                }
                local_state = {"repo_path": str(repo), "baseline_commit": head}
                state["local"] = local_state
                stopped = cancelled()
                if stopped is not None:
                    return stopped
                remote_binding = self.source.registry.bind_imported_project_remote(
                    project_id, origin_reference=source["repository"],
                    provider_id=repository.id, owner_id=repository.owner_id,
                    node_id=repository.node_id, owner=repository.owner,
                    name=repository.name, html_url=repository.html_url,
                    clone_url=repository.clone_url, default_branch=branch,
                    default_head=head, private=repository.private,
                )
                self.ledger.journal(
                    request_id, "remote-binding", "BOUND",
                    {"provider_id": remote_binding["provider_id"]},
                )
                self._configure_origin(request_id, repo, repository)
                local_state["origin_url"] = repository.clone_url
                state["baseline"] = {"ref": f"refs/heads/{branch}", "commit": head}
            stopped = cancelled()
            if stopped is not None:
                return stopped

            workspace = self.source.create_initial_workspace_source(request_id)
            state["workspace"] = {
                "path": workspace["worktree_path"],
                "branch": workspace["branch"],
                "head": workspace["head"],
            }
            binding = self.source.registry.bind_workspace_profile(
                project_id, "primary", template_id=template_id, profile_id=profile_id,
            )
            self.ledger.journal(request_id, "profile-binding", "BOUND", binding)
            stopped = cancelled()
            if stopped is not None:
                return stopped

            docs = self.drive.provision(request_id, project_id, "primary",
                                        command["params"]["name"])
            if (docs.installation_id != self.installation_id
                    or docs.project_id != project_id or docs.workspace_id != "primary"):
                raise ValueError("Drive registration identity changed")
            state["drive"] = {
                "project_folder_id": docs.project_folder_id,
                "workspace_folder_id": docs.workspace_folder_id,
                "command_doc_id": docs.command_doc_id,
                "changeset_doc_id": docs.changeset_doc_id,
                "receipt_doc_id": docs.receipt_doc_id,
                "latest_result_doc_id": docs.latest_result_doc_id,
                "project_state_doc_id": docs.project_state_doc_id,
                "context_doc_id": docs.context_doc_id,
                "project_command_doc_id": docs.project_command_doc_id,
                "project_receipt_doc_id": docs.project_receipt_doc_id,
            }
            if (docs.project_state_doc_id is None or docs.context_doc_id is None
                    or docs.project_command_doc_id is None
                    or docs.project_receipt_doc_id is None):
                raise ValueError("Provisioned project control documents are missing")
            stopped = cancelled()
            if stopped is not None:
                return stopped
            content = dict(self.populate(request_id, docs, self.source.registry, repository))
            if (set(content) != {"project_state_sha256", "context_sha256"}
                    or any(not isinstance(value, str)
                           or re.fullmatch(r"[0-9a-f]{64}", value) is None
                           for value in content.values())):
                raise ValueError("Provisioned project content proof is invalid")
            state["drive_content"] = content
            stopped = cancelled()
            if stopped is not None:
                return stopped
            self.source.registry.mark_project_ready(project_id, "primary")
            self.ledger.journal(request_id, "project-ready", "CONFIRMED",
                                {"project_id": project_id, "workspace_id": "primary"})
            state["project_state"] = "READY"
            if drive_origin:
                assert self.root_receipt is not None
                state["root_receipt"] = "UNCONFIRMED"
                try:
                    root_receipt_id = self.root_receipt.publish(
                        request_id, resources=state,
                    )
                except RootReceiptError as error:
                    return OperationResult("BLOCKED", error.code, state)
                except (sqlite3.Error, OSError):
                    raise
                except Exception:  # noqa: BLE001 -- no provider payload in partial result
                    return OperationResult("FAULT", "ROOT_RECEIPT_FAILED", state)
                if root_receipt_id != self.root_receipt.receipt_id:
                    return OperationResult("BLOCKED", "ROOT_RECEIPT_NOT_CONFIRMED", state)
                state["root_receipt"] = "CONFIRMED"
                state["root_receipt_doc_id"] = root_receipt_id
            return OperationResult("COMPLETED", result=state)
        except (sqlite3.Error, OSError):
            raise
        except (GitHubError, GitError, PublicationError, SourceError, ValueError) as error:
            return OperationResult("BLOCKED", getattr(error, "code", "PROVISION_INVALID"),
                                   state)
        except Exception:  # noqa: BLE001 -- never expose provider or token exception text
            return OperationResult("FAULT", "PROVISION_HANDLER_FAILED", state)
