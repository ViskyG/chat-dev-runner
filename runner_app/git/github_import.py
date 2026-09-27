"""Approved, read-only GitHub source import into a managed local bare repository.

An import copies committed branch refs without changing the GitHub repository or
an owner's existing checkout. The caller must admit CREATE_PROJECT and hold its
durable locks. The exact remote identity is owner-approved locally; REST preflight
and postflight reads pin that identity and its default-branch commit. A clone intent
is journaled before any local source path is created. Uncertain stages are never
silently adopted or replayed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from runner_app.adapters.github import GitHubBranch, GitHubRepository
from runner_app.domain.commands import validate_shape
from runner_app.git.argv import run_git, validate_branch
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.git.worktrees import (
    _check_tree_features,
    _id,
    _inspect_config,
    _reject_object_alternates,
)
from runner_app.storage.ledger import Ledger
from runner_app.storage.paths import ensure_private_directory

_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z", re.ASCII)
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z", re.ASCII)
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z", re.ASCII)
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.ASCII)


@dataclass(frozen=True)
class ApprovedGitHubImport:
    """Owner-local approval bound to a known private repository's stable IDs."""

    repository: str
    github_identity: str
    authenticated_login: str
    repository_id: int
    owner_id: int
    node_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.repository, str) or self.repository.count("/") != 1:
            raise ValueError("Approved GitHub repository must be owner/name")
        owner, name = self.repository.split("/")
        if (not _LOGIN.fullmatch(owner) or not _NAME.fullmatch(name)
                or not isinstance(self.github_identity, str)
                or not _SLUG.fullmatch(self.github_identity)
                or not isinstance(self.authenticated_login, str)
                or not _LOGIN.fullmatch(self.authenticated_login)
                or type(self.repository_id) is not int or self.repository_id <= 0
                or type(self.owner_id) is not int or self.owner_id <= 0
                or not isinstance(self.node_id, str) or not 1 <= len(self.node_id) <= 256):
            raise ValueError("Approved GitHub import identity is incomplete")


class GitHubImportReader(Protocol):
    expected_login: str

    def get_repository(self, owner: str, name: str, *,
                       expected_repo_id: int | None = None) -> GitHubRepository: ...

    def branch_head(self, repository: GitHubRepository,
                    branch: str) -> GitHubBranch | None: ...


class GitHubBareCloner(Protocol):
    repository: GitHubRepository

    def clone_bare(self, stage_parent: Path, destination: Path) -> None: ...


@dataclass(frozen=True)
class GitHubImportResult:
    project: Mapping[str, Any]
    repository: GitHubRepository
    default_head: str


def _confirmed_repository(
    observed: GitHubRepository, approval: ApprovedGitHubImport,
) -> str:
    owner, name = approval.repository.split("/")
    expected_url = f"https://github.com/{owner}/{name}.git"
    if (observed.id != approval.repository_id
            or observed.owner_id != approval.owner_id
            or observed.node_id != approval.node_id
            or observed.private is not True
            or observed.owner != owner or observed.name != name
            or observed.full_name != approval.repository
            or observed.clone_url != expected_url
            or observed.html_url != f"https://github.com/{owner}/{name}"
            or observed.ssh_url != f"git@github.com:{owner}/{name}.git"):
        raise SourceError("GITHUB_IMPORT_IDENTITY_CHANGED",
                          "Approved private GitHub repository identity differs")
    if observed.default_branch is None:
        raise SourceError("GITHUB_IMPORT_EMPTY", "GitHub source has no default branch")
    return validate_branch(observed.default_branch)


