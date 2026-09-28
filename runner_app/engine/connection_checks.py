"""Explicit, separately timed provider reads for the local UI.

These checks never infer connectivity from process health or startup configuration.
Their process-local observations reset to UNKNOWN after a restart. A Git remote
head read is deliberately distinct from fetching remote-tracking refs.
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from runner_app.adapters.drive_docs import FILE_FIELDS, DriveError
from runner_app.adapters.drive_index import recheck_attached_index
from runner_app.adapters.github import GitHubError
from runner_app.git.argv import GitError
from runner_app.git.github_transport import GitHubGitTransport
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.git.worktrees import inspect_workspace

if TYPE_CHECKING:
    from runner_app.engine.integrations import RuntimeIntegrations

_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
_MAX_REMOTE_CHECKS = 128


class CheckBusy(RuntimeError):
    """An identical class of explicit check is already running; no queue exists."""


def _unknown() -> dict[str, Any]:
    return {"status": "UNKNOWN", "last_checked_at": None,
            "last_success_at": None, "code": None, "observation": None}


def unknown_global_checks() -> dict[str, Any]:
    return {"drive": _unknown(), "github_api": _unknown(),
            "git_remote_scope": "PER_WORKSPACE"}


def _error_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and _CODE.fullmatch(code) else "CONNECTION_CHECK_FAILED"


class ConnectionChecks:
    """Keep sanitized observations, with nonblocking admission for each check type."""

    def __init__(self, *, drive: Callable[[], Mapping[str, Any]],
                 github_api: Callable[[], Mapping[str, Any]],
                 git_remote: Callable[[str, str], Mapping[str, Any]]) -> None:
        self._drive = drive
        self._github_api = github_api
        self._git_remote = git_remote
        self._guard = threading.Lock()
        self._locks = {"drive": threading.Lock(), "github_api": threading.Lock(),
                       "git_remote": threading.Lock()}
        self._global = {"drive": _unknown(), "github_api": _unknown()}
        self._remotes: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()

    def global_snapshot(self) -> dict[str, Any]:
        with self._guard:
            return {"drive": dict(self._global["drive"]),
                    "github_api": dict(self._global["github_api"]),
                    "git_remote_scope": "PER_WORKSPACE"}

    def remote_snapshot(self, project_id: str, workspace_id: str) -> dict[str, Any]:
        with self._guard:
            return dict(self._remotes.get((project_id, workspace_id), _unknown()))

    def _run(self, kind: str, probe: Callable[[], Mapping[str, Any]], *,
             scope: tuple[str, str] | None = None) -> dict[str, Any]:
        lock = self._locks[kind]
        if not lock.acquire(blocking=False):
            raise CheckBusy("A connection check is already in progress")
        try:
            try:
                observation = dict(probe())
            except Exception as error:  # noqa: BLE001 -- provider errors can contain credentials
                checked_at = datetime.now(timezone.utc).isoformat()
                with self._guard:
                    previous = (self._global[kind] if scope is None
                                else self._remotes.get(scope, _unknown()))
                    result = {"status": "ERROR", "last_checked_at": checked_at,
                              "last_success_at": previous["last_success_at"],
                              "code": _error_code(error),
                              "observation": previous["observation"]}
                    self._store(kind, result, scope)
                return dict(result)
            checked_at = datetime.now(timezone.utc).isoformat()
            result = {"status": "OK", "last_checked_at": checked_at,
                      "last_success_at": checked_at, "code": None,
                      "observation": observation}
            with self._guard:
                self._store(kind, result, scope)
            return dict(result)
        finally:
            lock.release()

    def _store(self, kind: str, result: dict[str, Any],
               scope: tuple[str, str] | None) -> None:
        if scope is None:
            self._global[kind] = result
            return
        self._remotes[scope] = result
        self._remotes.move_to_end(scope)
        if len(self._remotes) > _MAX_REMOTE_CHECKS:
            self._remotes.popitem(last=False)

    def check_drive(self) -> dict[str, Any]:
        return self._run("drive", self._drive)

    def check_github_api(self) -> dict[str, Any]:
        return self._run("github_api", self._github_api)

    def check_git_remote(self, project_id: str, workspace_id: str) -> dict[str, Any]:
        return self._run("git_remote", lambda: self._git_remote(project_id, workspace_id),
                         scope=(project_id, workspace_id))


def check_existing_drive(integrations: RuntimeIntegrations) -> dict[str, Any]:
    """Prove the exact installed root and unchanged Installation Index by provider reads."""
    attached = integrations.attached
    root = integrations.provider.get_file(attached.config.root_id, fields=FILE_FIELDS)
    if (root.get("id") != attached.config.root_id
            or root.get("name") != attached.config.root_name
            or root.get("mimeType") != "application/vnd.google-apps.folder"
            or root.get("trashed") is not False):
        raise DriveError("ROOT_METADATA_MISMATCH", "Existing Drive root changed")
    recheck_attached_index(integrations.provider, attached)
    return {"kind": "EXACT_INSTALLATION_READ", "root_id": attached.config.root_id,
            "index_id": attached.config.index_id,
            "index_revision": attached.index_revision}


def check_github_identity(integrations: RuntimeIntegrations) -> dict[str, Any]:
    """Prove owner API identity; no claim of repository creation or PR rights."""
    identity = integrations.github.authenticated_identity()
    if (identity.login.casefold() != integrations.config.github_login.casefold()
            or identity.id != integrations.config.github_user_id):
        raise GitHubError("GITHUB_IDENTITY_MISMATCH", "Owner GitHub identity changed")
    return {"kind": "API_IDENTITY_READ", "login": identity.login,
            "user_id": identity.id, "draft_pr_capability": "NOT_PROBED"}


def check_registered_git_remote(integrations: RuntimeIntegrations,
                                registry: SourceRegistry,
                                project_id: str, workspace_id: str) -> dict[str, Any]:
    """Read one selected branch through credential-safe ls-remote, without fetch."""
    workspace_path = registry.checked_workspace_path(project_id, workspace_id)
    workspace = registry.get_workspace(project_id, workspace_id)
    project = registry.get_project(project_id)
    remote = registry.get_project_remote(project_id)
    if workspace is None or project is None:
        raise SourceError("WORKSPACE_NOT_REGISTERED", "Managed source is missing")
    if remote is None:
        raise SourceError("PROJECT_REMOTE_UNBOUND", "Project has no approved remote")
    expected_repo = registry.managed_root / project_id / "repo.git"
    if project["repo_path"] != str(expected_repo):
        raise SourceError("OUTSIDE_MANAGED_ROOT", "Repository path differs from managed slot")
    repo_path = registry._managed_path(expected_repo, directory=True, expected=expected_repo)
    observed = inspect_workspace(repo_path, workspace_path)
    if (observed["head"] != workspace["head"]
            or observed["branch"] != workspace["branch"]):
        raise GitError("WORKSPACE_CHANGED", "Selected workspace changed")
    repository = integrations.github.get_repository(
        remote["owner"], remote["name"], expected_repo_id=remote["provider_id"],
    )
    if (not repository.private or repository.clone_url != remote["clone_url"]
            or repository.id != remote["provider_id"]):
        raise GitHubError("GITHUB_IDENTITY_MISMATCH", "Approved private remote changed")
    remote_head = GitHubGitTransport(repository, integrations.token_provider).read_head(
        Path(repo_path), workspace["branch"],
    )
    return {"kind": "REMOTE_HEAD_CHECK", "project_id": project_id,
            "workspace_id": workspace_id, "branch": workspace["branch"],
            "local_head": workspace["head"], "source_generation": workspace["generation"],
            "remote_head": remote_head, "last_fetch_at": None,
            "repository_id": repository.id}
