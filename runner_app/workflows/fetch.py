"""One-shot, project-scoped GitHub fetch into managed bare tracking refs.

The exact registered private repository is rechecked through GitHub REST before
any network Git mutation. A durable effect intent precedes the fixed fetch;
uncertainty leaves the repository lock retained for read-only reconciliation.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from runner_app.adapters.github import GitHubError, GitHubREST
from runner_app.domain.commands import FrozenCommand, validate_shape
from runner_app.engine.service import Context, OperationResult
from runner_app.git.argv import GitError
from runner_app.git.github_transport import (
    FETCH_REFSPEC,
    GitHubGitTransport,
    read_tracking_refs,
    tracking_refs_digest,
)
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.storage.ledger import Ledger


class FetchWorkflow:
    def __init__(self, ledger: Ledger, registry: SourceRegistry,
                 github: GitHubREST, token_provider: Callable[[], str]) -> None:
        if ledger.database.path != registry.database.path:
            raise ValueError("FETCH and source registry must share one ledger")
        self.ledger = ledger
        self.registry = registry
        self.github = github
        self.token_provider = token_provider

    def __call__(self, frozen: FrozenCommand, context: Context) -> OperationResult:
        command = frozen.command
        validate_shape(command)
        if command["action"] != "FETCH" or command["workspace_id"] is not None:
            return OperationResult("BLOCKED", "WRONG_WORKFLOW_ACTION")
        if context.ledger.database.path != self.ledger.database.path:
            return OperationResult("FAULT", "WORKFLOW_LEDGER_MISMATCH")
        request_id = command["request_id"]
        project_id = command["project_id"]
        operation = self.ledger.get(request_id)
        if (operation is None or operation["status"] != "RUNNING"
                or operation["command"] != command or operation["digest"] != frozen.digest
                or not self.ledger.holds_lock(request_id, f"repo:{project_id}")):
            return OperationResult("BLOCKED", "FETCH_ADMISSION_MISMATCH")
        if context.cancel.is_set():
            return OperationResult("CANCELLED", "CANCEL_REQUESTED")
        try:
            repo_path = self.registry.checked_project_path(project_id)
            remote = self.registry.get_project_remote(project_id)
            if remote is None:
                raise SourceError("PROJECT_REMOTE_UNBOUND", "Project has no approved remote")
            repository = self.github.get_repository(
                remote["owner"], remote["name"], expected_repo_id=remote["provider_id"],
            )
            if (repository.private is not True
                    or repository.id != remote["provider_id"]
                    or repository.node_id != remote["node_id"]
                    or repository.owner != remote["owner"]
                    or repository.name != remote["name"]
                    or repository.html_url != remote["html_url"]
                    or repository.clone_url != remote["clone_url"]
                    or ("owner_id" in remote and repository.owner_id != remote["owner_id"])
                    or ("provision_marker" in remote
                        and repository.description != remote["provision_marker"])):
                raise GitHubError("GITHUB_IDENTITY_MISMATCH", "Approved private remote changed")
            transport = GitHubGitTransport(repository, self.token_provider)
            before = read_tracking_refs(repo_path)
            advertised = transport.list_heads(repo_path)
        except (SourceError, GitHubError, GitError) as error:
            return OperationResult("BLOCKED", error.code)
        if context.cancel.is_set():
            return OperationResult("CANCELLED", "CANCEL_REQUESTED")
        intent = {
            "project_id": project_id,
            "repository_id": repository.id,
            "clone_url": repository.clone_url,
            "refspec": FETCH_REFSPEC,
            "advertised_heads": advertised,
            "before_refs_sha256": tracking_refs_digest(before),
        }
        effect_id = self.ledger.begin_effect(request_id, "git-fetch", "local_workspace", intent)
        try:
            tracking = transport.fetch_heads(repo_path)
        except GitError as error:
            # The subprocess may have updated refs before an error or timeout.
            # Ledger.finish changes the unconfirmed effect to UNKNOWN and retains
            # the repo lock; a new FETCH must never auto-replay this request.
            return OperationResult("BLOCKED", error.code)
        digest = tracking_refs_digest(tracking)
        self.ledger.confirm_effect(effect_id, digest)
        fetched_at = datetime.now(timezone.utc).isoformat()
        return OperationResult("COMPLETED", result={
            "fetch": {
                "kind": "TRACKING_REFS_FETCH",
                "project_id": project_id,
                "repository_id": repository.id,
                "clone_url": repository.clone_url,
                "refspec": FETCH_REFSPEC,
                "fetched_at": fetched_at,
                "advertised_ref_count": len(advertised),
                "remote_tracking_ref_count": len(tracking),
                "remote_tracking_refs_sha256": digest,
                "advertised_heads_present": all(
                    tracking.get(branch) == oid for branch, oid in advertised.items()
                ),
            },
        })
