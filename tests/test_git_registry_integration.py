"""EP-02 real Git snapshots stay distinct from tested-run publication authority."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from runner_app.domain.commands import freeze
from runner_app.git.argv import run_git
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.git.snapshots import snapshot_is_stale, stage_changeset
from runner_app.git.worktrees import create_repository, create_workspace
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger


def _time() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_manifest(run_id: str, request_id: str, *, commit: str, tree: str,
                  outcome: str = "PASSED") -> dict:
    return {
        "schema_version": "1.0", "run_id": run_id, "request_id": request_id,
        "project_id": "fixture", "workspace_id": "area",
        "snapshot": {"commit": commit, "tree": tree, "source_dirty_at_capture": False},
        "operation_status": "COMPLETED",
        "execution": {"outcome": outcome,
                      "exit_code": 0 if outcome == "PASSED" else 1 if outcome == "FAILED" else None,
                      "duration_seconds": 0.01},
        "delivery": {"status": "NOT_ATTEMPTED", "reason": None},
        "outputs": [], "created_at": _time(),
    }


def test_real_snapshot_requires_bound_tested_run_and_retained_ref(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    repo = create_repository(managed, "fixture")
    head = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()
    workspace = create_workspace(managed, "fixture", repo, "area", "feature/area", head)
    database = Database(tmp_path / "state" / "state.sqlite")
    registry = SourceRegistry(database, managed)
    registry.register_project("fixture", "Fixture", repo, origin_kind="new")
    registry.register_workspace("fixture", "area", "Area", workspace,
                                branch="feature/area", head=head)
    change = {
        "schema_version": "1.0", "change_id": str(uuid4()),
        "project_id": "fixture", "workspace_id": "area", "base_head": head,
        "files": [{"op": "upsert", "path": "src/main.py", "expected_sha256": None,
                   "content_utf8": "value = 42\n"}],
    }
    snapshot = stage_changeset(repo, workspace, managed / "fixture" / "snapshots", change,
                               project_id="fixture", workspace_id="area", expected_head=head,
                               source_generation=0)
    recorded = registry.register_snapshot_record(snapshot)
    assert recorded["snapshot_commit"] == snapshot.commit
    assert run_git(repo, ["rev-parse", f"refs/cdr/snapshots/{snapshot.snapshot_id}"]).strip() == snapshot.commit
    assert run_git(repo, ["rev-parse", f"{snapshot.commit}^{{tree}}"] ).strip() == snapshot.tree
    assert run_git(workspace, ["rev-parse", "HEAD"]).strip() == head
    assert not (workspace / "src/main.py").exists()  # Editable checkout was never promoted.

    ledger = Ledger(database)
    ledger.startup()
    state = ledger.arm()
    request_id = str(uuid4())
    command = {
        "schema_version": "1.0", "request_id": request_id, "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": _time(), "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "action": "VERIFY", "project_id": "fixture", "workspace_id": "area",
        "expected_head": head, "params": {"profile_id": "python-quick"},
        "intent": "Check a temporary snapshot fixture",
    }
    assert ledger.admit(command, freeze(command).digest, "local-owner",
                        ("workspace:fixture/area",)).execute
    effect = ledger.begin_effect(request_id, "source-snapshot", "git_ref",
                                 {"snapshot_id": snapshot.snapshot_id,
                                  "expected_head": snapshot.base_head})
    ledger.confirm_effect(effect, snapshot.commit)
    before_run = str(uuid4())
    with pytest.raises(SourceError) as error:
        registry.publication_target(before_run, project_id="fixture", workspace_id="area")
    assert error.value.code == "RUN_NOT_TESTED"

    def store_run(outcome: str, *, tree: str | None = None) -> str:
        run_id = str(uuid4())
        manifest = _run_manifest(run_id, request_id, commit=snapshot.commit,
                                 tree=tree or snapshot.tree, outcome=outcome)
        with closing(database.connect()) as connection:
            connection.execute(
                "INSERT INTO runs(run_id,request_id,project_id,workspace_id,manifest_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, request_id, "fixture", "area", json.dumps(manifest), _time()),
            )
        return run_id

    not_run = store_run("NOT_RUN")
    with pytest.raises(SourceError) as error:
        registry.bind_tested_run(not_run, snapshot.snapshot_id)
    assert error.value.code == "RUN_NOT_TESTED"
    mismatched = store_run("PASSED", tree="f" * len(snapshot.tree))
    with pytest.raises(SourceError) as error:
        registry.bind_tested_run(mismatched, snapshot.snapshot_id)
    assert error.value.code == "RUN_NOT_TESTED"
    def attest(run_id: str) -> None:
        with closing(database.connect()) as connection:
            connection.execute(
                "INSERT INTO run_executions(run_id,snapshot_id,profile_id,profile_revision,"
                "image_digest,backend,argv_sha256,started_at,ended_at,containment_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, snapshot.snapshot_id, "python-quick", "a" * 64,
                 "docker.io/library/python@sha256:" + "b" * 64, "rootless_docker",
                 "c" * 64, _time(), _time(), json.dumps({"verified": True, "output_collection_complete": True}), _time()),
            )

    passed = store_run("PASSED")
    with pytest.raises(SourceError) as error:
        registry.bind_tested_run(passed, snapshot.snapshot_id)
    assert error.value.code == "RUN_NOT_TESTED"  # A manifest alone is not execution proof.
    attest(passed)
    assert registry.bind_tested_run(passed, snapshot.snapshot_id)["snapshot_id"] == snapshot.snapshot_id
    assert registry.bind_tested_run(passed, snapshot.snapshot_id)["snapshot_id"] == snapshot.snapshot_id
    target = registry.publication_target(passed, project_id="fixture", workspace_id="area")
    assert target == {"snapshot_id": snapshot.snapshot_id, "commit": snapshot.commit,
                      "tree": snapshot.tree}
    failed = store_run("FAILED")  # Honest project findings can still select a draft target.
    attest(failed)
    registry.bind_tested_run(failed, snapshot.snapshot_id)
    assert registry.publication_target(failed, project_id="fixture", workspace_id="area") == target
    with pytest.raises(SourceError) as error:
        registry.publication_target(passed, project_id="fixture", workspace_id="other")
    assert error.value.code == "SNAPSHOT_SCOPE_MISMATCH"

    (workspace / "new.txt").write_bytes(b"human edit\n")
    assert snapshot_is_stale(snapshot, workspace, current_generation=0)
    assert registry.publication_target(passed, project_id="fixture", workspace_id="area") == target
    with closing(database.connect()) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE runs SET manifest_json='{}' WHERE run_id=?", (passed,))
    run_git(repo, ["update-ref", f"refs/cdr/snapshots/{snapshot.snapshot_id}", head, snapshot.commit])
    with pytest.raises(SourceError) as error:
        registry.publication_target(passed, project_id="fixture", workspace_id="area")
    assert error.value.code == "SNAPSHOT_REF_CHANGED"
