"""EP-03 phase attempts, cleanup proofs and legacy migration are durable offline."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import runner_app.storage.database as database_module
from runner_app.executors.broker import ContainerIntent, ContainerReconciliation
from runner_app.executors.reconciliation import reconcile_run_attempt
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger, LedgerError

_IMAGE = "example.invalid/fixture@sha256:" + "a" * 64
_PLAN = "1" * 64
_TREE = "2" * 64
_EVIDENCE = "3" * 64
_PREP_REVISION = "4" * 64
_TEST_REVISION = "5" * 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fixtures(connection: sqlite3.Connection, snapshot_id: str) -> None:
    now = _now()
    connection.execute(
        "INSERT INTO projects(project_id,display_name,state,created_at,updated_at) "
        "VALUES('phase-project','Phase project','ACTIVE',?,?)", (now, now),
    )
    connection.execute(
        "INSERT INTO workspaces(project_id,workspace_id,state,created_at,updated_at) "
        "VALUES('phase-project','main','ACTIVE',?,?)", (now, now),
    )
    connection.execute(
        "INSERT INTO source_projects(project_id,repo_path,origin_kind,created_at) "
        "VALUES('phase-project','/tmp/phase-project','new',?)", (now,),
    )
    connection.execute(
        "INSERT INTO source_workspaces(project_id,workspace_id,worktree_path,head,generation,updated_at) "
        "VALUES('phase-project','main','/tmp/phase-project/main',?,0,?)",
        ("a" * 40, now),
    )
    connection.execute(
        "INSERT INTO source_snapshots(snapshot_id,project_id,workspace_id,base_head,"
        "snapshot_commit,tree_digest,source_generation,capture_fingerprint,provenance,"
        "dirty_at_capture,included_paths_json,manifest_path,manifest_sha256,created_at) "
        "VALUES(?,'phase-project','main',?,?,?,0,?,'clean_head',0,'[]',?,?,?)",
        (snapshot_id, "a" * 40, "b" * 40, "c" * 40, "d" * 64,
         "/tmp/phase-snapshot.json", "e" * 64, now),
    )


def _command(ledger: Ledger, request_id: str) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "schema_version": "1.0", "request_id": request_id, "dispatch": "READY",
        "runner_id": "local-runner", "epoch": ledger.state()["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "VERIFY", "project_id": "phase-project", "workspace_id": "main",
        "expected_head": "a" * 40, "params": {"profile_id": "test"},
        "intent": "Offline phase test",
    }


def _admit(ledger: Ledger, request_id: str) -> None:
    command = _command(ledger, request_id)
    encoded = json.dumps(command, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode()
    assert ledger.admit(command, hashlib.sha256(encoded).hexdigest(), "local-ui",
                        ("workspace:phase-project/main",)).execute


def _intent(run_id: str, phase: str, **extra: str) -> dict[str, str]:
    return {
        "run_id": run_id, "daemon_id": "rootless-fixture-daemon",
        "source_path": "/tmp/phase-source",
        "profile_id": "prep" if phase == "PREPARATION" else "test",
        "profile_revision_sha256": (_PREP_REVISION if phase == "PREPARATION"
                                    else _TEST_REVISION),
        "image": _IMAGE, "image_id": "sha256:" + "6" * 64,
        "argv_sha256": "7" * 64,
        "nonce": "8" * 32 if phase == "PREPARATION" else "9" * 32,
        **extra,
    }


def _proof(run_id: str, container_id: str = "a" * 64) -> ContainerReconciliation:
    return ContainerReconciliation(run_id, "rootless-fixture-daemon", "REMOVED", container_id)


def _ledger(tmp_path: Path) -> tuple[Ledger, Database, str, str, str]:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    database = Database(parent / "state.sqlite")
    ledger = Ledger(database)
    ledger.startup()
    ledger.arm()
    request_id, snapshot_id, prep_id = str(uuid4()), str(uuid4()), str(uuid4())
    connection = database.connect()
    try:
        _fixtures(connection, snapshot_id)
    finally:
        connection.close()
    _admit(ledger, request_id)
    effect = ledger.begin_effect(request_id, "source-snapshot", "git_ref", {
        "snapshot_id": snapshot_id, "project_id": "phase-project",
        "workspace_id": "main", "kind": "clean_head", "expected_head": "a" * 40,
    })
    ledger.confirm_effect(effect, "b" * 40)
    return ledger, database, request_id, snapshot_id, prep_id


def _prepared(ledger: Ledger, request_id: str, snapshot_id: str,
              prep_id: str, *, outcome: str = "PASSED") -> None:
    ledger.reserve_phase_attempt(request_id, "PREPARATION", prep_id, snapshot_id)
    ledger.record_container_intent(request_id, _intent(prep_id, "PREPARATION"),
                                   phase="PREPARATION")
    ledger.record_container_cleaned(request_id, "PREPARATION", _proof(prep_id))
    ledger.record_preparation_result(
        request_id, run_id=prep_id, snapshot_id=snapshot_id,
        plan_identity_sha256=_PLAN,
        preparation_profile_revision_sha256=_PREP_REVISION,
        image=_IMAGE, outcome=outcome, exit_code=0 if outcome == "PASSED" else 1,
        evidence_sha256=_EVIDENCE, capture_complete=True, cleanup_verified=True,
    )


def _handoff(ledger: Ledger, request_id: str, snapshot_id: str,
             prep_id: str, volume: Path) -> None:
    ledger.record_preparation_handoff(
        request_id, run_id=prep_id, snapshot_id=snapshot_id, test_run_id=request_id,
        plan_identity_sha256=_PLAN,
        preparation_profile_revision_sha256=_PREP_REVISION,
        image=_IMAGE, build_tree_sha256=_TREE, evidence_sha256=_EVIDENCE,
        test_volume_path=str(volume),
    )


def _test_intent(request_id: str, prep_id: str, volume: Path) -> dict[str, str]:
    return _intent(request_id, "TEST", preparation_run_id=prep_id,
                   plan_identity_sha256=_PLAN, prepared_build_tree_sha256=_TREE,
                   test_volume_path=str(volume))


def test_phase_attempts_are_distinct_and_never_replayed(tmp_path: Path) -> None:
    ledger, database, request_id, snapshot_id, prep_id = _ledger(tmp_path)
    ledger.reserve_phase_attempt(request_id, "PREPARATION", prep_id, snapshot_id)
    ledger.reserve_phase_attempt(request_id, "TEST", request_id, snapshot_id)
    with pytest.raises(LedgerError, match="immutable attempt") as duplicate:
        ledger.reserve_phase_attempt(request_id, "PREPARATION", str(uuid4()), snapshot_id)
    assert duplicate.value.code == "RUN_ALREADY_ATTEMPTED"
    with pytest.raises(LedgerError) as reused:
        ledger.reserve_phase_attempt(request_id, "TEST", prep_id, snapshot_id)
    assert reused.value.code == "RUN_ALREADY_ATTEMPTED"
    with pytest.raises(LedgerError) as no_handoff:
        ledger.record_container_intent(request_id, _intent(request_id, "TEST"))
    assert no_handoff.value.code == "PREPARATION_HANDOFF_REQUIRED"
    connection = database.connect()
    try:
        assert connection.execute(
            "SELECT phase,run_id FROM run_attempts WHERE request_id=? ORDER BY phase",
            (request_id,),
        ).fetchall() == [("PREPARATION", prep_id), ("TEST", request_id)]
    finally:
        connection.close()


def test_failed_preparation_is_reviewable_but_cannot_launch_test(tmp_path: Path) -> None:
    ledger, database, request_id, snapshot_id, prep_id = _ledger(tmp_path)
    assert ledger.get_preparation_result(request_id) is None
    _prepared(ledger, request_id, snapshot_id, prep_id, outcome="FAILED")
    assert ledger.get_preparation_result(request_id) == {
        "run_id": prep_id, "outcome": "FAILED", "evidence_sha256": _EVIDENCE,
    }
    ledger.reserve_phase_attempt(request_id, "TEST", request_id, snapshot_id)
    volume = tmp_path / "volumes" / request_id
    with pytest.raises(LedgerError) as failed_handoff:
        _handoff(ledger, request_id, snapshot_id, prep_id, volume)
    assert failed_handoff.value.code == "PREPARATION_HANDOFF_NOT_ADMITTED"
    with pytest.raises(LedgerError) as no_test:
        ledger.record_container_intent(
            request_id, _test_intent(request_id, prep_id, volume))
    assert no_test.value.code == "PREPARATION_HANDOFF_REQUIRED"
    connection = database.connect()
    try:
        assert connection.execute(
            "SELECT outcome,evidence_sha256 FROM preparation_results WHERE request_id=?",
            (request_id,),
        ).fetchone() == ("FAILED", _EVIDENCE)
    finally:
        connection.close()
    ledger.finish(request_id, "BLOCKED", code="PREPARATION_FAILED")
    assert ledger.get(request_id)["reconcile_required"] == 0


def test_two_cleaned_containers_complete_and_hide_private_identity(tmp_path: Path) -> None:
    ledger, database, request_id, snapshot_id, prep_id = _ledger(tmp_path)
    volume = tmp_path / "volumes" / request_id
    _prepared(ledger, request_id, snapshot_id, prep_id)
    _handoff(ledger, request_id, snapshot_id, prep_id, volume)
    ledger.reserve_phase_attempt(request_id, "TEST", request_id, snapshot_id)
    with pytest.raises(LedgerError) as wrong_tree:
        ledger.record_container_intent(request_id, {
            **_test_intent(request_id, prep_id, volume),
            "prepared_build_tree_sha256": "0" * 64,
        })
    assert wrong_tree.value.code == "PREPARATION_HANDOFF_REQUIRED"
    ledger.record_container_intent(request_id, _test_intent(request_id, prep_id, volume))
    ledger.record_container_cleaned(request_id, "TEST", _proof(request_id, "b" * 64))
    ledger.finish(request_id, "COMPLETED")
    assert ledger.get(request_id)["reconcile_required"] == 0
    visible = [event["detail"] for event in ledger.events()
               if event["type"] == "CONTAINER_INTENDED"]
    assert len(visible) == 2
    assert all("nonce" not in event and "test_volume_path" not in event
               and "source_path" not in event for event in visible)
    connection = database.connect()
    try:
        raw = connection.execute(
            "SELECT detail_json FROM journal WHERE request_id=? "
            "AND event_type='CONTAINER_INTENDED' ORDER BY event_id DESC LIMIT 1",
            (request_id,),
        ).fetchone()[0]
        assert json.loads(raw)["test_volume_path"] == str(volume)
    finally:
        connection.close()


def test_recovery_skips_clean_prep_and_requires_exact_test_removal(tmp_path: Path) -> None:
    ledger, _, request_id, snapshot_id, prep_id = _ledger(tmp_path)
    volume = tmp_path / "volumes" / request_id
    _prepared(ledger, request_id, snapshot_id, prep_id)
    _handoff(ledger, request_id, snapshot_id, prep_id, volume)
    ledger.reserve_phase_attempt(request_id, "TEST", request_id, snapshot_id)
    ledger.record_container_intent(request_id, _test_intent(request_id, prep_id, volume))
    finished = ledger.finish(request_id, "FAULT", code="OTHER_HANDLER_ERROR")
    assert finished["code"] == "OTHER_HANDLER_ERROR"
    assert ledger.get(request_id)["reconcile_required"] == 1
    with pytest.raises(LedgerError) as unproved:
        ledger.confirm_stopped(request_id)
    assert unproved.value.code == "CONTAINER_PROOF_REQUIRED"
    with pytest.raises(LedgerError) as forged:
        ledger.journal(request_id, "fake-proof", "CONTAINER_CLEANED", {
            "phase": "TEST", "run_id": request_id, "state": "REMOVED",
        })
    assert forged.value.code == "RESERVED_EVENT_TYPE"
    calls: list[str] = []

    def remove_test(intent: ContainerIntent) -> ContainerReconciliation:
        run_id = intent.run_id
        calls.append(run_id)
        assert run_id == request_id
        return _proof(run_id, "b" * 64)

    proof = reconcile_run_attempt(
        ledger, SimpleNamespace(reconcile_container=remove_test), request_id,
    )
    assert proof.run_id == request_id
    assert calls == [request_id]
    assert ledger.get(request_id)["reconcile_required"] == 0


def test_uncertain_preparation_recovers_with_exact_source_mount(tmp_path: Path) -> None:
    ledger, _, request_id, snapshot_id, prep_id = _ledger(tmp_path)
    ledger.reserve_phase_attempt(request_id, "PREPARATION", prep_id, snapshot_id)
    ledger.record_container_intent(
        request_id, _intent(prep_id, "PREPARATION"), phase="PREPARATION",
    )
    ledger.finish(request_id, "FAULT", code="CONTAINER_CLEANUP_UNCERTAIN")
    seen: list[str | None] = []

    def remove_preparation(intent: ContainerIntent) -> ContainerReconciliation:
        seen.append(intent.source_path)
        assert intent.run_id == prep_id
        return _proof(intent.run_id)

    reconcile_run_attempt(
        ledger, SimpleNamespace(reconcile_container=remove_preparation), request_id,
    )
    assert seen == ["/tmp/phase-source"]
    assert ledger.get(request_id)["reconcile_required"] == 0


def test_unconfirmed_same_workspace_snapshot_cannot_reserve_phase(tmp_path: Path) -> None:
    ledger, _, first_id, snapshot_id, _ = _ledger(tmp_path)
    ledger.finish(first_id, "COMPLETED")
    another_id = str(uuid4())
    _admit(ledger, another_id)
    with pytest.raises(LedgerError) as missing_source:
        ledger.reserve_phase_attempt(another_id, "PREPARATION", str(uuid4()), snapshot_id)
    assert missing_source.value.code == "PHASE_ATTEMPT_NOT_ADMITTED"


def test_schema_six_attempt_migrates_to_test_and_failed_upgrade_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    database = Database(parent / "state.sqlite")
    with monkeypatch.context() as context:
        context.setattr(database_module, "SCHEMA_VERSION", 6)
        ledger = Ledger(database)
        ledger.startup()
        ledger.arm()
        request_id, snapshot_id = str(uuid4()), str(uuid4())
        connection = database.connect()
        try:
            _fixtures(connection, snapshot_id)
        finally:
            connection.close()
        _admit(ledger, request_id)
        connection = database.connect()
        try:
            connection.execute(
                "INSERT INTO run_attempts(request_id,run_id,snapshot_id,created_at) "
                "VALUES(?,?,?,?)", (request_id, request_id, snapshot_id, _now()),
            )
        finally:
            connection.close()
    with monkeypatch.context() as context:
        context.setitem(database_module._MIGRATIONS, 7,
                        database_module._MIGRATIONS[7] + ("INVALID MIGRATION SQL",))
        with pytest.raises(sqlite3.OperationalError):
            database.migrate()
    connection = database.connect()
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)
        assert "phase" not in [column[1] for column in
                               connection.execute("PRAGMA table_info(run_attempts)")]
        assert connection.execute(
            "SELECT run_id,snapshot_id FROM run_attempts WHERE request_id=?",
            (request_id,),
        ).fetchone() == (request_id, snapshot_id)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='preparation_handoffs'",
        ).fetchone() is None
    finally:
        connection.close()
    assert database.migrate() == 7
    connection = database.connect()
    try:
        assert connection.execute(
            "SELECT phase,run_id,snapshot_id FROM run_attempts WHERE request_id=?",
            (request_id,),
        ).fetchone() == ("TEST", request_id, snapshot_id)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE run_attempts SET phase='PREPARATION' WHERE request_id=?",
                (request_id,),
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
