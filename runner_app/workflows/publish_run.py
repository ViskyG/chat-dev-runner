"""Finite source-run PUBLISH with independent Git and draft-PR effect receipts.

The handler is inert until registered by an owner-configured Engine. It never
starts an executor or stages files: all authority comes from an attested,
immutable run binding and one already admitted repository-scoped operation.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, cast

from runner_app.adapters.github import GitHubError, GitHubRepository, GitHubREST, PullRequestResult
from runner_app.domain.commands import FrozenCommand, validate_shape
from runner_app.engine.service import Context, OperationResult
from runner_app.git.argv import GitError, run_git
from runner_app.git.publish import (
    GitHubPushPort,
    PublicationError,
    check_tested_snapshot_editor,
    publish_tested_snapshot,
    publish_tested_snapshot_github,
)
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.storage.ledger import TERMINAL, Ledger

_PROFILE_ID = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")
_REVISION = re.compile(r"[0-9a-f]{64}\Z")
_PROVISION_MARKER = re.compile(r"cdr:v1:[0-9a-f]{64}\Z")
_DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{8,256}\Z")


def _matches_project_remote(project: dict[str, Any], bound: dict[str, Any],
                            repository: GitHubRepository) -> bool:
    """Require the immutable source binding to name this exact approved repo."""
    common = {
        "provider": "github", "provider_id": repository.id,
        "node_id": repository.node_id, "owner": repository.owner,
        "name": repository.name, "html_url": repository.html_url,
        "clone_url": repository.clone_url, "private": True,
    }
    if (repository.private is not True or bound.get("private") is not True
            or type(bound.get("provider_id")) is not int
            or any(bound.get(key) != value for key, value in common.items())
            or ("owner_id" in bound and
                (type(bound["owner_id"]) is not int
                 or bound["owner_id"] != repository.owner_id))):
        return False
    origin_kind = project["origin_kind"]
    origin_reference = project["origin_reference"]
    if origin_kind == "new":
        return (origin_reference is None
                and isinstance(repository.description, str)
                and _PROVISION_MARKER.fullmatch(repository.description) is not None
                and bound.get("origin_kind") in {None, "new"}
                and bound.get("origin_reference") is None
                and bound.get("provision_marker") == repository.description)
    if origin_kind == "github":
        return (bound.get("origin_kind") == "github"
                and bound.get("origin_reference") == origin_reference
                and origin_reference == repository.full_name
                and bound.get("owner_id") == repository.owner_id
                and bound.get("default_branch") == repository.default_branch)
    if origin_kind == "local":
        return (isinstance(repository.description, str)
                and _PROVISION_MARKER.fullmatch(repository.description) is not None
                and bound.get("origin_kind") == "local"
                and bound.get("origin_reference") == origin_reference
                and bound.get("owner_id") == repository.owner_id
                and bound.get("provision_marker") == repository.description)
    return False


class PublishRunWorkflow:
    """Publish exactly one previously executed source run under a project lock.

    Local bare Git is an offline acceptance transport. An owner-configured
    GitHubGitTransport with OwnerTokenFile uses the same attested commit and
    journaled effect contract for live HTTPS publication.
    """

    def __init__(self, ledger: Ledger, registry: SourceRegistry, github: GitHubREST,
                 repository: GitHubRepository, *, remote_name: str | None = None,
                 remote_url: Path | None = None,
                 git_transport: GitHubPushPort | None = None) -> None:
        if ledger.database.path != registry.database.path:
            raise ValueError("PUBLISH and source records must use one admission database")
        if not repository.private or repository.default_branch is None:
            raise ValueError("An approved private repository with a default branch is required")
        if git_transport is None:
            if remote_name is None or remote_url is None:
                raise ValueError("One local bare receiver or GitHub Git transport is required")
        elif (remote_name is not None or remote_url is not None
              or git_transport.repository != repository
              or git_transport.url != repository.clone_url):
            raise ValueError("GitHub Git transport differs from approved repository")
        self.ledger = ledger
        self.registry = registry
        self.github = github
        self.repository = repository
        self.remote_name = remote_name
        self.remote_url = Path(remote_url) if remote_url is not None else None
        self.git_transport = git_transport

    def __call__(self, frozen: FrozenCommand, context: Context) -> OperationResult:
        command = frozen.command
        validate_shape(command)
        if command["action"] != "PUBLISH":
            return OperationResult("BLOCKED", "WRONG_WORKFLOW_ACTION")
        return self.publish_run(
            command["request_id"], command["params"]["source_run_id"],
            command["params"]["publication"], context,
        )

    def publish_run(self, request_id: str, run_id: str, policy: str,
                    context: Context) -> OperationResult:
        """Also serves the full route's `never` policy after it has recorded a run."""
        if policy not in {"draft_always", "on_pass", "never"}:
            return OperationResult("BLOCKED", "INVALID_PUBLICATION_POLICY")
        operation = self.ledger.get(request_id)
        if operation is None or operation["status"] != "RUNNING":
            return OperationResult("BLOCKED", "OPERATION_NOT_RUNNING")
        command = operation["command"]
        action = command.get("action")
        if action == "PUBLISH":
            if (command.get("params", {}).get("source_run_id") != run_id
                    or command["params"].get("publication") != policy):
                return OperationResult("BLOCKED", "SOURCE_RUN_MISMATCH")
        elif action == "DELIVER_VERIFY_PUBLISH":
            if command.get("params", {}).get("publication") != policy:
                return OperationResult("BLOCKED", "PUBLICATION_POLICY_MISMATCH")
        else:
            return OperationResult("BLOCKED", "WRONG_WORKFLOW_ACTION")
        project_id, workspace_id = command["project_id"], command["workspace_id"]
        with self.ledger._connection() as connection:
            held = connection.execute(
                "SELECT 1 FROM locks WHERE request_id=? AND scope=?",
                (request_id, f"repo:{project_id}"),
            ).fetchone()
        if held is None:
            return OperationResult("BLOCKED", "PUBLICATION_LOCK_MISSING")
        state: dict[str, Any] = {"source_run_id": run_id, "policy": policy,
                                 "github_repository_id": self.repository.id}
        try:
            target = self.registry.publication_target(
                run_id, project_id=project_id, workspace_id=workspace_id,
            )
            snapshot = self.registry.get_snapshot(target["snapshot_id"])
            run = self.ledger.get_run(run_id)
            if (snapshot is None or run is None
                    or run.get("run_id") != run_id
                    or run.get("project_id") != project_id
                    or run.get("workspace_id") != workspace_id
                    or run.get("snapshot", {}).get("commit") != target["commit"]
                    or run["snapshot"].get("tree") != target["tree"]
                    or snapshot["base_head"] != command["expected_head"]):
                return OperationResult("BLOCKED", "SOURCE_RUN_MISMATCH", state)
            source = self.ledger.get(run["request_id"])
            if (source is None or source["action"] not in
                    {"VERIFY", "DELIVER_VERIFY_PUBLISH"}
                    or action == "PUBLISH" and source["status"] not in TERMINAL
                    or action == "DELIVER_VERIFY_PUBLISH"
                    and source["request_id"] != request_id):
                return OperationResult("BLOCKED", "SOURCE_RUN_MISMATCH", state)
            with self.ledger._connection() as connection:
                profile_row = connection.execute(
                    "SELECT profile_id,profile_revision FROM run_executions WHERE run_id=?",
                    (run_id,),
                ).fetchone()
            if (profile_row is None
                    or not isinstance(profile_row["profile_id"], str)
                    or not isinstance(profile_row["profile_revision"], str)
                    or _PROFILE_ID.fullmatch(profile_row["profile_id"]) is None
                    or _REVISION.fullmatch(profile_row["profile_revision"]) is None):
                return OperationResult("BLOCKED", "SOURCE_RUN_MISMATCH", state)
            state["profile_id"] = profile_row["profile_id"]
            state["profile_revision"] = profile_row["profile_revision"]
            outcome = run["execution"]["outcome"]
            if outcome not in {"PASSED", "FAILED", "TIMED_OUT"}:
                return OperationResult("BLOCKED", "SOURCE_RUN_NOT_COMPLETE", state)
            workspace = self.registry.get_workspace(project_id, workspace_id)
            if workspace is None or workspace["archived"]:
                return OperationResult("BLOCKED", "WORKSPACE_CHANGED", state)
            branch = workspace["branch"]
            default_branch = self.repository.default_branch
            assert default_branch is not None
            if branch == default_branch:
                return OperationResult("BLOCKED", "PROTECTED_BRANCH", state)
            state.update({
                "snapshot_id": target["snapshot_id"], "tested_commit": target["commit"],
                "tested_tree": target["tree"], "execution_outcome": outcome,
                "editor_head": workspace["head"], "remote_ref": f"refs/heads/{branch}",
            })
            if policy == "never":
                state["publication"] = {"status": "SKIPPED_POLICY"}
                return OperationResult("COMPLETED", result=state)
            if policy == "on_pass" and outcome != "PASSED":
                state["publication"] = {"status": "SKIPPED_FAILED_TESTS"}
                return OperationResult("COMPLETED", result=state)
            if target["commit"] == snapshot["base_head"]:
                state["publication"] = {"status": "NO_DIFF", "remote_head": None}
                return OperationResult("COMPLETED", result=state)
            if context.cancel.is_set():
                state["publication"] = {"status": "NOT_ATTEMPTED"}
                return OperationResult("CANCELLED", "CANCEL_REQUESTED", state)

            project = self.registry.get_project(project_id)
            if project is None:
                return OperationResult("BLOCKED", "PROJECT_NOT_REGISTERED", state)
            bound_remote = self.registry.get_project_remote(project_id)
            if bound_remote is None:
                return OperationResult("BLOCKED", "PROJECT_REMOTE_UNBOUND", state)
            if not _matches_project_remote(project, bound_remote, self.repository):
                return OperationResult("BLOCKED", "PROJECT_REMOTE_MISMATCH", state)

            # Re-read the approved numeric repository identity and default
            # branch before any Git or PR write.
            current_repo = self.github.get_repository(
                self.repository.owner, self.repository.name,
                expected_repo_id=self.repository.id,
            )
            if (current_repo.node_id != self.repository.node_id
                    or current_repo.owner_id != self.repository.owner_id
                    or current_repo.full_name != self.repository.full_name
                    or current_repo.owner != self.repository.owner
                    or current_repo.name != self.repository.name
                    or current_repo.html_url != self.repository.html_url
                    or current_repo.clone_url != self.repository.clone_url
                    or current_repo.private is not True
                    or (project["origin_kind"] in {"new", "local"}
                        and current_repo.description != bound_remote["provision_marker"])):
                return OperationResult("BLOCKED", "GITHUB_IDENTITY_MISMATCH", state)
            if current_repo.default_branch != default_branch:
                return OperationResult("BLOCKED", "DEFAULT_BRANCH_CHANGED", state)
            provider_head = self.github.branch_head(self.repository, branch)
            observed = provider_head.sha if provider_head is not None else None
            if provider_head is not None and provider_head.protected and observed != target["commit"]:
                state["publication"] = {"status": "BLOCKED", "remote_head": observed}
                return OperationResult("BLOCKED", "BRANCH_PROTECTED", state)
            if observed not in {None, snapshot["base_head"], target["commit"]}:
                state["publication"] = {"status": "BLOCKED", "remote_head": observed}
                return OperationResult("BLOCKED", "REMOTE_CHANGED", state)
            if self.git_transport is not None:
                transport_head = self.git_transport.read_head(
                    Path(project["repo_path"]), branch,
                )
                if transport_head != observed:
                    state["publication"] = {"status": "BLOCKED",
                                            "remote_head": transport_head}
                    return OperationResult("BLOCKED", "REMOTE_VIEW_MISMATCH", state)
            # A known editor mismatch is a definite no-write refusal and must
            # not create an uncertain Git effect. The publisher repeats this
            # check after journaling to cover edits during the tiny handoff.
            if observed != target["commit"]:
                check_tested_snapshot_editor(
                    self.registry, run_id=run_id, project_id=project_id,
                    workspace_id=workspace_id, branch=branch,
                    default_branch=default_branch,
                )
            previous_git = _effect(operation, "tested-git-push", "git_ref")
            expected_remote_head = (previous_git["intent"]["expected_remote_head"]
                                    if previous_git is not None else
                                    snapshot["base_head"] if observed is not None else None)
            git_intent = {
                "repository_id": self.repository.id, "run_id": run_id,
                "snapshot_id": target["snapshot_id"], "commit": target["commit"],
                "tree": target["tree"], "ref": f"refs/heads/{branch}",
                "expected_remote_head": expected_remote_head,
            }
            git_effect = self.ledger.begin_effect(
                request_id, "tested-git-push", "git_ref", git_intent,
            )
            if previous_git is not None:
                if (previous_git["state"] == "CONFIRMED"
                        and previous_git["provider_id"] != target["commit"]):
                    return OperationResult("BLOCKED", "EFFECT_ID_CHANGED", state)
                # An old write may still arrive. Never issue a second one.
                current = self.github.branch_head(self.repository, branch)
                if current is None or current.sha != target["commit"]:
                    state["publication"] = {"status": "UNKNOWN",
                                            "remote_head": current.sha if current else None}
                    return OperationResult("BLOCKED", "PUSH_OUTCOME_UNKNOWN", state)
                if self.git_transport is not None:
                    try:
                        git_head = self.git_transport.read_head(
                            Path(project["repo_path"]), branch,
                        )
                    except GitError:
                        git_head = None
                    if git_head != target["commit"]:
                        state["publication"] = {"status": "UNKNOWN",
                                                "remote_head": git_head}
                        return OperationResult("BLOCKED", "PUSH_OUTCOME_UNKNOWN", state)
                self.ledger.confirm_effect(git_effect, target["commit"])
                git_status = "PUBLISHED_RECONCILED"
            else:
                if self.git_transport is not None:
                    pushed = publish_tested_snapshot_github(
                        self.registry, self.git_transport, run_id=run_id,
                        project_id=project_id, workspace_id=workspace_id,
                        branch=branch, default_branch=default_branch,
                        expected_remote_head=expected_remote_head,
                    )
                else:
                    assert self.remote_name is not None and self.remote_url is not None
                    pushed = publish_tested_snapshot(
                        self.registry, run_id=run_id, project_id=project_id,
                        workspace_id=workspace_id, branch=branch,
                        remote_name=self.remote_name, remote_url=self.remote_url,
                        default_branch=default_branch,
                        expected_remote_head=expected_remote_head,
                    )
                self.ledger.confirm_effect(git_effect, pushed.commit)
                git_status = pushed.status
            try:
                state["editor_head"] = run_git(
                    Path(workspace["worktree_path"]), ["rev-parse", "HEAD"],
                ).strip()
            except GitError:
                state["editor_head"] = None
            state["editor_matches_tested"] = state["editor_head"] == target["commit"]
            state["publication"] = {
                "status": "NO_DIFF" if git_status == "NO_DIFF" else "PUSHED",
                "git_status": git_status, "remote_head": target["commit"],
                "remote_ref": f"refs/heads/{branch}",
            }
            if context.cancel.is_set():
                return OperationResult("CANCELLED", "CANCEL_REQUESTED", state)

            title = f"[{outcome}] {project_id}/{workspace_id} run {run_id[:8]}"
            body = (f"Source run: {run_id}\n"
                    f"Tested commit: {target['commit']}\n"
                    f"Tested tree: {target['tree']}\n"
                    f"Execution outcome: {outcome}\n"
                    f"Approved profile: {state['profile_id']}\n"
                    f"Profile revision: {state['profile_revision']}\n")
            if action == "PUBLISH" and source["delivery_status"] == "DELIVERED":
                folder_id = _confirmed_report_folder(source, run_id)
                if folder_id is not None:
                    body += (f"Report: https://drive.google.com/drive/folders/"
                             f"{folder_id}\n")
            pr_intent = {
                "repository_id": self.repository.id, "run_id": run_id,
                "head": branch, "base": default_branch,
                "commit": target["commit"], "title": title, "body": body,
            }
            latest = self.ledger.get(request_id)
            previous_pr = _effect(latest, "draft-pull-request", "pull_request")
            linked = _effect(latest, "attach-run-report-link", "pull_request")
            observed_body = body
            if (action == "DELIVER_VERIFY_PUBLISH"
                    and linked is not None and linked["state"] == "CONFIRMED"
                    and linked["intent"].get("run_id") == run_id
                    and linked["intent"].get("previous_body") == body
                    and isinstance(linked["intent"].get("body"), str)):
                observed_body = linked["intent"]["body"]
            pr_effect = self.ledger.begin_effect(
                request_id, "draft-pull-request", "pull_request", pr_intent,
            )
            if previous_pr is None:
                try:
                    pr = self.github.create_or_update_draft_pr(
                        self.repository, head_branch=branch,
                        base_branch=default_branch, expected_head_sha=target["commit"],
                        title=title, body=body,
                    )
                except GitHubError as error:
                    if error.code not in {"PULL_REQUEST_CREATE_UNKNOWN",
                                          "PULL_REQUEST_UPDATE_UNKNOWN"}:
                        raise
                    try:
                        pr = self._reconcile_pr(branch, default_branch, target["commit"],
                                                title, body, None)
                    except GitHubError as read_error:
                        if read_error.code in {"GITHUB_PROVIDER_UNAVAILABLE",
                                               "PULL_REQUEST_CREATE_UNKNOWN"}:
                            raise GitHubError(error.code, "Draft PR outcome remains unknown") from None
                        raise
            else:
                expected_pr_id = (int(previous_pr["provider_id"])
                                  if previous_pr["state"] == "CONFIRMED" else None)
                try:
                    pr = self._reconcile_pr(branch, default_branch, target["commit"],
                                            title, observed_body, expected_pr_id)
                except GitHubError as read_error:
                    if read_error.code == "GITHUB_PROVIDER_UNAVAILABLE":
                        raise GitHubError("PULL_REQUEST_OUTCOME_UNKNOWN",
                                          "Draft PR readback remains unavailable") from None
                    raise
            _check_pr(pr, title, observed_body, target["commit"])
            self.ledger.confirm_effect(pr_effect, str(pr.pull_request.id))
            state["publication"].update({
                "status": "DRAFT_PUBLISHED", "pull_request_id": pr.pull_request.id,
                "pull_request_number": pr.pull_request.number,
                "pull_request_url": pr.pull_request.html_url,
                "pull_request_disposition": pr.disposition,
            })
            if context.cancel.is_set():
                return OperationResult("CANCELLED", "CANCEL_REQUESTED", state)
            return OperationResult("COMPLETED", result=state)
        except (PublicationError, GitHubError, GitError, SourceError, ValueError) as error:
            code = getattr(error, "code", "PUBLICATION_INVALID")
            publication = state.setdefault("publication", {})
            publication["status"] = ("UNKNOWN" if code in {
                "PUSH_OUTCOME_UNKNOWN", "PULL_REQUEST_CREATE_UNKNOWN",
                "PULL_REQUEST_UPDATE_UNKNOWN", "PULL_REQUEST_OUTCOME_UNKNOWN",
            } else "BLOCKED")
            if isinstance(error, PublicationError):
                publication["remote_head"] = error.remote_head
            return OperationResult("BLOCKED", code, state)
        except (sqlite3.Error, OSError):
            raise

    def attach_report_link(self, request_id: str, run_id: str, run_folder_id: str,
                           context: Context) -> OperationResult:
        """Attach one verified Drive run folder to the already confirmed draft PR."""
        report: dict[str, Any] = {"status": "BLOCKED"}
        state: dict[str, Any] = {"report_link": report}
        operation = self.ledger.get(request_id)
        if (operation is None or operation["status"] != "RUNNING"
                or operation["action"] != "DELIVER_VERIFY_PUBLISH"
                or context.ledger.database.path != self.ledger.database.path):
            return OperationResult("BLOCKED", "REPORT_LINK_OPERATION_INVALID", state)
        command = operation["command"]
        project_id, workspace_id = command["project_id"], command["workspace_id"]
        if not self.ledger.holds_lock(request_id, f"repo:{project_id}"):
            return OperationResult("BLOCKED", "PUBLICATION_LOCK_MISSING", state)
        run = self.ledger.get_run(run_id)
        if (run is None or run["request_id"] != request_id
                or run["project_id"] != project_id
                or run["workspace_id"] != workspace_id):
            return OperationResult("BLOCKED", "SOURCE_RUN_MISMATCH", state)
        if (not isinstance(run_folder_id, str)
                or _DRIVE_ID.fullmatch(run_folder_id) is None
                or operation["delivery_status"] != "DELIVERED"
                or _confirmed_report_folder(operation, run_id) != run_folder_id):
            return OperationResult("BLOCKED", "REPORT_FOLDER_UNCONFIRMED", state)
        pr_effect = _effect(operation, "draft-pull-request", "pull_request")
        git_effect = _effect(operation, "tested-git-push", "git_ref")
        if (pr_effect is None or pr_effect["state"] != "CONFIRMED"
                or git_effect is None or git_effect["state"] != "CONFIRMED"):
            return OperationResult("BLOCKED", "DRAFT_PR_UNCONFIRMED", state)
        original = pr_effect["intent"]
        try:
            pr_id = int(pr_effect["provider_id"])
        except (TypeError, ValueError):
            return OperationResult("BLOCKED", "DRAFT_PR_UNCONFIRMED", state)
        if (pr_id <= 0 or original.get("repository_id") != self.repository.id
                or original.get("run_id") != run_id
                or original.get("commit") != git_effect["provider_id"]
                or original.get("commit") != run["snapshot"].get("commit")
                or git_effect["intent"].get("commit") != original.get("commit")
                or original.get("head") == original.get("base")
                or original.get("base") != self.repository.default_branch
                or not isinstance(original.get("title"), str)
                or not isinstance(original.get("body"), str)):
            return OperationResult("BLOCKED", "DRAFT_PR_UNCONFIRMED", state)
        folder_url = f"https://drive.google.com/drive/folders/{run_folder_id}"
        body = original["body"] + f"Report: {folder_url}\n"
        report.update({"url": folder_url, "pull_request_id": pr_id})
        if context.cancel.is_set():
            report["status"] = "NOT_ATTEMPTED"
            return OperationResult("CANCELLED", "CANCEL_REQUESTED", state)
        try:
            project = self.registry.get_project(project_id)
            bound_remote = self.registry.get_project_remote(project_id)
            if (project is None or bound_remote is None
                    or not _matches_project_remote(project, bound_remote, self.repository)):
                return OperationResult("BLOCKED", "PROJECT_REMOTE_MISMATCH", state)
            current_repo = self.github.get_repository(
                self.repository.owner, self.repository.name,
                expected_repo_id=self.repository.id,
            )
            if (current_repo != self.repository
                    or (project["origin_kind"] in {"new", "local"}
                        and current_repo.description != bound_remote["provision_marker"])):
                return OperationResult("BLOCKED", "GITHUB_IDENTITY_MISMATCH", state)
            observed = self.github.reconcile_draft_pr(
                self.repository, head_branch=original["head"],
                base_branch=original["base"],
                expected_head_sha=original["commit"], expected_pr_id=pr_id,
            ).pull_request
            report["pull_request_url"] = observed.html_url
            if observed.title != original["title"]:
                return OperationResult("BLOCKED", "PULL_REQUEST_CHANGED", state)
            previous_link = _effect(operation, "attach-run-report-link", "pull_request")
            intent = {
                "repository_id": self.repository.id, "run_id": run_id,
                "pull_request_id": pr_id, "commit": original["commit"],
                "run_folder_id": run_folder_id, "url": folder_url,
                "title": original["title"], "previous_body": original["body"],
                "body": body,
            }
            if previous_link is not None:
                if previous_link["intent"] != intent:
                    return OperationResult("BLOCKED", "REPORT_LINK_INTENT_CHANGED", state)
                if (previous_link["state"] == "CONFIRMED"
                        and previous_link["provider_id"] != str(pr_id)):
                    return OperationResult("BLOCKED", "EFFECT_ID_CHANGED", state)
                if observed.body != body:
                    code = ("REPORT_LINK_CHANGED" if previous_link["state"] == "CONFIRMED"
                            else "PULL_REQUEST_UPDATE_UNKNOWN")
                    if code == "PULL_REQUEST_UPDATE_UNKNOWN":
                        report["status"] = "UNKNOWN"
                    return OperationResult("BLOCKED", code, state)
                self.ledger.confirm_effect(previous_link["effect_id"], str(pr_id))
                disposition = "reconciled"
            else:
                if observed.body not in {original["body"], body}:
                    return OperationResult("BLOCKED", "PULL_REQUEST_CHANGED", state)
                effect_id = self.ledger.begin_effect(
                    request_id, "attach-run-report-link", "pull_request", intent,
                )
                if observed.body == body:
                    disposition = "attached"
                else:
                    updated = self.github.update_draft_pr_body(
                        self.repository, head_branch=original["head"],
                        base_branch=original["base"],
                        expected_head_sha=original["commit"], expected_pr_id=pr_id,
                        expected_title=original["title"],
                        previous_body=original["body"], body=body,
                    )
                    _check_pr(updated, original["title"], body, original["commit"])
                    disposition = updated.disposition
                self.ledger.confirm_effect(effect_id, str(pr_id))
            report.update({"status": "ATTACHED", "disposition": disposition})
            if context.cancel.is_set():
                return OperationResult("CANCELLED", "CANCEL_REQUESTED", state)
            return OperationResult("COMPLETED", result=state)
        except (GitHubError, SourceError, ValueError) as error:
            code = getattr(error, "code", "REPORT_LINK_INVALID")
            report["status"] = ("UNKNOWN" if code in {
                "PULL_REQUEST_UPDATE_UNKNOWN", "PULL_REQUEST_OUTCOME_UNKNOWN",
            } else "BLOCKED")
            return OperationResult("BLOCKED", code, state)

    def _reconcile_pr(self, head_branch: str, base_branch: str,
                      commit: str, title: str, body: str,
                      expected_pr_id: int | None) -> PullRequestResult:
        result = self.github.reconcile_draft_pr(
            self.repository, head_branch=head_branch,
            base_branch=base_branch, expected_head_sha=commit,
            expected_pr_id=expected_pr_id,
        )
        _check_pr(result, title, body, commit)
        return result


def _confirmed_report_folder(operation: dict[str, Any], run_id: str) -> str | None:
    effect = _effect(operation, "drive-run-folder", "drive_file")
    if effect is None or effect["state"] != "CONFIRMED":
        return None
    folder_id = effect["provider_id"]
    intent = effect["intent"]
    if (not isinstance(folder_id, str) or _DRIVE_ID.fullmatch(folder_id) is None
            or intent.get("key") != f"result-run:{run_id}"
            or intent.get("name") != run_id
            or intent.get("mime_type") != "application/vnd.google-apps.folder"):
        return None
    return folder_id


def _effect(operation: dict[str, Any] | None, step: str, kind: str) -> dict[str, Any] | None:
    if operation is None:
        return None
    for item in operation["effects"]:
        if item["step_id"] == step and item["kind"] == kind:
            return cast(dict[str, Any], item)
    return None


def _check_pr(result: PullRequestResult, title: str, body: str, commit: str) -> None:
    pr = result.pull_request
    if (not pr.draft or pr.head_sha != commit
            or pr.title != title or pr.body != body):
        raise GitHubError("PULL_REQUEST_OUTCOME_UNKNOWN",
                          "Draft pull request does not reflect the tested run")
