"""EP-05 publication to a disposable real bare Git remote."""
from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from runner_app.domain.commands import freeze
from runner_app.git.argv import GitError, run_git
from runner_app.git.publish import (
    PublicationError,
    bootstrap_new_project_main,
    publish_tested_snapshot,
)
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.git.snapshots import stage_changeset
from runner_app.git.worktrees import create_repository, create_workspace
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger


def _time() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Case:
    registry: SourceRegistry
    repo: Path
    workspace: Path
    other_workspace: Path
    remote: Path
    run_id: str
    base: str
    commit: str
    tree: str


def _case(tmp_path: Path, *, outcome: str = "PASSED",
          source_policy: str = "draft_always",
          origin_kind: str = "new") -> Case:
    managed = tmp_path / "managed"
    repo = create_repository(managed, "fixture")
    base = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()
    workspace = create_workspace(managed, "fixture", repo, "area", "feature/area", base)
    other = create_workspace(managed, "fixture", repo, "other", "feature/other", base)
    remote = tmp_path / "receiver.git"
    run_git(tmp_path, ["init", "--bare", "-b", "main", str(remote)])
    run_git(repo, ["remote", "add", "publish", str(remote)])
    run_git(repo, ["push", "publish", f"{base}:refs/heads/main"])

    database = Database(tmp_path / "state" / "state.sqlite")
    registry = SourceRegistry(database, managed)
    registry.register_project(
        "fixture", "Fixture", repo, origin_kind=origin_kind,
        origin_reference=({"github": "alice/fixture", "local": "f" * 48}
                          .get(origin_kind)),
    )
    registry.register_workspace("fixture", "area", "Area", workspace,
                                branch="feature/area", head=base)
    registry.register_workspace("fixture", "other", "Other", other,
                                branch="feature/other", head=base)
    change = {
        "schema_version": "1.0", "change_id": str(uuid4()),
        "project_id": "fixture", "workspace_id": "area", "base_head": base,
        "files": [{"op": "upsert", "path": "src/main.py", "expected_sha256": None,
                   "content_utf8": "value = 42\n"}],
    }
    snapshot = stage_changeset(repo, workspace, managed / "fixture" / "snapshots", change,
                               project_id="fixture", workspace_id="area", expected_head=base,
                               source_generation=0)
    registry.register_snapshot_record(snapshot)

    ledger = Ledger(database)
    ledger.startup()
    state = ledger.arm()
    request_id = str(uuid4())
    command = {
        "schema_version": "1.0", "request_id": request_id, "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": _time(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "action": "DELIVER_VERIFY_PUBLISH", "project_id": "fixture", "workspace_id": "area",
        "expected_head": base,
        "params": {"changeset_doc_id": "test-doc", "changeset_revision": "1",
                   "profile_id": "python-quick", "publication": source_policy},
        "intent": "Test a disposable publication snapshot",
    }
    assert ledger.admit(command, freeze(command).digest, "local-owner",
                        ("repo:fixture",)).execute
    effect = ledger.begin_effect(request_id, "source-snapshot", "git_ref",
                                 {"snapshot_id": snapshot.snapshot_id,
                                  "expected_head": snapshot.base_head})
    ledger.confirm_effect(effect, snapshot.commit)
    run_id = str(uuid4())
    manifest = {
        "schema_version": "1.0", "run_id": run_id, "request_id": request_id,
        "project_id": "fixture", "workspace_id": "area",
        "snapshot": {"commit": snapshot.commit, "tree": snapshot.tree,
                     "source_dirty_at_capture": False},
        "operation_status": "COMPLETED",
        "execution": {"outcome": outcome, "exit_code": 0 if outcome == "PASSED" else 1,
                      "duration_seconds": 0.01},
        "delivery": {"status": "NOT_ATTEMPTED", "reason": None},
        "outputs": [], "created_at": _time(),
    }
    with closing(database.connect()) as connection:
        connection.execute(
            "INSERT INTO runs(run_id,request_id,project_id,workspace_id,manifest_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (run_id, request_id, "fixture", "area", json.dumps(manifest), _time()),
        )
        connection.execute(
            "INSERT INTO run_executions(run_id,snapshot_id,profile_id,profile_revision,"
            "image_digest,backend,argv_sha256,started_at,ended_at,containment_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, snapshot.snapshot_id, "python-quick", "a" * 64,
             "docker.io/library/python@sha256:" + "b" * 64, "rootless_docker",
             "c" * 64, _time(), _time(),
             json.dumps({"verified": True, "output_collection_complete": True}), _time()),
        )
    registry.bind_tested_run(run_id, snapshot.snapshot_id)
    return Case(registry, repo, workspace, other, remote, run_id, base,
                snapshot.commit, snapshot.tree)


