"""Receipt delivery boundaries using real Git trees and the production HTTP adapter."""
from __future__ import annotations

import json

import pytest
from test_github_mailbox_v2 import Fixture as GitFixture
from test_intake_v2 import accept
from test_v2_task_integration import Fixture as TaskFixture

from runner_app.adapters.github_mailbox_v2 import GitHubResultTransport
from runner_app.git.argv import run_git, run_git_bytes
from runner_app.git.results_v2 import COMMIT_AUTHOR, ResultPushRejected, _write_object
from runner_app.git.state_v2 import publish_state


def document():
    return {"schema_version": 1, "runner_id": "node", "project_id": "project",
            "request_id": "request", "request_digest": "same-command", "status": "FAILED",
            "code": "SOURCE_BRANCH_MISSING", "updated_at": "2026-10-20T00:00:00+00:00"}


def send(fixture, value=None):
    intents = []
    publication = publish_state(
        fixture.repo, GitHubResultTransport(fixture.client, fetch_objects=fixture.fetch),
        branch="tested", state=value or document(), record_intent=intents.append)
    return publication, intents


def test_state_updates_preserve_source_and_sealed_result_bytes(tmp_path):
    fixture = GitFixture(tmp_path)
    delivered, _ = fixture.publish()
    assert delivered.status == "DELIVERED"
    before = run_git_bytes(fixture.remote, ["ls-tree", "-r", "tested:.runner/results"])
    first, _ = send(fixture)
    assert first.status == "DELIVERED"
    newer = {**document(), "status": "COMPLETED", "updated_at": "2026-10-21T00:00:00+00:00"}
    second, _ = send(fixture, newer)
    assert second.status == "DELIVERED"
    assert json.loads(run_git_bytes(fixture.remote, ["show", "tested:.runner/state/node/request.json"])) == newer
    assert run_git_bytes(fixture.remote, ["ls-tree", "-r", "tested:.runner/results"]) == before
    assert run_git_bytes(fixture.remote, ["show", "tested:app.txt"]) == b"tested source\n"
    assert all(body["force"] is False for method, _, body in fixture.api.calls if method == "PATCH")
    assert send(fixture, document())[0].code == "STATE_SUPERSEDED"


def test_state_rebuilds_after_definite_source_race(tmp_path):
    fixture = GitFixture(tmp_path)
    fixture.api.before_patch = lambda: fixture.api.advance({"app.txt": b"concurrent edit\n"})
    publication, intents = send(fixture)
    assert publication.status == "DELIVERED" and len(intents) == 2
    assert run_git_bytes(fixture.remote, ["show", "tested:app.txt"]) == b"concurrent edit\n"


@pytest.mark.parametrize("unavailable", [False, True])
def test_lost_state_response_is_reconciled_without_repeated_push(tmp_path, unavailable):
    fixture = GitFixture(tmp_path)
    fixture.api.lose_response = True
    fixture.api.unreadable_after_patch = unavailable
    publication, intents = send(fixture)
    assert publication.status == ("UNKNOWN" if unavailable else "DELIVERED")
    assert len(intents) == len([c for c in fixture.api.calls if c[0] == "PATCH"]) == 1


def test_state_prefix_does_not_authorize_source_changes(tmp_path):
    fixture = GitFixture(tmp_path)
    tree = fixture.api.tree([
        {"path": "app.txt", "mode": "100644", "type": "blob",
         "sha": _write_object(fixture.remote, "blob", b"unauthorized source")},
    ], base=run_git(fixture.repo, ["rev-parse", "tested^{tree}"]).strip())
    # Install the tree object locally, as a trusted publisher normally would.
    fixture.fetch(fixture.repo, "tested", fixture.initial)
    run_git(fixture.repo, ["fetch", str(fixture.remote), tree])
    identity = f"{COMMIT_AUTHOR} 1700000000 +0000"
    candidate = _write_object(fixture.repo, "commit", (
        f"tree {tree}\nparent {fixture.initial}\nauthor {identity}\ncommitter {identity}\n\n"
        "Publish runner state: node/request\n").encode())
    with pytest.raises(ResultPushRejected, match="RESULT_OBJECT_UPLOAD_FAILED"):
        GitHubResultTransport(fixture.client, fetch_objects=fixture.fetch).push_commit(
            fixture.repo, candidate, "tested")
    assert fixture.api.head() == fixture.initial


