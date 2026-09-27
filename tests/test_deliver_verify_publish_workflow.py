"""Offline full-route integration: real Git, SQLite, frozen slot and bundle; fake providers."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from test_publish_report_workflow import FakePublisher, report_command
from test_publish_run_workflow import FakeGitHubREST
from test_verify_handler import Fixture, setup  # noqa: F401 -- imported pytest fixture

from runner_app.adapters.drive_delivery import DeliveryBundle
from runner_app.adapters.drive_docs import DriveError, FrozenDriveInput
from runner_app.adapters.drive_poller import FrozenSlotStore
from runner_app.adapters.drive_publish import DeliveryResult
from runner_app.adapters.github import GitHubREST
from runner_app.domain.commands import Provenance, freeze, parse_command
from runner_app.engine.service import Context
from runner_app.executors.profiles import ProfileRegistry
from runner_app.executors.run_service import RunService
from runner_app.git.argv import GitError, run_git
from runner_app.git.changes import validate_changeset
from runner_app.git.service import SourceService
from runner_app.storage.ledger import Ledger
from runner_app.workflows.deliver_verify_publish import DeliverVerifyPublishWorkflow
from runner_app.workflows.publish_report import PublishReportWorkflow
from runner_app.workflows.publish_run import PublishRunWorkflow


class FakeDelivery:
    def __init__(self, ledger: Ledger, status: str = "DELIVERED") -> None:
        self.ledger = ledger
        self.status = status
        self.calls: list[tuple[DeliveryBundle, dict[str, Any]]] = []

    def publish(self, bundle: DeliveryBundle, *, receipt_key: str,
                latest_result_key: str) -> DeliveryResult:
        assert receipt_key == "receipt" and latest_result_key == "latest-result"
        assert bundle.source_request_id == bundle.delivery_request_id == bundle.run_id
        preflight = self.ledger.require_delivery_preflight(
            bundle.delivery_request_id, bundle.run_id,
        )
        assert preflight.source_status == "RUNNING"
        assert preflight.action == "DELIVER_VERIFY_PUBLISH"
        manifest = json.loads((bundle.root / "operation-manifest.json").read_text())
        run = json.loads((bundle.root / "run-manifest.json").read_text())
        command = json.loads((bundle.root / "frozen-command.json").read_text())
        assert manifest["run_ids"] == [bundle.run_id]
        assert manifest["status"] in {"COMPLETED", "BLOCKED"}
        assert manifest["delivery"] == {"status": "PENDING", "reason": None}
        assert run["run_id"] == bundle.run_id
        assert command["command"]["intent"] == "[REDACTED FOR DELIVERY]"
        assert (bundle.root / "input-manifest.json").is_file()
        assert (bundle.root / "bundle-index.json").is_file()
        self.calls.append((bundle, manifest))
        if self.status == "DELIVERED":
            effect = self.ledger.begin_effect(
                bundle.delivery_request_id, "drive-run-folder", "drive_file",
                {"parent_id": "request-folder", "installation_id": "test-installation",
                 "key": f"result-run:{bundle.run_id}", "name": bundle.run_id,
                 "mime_type": "application/vnd.google-apps.folder"},
            )
            self.ledger.confirm_effect(effect, "run-folder")
        return DeliveryResult(
            self.status, None if self.status == "DELIVERED" else "FAKE_UPLOAD_PENDING",
            "request-folder", "run-folder", ("operation-manifest.json",),
        )


@dataclass
class RouteCase:
    fixture: Fixture
    workflow: DeliverVerifyPublishWorkflow
    frozen: Any
    context: Context
    delivery: FakeDelivery
    github: FakeGitHubREST
    remote: Path
    request_id: str


def _route(tmp_path: Path, source: Fixture, *, policy: str = "draft_always",
           delivery_status: str = "DELIVERED",
           approved_output_ids: tuple[str, ...] = ()) -> RouteCase:
    project = source.registry.get_project("fixture")
    assert project is not None
    repo = Path(project["repo_path"])
    remote = tmp_path / "receiver.git"
    run_git(tmp_path, ["init", "--bare", "-b", "main", str(remote)])
    run_git(repo, ["remote", "add", "publish", str(remote)])
    run_git(repo, ["push", "publish", f"{source.head}:refs/heads/main"])
    source.registry.bind_project_remote(
        "fixture", provider_id=101, node_id="R_101", owner="alice", name="fixture",
        html_url="https://github.com/alice/fixture",
        clone_url="https://github.com/alice/fixture.git",
        provision_marker="cdr:v1:" + "a" * 64,
    )
    fake_rest = FakeGitHubREST(SimpleNamespace(remote=remote))
    github = GitHubREST(fake_rest, expected_login="alice", expected_user_id=1)
    repository = github.get_repository("alice", "fixture", expected_repo_id=101)
    publishing = PublishRunWorkflow(
        source.ledger, source.registry, github, repository,
        remote_name="publish", remote_url=remote,
    )
    profile_file = tmp_path / "owner-profiles" / "fixture-run.json"
    profiles = ProfileRegistry(profile_file.parent, {
        "fixture-run": hashlib.sha256(profile_file.read_bytes()).hexdigest(),
    })
    runs = RunService(
        source.ledger, source.registry, profiles, source.broker, source.store,
        sources_root=tmp_path / "state" / "sealed-sources",
        runs_root=tmp_path / "state" / "run-slots",
    )
    request_id = str(uuid4())
    command = source.command("DELIVER_VERIFY_PUBLISH")
    command["request_id"] = request_id
    command["params"] = {
        "changeset_doc_id": "changeset-doc", "changeset_revision": "change-rev-1",
        "profile_id": "fixture-run", "publication": policy,
    }
    change = {
        "schema_version": "1.0", "change_id": str(uuid4()),
        "project_id": "fixture", "workspace_id": "area", "base_head": source.head,
        "files": [{"op": "upsert", "path": "src/main.py", "expected_sha256": None,
                   "content_utf8": "value = 42\n"}],
    }
    raw_command = json.dumps(command).encode()
    raw_change = json.dumps(change).encode()
    frozen = parse_command(raw_command)
    provenance = Provenance(kind="drive", source_id="command-doc",
                            project_id="fixture", workspace_id="area")
    slot = FrozenDriveInput(
        command=frozen, raw_command=raw_command,
        command_doc_id="command-doc", command_revision="command-rev-1",
        command_text_sha256=hashlib.sha256(raw_command).hexdigest(),
        raw_changeset=raw_change, changeset_doc_id="changeset-doc",
        changeset_revision="change-rev-1",
        changeset_text_sha256=hashlib.sha256(raw_change).hexdigest(),
        change_plan=validate_changeset(
            change, project_id="fixture", workspace_id="area", expected_head=source.head,
        ),
    )
    slots = FrozenSlotStore(tmp_path / "state" / "frozen-inputs")
    assert slots.freeze(slot, provenance) == (request_id, True)
    admitted = source.ledger.admit(
        command, frozen.digest, provenance.source_id, ("repo:fixture",),
    )
    assert admitted.execute, admitted.receipt
    volume = source.prepare_volume(request_id)
    assert volume.name == request_id
    delivery = FakeDelivery(source.ledger, delivery_status)
    workflow = DeliverVerifyPublishWorkflow(
        SourceService(source.ledger, source.registry), runs, slots, publishing, delivery,
        volume_for_run=lambda run_id: source.volumes / run_id,
        bundle_parent=tmp_path / "state" / "delivery-bundles",
        receipt_key="receipt", latest_result_key="latest-result",
        export_policy_for_run=lambda _completion: (approved_output_ids, ()),
    )
    return RouteCase(source, workflow, frozen, Context(Event(), source.ledger),
                     delivery, fake_rest, remote, request_id)


@pytest.fixture
def source_fixture(request: pytest.FixtureRequest) -> Fixture:
    return request.getfixturevalue("setup")


def _remote_head(remote: Path) -> str | None:
    try:
        return run_git(remote, ["rev-parse", "--verify", "refs/heads/feature/area"]).strip()
    except GitError:
        return None


def test_pass_runs_once_and_delivers_real_immutable_bundle(tmp_path: Path,
                                                          source_fixture: Fixture) -> None:
    source_fixture.broker.outcome = "PASSED"
    case = _route(tmp_path, source_fixture)
    result = case.workflow(case.frozen, case.context)
    assert result.status == "COMPLETED"
    assert result.result["execution"]["outcome"] == "PASSED"
    assert result.result["publication"]["status"] == "DRAFT_PUBLISHED"
    assert result.result["delivery"]["status"] == "DELIVERED"
    assert result.result["report_link"] == {
        "status": "ATTACHED",
        "url": "https://drive.google.com/drive/folders/run-folder",
        "pull_request_id": 808,
        "pull_request_url": "https://github.com/alice/fixture/pull/8",
        "disposition": "updated",
    }
    assert case.github.prs[0]["body"].endswith(
        "Report: https://drive.google.com/drive/folders/run-folder\n"
    )
    assert case.github.pr_patch_count == 1
    assert result.result["tested_commit"] == _remote_head(case.remote)
    assert result.result["editor_head"] == source_fixture.head
    assert run_git(source_fixture.workspace, ["rev-parse", "HEAD"]).strip() == source_fixture.head
    assert len(source_fixture.broker.calls) == len(case.delivery.calls) == case.github.pr_post_count == 1
    manifest = case.delivery.calls[0][1]
    assert manifest["publication"]["status"] == "PR_UPDATED"
    assert manifest["publication"]["commit"] == result.result["tested_commit"]
    assert manifest["publication"]["pull_request_url"] == case.github.prs[0]["html_url"]
    assert {effect["kind"] for effect in manifest["effects"]} == {"git_ref", "pull_request"}
    source_fixture.ledger.finish(case.request_id, result.status, result=result.result)
    durable = source_fixture.ledger.get(case.request_id)
    assert durable is not None
    assert durable["delivery_status"] == "DELIVERED"
    assert durable["result"]["operation_manifest"] == manifest


def test_failed_tests_draft_always_publishes_honest_report(tmp_path: Path,
                                                          source_fixture: Fixture) -> None:
    case = _route(tmp_path, source_fixture)
    result = case.workflow(case.frozen, case.context)
    assert result.status == "COMPLETED"
    assert result.result["execution"]["outcome"] == "FAILED"
    assert result.result["execution"]["exit_code"] == 1
    assert result.result["publication"]["status"] == "DRAFT_PUBLISHED"
    assert result.result["delivery"]["status"] == "DELIVERED"
    assert "FAILED" in case.github.prs[0]["title"]
    assert "FAILED" in case.github.prs[0]["body"]
    assert _remote_head(case.remote) == result.result["tested_commit"]
    assert case.delivery.calls[0][1]["publication"]["status"] == "PR_UPDATED"
    assert len(source_fixture.broker.calls) == 1


def test_on_pass_failed_tests_skips_git_but_delivers(tmp_path: Path,
                                                      source_fixture: Fixture) -> None:
    case = _route(tmp_path, source_fixture, policy="on_pass")
    result = case.workflow(case.frozen, case.context)
    assert result.status == "COMPLETED"
    assert result.result["publication"]["status"] == "SKIPPED_FAILED_TESTS"
    assert result.result["report_link"] == {"status": "NOT_REQUESTED"}
    assert case.delivery.calls[0][1]["publication"]["reason"] == "FAILED_TESTS"
    assert case.delivery.calls[0][1]["publication"]["status"] == "NOT_REQUESTED"
    assert _remote_head(case.remote) is None
    assert case.github.pr_post_count == 0
    assert len(source_fixture.broker.calls) == 1


def test_cancel_after_push_preserves_git_and_skips_drive(tmp_path: Path,
                                                          source_fixture: Fixture,
                                                          monkeypatch: Any) -> None:
    source_fixture.broker.outcome = "PASSED"
    case = _route(tmp_path, source_fixture)
    from runner_app.workflows import publish_run as module
    original = module.publish_tested_snapshot

    def push_then_cancel(*args: Any, **kwargs: Any) -> Any:
        pushed = original(*args, **kwargs)
        case.context.cancel.set()
        return pushed

    monkeypatch.setattr(module, "publish_tested_snapshot", push_then_cancel)
    result = case.workflow(case.frozen, case.context)
    assert result.status == "CANCELLED"
    assert result.result["publication"]["status"] == "PUSHED"
    assert result.result["publication"]["remote_head"] == result.result["tested_commit"]
    assert _remote_head(case.remote) == result.result["tested_commit"]
    assert result.result["delivery"]["status"] == "NOT_ATTEMPTED"
    assert result.result["operation_manifest"]["status"] == "CANCELLED"
    assert result.result["operation_manifest"]["delivery"]["status"] == "NOT_ATTEMPTED"
    assert result.result["operation_manifest"]["publication"]["status"] == "PUSHED"
    assert result.result["operation_manifest"]["run_ids"] == [case.request_id]
    assert len(case.delivery.calls) == case.github.pr_post_count == 0
    assert len(source_fixture.broker.calls) == 1
    assert run_git(source_fixture.workspace, ["rev-parse", "HEAD"]).strip() == source_fixture.head

    source_fixture.ledger.finish(
        case.request_id, result.status, code=result.code, result=result.result,
    )
    retry = report_command(source_fixture.ledger, source_fixture.head, case.request_id)
    publisher = FakePublisher(source_fixture.ledger)
    report = PublishReportWorkflow(
        source_fixture.ledger, source_fixture.store, publisher,
        bundle_parent=tmp_path / "state" / "retry-bundles",
        receipt_key="receipt", latest_result_key="latest-result",
    )
    retried = report(freeze(retry), Context(Event(), source_fixture.ledger))
    assert retried.status == "COMPLETED" and retried.result["delivery"]["status"] == "DELIVERED"
    assert len(publisher.calls) == 1 and len(source_fixture.broker.calls) == 1
    assert _remote_head(case.remote) == result.result["tested_commit"]


def test_cancel_after_contained_run_retains_report_without_publishing(
    tmp_path: Path, source_fixture: Fixture, monkeypatch: Any,
) -> None:
    source_fixture.broker.outcome = "PASSED"
    case = _route(tmp_path, source_fixture)
    original = case.workflow.runs.execute

    def run_then_cancel(*args: Any, **kwargs: Any) -> Any:
        completion = original(*args, **kwargs)
        case.context.cancel.set()
        return completion

    monkeypatch.setattr(case.workflow.runs, "execute", run_then_cancel)
    result = case.workflow(case.frozen, case.context)
    assert result.status == "CANCELLED"
    assert result.result["execution"]["outcome"] == "PASSED"
    assert result.result["operation_manifest"]["status"] == "CANCELLED"
    assert result.result["operation_manifest"]["delivery"]["status"] == "NOT_ATTEMPTED"
    assert result.result["operation_manifest"]["publication"]["status"] == "NOT_REQUESTED"
    assert result.result["operation_manifest"]["run_ids"] == [case.request_id]
    assert len(source_fixture.broker.calls) == 1
    assert _remote_head(case.remote) is None
    assert not case.delivery.calls

    source_fixture.ledger.finish(
        case.request_id, result.status, code=result.code, result=result.result,
    )
    retry = report_command(source_fixture.ledger, source_fixture.head, case.request_id)
    publisher = FakePublisher(source_fixture.ledger)
    report = PublishReportWorkflow(
        source_fixture.ledger, source_fixture.store, publisher,
        bundle_parent=tmp_path / "state" / "retry-bundles",
        receipt_key="receipt", latest_result_key="latest-result",
    )
    retried = report(freeze(retry), Context(Event(), source_fixture.ledger))
    assert retried.status == "COMPLETED" and retried.result["delivery"]["status"] == "DELIVERED"
    assert len(publisher.calls) == 1 and len(source_fixture.broker.calls) == 1
    saved = json.loads((publisher.calls[0].root / "operation-manifest.json").read_text())
    assert saved == result.result["operation_manifest"]


def test_drive_partial_preserves_successful_execution_and_git(tmp_path: Path,
                                                                source_fixture: Fixture) -> None:
    source_fixture.broker.outcome = "PASSED"
    case = _route(tmp_path, source_fixture, delivery_status="PARTIAL")
    result = case.workflow(case.frozen, case.context)
    assert result.status == "BLOCKED" and result.code == "FAKE_UPLOAD_PENDING"
    assert result.result["execution"]["outcome"] == "PASSED"
    assert result.result["publication"]["status"] == "DRAFT_PUBLISHED"
    assert result.result["delivery"]["status"] == "PARTIAL"
    assert result.result["report_link"] == {"status": "NOT_REQUESTED"}
    assert _remote_head(case.remote) == result.result["tested_commit"]
    assert len(source_fixture.broker.calls) == 1


def test_report_link_timeout_keeps_delivered_report_and_confirmed_draft(
    tmp_path: Path, source_fixture: Fixture,
) -> None:
    source_fixture.broker.outcome = "PASSED"
    case = _route(tmp_path, source_fixture)
    case.github.pr_patch_timeout = True
    case.github.pr_get_timeout_after_patch = True
    result = case.workflow(case.frozen, case.context)
    assert result.status == "BLOCKED" and result.code == "PULL_REQUEST_UPDATE_UNKNOWN"
    assert result.result["execution"]["outcome"] == "PASSED"
    assert result.result["publication"]["status"] == "DRAFT_PUBLISHED"
    assert result.result["delivery"]["status"] == "DELIVERED"
    assert result.result["report_link"]["status"] == "UNKNOWN"
    assert _remote_head(case.remote) == result.result["tested_commit"]
    assert case.github.pr_post_count == case.github.pr_patch_count == 1
    assert len(source_fixture.broker.calls) == len(case.delivery.calls) == 1
    effects = {item["step_id"]: item for item in
               source_fixture.ledger.get(case.request_id)["effects"]}
    assert effects["tested-git-push"]["state"] == "CONFIRMED"
    assert effects["draft-pull-request"]["state"] == "CONFIRMED"
    assert effects["drive-run-folder"]["state"] == "CONFIRMED"
    assert effects["attach-run-report-link"]["state"] == "INTENDED"


def test_frozen_slot_tamper_blocks_before_source_or_run(tmp_path: Path,
                                                        source_fixture: Fixture) -> None:
    case = _route(tmp_path, source_fixture)
    slot_path = tmp_path / "state" / "frozen-inputs" / f"{case.request_id}.frozen.json"
    value = json.loads(slot_path.read_bytes())
    value["changeset"]["revision"] = "forged-revision"
    slot_path.write_text(json.dumps(value))
    result = case.workflow(case.frozen, case.context)
    assert result.status == "BLOCKED" and result.code == "FROZEN_SLOT_INVALID"
    assert len(source_fixture.broker.calls) == len(case.delivery.calls) == 0
    assert _remote_head(case.remote) is None


def test_cancel_after_snapshot_never_starts_test_or_provider_write(
    tmp_path: Path, source_fixture: Fixture, monkeypatch: Any,
) -> None:
    case = _route(tmp_path, source_fixture)
    original = case.workflow.sources.stage_changeset_source

    def stage_then_cancel(*args: Any, **kwargs: Any) -> Any:
        snapshot = original(*args, **kwargs)
        case.context.cancel.set()
        return snapshot

    monkeypatch.setattr(case.workflow.sources, "stage_changeset_source", stage_then_cancel)
    result = case.workflow(case.frozen, case.context)
    assert result.status == "CANCELLED"
    assert result.result["snapshot_id"]
    assert result.result["execution"]["outcome"] == "NOT_RUN"
    assert result.result["publication"]["status"] == "NOT_REQUESTED"
    assert len(source_fixture.broker.calls) == len(case.delivery.calls) == 0
    assert _remote_head(case.remote) is None


def test_editor_change_blocks_git_but_still_delivers_test_report(
    tmp_path: Path, source_fixture: Fixture, monkeypatch: Any,
) -> None:
    source_fixture.broker.outcome = "PASSED"
    case = _route(tmp_path, source_fixture)
    original = case.workflow.publication.publish_run
    editor_note = source_fixture.workspace / "editor-note.txt"

    def edit_then_publish(*args: Any, **kwargs: Any) -> Any:
        editor_note.write_text("user edit stays\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(case.workflow.publication, "publish_run", edit_then_publish)
    result = case.workflow(case.frozen, case.context)
    assert result.status == "BLOCKED" and result.code == "WORKSPACE_CHANGED"
    assert result.result["execution"]["outcome"] == "PASSED"
    assert result.result["publication"]["status"] == "BLOCKED"
    assert result.result["delivery"]["status"] == "DELIVERED"
    assert editor_note.read_text() == "user edit stays\n"
    assert _remote_head(case.remote) is None
    assert case.github.pr_post_count == 0
    assert case.delivery.calls[0][1]["publication"]["reason"] == "WORKSPACE_CHANGED"
    assert len(source_fixture.broker.calls) == 1


def test_trusted_local_policy_exports_sanitized_test_result(
    tmp_path: Path, source_fixture: Fixture,
) -> None:
    case = _route(tmp_path, source_fixture, policy="never",
                  approved_output_ids=("junit",))
    result = case.workflow(case.frozen, case.context)
    assert result.status == "COMPLETED"
    assert result.result["execution"]["outcome"] == "FAILED"
    assert result.result["publication"]["status"] == "SKIPPED_POLICY"
    bundle = case.delivery.calls[0][0]
    exported = [item for item in bundle.items if item.source_output_id == "junit"]
    assert len(exported) == 1
    sanitized = exported[0].path.read_bytes()
    assert sanitized.startswith(b"Sanitized export")
    assert b"<testsuite>" in sanitized
    assert _remote_head(case.remote) is None
    assert len(source_fixture.broker.calls) == 1


def test_report_view_survives_export_policy_failure_after_run(
    tmp_path: Path, source_fixture: Fixture,
) -> None:
    source_fixture.broker.outcome = "PASSED"
    case = _route(tmp_path, source_fixture)

    def broken_policy(_completion: Any) -> Any:
        raise RuntimeError("owner policy unavailable")

    case.workflow.export_policy_for_run = broken_policy
    result = case.workflow(case.frozen, case.context)
    assert result.status == "BLOCKED" and result.code == "EXPORT_POLICY_PROVIDER_FAILED"
    assert result.result["execution"]["outcome"] == "PASSED"
    assert result.result["operation_manifest"]["run_ids"] == [case.request_id]
    assert result.result["operation_manifest"]["publication"]["status"] == "PR_UPDATED"
    assert result.result["delivery"] == {
        "status": "FAILED", "reason": "EXPORT_POLICY_PROVIDER_FAILED",
    }
    assert len(source_fixture.broker.calls) == 1
    assert not case.delivery.calls


def test_drive_provider_uncertainty_preserves_confirmed_git_and_one_run(
    tmp_path: Path, source_fixture: Fixture, monkeypatch: Any,
) -> None:
    source_fixture.broker.outcome = "PASSED"
    case = _route(tmp_path, source_fixture)

    def lost_response(*_args: Any, **_kwargs: Any) -> Any:
        raise DriveError("DRIVE_PROVIDER_UNAVAILABLE", "synthetic lost response")

    monkeypatch.setattr(case.delivery, "publish", lost_response)
    result = case.workflow(case.frozen, case.context)
    assert result.status == "BLOCKED" and result.code == "DELIVERY_OUTCOME_UNKNOWN"
    assert result.result["execution"]["outcome"] == "PASSED"
    assert result.result["publication"]["status"] == "DRAFT_PUBLISHED"
    assert result.result["delivery"] == {
        "status": "PENDING", "reason": "DELIVERY_OUTCOME_UNKNOWN",
    }
    assert _remote_head(case.remote) == result.result["tested_commit"]
    assert len(source_fixture.broker.calls) == 1
    durable = source_fixture.ledger.get(case.request_id)
    assert durable is not None and durable["delivery_status"] == "PENDING"