def _publish(case: Case, *, expected_remote_head: str | None = None):
    return publish_tested_snapshot(
        case.registry, run_id=case.run_id, project_id="fixture", workspace_id="area",
        branch="feature/area", remote_name="publish", remote_url=case.remote,
        default_branch="main", expected_remote_head=expected_remote_head,
    )


def test_publish_exact_tested_commit_with_parallel_edits_elsewhere(tmp_path: Path) -> None:
    case = _case(tmp_path)
    (case.other_workspace / "human.txt").write_text("unrelated user edit\n")
    published = _publish(case)
    assert published.status == "PUBLISHED"
    assert published.commit == case.commit
    assert published.tree == case.tree
    assert published.remote_ref == "refs/heads/feature/area"
    assert run_git(case.remote, ["rev-parse", "refs/heads/feature/area"]).strip() == case.commit
    assert run_git(case.remote, ["rev-parse", "refs/heads/main"]).strip() == case.base
    assert run_git(case.remote, ["rev-parse", f"{case.commit}^{{tree}}"] ).strip() == case.tree
    assert run_git(case.workspace, ["rev-parse", "HEAD"]).strip() == case.base
    assert (case.other_workspace / "human.txt").read_text() == "unrelated user edit\n"
    (case.workspace / "after-push.txt").write_text("new user edit\n")
    assert _publish(case).status == "ALREADY_PUBLISHED"
    assert (case.workspace / "after-push.txt").read_text() == "new user edit\n"


def test_target_editor_edit_blocks_push_without_overwrite(tmp_path: Path) -> None:
    case = _case(tmp_path)
    (case.workspace / "human.txt").write_text("keep me\n")
    with pytest.raises(PublicationError, match="WORKSPACE_CHANGED"):
        _publish(case)
    assert (case.workspace / "human.txt").read_text() == "keep me\n"
    assert not run_git(case.repo, ["ls-remote", "--refs", "--heads", "publish",
                                   "refs/heads/feature/area"])


def test_default_branch_and_remote_conflict_are_refused(tmp_path: Path) -> None:
    case = _case(tmp_path)
    with pytest.raises(PublicationError, match="PROTECTED_BRANCH"):
        publish_tested_snapshot(
            case.registry, run_id=case.run_id, project_id="fixture", workspace_id="area",
            branch="main", remote_name="publish", remote_url=case.remote,
            default_branch="main", expected_remote_head=case.base,
        )
    run_git(case.repo, ["push", "publish", f"{case.base}:refs/heads/feature/area"])
    with pytest.raises(PublicationError, match="REMOTE_CHANGED") as error:
        _publish(case)
    assert error.value.remote_head == case.base
    assert run_git(case.remote, ["rev-parse", "refs/heads/feature/area"]).strip() == case.base


def test_response_lost_after_real_push_reconciles_exact_remote_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path)
    original = run_git

    def lost_response(cwd: Path, args: list[str], timeout: float = 30) -> str:
        if args and args[0] == "push":
            original(cwd, args, timeout=timeout)
            raise GitError("GIT_TIMEOUT", "Synthetic lost acknowledgement")
        return original(cwd, args, timeout=timeout)

    monkeypatch.setattr("runner_app.git.publish.run_git", lost_response)
    result = _publish(case)
    assert result.status == "PUBLISHED_RECONCILED"
    assert result.remote_head == case.commit
    assert _publish(case).status == "ALREADY_PUBLISHED"


def test_known_divergent_branch_never_force_pushes(tmp_path: Path) -> None:
    case = _case(tmp_path)
    sibling = run_git(case.repo, [
        "-c", "user.name=Test User", "-c", "user.email=test@local.invalid",
        "commit-tree", case.tree, "-p", case.base, "-m", "Independent branch edit",
    ]).strip()
    run_git(case.repo, ["push", "publish", f"{sibling}:refs/heads/feature/area"])
    with pytest.raises(PublicationError, match="REMOTE_DIVERGED") as error:
        _publish(case, expected_remote_head=sibling)
    assert error.value.remote_head == sibling
    assert run_git(case.remote, ["rev-parse", "refs/heads/feature/area"]).strip() == sibling


def test_unattested_run_cannot_publish(tmp_path: Path) -> None:
    case = _case(tmp_path)
    with pytest.raises(SourceError) as error:
        publish_tested_snapshot(
            case.registry, run_id=str(uuid4()), project_id="fixture", workspace_id="area",
            branch="feature/area", remote_name="publish", remote_url=case.remote,
            default_branch="main", expected_remote_head=None,
        )
    assert error.value.code == "RUN_NOT_TESTED"
    assert not run_git(case.repo, ["ls-remote", "--refs", "--heads", "publish",
                                   "refs/heads/feature/area"])


@dataclass(frozen=True)
class BootstrapCase:
    registry: SourceRegistry
    repo: Path
    remote: Path
    initial: str


