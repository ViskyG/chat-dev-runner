"""Real SQLite and threads; deterministic fake handlers never execute project code."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from queue import Queue
from threading import Event

import pytest

from runner_app.domain.commands import DomainError, LocalPolicy, Provenance
from runner_app.engine.service import Engine, OperationResult
from runner_app.engine.singleton import AlreadyRunning, Singleton
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger, LedgerError

LOCAL = Provenance(kind="local", source_id="local-owner")


def make_command(ledger: Ledger, *, workspace: str = "a", action: str = "VERIFY") -> dict:
    now = datetime.now(timezone.utc)
    return {"schema_version": "1.0", "request_id": str(uuid.uuid4()), "dispatch": "READY",
            "runner_id": "local-runner", "epoch": ledger.state()["epoch"],
            "issued_at": now.isoformat(), "expires_at": (now + timedelta(minutes=5)).isoformat(),
            "action": action, "project_id": "project", "workspace_id": workspace,
            "expected_head": "a" * 40, "params": {"profile_id": "python-quick"},
            "intent": "Run the explicitly requested finite check once."}


def submit(engine: Engine, command: dict):
    return engine.submit(json.dumps(command).encode(), LOCAL)


def wait_terminal(ledger: Ledger, request_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = ledger.get(request_id)
        if value["status"] != "RUNNING":
            return value
        time.sleep(.005)
    pytest.fail("Operation never became terminal")


@pytest.fixture
def running(tmp_path):
    ledger = Ledger(Database(tmp_path / "state.sqlite"), max_parallel=1)
    ledger.startup()
    ledger.arm()
    entered, release = Event(), Event()
    calls = []

    def handler(frozen, context):
        calls.append(frozen.digest)
        entered.set()
        while not release.wait(.01):
            if context.cancel.is_set():
                return OperationResult("CANCELLED")
        return OperationResult(result={"execution": {"outcome": "FAILED"},
                                       "delivery": {"status": "NOT_ATTEMPTED"}})

    policy = LocalPolicy(project_ids=frozenset({"project"}),
                         workspace_pairs=frozenset({("project", "a"), ("project", "b")}),
                         approved_profiles=frozenset({"python-quick"}))
    engine = Engine(ledger, policy=policy, handlers={"VERIFY": handler})
    yield engine, entered, release, calls
    release.set()
    engine.shutdown()


def test_concurrent_duplicates_execute_exactly_once(running):
    engine, entered, release, calls = running
    command = make_command(engine.ledger)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: submit(engine, command), range(20)))
    assert entered.wait(1)
    assert sum(result.execute for result in results) == 1
    assert len(calls) == 1
    release.set()
    terminal = wait_terminal(engine.ledger, command["request_id"])
    assert terminal["status"] == "COMPLETED"
    assert terminal["result"]["execution"]["outcome"] == "FAILED"
    assert not submit(engine, command).execute


def test_busy_is_durable_and_never_launches_when_capacity_frees(running):
    engine, entered, release, calls = running
    one, busy = make_command(engine.ledger), make_command(engine.ledger, workspace="b")
    assert submit(engine, one).execute
    assert entered.wait(1)
    result = submit(engine, busy)
    assert result.receipt["status"] == "REJECTED"
    assert result.receipt["code"] == "BUSY"
    release.set()
    wait_terminal(engine.ledger, one["request_id"])
    assert submit(engine, busy).duplicate
    assert len(calls) == 1


def test_cancel_reaches_worker_with_all_slots_busy_and_while_disarmed(running):
    engine, entered, _, _ = running
    command = make_command(engine.ledger)
    submit(engine, command)
    assert entered.wait(1)
    engine.ledger.disarm()
    cancel = make_command(engine.ledger, action="CANCEL_OPERATION")
    cancel["params"] = {"target_request_id": command["request_id"]}
    result = submit(engine, cancel)
    assert result.execute
    assert result.receipt["status"] == "COMPLETED"
    assert wait_terminal(engine.ledger, command["request_id"])["status"] == "CANCELLED"


def test_cancel_cannot_cross_workspace_authority(running):
    engine, entered, _, _ = running
    command = make_command(engine.ledger)
    submit(engine, command)
    assert entered.wait(1)
    cancel = make_command(engine.ledger, workspace="b", action="CANCEL_OPERATION")
    cancel["params"] = {"target_request_id": command["request_id"]}
    result = submit(engine, cancel)
    assert not result.execute
    assert result.receipt["code"] == "CANCEL_SCOPE_MISMATCH"
    assert engine.ledger.get(command["request_id"])["status"] == "RUNNING"


def test_schema_refusal_with_safe_id_is_persisted_and_original_not_overwritten(running):
    engine, _, _, calls = running
    malformed = make_command(engine.ledger)
    malformed["shell"] = "arbitrary remote command"
    result = submit(engine, malformed)
    assert result.receipt["code"] == "INVALID_SCHEMA"
    assert engine.ledger.get(malformed["request_id"])["status"] == "REJECTED"
    del malformed["shell"]
    changed = submit(engine, malformed)
    assert changed.receipt["code"] == "REQUEST_ID_REUSED"
    assert not calls


def test_unavailable_handler_refuses_without_fake_success(running):
    engine, _, _, _ = running
    command = make_command(engine.ledger)
    command["action"] = "EXPORT_CONTEXT"
    command["params"] = {"paths": [], "include_diff": False}
    result = submit(engine, command)
    assert not result.execute
    assert result.receipt["code"] == "ACTION_UNAVAILABLE"


@pytest.fixture
def scoped_running(tmp_path):
    ledger = Ledger(Database(tmp_path / "state.sqlite"), max_parallel=3)
    ledger.startup()
    ledger.arm()
    started: Queue[str] = Queue()
    release = Event()

    def handler(frozen, _context):
        started.put(frozen.command["request_id"])
        release.wait(5)
        return OperationResult()

    policy = LocalPolicy(project_ids=frozenset({"project"}),
                         workspace_pairs=frozenset({("project", "a"), ("project", "b")}),
                         approved_profiles=frozenset({"python-quick"}),
                         approved_templates=frozenset({"python-basic"}),
                         github_identities=frozenset({"github-owner"}))
    actions = ("VERIFY", "PUBLISH", "SYNC_WORKSPACE", "DELIVER_VERIFY_PUBLISH", "CREATE_PROJECT")
    engine = Engine(ledger, policy=policy, handlers={action: handler for action in actions})
    yield engine, started, release
    release.set()
    engine.shutdown()


def test_verify_operations_in_distinct_workspaces_run_in_parallel(scoped_running):
    engine, started, _release = scoped_running
    first = make_command(engine.ledger, workspace="a")
    second = make_command(engine.ledger, workspace="b")
    assert submit(engine, first).execute
    assert started.get(timeout=2) == first["request_id"]
    assert submit(engine, second).execute
    assert started.get(timeout=2) == second["request_id"]


@pytest.mark.parametrize("contender_action", ["PUBLISH", "SYNC_WORKSPACE", "DELIVER_VERIFY_PUBLISH"])
def test_repository_mutation_conflicts_across_workspaces(scoped_running, contender_action):
    engine, started, release = scoped_running
    first = make_command(engine.ledger, workspace="a", action="PUBLISH")
    first["params"] = {"source_run_id": str(uuid.uuid4()), "publication": "draft_always"}
    assert submit(engine, first).execute
    assert started.get(timeout=2) == first["request_id"]

    contender = make_command(engine.ledger, workspace="b", action=contender_action)
    if contender_action == "PUBLISH":
        contender["params"] = {"source_run_id": str(uuid.uuid4()), "publication": "draft_always"}
    elif contender_action == "SYNC_WORKSPACE":
        contender["params"] = {"mode": "remote_ff", "remote_ref": "feature-b",
                               "target_commit": "b" * 40}
    else:
        contender["params"] = {"changeset_doc_id": "doc-b", "changeset_revision": "1",
                               "profile_id": "python-quick", "publication": "draft_always"}
    rejected = submit(engine, contender)
    assert rejected.receipt["status"] == "REJECTED"
    assert rejected.receipt["code"] == "BUSY"
    assert started.empty()
    release.set()
    wait_terminal(engine.ledger, first["request_id"])
    duplicate = submit(engine, contender)
    assert duplicate.duplicate
    assert duplicate.receipt == rejected.receipt


def test_project_creation_locks_shared_github_identity(scoped_running):
    engine, started, _release = scoped_running

    def create(project_id):
        command = make_command(engine.ledger, action="CREATE_PROJECT")
        command.update(project_id=project_id, workspace_id=None, expected_head=None,
                       params={"name": project_id, "source": {"kind": "new"},
                               "template_id": "python-basic", "github_identity": "github-owner",
                               "visibility": "private"})
        return command

    first, contender = create("project-one"), create("project-two")
    assert submit(engine, first).execute
    assert started.get(timeout=2) == first["request_id"]
    with closing(engine.ledger.database.connect()) as connection:
        scopes = [row[0] for row in connection.execute(
            "SELECT scope FROM locks WHERE request_id=?", (first["request_id"],)
        )]
    assert any(scope.startswith("global:") for scope in scopes)
    rejected = submit(engine, contender)
    assert rejected.receipt["status"] == "REJECTED"
    assert rejected.receipt["code"] == "BUSY"
    assert started.empty()


def test_expiry_crossing_dispatch_handoff_is_rejected(tmp_path):
    ledger = Ledger(Database(tmp_path / "state.sqlite"))
    ledger.startup()
    ledger.arm()
    validated = Event()
    calls = []

    class SignalingPolicy(LocalPolicy):
        def authorize(self, command, provenance):
            super().authorize(command, provenance)
            validated.set()

    def handler(frozen, _context):
        calls.append(frozen.digest)
        return OperationResult()

    policy = SignalingPolicy(project_ids=frozenset({"project"}),
                             workspace_pairs=frozenset({("project", "a")}),
                             approved_profiles=frozenset({"python-quick"}))
    engine = Engine(ledger, policy=policy, handlers={"VERIFY": handler})
    command = make_command(ledger)
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(seconds=1)
    command["issued_at"] = (now - timedelta(seconds=1)).isoformat()
    command["expires_at"] = expiry.isoformat()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with engine._handoff:
                future = pool.submit(submit, engine, command)
                assert validated.wait(2)
                assert datetime.now(timezone.utc) < expiry
                remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
                time.sleep(max(0, remaining) + .05)
            admission = future.result(timeout=3)
        assert admission.receipt["status"] == "REJECTED"
        assert admission.receipt["code"] == "EXPIRED"
        assert not admission.execute
        assert not calls
    finally:
        engine.shutdown()


def test_singleton_nonblocking_and_releasable(tmp_path):
    path = tmp_path / "runner.lock"
    first = Singleton(path)
    try:
        with pytest.raises(AlreadyRunning):
            Singleton(path)
    finally:
        first.close()
    second = Singleton(path)
    second.close()


def test_delivery_progress_is_durable_and_cannot_revert_after_confirmed(running):
    engine, entered, release, _ = running
    command = make_command(engine.ledger)
    assert submit(engine, command).execute
    assert entered.wait(1)
    request_id = command["request_id"]
    engine.ledger.record_delivery(request_id, "PENDING")
    engine.ledger.record_delivery(request_id, "PARTIAL", "UPLOAD_WRITE_UNCERTAIN")
    assert engine.ledger.get(request_id)["delivery_status"] == "PARTIAL"
    engine.ledger.record_delivery(request_id, "DELIVERED")
    with pytest.raises(LedgerError) as error:
        engine.ledger.record_delivery(request_id, "PENDING")
    assert error.value.code == "INVALID_TRANSITION"
    release.set()
    assert wait_terminal(engine.ledger, request_id)["delivery_status"] == "DELIVERED"


@pytest.mark.parametrize("commit_before_error", [False, True])
def test_ambiguous_terminal_database_write_stops_new_admissions(
    running, monkeypatch: pytest.MonkeyPatch, commit_before_error: bool,
) -> None:
    engine, entered, release, calls = running
    command = make_command(engine.ledger)
    assert submit(engine, command).execute
    assert entered.wait(1)
    original_finish = engine.ledger.finish

    def uncertain_finish(*args, **kwargs):
        if commit_before_error:
            original_finish(*args, **kwargs)
        raise sqlite3.OperationalError("synthetic ambiguous SQLite commit")

    monkeypatch.setattr(engine.ledger, "finish", uncertain_finish)
    release.set()
    deadline = time.monotonic() + 5
    while not engine.storage_faulted and time.monotonic() < deadline:
        time.sleep(.005)
    assert engine.storage_faulted
    while engine.ledger.state()["armed"] and time.monotonic() < deadline:
        time.sleep(.005)
    assert not engine.ledger.state()["armed"]
    assert len(calls) == 1
    other = make_command(engine.ledger, workspace="b")
    with pytest.raises(DomainError) as error:
        submit(engine, other)
    assert error.value.code == "STORAGE_UNAVAILABLE"
    assert engine.ledger.get(other["request_id"]) is None
    operation = engine.ledger.get(command["request_id"])
    assert operation is not None
    if commit_before_error:
        assert operation["status"] == "COMPLETED"
    else:
        assert operation["status"] == "RUNNING"
        assert operation["receipt"]["status"] == "RUNNING"
        engine.ledger.startup()
        assert engine.ledger.get(command["request_id"])["status"] == "INTERRUPTED"


@pytest.mark.parametrize("commit_before_error", [False, True])
def test_ambiguous_admission_database_write_never_dispatches(
    running, monkeypatch: pytest.MonkeyPatch, commit_before_error: bool,
) -> None:
    engine, entered, _, calls = running
    command = make_command(engine.ledger)
    original_admit = engine.ledger.admit

    def uncertain_admit(*args, **kwargs):
        if commit_before_error:
            original_admit(*args, **kwargs)
        raise sqlite3.OperationalError("synthetic ambiguous admission commit")

    monkeypatch.setattr(engine.ledger, "admit", uncertain_admit)
    with pytest.raises(DomainError) as error:
        submit(engine, command)
    assert error.value.code == "STORAGE_UNAVAILABLE"
    assert engine.storage_faulted
    assert not engine.ledger.state()["armed"]
    assert not entered.is_set() and calls == []
    operation = engine.ledger.get(command["request_id"])
    assert (operation is not None) is commit_before_error
    if operation is not None:
        assert operation["status"] == "RUNNING"
        engine.ledger.startup()
        assert engine.ledger.get(command["request_id"])["status"] == "INTERRUPTED"
