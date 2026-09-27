"""Real local Git and fake REST prove GitHub RECONCILE ports are read-only."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from test_git_publish import _case
from test_publish_run_workflow import (
    FakeGitHubREST,
    _full_route_published,
    _mark_run_folder_delivered,
    _workflow,
)

from runner_app.adapters.github import GitHubREST, make_provision_marker
from runner_app.workflows.reconcile_github import GitHubEffectReader


def _uncertain(target: dict, step: str) -> dict:
    selected = next(item for item in target["effects"] if item["step_id"] == step)
    return {**selected, "state": "INTENDED", "provider_id": None}


def test_confirmed_private_repository_marker_proves_lost_create_ack(tmp_path: Path) -> None:
    case = _case(tmp_path)
    fake = FakeGitHubREST(case)
    installation = str(uuid4())
    request_id = str(uuid4())
    marker = make_provision_marker(installation, "fixture", request_id)
    original = fake._repo
    fake._repo = lambda: {**original(), "description": marker}  # type: ignore[method-assign]
    reader = GitHubEffectReader(
        GitHubREST(fake, expected_login="alice", expected_user_id=1),
        case.registry, installation_id=installation, github_owner="alice",
    )
    target = {
        "request_id": request_id, "project_id": "fixture", "action": "CREATE_PROJECT",
        "status": "BLOCKED",
        "command": {"action": "CREATE_PROJECT", "params": {"source": {"kind": "new"}}},
    }
    effect = {
        "effect_id": str(uuid4()), "step_id": "github-repository",
        "kind": "github_repository", "state": "INTENDED",
        "intent": {"owner": "alice", "name": "fixture", "private": True,
                   "marker": marker},
    }
    before = len(fake.calls)
    proof = reader.repository(target, effect)
    assert proof is not None and proof.provider_id == "101"
    assert proof.observed_intent == effect["intent"]
    assert all(method == "GET" for method, _ in fake.calls[before:])

    effect["intent"] = {**effect["intent"], "marker": "cdr:v1:" + "f" * 64}
    assert reader.repository(target, effect) is None
    assert all(method == "GET" for method, _ in fake.calls)


def test_ref_and_draft_pr_readback_match_exact_published_commit(tmp_path: Path) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, frozen, fake = _workflow(case)
    published = workflow(frozen, context)
    assert published.status == "COMPLETED"
    ledger.finish(frozen.command["request_id"], published.status, result=published.result)
    target = ledger.get(frozen.command["request_id"])
    assert target is not None
    target = {**target, "status": "BLOCKED"}
    reader = GitHubEffectReader(
        workflow.github, case.registry, installation_id=str(uuid4()),
        github_owner="alice",
    )
    git_effect = _uncertain(target, "tested-git-push")
    pr_effect = _uncertain(target, "draft-pull-request")
    before = len(fake.calls)
    git_proof = reader.ref(target, git_effect)
    pr_proof = reader.pull_request(target, pr_effect)
    assert git_proof is not None and git_proof.provider_id == case.commit
    assert pr_proof is not None and pr_proof.provider_id == "808"
    assert pr_proof.observed_intent == pr_effect["intent"]
    assert all(method == "GET" for method, _ in fake.calls[before:])

    changed = {**git_effect, "intent": {**git_effect["intent"], "commit": case.base}}
    assert reader.ref(target, changed) is None
    fake.prs[0]["body"] = "manual edit"
    assert reader.pull_request(target, pr_effect) is None


def test_report_link_port_reads_same_numbered_draft_and_body(tmp_path: Path) -> None:
    case = _case(tmp_path)
    workflow, ledger, context, fake, request_id = _full_route_published(case)
    _mark_run_folder_delivered(ledger, request_id, case.run_id)
    linked = workflow.attach_report_link(request_id, case.run_id, "folder1234", context)
    assert linked.status == "COMPLETED"
    target = ledger.get(request_id)
    assert target is not None
    target = {**target, "status": "BLOCKED"}
    reader = GitHubEffectReader(
        workflow.github, case.registry, installation_id=str(uuid4()),
        github_owner="alice",
    )
    effect = _uncertain(target, "attach-run-report-link")
    before = len(fake.calls)
    proof = reader.pull_request(target, effect)
    assert proof is not None and proof.provider_id == "808"
    assert proof.observed_intent == effect["intent"]
    assert all(method == "GET" for method, _ in fake.calls[before:])
    fake.prs[0]["body"] = "manual edit"
    assert reader.pull_request(target, effect) is None