def _bootstrap_case(tmp_path: Path, *, origin_kind: str = "new") -> BootstrapCase:
    managed = tmp_path / "managed"
    repo = create_repository(managed, "fresh")
    initial = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()
    remote = tmp_path / "empty-receiver.git"
    run_git(tmp_path, ["init", "--bare", "-b", "main", str(remote)])
    run_git(repo, ["remote", "add", "bootstrap", str(remote)])
    registry = SourceRegistry(Database(tmp_path / "state" / "state.sqlite"), managed)
    registry.register_project("fresh", "Fresh", repo, origin_kind=origin_kind)
    return BootstrapCase(registry, repo, remote, initial)


def _bootstrap(case: BootstrapCase):
    return bootstrap_new_project_main(
        case.registry, project_id="fresh", remote_name="bootstrap",
        remote_url=case.remote, expected_initial_commit=case.initial,
    )


def test_new_project_bootstrap_installs_exact_initial_main_once(tmp_path: Path) -> None:
    case = _bootstrap_case(tmp_path)
    result = _bootstrap(case)
    assert result.status == "BOOTSTRAPPED"
    assert result.commit == case.initial
    assert result.remote_ref == "refs/heads/main"
    assert run_git(case.remote, ["rev-parse", "refs/heads/main"]).strip() == case.initial
    later_tree = run_git(case.repo, ["rev-parse", f"{case.initial}^{{tree}}"] ).strip()
    later = run_git(case.repo, [
        "-c", "user.name=Owner", "-c", "user.email=owner@local.invalid",
        "commit-tree", later_tree, "-p", case.initial, "-m", "Later local commit",
    ]).strip()
    run_git(case.repo, ["update-ref", "refs/heads/main", later, case.initial])
    assert _bootstrap(case).status == "ALREADY_BOOTSTRAPPED"


def test_new_project_bootstrap_refuses_existing_refs_and_divergent_main(tmp_path: Path) -> None:
    existing = _bootstrap_case(tmp_path / "other-ref")
    run_git(existing.repo, ["push", "bootstrap",
                            f"{existing.initial}:refs/heads/existing"])
    with pytest.raises(PublicationError, match="REMOTE_NOT_EMPTY"):
        _bootstrap(existing)
    assert not run_git(existing.repo, ["ls-remote", "--refs", "--heads", "bootstrap",
                                       "refs/heads/main"])

    changed = _bootstrap_case(tmp_path / "divergent-main")
    empty_tree = run_git(changed.repo, ["rev-parse", f"{changed.initial}^{{tree}}"] ).strip()
    other_initial = run_git(changed.repo, [
        "-c", "user.name=Other", "-c", "user.email=other@local.invalid",
        "commit-tree", empty_tree, "-m", "Other project",
    ]).strip()
    run_git(changed.repo, ["push", "bootstrap",
                           f"{other_initial}:refs/heads/main"])
    with pytest.raises(PublicationError, match="REMOTE_CHANGED") as error:
        _bootstrap(changed)
    assert error.value.remote_head == other_initial
    assert run_git(changed.remote, ["rev-parse", "refs/heads/main"]).strip() == other_initial


def test_new_project_bootstrap_reconciles_lost_push_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _bootstrap_case(tmp_path)
    original = run_git

    def lost_response(cwd: Path, args: list[str], timeout: float = 30) -> str:
        if args and args[0] == "push":
            original(cwd, args, timeout=timeout)
            raise GitError("GIT_TIMEOUT", "Synthetic lost acknowledgement")
        return original(cwd, args, timeout=timeout)

    monkeypatch.setattr("runner_app.git.publish.run_git", lost_response)
    assert _bootstrap(case).status == "BOOTSTRAPPED_RECONCILED"
    assert _bootstrap(case).status == "ALREADY_BOOTSTRAPPED"


def test_new_project_bootstrap_refuses_imported_source(tmp_path: Path) -> None:
    case = _bootstrap_case(tmp_path, origin_kind="local")
    with pytest.raises(PublicationError, match="WRONG_SOURCE_KIND"):
        _bootstrap(case)
    assert not run_git(case.repo, ["ls-remote", "--refs", "--heads", "bootstrap",
                                   "refs/heads/main"])


def test_new_project_bootstrap_requires_exact_local_main_before_first_push(tmp_path: Path) -> None:
    case = _bootstrap_case(tmp_path)
    initial_tree = run_git(case.repo, ["rev-parse", f"{case.initial}^{{tree}}"] ).strip()
    later = run_git(case.repo, [
        "-c", "user.name=Owner", "-c", "user.email=owner@local.invalid",
        "commit-tree", initial_tree, "-p", case.initial, "-m", "Moved before bootstrap",
    ]).strip()
    run_git(case.repo, ["update-ref", "refs/heads/main", later, case.initial])
    with pytest.raises(PublicationError, match="SOURCE_CHANGED"):
        _bootstrap(case)
    assert not run_git(case.repo, ["ls-remote", "--refs", "--heads", "bootstrap",
                                   "refs/heads/main"])
