"""EP-05 source-run publication uses real local Git and a fake GitHub REST peer."""
from __future__ import annotations

import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from test_git_publish import Case, _case

from runner_app.adapters.github import GitHubHTTPResponse, GitHubREST
from runner_app.domain.commands import freeze
from runner_app.engine.service import Context
from runner_app.git.argv import GitError, run_git
from runner_app.storage.ledger import Ledger
from runner_app.workflows.publish_run import PublishRunWorkflow, _matches_project_remote


def _time() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakeGitHubREST:
    def __init__(self, case: Case) -> None:
        self.case = case
        self.prs: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.pr_create_timeout = False
        self.pr_create_status: int | None = None
        self.pr_readback_unavailable = False
        self.pr_readback_after_post = False
        self.cancel_on_branch_read: threading.Event | None = None
        self.pr_post_count = 0
        self.pr_patch_count = 0
        self.pr_patch_timeout = False
        self.pr_get_timeout_after_patch = False

    def _head(self, branch: str) -> str | None:
        try:
            return run_git(self.case.remote, ["rev-parse", "--verify",
                                              f"refs/heads/{branch}^{{commit}}"] ).strip()
        except GitError:
            return None

    def _repo(self) -> dict[str, Any]:
        return {
            "id": 101, "node_id": "R_101", "owner": {"login": "alice", "id": 1},
            "name": "fixture", "full_name": "alice/fixture", "private": True,
            "default_branch": "main", "description": "cdr:v1:" + "a" * 64,
            "html_url": "https://github.com/alice/fixture",
            "clone_url": "https://github.com/alice/fixture.git",
            "ssh_url": "git@github.com:alice/fixture.git",
        }

    def request(self, method: str, path: str, *, params: Any = None,
                body: Any = None) -> GitHubHTTPResponse:
        self.calls.append((method, path))
        if method == "GET" and path == "/user":
            return GitHubHTTPResponse(200, {"login": "alice", "id": 1})
        if method == "GET" and path == "/users/alice":
            return GitHubHTTPResponse(200, {"login": "alice", "id": 1, "type": "User"})
        if method == "GET" and path == "/repos/alice/fixture":
            return GitHubHTTPResponse(200, self._repo())
        prefix = "/repos/alice/fixture"
        if method == "GET" and path.startswith(prefix + "/branches/"):
            branch = urllib.parse.unquote(path[len(prefix + "/branches/"):])
            head = self._head(branch)
            if self.cancel_on_branch_read is not None and branch == "feature/area" and head:
                self.cancel_on_branch_read.set()
                self.cancel_on_branch_read = None
            return (GitHubHTTPResponse(200, {"name": branch, "commit": {"sha": head},
                                             "protected": branch == "main"}) if head
                    else GitHubHTTPResponse(404, {}))
        if method == "GET" and path == prefix + "/pulls":
            if self.pr_readback_unavailable or (self.pr_readback_after_post
                                                 and self.pr_post_count > 0):
                raise TimeoutError("Fake readback outage")
            return GitHubHTTPResponse(200, list(self.prs))
        if method == "POST" and path == prefix + "/pulls":
            self.pr_post_count += 1
            if self.pr_create_status is not None:
                return GitHubHTTPResponse(self.pr_create_status, {})
            head = self._head("feature/area")
            assert head is not None
            pr = {
                "id": 808, "node_id": "PR_8", "number": 8,
                "html_url": "https://github.com/alice/fixture/pull/8",
                "state": "open", "draft": body["draft"],
                "title": body["title"], "body": body["body"],
                "head": {"ref": "feature/area", "sha": head, "repo": {"id": 101}},
                "base": {"ref": "main", "repo": {"id": 101}},
            }
            self.prs.append(pr)
            if self.pr_create_timeout:
                raise TimeoutError("Fake lost POST response")
            return GitHubHTTPResponse(201, pr)
        if method == "GET" and path == prefix + "/pulls/8":
            if self.pr_get_timeout_after_patch and self.pr_patch_count:
                raise TimeoutError("Fake lost PR readback")
            return GitHubHTTPResponse(200, self.prs[0])
        if method == "PATCH" and path == prefix + "/pulls/8":
            self.pr_patch_count += 1
            assert len(self.prs) == 1 and isinstance(body, dict)
            assert set(body) == {"body"}
            self.prs[0]["body"] = body["body"]
            if self.pr_patch_timeout:
                raise TimeoutError("Fake lost PATCH response")
            return GitHubHTTPResponse(200, self.prs[0])
        raise AssertionError((method, path, params, body))