class GitHubImportService:
    """One-shot import step; it does not create a GitHub repository or workspace."""

    def __init__(
        self,
        ledger: Ledger,
        registry: SourceRegistry,
        rest: GitHubImportReader,
        cloner_factory: Callable[[GitHubRepository], GitHubBareCloner],
        approvals: Mapping[str, ApprovedGitHubImport],
    ) -> None:
        if ledger.database.path != registry.database.path:
            raise ValueError("GitHub import and admission ledger must share one database")
        if not callable(cloner_factory):
            raise TypeError("A fixed-URL authenticated GitHub cloner factory is required")
        if any(key != approval.repository for key, approval in approvals.items()):
            raise ValueError("GitHub approval keys must match exact repository identities")
        if len({key.casefold() for key in approvals}) != len(approvals):
            raise ValueError("Ambiguous case variants are not valid GitHub approvals")
        self.ledger = ledger
        self.registry = registry
        self.rest = rest
        self.cloner_factory = cloner_factory
        self.approvals = dict(approvals)

    def _command(self, request_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        operation = self.ledger.get(request_id)
        if operation is None or operation["status"] != "RUNNING":
            raise SourceError("OPERATION_NOT_RUNNING", "GitHub import requires an admitted operation")
        command: dict[str, Any] = operation["command"]
        validate_shape(command)
        if command["action"] != "CREATE_PROJECT":
            raise SourceError("WRONG_SOURCE_ACTION", "GitHub import needs CREATE_PROJECT")
        if command["params"]["source"]["kind"] != "github":
            raise SourceError("WRONG_SOURCE_KIND", "Project request is not a GitHub source")
        project_id = _id(command["project_id"], "Project ID")
        required = {
            f"repo:{project_id}",
            f"global:{command['params']['github_identity']}",
            "global:drive-installation",
        }
        with closing(self.registry.database.connect()) as connection:
            held = {
                str(row[0]) for row in connection.execute(
                    "SELECT scope FROM locks WHERE request_id=?", (request_id,),
                )
            }
        if not required <= held:
            raise SourceError("SOURCE_LOCK_MISSING", "Required GitHub import locks are not held")
        return command, operation

    def import_source(self, request_id: str) -> GitHubImportResult:
        command, operation = self._command(request_id)
        params = command["params"]
        full_name = params["source"]["repository"]
        approval = self.approvals.get(full_name)
        if (approval is None or approval.github_identity != params["github_identity"]
                or self.rest.expected_login != approval.authenticated_login):
            raise SourceError("GITHUB_IMPORT_NOT_APPROVED",
                              "Exact GitHub source and authenticated identity are not approved")
        if any(item["step_id"] == "local-project" for item in operation["effects"]):
            raise SourceError("GITHUB_IMPORT_NEEDS_RECONCILIATION",
                              "Prior GitHub import effect must be reconciled without replay")

        project_id = _id(command["project_id"], "Project ID")
        project_dir = self.registry.managed_root / project_id
        stage = project_dir / f".github-import-{request_id}"
        bare = stage / "repo.git"
        final = project_dir / "repo.git"
        if (self.registry.get_project(project_id) is not None
                or os.path.lexists(project_dir) or os.path.lexists(stage)
                or os.path.lexists(final)):
            raise SourceError("GITHUB_IMPORT_PATH_EXISTS",
                              "Managed project path already exists; import cannot adopt it")

        owner, name = approval.repository.split("/")
        observed = self.rest.get_repository(
            owner, name, expected_repo_id=approval.repository_id,
        )
        branch = _confirmed_repository(observed, approval)
        preflight = self.rest.branch_head(observed, branch)
        if preflight is None:
            raise SourceError("GITHUB_IMPORT_EMPTY", "GitHub default branch has no commit")
        head = preflight.sha
        if preflight.name != branch or not _OID.fullmatch(head):
            raise SourceError("GITHUB_IMPORT_SOURCE_CHANGED",
                              "GitHub default branch response is invalid")
        cloner = self.cloner_factory(observed)
        if cloner.repository != observed:
            raise SourceError("GITHUB_IMPORT_IDENTITY_CHANGED",
                              "GitHub cloner is bound to another repository")

        intent = {
            "project_id": project_id,
            "kind": "github",
            "repository": approval.repository,
            "repository_id": approval.repository_id,
            "owner_id": approval.owner_id,
            "node_id": approval.node_id,
            "clone_url": observed.clone_url,
            "default_branch": branch,
            "remote_head": head,
            "stage_path": str(stage),
            "repo_path": str(final),
        }
        effect = self.ledger.begin_effect(
            request_id, "local-project", "local_workspace", intent,
        )
        # Exclusive directory creation prevents a concurrent or leftover local path
        # from being silently adopted after the preflight checks.
        try:
            os.mkdir(project_dir, mode=0o700)
            ensure_private_directory(project_dir)
            os.mkdir(stage, mode=0o700)
            ensure_private_directory(stage)
        except FileExistsError:
            raise SourceError("GITHUB_IMPORT_PATH_EXISTS",
                              "Managed import path appeared after intent") from None
        cloner.clone_bare(stage, bare)

        if bare.is_symlink() or not bare.is_dir():
            raise SourceError("GITHUB_IMPORT_CLONE_INVALID", "GitHub bare clone is unavailable")
        ensure_private_directory(bare)
        if run_git(bare, ["rev-parse", "--is-bare-repository"]).strip() != "true":
            raise SourceError("GITHUB_IMPORT_CLONE_INVALID", "GitHub import is not a bare repository")
        _inspect_config(bare)
        _reject_object_alternates(bare)
        if run_git(bare, ["symbolic-ref", "--quiet", "HEAD"]).strip() != f"refs/heads/{branch}":
            raise SourceError("GITHUB_IMPORT_SOURCE_CHANGED",
                              "Cloned default branch differs from REST preflight")
        local_head = run_git(
            bare, ["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"],
        ).strip()
        if local_head != head:
            raise SourceError("GITHUB_IMPORT_SOURCE_CHANGED",
                              "Cloned default branch moved during import")
        _check_tree_features(bare, head)

        remotes = run_git(bare, ["remote"]).splitlines()
        if remotes == ["origin"]:
            origin = run_git(bare, ["config", "--local", "--get", "remote.origin.url"]).strip()
            if origin != observed.clone_url:
                raise SourceError("GITHUB_IMPORT_CLONE_INVALID",
                                  "Cloned remote URL differs from approved repository")
            run_git(bare, ["remote", "remove", "origin"])
        elif remotes:
            raise SourceError("GITHUB_IMPORT_CLONE_INVALID",
                              "Cloned repository has unexpected remotes")
        _inspect_config(bare)
        if run_git(bare, ["remote"]).strip():
            raise SourceError("GITHUB_IMPORT_CLONE_INVALID",
                              "Cloned repository retained a remote")

        postflight_repo = self.rest.get_repository(
            owner, name, expected_repo_id=approval.repository_id,
        )
        if _confirmed_repository(postflight_repo, approval) != branch:
            raise SourceError("GITHUB_IMPORT_SOURCE_CHANGED",
                              "GitHub default branch changed during import")
        postflight = self.rest.branch_head(postflight_repo, branch)
        if postflight is None or postflight.name != branch or postflight.sha != head:
            raise SourceError("GITHUB_IMPORT_SOURCE_CHANGED",
                              "GitHub default branch moved during import")
        if os.path.lexists(final):
            raise SourceError("GITHUB_IMPORT_PATH_EXISTS",
                              "Managed repository appeared during import")
        os.rename(bare, final)
        record = self.registry.register_project(
            project_id, params["name"], final, origin_kind="github",
            origin_reference=approval.repository,
        )
        self.ledger.confirm_effect(effect, str(final))
        try:
            stage.rmdir()
        except OSError:
            pass  # Empty-stage cleanup is optional after durable confirmation.
        return GitHubImportResult(record, postflight_repo, head)