def task_fixture(tmp_path):
    fixture = TaskFixture(tmp_path)
    run_git(fixture.receiver, ["update-ref", "refs/heads/commands", fixture.initial])
    fixture.worker.terminal_observer = fixture.runner.publish_state
    fixture.coordinator.terminal_observer = fixture.runner.publish_state
    return fixture


def receipt(fixture, request_id):
    return json.loads(run_git_bytes(fixture.receiver, [
        "show", f"commands:.runner/state/runner-1/{request_id}.json"]))


@pytest.mark.parametrize("failure", ["source", "task", "manifest"])
def test_pre_execution_errors_are_returned_on_command_branch(tmp_path, failure):
    fixture = task_fixture(tmp_path)
    if failure == "source":
        fixture.port.value["request"]["source"]["branch"] = "absent-source"
    elif failure == "task":
        fixture.port.value["request"]["task"] = "absent-task"
    else:
        (fixture.source / ".runner/project.json").write_text("invalid JSON")
        from test_v2_task_integration import commit
        commit(fixture.source, "Invalid manifest fixture")
        run_git(fixture.source, ["push", "receiver", "HEAD:feature/test"])
    operation = fixture.run()
    remote = receipt(fixture, operation["request_id"])
    assert remote["status"] == operation["status"] == "FAILED"
    assert remote["code"] == operation["code"]
    assert remote["code"] and remote["execution"]["outcome"] == "NOT_RUN"
    assert fixture.store.claim_next(fixture.session) is None


def test_completed_state_has_final_delivery_and_status_returns_it(tmp_path):
    fixture = task_fixture(tmp_path)
    operation = fixture.run()
    remote = receipt(fixture, operation["request_id"])
    assert remote["delivery"]["status"] == "DELIVERED"
    assert remote["delivery"]["commit"] == operation["result"]["delivery"]["commit"]
    assert remote["execution"]["outcome"] == "PASSED"
    assert not any("stdout:FORMAT_NOT_REGISTERED" in w or "stderr:FORMAT_NOT_REGISTERED" in w
                   for w in operation["result"]["manifest"]["warnings"])
    fixture.port.ready(request={"request_id": None, "action": "STATUS",
                                "target_request_id": operation["request_id"]})
    control = fixture.coordinator.poll_once("slot", fixture.port)
    reply = receipt(fixture, control.request_id)
    assert reply["status"] == "COMPLETED"
    assert reply["target"]["request_id"] == operation["request_id"]
    assert reply["target"]["delivery"]["status"] == "DELIVERED"


def test_status_during_run_does_not_occupy_sealed_result_directory(tmp_path):
    fixture = task_fixture(tmp_path)
    target = accept(fixture.coordinator, fixture.port)
    fixture.store.claim_next(fixture.session)
    fixture.store.mark_running(target, {"fixture": "process pending"})
    fixture.port.ready(request={"request_id": None, "action": "STATUS", "target_request_id": target})
    control = fixture.coordinator.poll_once("slot", fixture.port)
    assert receipt(fixture, control.request_id)["target"]["status"] == "RUNNING"
    assert not (fixture.results / "runner-1" / target).exists()


def test_cancelled_queued_request_is_visible_without_worker_execution(tmp_path):
    fixture = task_fixture(tmp_path)
    target = accept(fixture.coordinator, fixture.port)
    fixture.port.ready(request={"request_id": None, "action": "CANCEL", "target_request_id": target})
    fixture.coordinator.poll_once("slot", fixture.port)
    assert receipt(fixture, target)["status"] == "CANCELLED"
    assert receipt(fixture, target)["execution"]["outcome"] == "NOT_RUN"
    assert not fixture.worker.run_once()