def _workflow(case: Case, policy: str = "draft_always", *, source_run_id: str | None = None,
              bind_remote: bool = True,
              ) -> tuple[PublishRunWorkflow, Ledger, Context, Any, FakeGitHubREST]:
    if bind_remote:
        case.registry.bind_project_remote(
            "fixture", provider_id=101, node_id="R_101", owner="alice", name="fixture",
            html_url="https://github.com/alice/fixture",
            clone_url="https://github.com/alice/fixture.git",
            provision_marker="cdr:v1:" + "a" * 64,
        )
    ledger = Ledger(case.registry.database)
    source_run = ledger.get_run(case.run_id)
    assert source_run is not None
    source_request_id = source_run["request_id"]
    if ledger.get(source_request_id)["status"] == "RUNNING":
        ledger.finish(source_request_id, "COMPLETED", result={"run_id": case.run_id})
    state = ledger.state()
    request_id = str(uuid4())
    command = {
        "schema_version": "1.0", "request_id": request_id, "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": _time(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "action": "PUBLISH", "project_id": "fixture", "workspace_id": "area",
        "expected_head": case.base,
        "params": {"source_run_id": source_run_id or case.run_id, "publication": policy},
        "intent": "Publish a previously tested disposable source run",
    }
    frozen = freeze(command)
    assert ledger.admit(command, frozen.digest, "local-owner", ("repo:fixture",)).execute
    fake = FakeGitHubREST(case)
    github = GitHubREST(fake, expected_login="alice", expected_user_id=1)
    repository = github.get_repository("alice", "fixture", expected_repo_id=101)
    context = Context(threading.Event(), ledger)
    workflow = PublishRunWorkflow(ledger, case.registry, github, repository,
                                  remote_name="publish", remote_url=case.remote)
    return workflow, ledger, context, frozen, fake


def test_source_run_publish_pushes_exact_tested_commit_and_draft(tmp_path: Path) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, frozen, fake = _workflow(case)
    (case.other_workspace / "parallel-edit.txt").write_text("keep\n")
    result = workflow(frozen, context)
    assert result.status == "COMPLETED"
    assert result.result["execution_outcome"] == "PASSED"
    assert result.result["tested_commit"] == case.commit
    assert result.result["editor_head"] == case.base
    assert result.result["editor_matches_tested"] is False
    assert result.result["publication"]["status"] == "DRAFT_PUBLISHED"
    assert result.result["publication"]["pull_request_id"] == 808
    assert run_git(case.remote, ["rev-parse", "refs/heads/feature/area"]).strip() == case.commit
    assert run_git(case.workspace, ["rev-parse", "HEAD"]).strip() == case.base
    assert (case.other_workspace / "parallel-edit.txt").read_text() == "keep\n"
    assert fake.pr_post_count == 1 and fake.prs[0]["draft"] is True
    assert "Approved profile: python-quick" in fake.prs[0]["body"]
    assert "Profile revision: " + "a" * 64 in fake.prs[0]["body"]
    effects = ledger.get(frozen.command["request_id"])["effects"]
    assert [(effect["kind"], effect["state"], effect["provider_id"])
            for effect in effects] == [
                ("git_ref", "CONFIRMED", case.commit), ("pull_request", "CONFIRMED", "808"),
            ]


def test_on_pass_skips_failing_run_but_draft_always_labels_failure(tmp_path: Path) -> None:
    skipped_case = _case(tmp_path / "skip", outcome="FAILED")
    skipped, _, skipped_context, skipped_frozen, skipped_fake = _workflow(
        skipped_case, "on_pass",
    )
    result = skipped(skipped_frozen, skipped_context)
    assert result.status == "COMPLETED"
    assert result.result["publication"]["status"] == "SKIPPED_FAILED_TESTS"
    assert skipped_fake.pr_post_count == 0
    assert not run_git(skipped_case.repo, ["ls-remote", "--refs", "--heads", "publish",
                                          "refs/heads/feature/area"])

    failed_case = _case(tmp_path / "draft", outcome="FAILED")
    workflow, _, context, frozen, fake = _workflow(failed_case)
    published = workflow(frozen, context)
    assert published.status == "COMPLETED"
    assert published.result["execution_outcome"] == "FAILED"
    assert published.result["publication"]["status"] == "DRAFT_PUBLISHED"
    assert "FAILED" in fake.prs[0]["title"] and "FAILED" in fake.prs[0]["body"]


def test_cancel_after_push_keeps_confirmed_commit_without_pr(tmp_path: Path,
                                                               monkeypatch: Any) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, frozen, fake = _workflow(case)
    from runner_app.workflows import publish_run as module
    original = module.publish_tested_snapshot

    def push_then_cancel(*args: Any, **kwargs: Any):
        result = original(*args, **kwargs)
        context.cancel.set()
        return result

    monkeypatch.setattr(module, "publish_tested_snapshot", push_then_cancel)
    result = workflow(frozen, context)
    assert result.status == "CANCELLED"
    assert result.result["publication"]["status"] == "PUSHED"
    assert result.result["publication"]["remote_head"] == case.commit
    assert result.result["editor_head"] == case.base
    assert run_git(case.remote, ["rev-parse", "refs/heads/feature/area"]).strip() == case.commit
    assert fake.pr_post_count == 0
    effects = ledger.get(frozen.command["request_id"])["effects"]
    assert len(effects) == 1 and effects[0]["state"] == "CONFIRMED"


def test_lost_pr_response_is_reconciled_read_only_without_duplicate(tmp_path: Path) -> None:
    case = _case(tmp_path)
    workflow, _, context, frozen, fake = _workflow(case)
    fake.pr_create_timeout = True
    result = workflow(frozen, context)
    assert result.status == "COMPLETED"
    assert result.result["publication"]["status"] == "DRAFT_PUBLISHED"
    assert result.result["publication"]["pull_request_disposition"] == "reconciled"
    assert fake.pr_post_count == 1


def test_pr_rate_limit_preserves_pushed_commit_and_reports_quota(tmp_path: Path) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, frozen, fake = _workflow(case)
    fake.pr_create_status = 429

    result = workflow(frozen, context)

    assert result.status == "BLOCKED" and result.code == "GITHUB_QUOTA_EXCEEDED"
    assert result.result["publication"]["status"] == "BLOCKED"
    assert result.result["publication"]["remote_head"] == case.commit
    assert run_git(case.remote, ["rev-parse", "refs/heads/feature/area"]).strip() == case.commit
    assert fake.pr_post_count == 1 and fake.prs == []
    effects = ledger.get(frozen.command["request_id"])["effects"]
    assert [(effect["kind"], effect["state"]) for effect in effects] == [
        ("git_ref", "CONFIRMED"), ("pull_request", "INTENDED"),
    ]
    fake.pr_create_status = None
    retried = workflow(frozen, context)
    assert retried.status == "BLOCKED" and retried.code == "PULL_REQUEST_CREATE_UNKNOWN"
    assert fake.pr_post_count == 1 and fake.prs == []


def test_unavailable_pr_readback_keeps_intent_and_never_rewrites(tmp_path: Path) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, frozen, fake = _workflow(case)
    fake.pr_create_timeout = True
    fake.pr_readback_after_post = True
    first = workflow(frozen, context)
    assert first.status == "BLOCKED"
    assert first.code == "PULL_REQUEST_CREATE_UNKNOWN"
    assert first.result["publication"]["status"] == "UNKNOWN"
    assert first.result["publication"]["remote_head"] == case.commit
    assert fake.pr_post_count == 1
    fake.pr_readback_after_post = False
    second = workflow(frozen, context)
    assert second.status == "COMPLETED"
    assert second.result["publication"]["status"] == "DRAFT_PUBLISHED"
    assert fake.pr_post_count == 1
    effects = ledger.get(frozen.command["request_id"])["effects"]
    assert [effect["state"] for effect in effects] == ["CONFIRMED", "CONFIRMED"]


def test_never_policy_is_a_no_effect_full_route_step(tmp_path: Path) -> None:
    case = _case(tmp_path, source_policy="never")
    ledger = Ledger(case.registry.database)
    run = ledger.get_run(case.run_id)
    assert run is not None
    source_request_id = run["request_id"]
    fake = FakeGitHubREST(case)
    github = GitHubREST(fake, expected_login="alice", expected_user_id=1)
    repository = github.get_repository("alice", "fixture", expected_repo_id=101)
    workflow = PublishRunWorkflow(ledger, case.registry, github, repository,
                                  remote_name="publish", remote_url=case.remote)
    result = workflow.publish_run(source_request_id, case.run_id, "never",
                                  Context(threading.Event(), ledger))
    assert result.status == "COMPLETED"
    assert result.result["publication"]["status"] == "SKIPPED_POLICY"
    assert fake.pr_post_count == 0
    assert not run_git(case.repo, ["ls-remote", "--refs", "--heads", "publish",
                                   "refs/heads/feature/area"])
    assert not any(effect["step_id"] == "tested-git-push"
                   for effect in ledger.get(source_request_id)["effects"])


def test_unattested_source_run_is_rejected_before_any_git_effect(tmp_path: Path) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, frozen, fake = _workflow(
        case, source_run_id=str(uuid4()),
    )
    result = workflow(frozen, context)
    assert result.status == "BLOCKED"
    assert result.code == "RUN_NOT_TESTED"
    assert fake.pr_post_count == 0
    assert ledger.get(frozen.command["request_id"])["effects"] == []
    assert not run_git(case.repo, ["ls-remote", "--refs", "--heads", "publish",
                                   "refs/heads/feature/area"])


def test_conflicting_remote_head_blocks_before_git_or_pr_write(tmp_path: Path) -> None:
    case = _case(tmp_path)
    tree = run_git(case.repo, ["rev-parse", f"{case.base}^{{tree}}"] ).strip()
    sibling = run_git(case.repo, [
        "-c", "user.name=Another Actor", "-c", "user.email=other@local.invalid",
        "commit-tree", tree, "-p", case.base, "-m", "Independent change",
    ]).strip()
    run_git(case.repo, ["push", "publish", f"{sibling}:refs/heads/feature/area"])
    workflow, ledger, context, frozen, fake = _workflow(case)
    result = workflow(frozen, context)
    assert result.status == "BLOCKED" and result.code == "REMOTE_CHANGED"
    assert result.result["publication"]["remote_head"] == sibling
    assert fake.pr_post_count == 0
    assert ledger.get(frozen.command["request_id"])["effects"] == []
    assert run_git(case.remote, ["rev-parse", "refs/heads/feature/area"]).strip() == sibling



def test_wrong_bound_repository_refuses_before_remote_read_or_git_effect(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    case.registry.bind_project_remote(
        "fixture", provider_id=202, node_id="R_202", owner="alice", name="fixture",
        html_url="https://github.com/alice/fixture",
        clone_url="https://github.com/alice/fixture.git",
        provision_marker="cdr:v1:" + "a" * 64,
    )
    workflow, ledger, context, frozen, fake = _workflow(case, bind_remote=False)
    prior_calls = list(fake.calls)
    result = workflow(frozen, context)
    assert result.status == "BLOCKED" and result.code == "PROJECT_REMOTE_MISMATCH"
    assert fake.calls == prior_calls
    assert ledger.get(frozen.command["request_id"])["effects"] == []
    assert not run_git(case.repo, ["ls-remote", "--refs", "--heads", "publish",
                                   "refs/heads/feature/area"])


def test_unbound_project_refuses_before_remote_read_or_git_effect(tmp_path: Path) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, frozen, fake = _workflow(case, bind_remote=False)
    prior_calls = list(fake.calls)
    result = workflow(frozen, context)
    assert result.status == "BLOCKED" and result.code == "PROJECT_REMOTE_UNBOUND"
    assert fake.calls == prior_calls
    assert ledger.get(frozen.command["request_id"])["effects"] == []


@pytest.mark.parametrize("origin_kind", ["github", "local"])
def test_imported_project_exact_binding_allows_publication(
    tmp_path: Path, origin_kind: str,
) -> None:
    case = _case(tmp_path, origin_kind=origin_kind)
    if origin_kind == "github":
        case.registry.bind_imported_project_remote(
            "fixture", origin_reference="alice/fixture", provider_id=101,
            owner_id=1, node_id="R_101", owner="alice", name="fixture",
            html_url="https://github.com/alice/fixture",
            clone_url="https://github.com/alice/fixture.git",
            default_branch="main", default_head=case.base, private=True,
        )
    else:
        case.registry.bind_local_import_project_remote(
            "fixture", origin_reference="f" * 48, provider_id=101,
            owner_id=1, node_id="R_101", owner="alice", name="fixture",
            html_url="https://github.com/alice/fixture",
            clone_url="https://github.com/alice/fixture.git",
            provision_marker="cdr:v1:" + "a" * 64,
            source_branch="main", source_head=case.base, private=True,
        )
    workflow, _, context, frozen, fake = _workflow(case, bind_remote=False)
    result = workflow(frozen, context)
    assert result.status == "COMPLETED"
    assert result.result["publication"]["status"] == "DRAFT_PUBLISHED"
    assert fake.pr_post_count == 1


@pytest.mark.parametrize("field,wrong", [
    ("provider_id", 202), ("node_id", "R_202"), ("owner", "bob"),
    ("name", "elsewhere"), ("html_url", "https://github.com/alice/elsewhere"),
    ("clone_url", "https://github.com/alice/elsewhere.git"),
    ("private", False), ("provision_marker", "cdr:v1:" + "b" * 64),
])
def test_project_remote_common_identity_is_exact(
    tmp_path: Path, field: str, wrong: Any,
) -> None:
    case = _case(tmp_path)
    workflow, _, _, _, _ = _workflow(case)
    project = case.registry.get_project("fixture")
    bound = case.registry.get_project_remote("fixture")
    assert project is not None and bound is not None
    assert _matches_project_remote(project, bound, workflow.repository)
    changed = {**bound, field: wrong}
    assert not _matches_project_remote(project, changed, workflow.repository)



def _full_route_published(
    case: Case,
) -> tuple[PublishRunWorkflow, Ledger, Context, FakeGitHubREST, str]:
    case.registry.bind_project_remote(
        "fixture", provider_id=101, node_id="R_101", owner="alice", name="fixture",
        html_url="https://github.com/alice/fixture",
        clone_url="https://github.com/alice/fixture.git",
        provision_marker="cdr:v1:" + "a" * 64,
    )
    ledger = Ledger(case.registry.database)
    fake = FakeGitHubREST(case)
    github = GitHubREST(fake, expected_login="alice", expected_user_id=1)
    repository = github.get_repository("alice", "fixture", expected_repo_id=101)
    workflow = PublishRunWorkflow(ledger, case.registry, github, repository,
                                  remote_name="publish", remote_url=case.remote)
    source = ledger.get_run(case.run_id)
    assert source is not None
    request_id = source["request_id"]
    context = Context(threading.Event(), ledger)
    published = workflow.publish_run(request_id, case.run_id, "draft_always", context)
    assert published.status == "COMPLETED"
    assert published.result["publication"]["status"] == "DRAFT_PUBLISHED"
    return workflow, ledger, context, fake, request_id


def _mark_run_folder_delivered(
    ledger: Ledger, request_id: str, run_id: str, folder_id: str = "folder1234",
) -> None:
    effect = ledger.begin_effect(
        request_id, "drive-run-folder", "drive_file",
        {"parent_id": "request1234", "installation_id": "install1234",
         "key": f"result-run:{run_id}", "name": run_id,
         "mime_type": "application/vnd.google-apps.folder"},
    )
    ledger.confirm_effect(effect, folder_id)
    ledger.record_delivery(request_id, "DELIVERED")


def test_confirmed_drive_run_report_attaches_to_same_draft_once(tmp_path: Path) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, fake, request_id = _full_route_published(case)
    _mark_run_folder_delivered(ledger, request_id, case.run_id)
    first = workflow.attach_report_link(request_id, case.run_id, "folder1234", context)
    assert first.status == "COMPLETED"
    assert first.result["report_link"]["status"] == "ATTACHED"
    assert first.result["report_link"]["url"] == (
        "https://drive.google.com/drive/folders/folder1234"
    )
    assert first.result["report_link"]["pull_request_id"] == 808
    assert first.result["report_link"]["pull_request_url"] == (
        "https://github.com/alice/fixture/pull/8"
    )
    assert fake.pr_post_count == fake.pr_patch_count == 1
    assert fake.prs[0]["body"].endswith(
        "Report: https://drive.google.com/drive/folders/folder1234\n"
    )
    again = workflow.attach_report_link(request_id, case.run_id, "folder1234", context)
    assert again.status == "COMPLETED"
    assert again.result["report_link"]["disposition"] == "reconciled"
    assert fake.pr_post_count == fake.pr_patch_count == 1
    effect = next(item for item in ledger.get(request_id)["effects"]
                  if item["step_id"] == "attach-run-report-link")
    assert effect["state"] == "CONFIRMED" and effect["provider_id"] == "808"


def test_report_link_reconciles_lost_patch_without_rewriting(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, fake, request_id = _full_route_published(case)
    _mark_run_folder_delivered(ledger, request_id, case.run_id)
    fake.pr_patch_timeout = True
    attached = workflow.attach_report_link(request_id, case.run_id, "folder1234", context)
    assert attached.status == "COMPLETED"
    assert attached.result["report_link"]["disposition"] == "reconciled"
    assert fake.pr_post_count == fake.pr_patch_count == 1
    effect = next(item for item in ledger.get(request_id)["effects"]
                  if item["step_id"] == "attach-run-report-link")
    assert effect["state"] == "CONFIRMED"


def test_uncertain_report_link_is_read_only_on_retry(tmp_path: Path) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, fake, request_id = _full_route_published(case)
    _mark_run_folder_delivered(ledger, request_id, case.run_id)
    fake.pr_patch_timeout = True
    fake.pr_get_timeout_after_patch = True
    first = workflow.attach_report_link(request_id, case.run_id, "folder1234", context)
    assert first.status == "BLOCKED" and first.code == "PULL_REQUEST_UPDATE_UNKNOWN"
    assert first.result["report_link"]["status"] == "UNKNOWN"
    assert fake.pr_patch_count == 1
    fake.pr_get_timeout_after_patch = False
    second = workflow.attach_report_link(request_id, case.run_id, "folder1234", context)
    assert second.status == "COMPLETED"
    assert fake.pr_patch_count == 1


def test_unconfirmed_report_folder_blocks_without_github_read(tmp_path: Path) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, fake, request_id = _full_route_published(case)
    _mark_run_folder_delivered(ledger, request_id, case.run_id)
    previous_calls = list(fake.calls)
    denied = workflow.attach_report_link(request_id, case.run_id, "different1234", context)
    assert denied.status == "BLOCKED" and denied.code == "REPORT_FOLDER_UNCONFIRMED"
    assert fake.calls == previous_calls
    assert fake.pr_patch_count == 0
    assert not any(item["step_id"] == "attach-run-report-link"
                   for item in ledger.get(request_id)["effects"])


def test_later_publish_of_same_run_preserves_verified_report_link(tmp_path: Path) -> None:
    case = _case(tmp_path)
    original, ledger, context, fake, request_id = _full_route_published(case)
    _mark_run_folder_delivered(ledger, request_id, case.run_id)
    linked = original.attach_report_link(request_id, case.run_id, "folder1234", context)
    assert linked.status == "COMPLETED"
    ledger.finish(request_id, "COMPLETED", result={"run_id": case.run_id})
    again, _, second_context, frozen, second_fake = _workflow(case)
    second_fake.prs = fake.prs
    result = again(frozen, second_context)
    assert result.status == "COMPLETED"
    assert second_fake.pr_post_count == second_fake.pr_patch_count == 0
    assert second_fake.prs[0]["body"].endswith(
        "Report: https://drive.google.com/drive/folders/folder1234\n"
    )



def test_report_link_refuses_manual_pr_body_change_without_patch(tmp_path: Path) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, fake, request_id = _full_route_published(case)
    _mark_run_folder_delivered(ledger, request_id, case.run_id)
    fake.prs[0]["body"] += "Owner note\n"
    result = workflow.attach_report_link(request_id, case.run_id, "folder1234", context)
    assert result.status == "BLOCKED" and result.code == "PULL_REQUEST_CHANGED"
    assert result.result["report_link"]["status"] == "BLOCKED"
    assert fake.pr_post_count == 1 and fake.pr_patch_count == 0
    assert not any(item["step_id"] == "attach-run-report-link"
                   for item in ledger.get(request_id)["effects"])


def test_report_link_refuses_missing_original_pr_without_new_post(tmp_path: Path) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, fake, request_id = _full_route_published(case)
    _mark_run_folder_delivered(ledger, request_id, case.run_id)
    fake.prs.clear()
    result = workflow.attach_report_link(request_id, case.run_id, "folder1234", context)
    assert result.status == "BLOCKED" and result.code == "PULL_REQUEST_CREATE_UNKNOWN"
    assert fake.pr_post_count == 1 and fake.pr_patch_count == 0


def test_full_route_publish_retry_keeps_attached_report_url(tmp_path: Path) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, fake, request_id = _full_route_published(case)
    _mark_run_folder_delivered(ledger, request_id, case.run_id)
    linked = workflow.attach_report_link(request_id, case.run_id, "folder1234", context)
    assert linked.status == "COMPLETED"
    retried = workflow.publish_run(request_id, case.run_id, "draft_always", context)
    assert retried.status == "COMPLETED"
    assert retried.result["publication"]["status"] == "DRAFT_PUBLISHED"
    assert fake.pr_post_count == fake.pr_patch_count == 1
    assert fake.prs[0]["body"].endswith(
        "Report: https://drive.google.com/drive/folders/folder1234\n"
    )
