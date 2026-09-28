"""Fake-backed canonical FETCH admission, durable freshness, and no-replay recovery."""
from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from runner_app.adapters.github import GitHubRepository
from runner_app.domain.commands import LocalPolicy, Provenance
from runner_app.engine.service import Engine
from runner_app.git.argv import GitError, run_git
from runner_app.git.github_transport import (
    FETCH_REFSPEC,
    read_tracking_refs,
    tracking_refs_digest,
)
from runner_app.git.registry import SourceRegistry
from runner_app.git.worktrees import create_repository
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger
from runner_app.workflows.fetch import FetchWorkflow
from runner_app.workflows.reconcile_local import ManagedGitEffectReader

PROJECT = "fixture"
MARKER = "cdr:v1:" + "a" * 64


def _repository() -> GitHubRepository:
    return GitHubRepository(
        id=42, node_id="R_42", owner="alice", owner_id=7, name=PROJECT,
        full_name="alice/fixture", html_url="https://github.com/alice/fixture",
        clone_url="https://github.com/alice/fixture.git",
        ssh_url="git@github.com:alice/fixture.git", private=True,
        default_branch="main", description=MARKER,
    )


class _GitHub:
    def __init__(self, repository: GitHubRepository) -> None:
        self.repository = repository
        self.calls: list[tuple[str, str, int | None]] = []

    def get_repository(self, owner: str, name: str, *,
                       expected_repo_id: int | None = None) -> GitHubRepository:
        self.calls.append((owner, name, expected_repo_id))
        return self.repository


def _setup(tmp_path: Path) -> tuple[Ledger, SourceRegistry, Path, str]:
    database = Database(tmp_path / "state.sqlite")
    ledger = Ledger(database)
    ledger.startup()
    ledger.arm()
    managed = tmp_path / "managed"
    managed.mkdir(mode=0o700)
    (managed / PROJECT).mkdir(mode=0o700)
    repo = create_repository(managed, PROJECT)
    registry = SourceRegistry(database, managed)
    registry.register_project(PROJECT, "Fixture", repo, origin_kind="new")
    registry.bind_project_remote(
        PROJECT, provider_id=42, node_id="R_42", owner="alice", name=PROJECT,
        html_url="https://github.com/alice/fixture",
        clone_url="https://github.com/alice/fixture.git",
        provision_marker=MARKER,
    )
    return ledger, registry, repo, run_git(repo, ["rev-parse", "refs/heads/main"]).strip()


def _command(ledger: Ledger) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    state = ledger.state()
    return {
        "schema_version": "1.0", "request_id": str(uuid4()),
        "dispatch": "READY", "runner_id": state["runner_id"],
        "epoch": state["epoch"], "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "FETCH", "project_id": PROJECT, "workspace_id": None,
        "expected_head": None, "params": {}, "intent": "Fetch registered GitHub heads",
    }


def _submit(engine: Engine, ledger: Ledger) -> dict[str, object]:
    command = _command(ledger)
    admitted = engine.submit(
        json.dumps(command, separators=(",", ":")).encode(),
        Provenance("local", "local-owner"),
    )
    assert admitted.execute
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        operation = ledger.get(command["request_id"])
        if operation is not None and operation["status"] != "RUNNING":
            return operation
        time.sleep(0.01)
    pytest.fail("FETCH worker did not finish")


