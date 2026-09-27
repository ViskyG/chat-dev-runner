"""GitHub publish orchestration with an exact fake Git transport and real local Git."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_git_publish import Case, _case
from test_publish_run_workflow import _workflow

from runner_app.git.argv import GitError, run_git
from runner_app.git.github_transport import GitHubGitTransport, OwnerTokenFile
from runner_app.storage.ledger import Ledger
from runner_app.workflows.publish_run import PublishRunWorkflow


class FakeGitPush:
    def __init__(self, case: Case, repository: Any) -> None:
        self.case = case
        self.repository = repository
        self.url = repository.clone_url
        self.push_calls = 0
        self.read_calls = 0
        self.fail_auth = False
        self.lost_response = False
        self.read_unavailable_after_push = False
        self.wrong_view = False

    def read_head(self, repo: Path, branch: str) -> str | None:
        assert repo == self.case.repo and branch == "feature/area"
        self.read_calls += 1
        if self.fail_auth:
            raise GitError("GITHUB_AUTH_NOT_CONFIGURED", "Synthetic auth gate")
        if self.read_unavailable_after_push and self.push_calls:
            raise GitError("GITHUB_GIT_TIMEOUT", "Synthetic read outage")
        if self.wrong_view:
            return self.case.base
        lines = run_git(repo, ["ls-remote", "--refs", "--heads", "publish",
                               f"refs/heads/{branch}"]).splitlines()
        return lines[0].split("\t")[0] if lines else None

    def push_commit(self, repo: Path, commit: str, branch: str) -> None:
        assert repo == self.case.repo and branch == "feature/area"
        assert commit == self.case.commit
        self.push_calls += 1
        run_git(repo, ["push", "--porcelain", "--no-verify", "--no-follow-tags",
                       "publish", f"{commit}:refs/heads/{branch}"])
        if self.lost_response:
            raise GitError("GITHUB_GIT_TIMEOUT", "Synthetic lost push response")


def _live(case: Case) -> tuple[PublishRunWorkflow, Ledger, Any, Any, Any, FakeGitPush]:
    local, ledger, context, frozen, fake_rest = _workflow(case)
    transport = FakeGitPush(case, local.repository)
    live = PublishRunWorkflow(ledger, case.registry, local.github,
                              local.repository, git_transport=transport)
    return live, ledger, context, frozen, fake_rest, transport


def test_authenticated_path_pushes_exact_attested_commit_and_draft(tmp_path: Path) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, frozen, rest, git = _live(case)
    result = workflow(frozen, context)
    assert result.status == "COMPLETED"
    assert result.result["tested_commit"] == case.commit
    assert result.result["publication"]["status"] == "DRAFT_PUBLISHED"
    assert result.result["publication"]["remote_head"] == case.commit
    assert git.push_calls == rest.pr_post_count == 1
    assert run_git(case.remote, ["rev-parse", "refs/heads/feature/area"]).strip() == case.commit
    assert run_git(case.workspace, ["rev-parse", "HEAD"]).strip() == case.base
    effects = ledger.get(frozen.command["request_id"])["effects"]
    assert [(item["kind"], item["state"]) for item in effects] == [
        ("git_ref", "CONFIRMED"), ("pull_request", "CONFIRMED"),
    ]


def test_lost_push_reply_reconciles_read_only_and_never_repushes(tmp_path: Path) -> None:
    case = _case(tmp_path)
    workflow, _, context, frozen, rest, git = _live(case)
    git.lost_response = True
    result = workflow(frozen, context)
    assert result.status == "COMPLETED"
    assert result.result["publication"]["git_status"] == "PUBLISHED_RECONCILED"
    assert git.push_calls == rest.pr_post_count == 1
    assert git.read_calls >= 4


def test_unreadable_push_outcome_keeps_intent_for_read_only_reconciliation(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, frozen, rest, git = _live(case)
    git.lost_response = True
    git.read_unavailable_after_push = True
    first = workflow(frozen, context)
    assert first.status == "BLOCKED" and first.code == "PUSH_OUTCOME_UNKNOWN"
    assert first.result["publication"]["status"] == "UNKNOWN"
    assert git.push_calls == 1 and rest.pr_post_count == 0
    effects = ledger.get(frozen.command["request_id"])["effects"]
    assert len(effects) == 1 and effects[0]["state"] == "INTENDED"
    git.read_unavailable_after_push = False
    second = workflow(frozen, context)
    assert second.status == "COMPLETED"
    assert second.result["publication"]["status"] == "DRAFT_PUBLISHED"
    assert git.push_calls == rest.pr_post_count == 1


def test_auth_failure_or_divergent_view_blocks_before_push_intent(tmp_path: Path) -> None:
    auth_case = _case(tmp_path / "auth")
    workflow, ledger, context, frozen, _, git = _live(auth_case)
    git.fail_auth = True
    denied = workflow(frozen, context)
    assert denied.status == "BLOCKED" and denied.code == "GITHUB_AUTH_NOT_CONFIGURED"
    assert git.push_calls == 0
    assert ledger.get(frozen.command["request_id"])["effects"] == []

    view_case = _case(tmp_path / "view")
    workflow, ledger, context, frozen, _, git = _live(view_case)
    git.wrong_view = True
    mismatch = workflow(frozen, context)
    assert mismatch.status == "BLOCKED" and mismatch.code == "REMOTE_VIEW_MISMATCH"
    assert mismatch.result["publication"]["remote_head"] == view_case.base
    assert git.push_calls == 0
    assert ledger.get(frozen.command["request_id"])["effects"] == []


def test_target_editor_change_blocks_fresh_github_push(tmp_path: Path) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, frozen, rest, git = _live(case)
    (case.workspace / "editor-note.txt").write_text("keep this\n")
    result = workflow(frozen, context)
    assert result.status == "BLOCKED" and result.code == "WORKSPACE_CHANGED"
    assert git.push_calls == rest.pr_post_count == 0
    assert ledger.get(frozen.command["request_id"])["effects"] == []
    assert (case.workspace / "editor-note.txt").read_text() == "keep this\n"
    assert not run_git(case.repo, ["ls-remote", "--refs", "--heads", "publish",
                                   "refs/heads/feature/area"])


def test_live_transport_constructor_refuses_wrong_repository_identity(tmp_path: Path) -> None:
    case = _case(tmp_path)
    local, ledger, _, _, _ = _workflow(case)
    mismatch = FakeGitPush(case, replace(local.repository, id=202))
    with pytest.raises(ValueError, match="transport differs"):
        PublishRunWorkflow(ledger, case.registry, local.github,
                           local.repository, git_transport=mismatch)


def test_real_owner_token_transport_uses_fixed_https_url_with_fake_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path)
    local, ledger, context, frozen, rest = _workflow(case)
    credential_dir = tmp_path / "owner-token"
    credential_dir.mkdir(mode=0o700)
    token_file = credential_dir / "github-token"
    token_file.write_text("test-only-token\n")
    token_file.chmod(0o600)
    transport = GitHubGitTransport(local.repository, OwnerTokenFile(token_file))
    observed: list[str] = []

    def fake_network(repo: Path, operation: list[str], token: bytes, *, timeout: float) -> bytes:
        assert repo == case.repo and token == b"test-only-token"
        assert local.repository.clone_url in operation
        assert timeout in {30, 120}
        observed.append(operation[0])
        if operation[0] == "ls-remote":
            text = run_git(case.repo, ["ls-remote", "--refs", "--heads", "publish",
                                       operation[-1]])
            return text.encode()
        assert operation[0] == "push"
        run_git(case.repo, ["push", "--porcelain", "--no-verify", "--no-follow-tags",
                                "publish", operation[-1]])
        return b""

    monkeypatch.setattr("runner_app.git.github_transport._run_network_git", fake_network)
    workflow = PublishRunWorkflow(ledger, case.registry, local.github,
                                  local.repository, git_transport=transport)
    result = workflow(frozen, context)
    assert result.status == "COMPLETED"
    assert result.result["publication"]["status"] == "DRAFT_PUBLISHED"
    assert observed.count("push") == 1
    assert rest.pr_post_count == 1


def test_confirmed_github_push_reconciles_after_later_editor_change(tmp_path: Path) -> None:
    case = _case(tmp_path)
    workflow, _, context, frozen, rest, git = _live(case)
    first = workflow(frozen, context)
    assert first.status == "COMPLETED"
    (case.workspace / "later-editor.txt").write_text("unpublished edit\n")
    again = workflow(frozen, context)
    assert again.status == "COMPLETED"
    assert again.result["publication"]["status"] == "DRAFT_PUBLISHED"
    assert again.result["editor_head"] == case.base
    assert again.result["editor_matches_tested"] is False
    assert git.push_calls == rest.pr_post_count == 1
    assert (case.workspace / "later-editor.txt").read_text() == "unpublished edit\n"


def test_successful_push_with_unavailable_readback_is_unknown_until_reconciled(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, frozen, rest, git = _live(case)
    git.read_unavailable_after_push = True
    first = workflow(frozen, context)
    assert first.status == "BLOCKED" and first.code == "PUSH_OUTCOME_UNKNOWN"
    assert first.result["publication"]["status"] == "UNKNOWN"
    assert git.push_calls == 1 and rest.pr_post_count == 0
    assert run_git(case.remote, ["rev-parse", "refs/heads/feature/area"]).strip() == case.commit
    effects = ledger.get(frozen.command["request_id"])["effects"]
    assert len(effects) == 1 and effects[0]["state"] == "INTENDED"
    git.read_unavailable_after_push = False
    second = workflow(frozen, context)
    assert second.status == "COMPLETED"
    assert git.push_calls == rest.pr_post_count == 1