def test_fetch_confirms_exact_tracking_scope_and_durable_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, registry, repo, main = _setup(tmp_path)
    github = _GitHub(_repository())
    calls: list[tuple[str, Path]] = []

    class Transport:
        def __init__(self, repository: GitHubRepository, token_provider: object) -> None:
            assert repository == github.repository and callable(token_provider)

        def list_heads(self, path: Path) -> dict[str, str]:
            calls.append(("advertise", path))
            return {"main": main}

        def fetch_heads(self, path: Path) -> dict[str, str]:
            calls.append(("fetch", path))
            run_git(path, ["update-ref", "refs/remotes/origin/main", main])
            return read_tracking_refs(path)

    monkeypatch.setattr("runner_app.workflows.fetch.GitHubGitTransport", Transport)
    engine = Engine(ledger, policy=LocalPolicy(project_ids=frozenset({PROJECT})),
                    handlers={"FETCH": FetchWorkflow(ledger, registry, github, lambda: "fixture-token")})
    operation = _submit(engine, ledger)
    assert operation["status"] == "COMPLETED"
    assert [item["state"] for item in operation["effects"]] == ["CONFIRMED"]
    intent = operation["effects"][0]["intent"]
    assert intent["refspec"] == FETCH_REFSPEC
    assert intent["repository_id"] == 42
    assert intent["advertised_heads"] == {"main": main}
    assert calls == [("advertise", repo), ("fetch", repo)]
    assert github.calls == [("alice", PROJECT, 42)]
    assert run_git(repo, ["rev-parse", "refs/heads/main"]).strip() == main
    assert read_tracking_refs(repo) == {"main": main}
    snapshot = ledger.project_fetch_snapshot(PROJECT)
    assert snapshot["status"] == "OK"
    assert snapshot["last_success_at"] == operation["effects"][0]["updated_at"]
    assert snapshot["observation"]["project_id"] == PROJECT
    assert snapshot["observation"]["repository_id"] == 42
    assert snapshot["observation"]["remote_tracking_ref_count"] == 1


def test_uncertain_fetch_retains_repo_lock_and_never_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, registry, repo, main = _setup(tmp_path)
    calls = 0

    class Transport:
        def __init__(self, repository: GitHubRepository, token_provider: object) -> None:
            pass

        def list_heads(self, path: Path) -> dict[str, str]:
            return {"main": main}

        def fetch_heads(self, path: Path) -> dict[str, str]:
            nonlocal calls
            calls += 1
            run_git(path, ["update-ref", "refs/remotes/origin/main", main])
            raise GitError("GITHUB_GIT_TIMEOUT", "Ambiguous transfer")

    monkeypatch.setattr("runner_app.workflows.fetch.GitHubGitTransport", Transport)
    engine = Engine(ledger, policy=LocalPolicy(project_ids=frozenset({PROJECT})),
                    handlers={"FETCH": FetchWorkflow(ledger, registry, _GitHub(_repository()),
                                                      lambda: "fixture-token")})
    operation = _submit(engine, ledger)
    assert operation["status"] == "BLOCKED"
    assert operation["code"] == "NEEDS_RECONCILIATION"
    assert operation["effects"][0]["state"] == "UNKNOWN"
    assert ledger.holds_lock(operation["request_id"], f"repo:{PROJECT}")
    assert calls == 1
    duplicate = engine.submit(
        json.dumps(operation["command"], separators=(",", ":")).encode(),
        Provenance("local", "local-owner"),
    )
    assert duplicate.duplicate and calls == 1
    another = engine.submit(
        json.dumps(_command(ledger), separators=(",", ":")).encode(),
        Provenance("local", "local-owner"),
    )
    assert another.receipt["code"] == "BUSY" and calls == 1
    snapshot = ledger.project_fetch_snapshot(PROJECT)
    assert snapshot["status"] == "ERROR"
    assert snapshot["last_success_at"] is None
    reader = ManagedGitEffectReader(registry)
    proof = reader(operation, operation["effects"][0])
    assert proof is not None
    assert proof.provider_id == tracking_refs_digest(read_tracking_refs(repo))
    assert proof.observed_intent == operation["effects"][0]["intent"]


@pytest.mark.parametrize("change", [
    {"id": 43},
    {"clone_url": "https://github.com/alice/other.git"},
    {"private": False},
])
def test_changed_private_repository_identity_refuses_before_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, object],
) -> None:
    ledger, registry, repo, _ = _setup(tmp_path)
    github = _GitHub(replace(_repository(), **change))
    monkeypatch.setattr(
        "runner_app.workflows.fetch.GitHubGitTransport",
        lambda *_args: pytest.fail("Git transport must not start"),
    )
    engine = Engine(ledger, policy=LocalPolicy(project_ids=frozenset({PROJECT})),
                    handlers={"FETCH": FetchWorkflow(ledger, registry, github, lambda: "fixture-token")})
    operation = _submit(engine, ledger)
    assert operation["status"] == "BLOCKED"
    assert operation["code"] == "GITHUB_IDENTITY_MISMATCH"
    assert operation["effects"] == []
    assert read_tracking_refs(repo) == {}
    assert not ledger.holds_lock(operation["request_id"], f"repo:{PROJECT}")
