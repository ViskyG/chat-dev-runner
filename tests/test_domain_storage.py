"""EP-01 durable admission and conservative crash recovery on private fixtures."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import jsonschema
import pytest

from runner_app.storage import Database
from runner_app.storage.ledger import Ledger, LedgerError


def make_command(ledger: Ledger, **changes: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    command: dict[str, Any] = {
        "schema_version": "1.0",
        "request_id": str(uuid4()),
        "dispatch": "READY",
        "runner_id": "local-runner",
        "epoch": ledger.state()["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "VERIFY",
        "project_id": "project",
        "workspace_id": "main",
        "expected_head": "a" * 40,
        "params": {"profile_id": "offline"},
        "intent": "Verify this disposable offline fixture",
    }
    command.update(changes)
    return command


def digest(command: dict[str, Any]) -> str:
    canonical = json.dumps(command, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def admit(ledger: Ledger, command: dict[str, Any], scopes: tuple[str, ...] = ("workspace:project/main",),
          **options: Any) -> Any:
    return ledger.admit(command, digest(command), "local-ui", scopes, **options)


def assert_receipt(receipt: dict[str, Any], status: str, code: str | None = None) -> None:
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas/receipt.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        receipt
    )
    assert receipt["status"] == status
    assert receipt["code"] == code


@pytest.fixture
def database(tmp_path: Path) -> Database:
    private = tmp_path / "state"
    private.mkdir(mode=0o700)
    return Database(private / "state.sqlite")


@pytest.fixture
def ledger(database: Database) -> Ledger:
    result = Ledger(database, runner_id="local-runner", max_parallel=2)
    result.startup()
    result.arm()
    return result


def test_startup_and_arming_epochs_are_durable(database: Database) -> None:
    ledger = Ledger(database, runner_id="local-runner")
    first = ledger.startup()
    assert first["runner_id"] == "local-runner"
    assert first["armed"] is False
    UUID(first["epoch"])
    armed = ledger.arm()
    assert armed["armed"] is True
    assert armed["epoch"] != first["epoch"]
    assert ledger.arm() == armed
    # Constructing another read model must not restart the active service.
    reopened = Ledger(database, runner_id="local-runner")
    assert {key: reopened.state()[key] for key in armed} == armed
    restarted = reopened.startup()
    assert restarted["armed"] is False
    assert restarted["epoch"] != armed["epoch"]
    assert {key: ledger.state()[key] for key in restarted} == restarted


def test_concurrent_identical_requests_execute_once(ledger: Ledger, database: Database) -> None:
    command = make_command(ledger)
    copies = [Ledger(database, runner_id="local-runner", max_parallel=2) for _ in range(12)]
    barrier = Barrier(len(copies))

    def submit(instance: Ledger) -> Any:
        barrier.wait(timeout=10)
        return admit(instance, command)

    with ThreadPoolExecutor(max_workers=len(copies)) as pool:
        results = list(pool.map(submit, copies))
    assert sum(result.execute for result in results) == 1
    assert sum(result.duplicate for result in results) == len(results) - 1
    assert all(result.receipt == results[0].receipt for result in results)
    assert_receipt(results[0].receipt, "RUNNING")
    assert len(ledger.list_operations()["items"]) == 1


@pytest.mark.parametrize("changed_source", [False, True])
def test_request_identity_collision_preserves_original(ledger: Ledger, changed_source: bool) -> None:
    command = make_command(ledger)
    original = admit(ledger, command)
    ledger.finish(command["request_id"], "COMPLETED", result={"execution": "PASSED"})
    before = ledger.get(command["request_id"])
    event_count = len(ledger.events())
    replacement = copy.deepcopy(command)
    source = "another-mailbox" if changed_source else "local-ui"
    if not changed_source:
        replacement["intent"] = "Changed content must never replace the accepted command"
    collision = ledger.admit(replacement, digest(replacement), source, ("workspace:project/main",))
    assert collision.execute is False
    assert_receipt(collision.receipt, "REJECTED", "REQUEST_ID_REUSED")
    assert ledger.get(command["request_id"]) == before
    assert original.receipt["request_digest"] == digest(command)
    assert len(ledger.events()) == event_count + 1
    evidence = json.dumps(ledger.events()[-1])
    assert digest(command) in evidence
    assert digest(replacement) in evidence


def test_terminal_duplicate_returns_original_receipt_after_restart(ledger: Ledger,
                                                                 database: Database) -> None:
    command = make_command(ledger)
    assert admit(ledger, command).execute
    terminal = ledger.finish(command["request_id"], "COMPLETED", result={"execution": "FAILED"})
    assert_receipt(terminal, "COMPLETED")
    restarted = Ledger(database, runner_id="local-runner")
    restarted.startup()
    duplicate = admit(restarted, command)
    assert duplicate.execute is False
    assert duplicate.duplicate is True
    assert duplicate.receipt == terminal
    assert restarted.get(command["request_id"])["result"] == {"execution": "FAILED"}


def test_captured_command_preserves_unicode_and_newlines(ledger: Ledger) -> None:
    command = make_command(ledger, intent="Source: e\u0301\r\n\u00e9\n\U0001f4be")
    frozen = copy.deepcopy(command)
    original_digest = digest(command)
    accepted = admit(ledger, command)
    command["intent"] = "mutated by caller"
    command["params"]["profile_id"] = "mutated"
    stored = ledger.get(command["request_id"])
    assert stored["command"] == frozen
    assert stored["digest"] == original_digest
    assert accepted.receipt["request_digest"] == original_digest
    stored["command"]["params"]["profile_id"] = "read-model mutation"
    assert ledger.get(command["request_id"])["command"] == frozen


@pytest.mark.parametrize("occupied, requested", [
    ("workspace:project/main", "workspace:project/main"),
    ("repo:project", "workspace:project/main"),
    ("workspace:project/main", "repo:project"),
    ("repo:project", "repo:project"),
    ("global:provision", "global:provision"),
])
def test_conflicting_scopes_are_terminal_busy(ledger: Ledger, occupied: str, requested: str) -> None:
    first = make_command(ledger)
    waiting = make_command(ledger)
    assert admit(ledger, first, (occupied,)).execute
    rejected = admit(ledger, waiting, (requested,))
    assert_receipt(rejected.receipt, "REJECTED", "BUSY")
    assert rejected.execute is False
    ledger.finish(first["request_id"], "COMPLETED")
    duplicate = admit(ledger, waiting, (requested,))
    assert duplicate.duplicate is True
    assert duplicate.execute is False
    assert duplicate.receipt == rejected.receipt
    assert admit(ledger, make_command(ledger), (requested,)).execute


@pytest.mark.parametrize("first_scope, second_scope", [
    ("workspace:project/one", "workspace:project/two"),
    ("repo:project", "workspace:project-two/main"),
    ("workspace:project/main", "workspace:project/main-two"),
])
def test_independent_scopes_admit_in_parallel(ledger: Ledger, first_scope: str,
                                            second_scope: str) -> None:
    assert admit(ledger, make_command(ledger), (first_scope,)).execute
    assert admit(ledger, make_command(ledger), (second_scope,)).execute


def test_lock_set_is_atomic_and_has_no_partial_reservation(ledger: Ledger) -> None:
    assert admit(ledger, make_command(ledger), ("repo:second",)).execute
    rejected = admit(ledger, make_command(ledger), ("repo:first", "repo:second"))
    assert_receipt(rejected.receipt, "REJECTED", "BUSY")
    assert admit(ledger, make_command(ledger), ("repo:first",)).execute


def test_concurrent_competing_scopes_admit_only_one(database: Database) -> None:
    ledger = Ledger(database, runner_id="local-runner", max_parallel=10)
    ledger.startup()
    ledger.arm()
    commands = [make_command(ledger) for _ in range(10)]
    barrier = Barrier(len(commands))

    def submit(command: dict[str, Any]) -> Any:
        barrier.wait(timeout=10)
        return admit(ledger, command)

    with ThreadPoolExecutor(max_workers=len(commands)) as pool:
        results = list(pool.map(submit, commands))
    assert sum(result.execute for result in results) == 1
    assert sum(result.receipt["code"] == "BUSY" for result in results) == 9


def test_concurrent_global_capacity_is_not_oversubscribed(database: Database) -> None:
    ledger = Ledger(database, runner_id="local-runner", max_parallel=3)
    ledger.startup()
    ledger.arm()
    commands = [make_command(ledger) for _ in range(12)]
    barrier = Barrier(len(commands))

    def submit(command: dict[str, Any]) -> Any:
        barrier.wait(timeout=10)
        return admit(ledger, command, (f"repo:{command['request_id']}",))

    with ThreadPoolExecutor(max_workers=len(commands)) as pool:
        results = list(pool.map(submit, commands))
    assert sum(result.execute for result in results) == 3
    assert sum(result.receipt["code"] == "BUSY" for result in results) == 9
    for command, result in zip(commands, results):
        if result.execute:
            ledger.finish(command["request_id"], "COMPLETED")
    for command, result in zip(commands, results):
        if not result.execute:
            duplicate = admit(ledger, command, (f"repo:{command['request_id']}",))
            assert duplicate.execute is False
            assert duplicate.receipt == result.receipt


def test_cancellation_is_admitted_disarmed_with_every_slot_occupied(ledger: Ledger) -> None:
    first = make_command(ledger)
    assert admit(ledger, first, ("workspace:project/one",)).execute
    assert admit(ledger, make_command(ledger), ("workspace:project/two",)).execute
    ledger.disarm()
    cancel = make_command(ledger, action="CANCEL_OPERATION",
                          params={"target_request_id": first["request_id"]})
    accepted = admit(ledger, cancel, (), exempt_capacity=True)
    assert accepted.execute is True
    assert_receipt(accepted.receipt, "RUNNING")
    ledger.finish(cancel["request_id"], "COMPLETED")
    assert ledger.get(first["request_id"])["status"] == "RUNNING"


@pytest.mark.parametrize("change", [{"epoch": str(uuid4())}, {"runner_id": "other-runner"}])
def test_admission_rechecks_runner_and_epoch_in_transaction(ledger: Ledger,
                                                          change: dict[str, str]) -> None:
    command = make_command(ledger, **change)
    rejected = admit(ledger, command)
    assert rejected.execute is False
    assert_receipt(rejected.receipt, "REJECTED", "WRONG_RUNNER_EPOCH")
    assert ledger.get(command["request_id"])["receipt"] == rejected.receipt


def test_disarm_invalidates_previously_valid_admission(ledger: Ledger) -> None:
    command = make_command(ledger)
    ledger.disarm()
    rejected = admit(ledger, command)
    assert rejected.execute is False
    assert rejected.receipt["status"] == "REJECTED"
    assert rejected.receipt["code"] in {"UNARMED", "WRONG_RUNNER_EPOCH"}
    assert ledger.get(command["request_id"])["receipt"] == rejected.receipt


def test_engine_rejection_takes_priority_and_is_durable(ledger: Ledger) -> None:
    ledger.disarm()
    malformed = {"request_id": str(uuid4()), "params": {"untrusted": True}}
    rejected = admit(ledger, malformed, (), rejection_code="INVALID_SCHEMA")
    assert rejected.execute is False
    assert_receipt(rejected.receipt, "REJECTED", "INVALID_SCHEMA")
    stored = ledger.get(malformed["request_id"])
    assert stored["command"] == malformed
    assert stored["receipt"] == rejected.receipt
    ledger.arm()
    duplicate = admit(ledger, malformed, (), rejection_code="WRONG_RUNNER_EPOCH")
    assert duplicate.duplicate is True
    assert duplicate.execute is False
    assert duplicate.receipt == rejected.receipt


@pytest.mark.parametrize("request_id", ["../state", "", "not-a-uuid", None, 42])
def test_malformed_identity_cannot_enter_database(ledger: Ledger, request_id: Any) -> None:
    command = make_command(ledger, request_id=request_id)
    with pytest.raises(ValueError):
        admit(ledger, command, rejection_code="INVALID_SCHEMA")
    assert ledger.list_operations()["items"] == []


def test_crash_interrupts_running_work_and_retains_uncertain_effects(ledger: Ledger,
                                                                  database: Database) -> None:
    command = make_command(ledger)
    assert admit(ledger, command).execute
    pending = ledger.begin_effect(command["request_id"], "publish", "git_ref",
                                  {"branch": "fixture"})
    confirmed = ledger.begin_effect(command["request_id"], "report", "drive_file",
                                    {"name": "fixture-result"})
    ledger.confirm_effect(confirmed, "provider-result-123")
    restarted = Ledger(database, runner_id="local-runner")
    restarted.startup()
    interrupted = restarted.get(command["request_id"])
    assert_receipt(interrupted["receipt"], "INTERRUPTED", "NEEDS_RECONCILIATION")
    effects = {effect["effect_id"]: effect for effect in interrupted["effects"]}
    assert effects[pending]["state"] == "UNKNOWN"
    assert effects[confirmed]["state"] == "CONFIRMED"
    assert effects[confirmed]["provider_id"] == "provider-result-123"
    with closing(database.connect()) as connection:
        assert connection.execute("SELECT count(*) FROM locks").fetchone()[0] == 1
        assert connection.execute("SELECT capacity_held FROM operations").fetchone()[0] == 1
    with pytest.raises(LedgerError) as error:
        restarted.arm()
    assert error.value.code == "NEEDS_RECONCILIATION"
    with pytest.raises(ValueError):
        restarted.begin_effect(command["request_id"], "publish", "git_ref", {})
    assert admit(restarted, command).execute is False
    with pytest.raises(LedgerError) as error:
        restarted.confirm_stopped(command["request_id"])
    assert error.value.code == "NEEDS_RECONCILIATION"
    restarted.confirm_effect(pending, "provider-publish-123")  # simulated verified remote read
    restarted.confirm_stopped(command["request_id"])
    assert restarted.arm()["armed"] is True
    assert admit(restarted, make_command(restarted)).execute
    reconciled = {effect["effect_id"]: effect for effect in restarted.get(command["request_id"])["effects"]}
    assert reconciled[pending]["state"] == "CONFIRMED"
    assert reconciled[pending]["provider_id"] == "provider-publish-123"
    assert reconciled[confirmed] == effects[confirmed]


def test_explicit_interruption_holds_capacity_until_workers_confirmed_stopped(ledger: Ledger) -> None:
    first = make_command(ledger)
    second = make_command(ledger)
    assert admit(ledger, first, ("repo:one",)).execute
    assert admit(ledger, second, ("repo:two",)).execute
    interrupted = ledger.finish(first["request_id"], "INTERRUPTED", code="PROCESS_STATE_UNKNOWN")
    assert_receipt(interrupted, "INTERRUPTED", "PROCESS_STATE_UNKNOWN")
    assert_receipt(admit(ledger, make_command(ledger), ("repo:three",)).receipt, "REJECTED", "BUSY")
    assert_receipt(admit(ledger, make_command(ledger), ("repo:one",)).receipt, "REJECTED", "BUSY")
    ledger.confirm_stopped(first["request_id"])
    assert admit(ledger, make_command(ledger), ("repo:one",)).execute


@pytest.mark.parametrize("requested_status", ["FAULT", "COMPLETED"])
def test_uncertain_effect_cannot_release_locks_or_report_success(
    database: Database, requested_status: str,
) -> None:
    ledger = Ledger(database, max_parallel=2)
    ledger.startup()
    ledger.arm()
    first = make_command(ledger)
    assert admit(ledger, first, ("repo:project",)).execute
    effect_id = ledger.begin_effect(first["request_id"], "publish", "git_ref",
                                    {"branch": "feature"})

    code = "HANDLER_FAILED" if requested_status == "FAULT" else None
    receipt = ledger.finish(first["request_id"], requested_status, code=code)
    assert_receipt(receipt, "FAULT", "NEEDS_RECONCILIATION")
    retained = ledger.get(first["request_id"])
    assert retained["effects"][0]["state"] == "UNKNOWN"
    assert retained["reconcile_required"] == 1
    assert retained["capacity_held"] == 1
    with closing(database.connect()) as connection:
        assert connection.execute(
            "SELECT scope FROM locks WHERE request_id=?", (first["request_id"],)
        ).fetchall() == [("repo:project",)]

    same_resource = admit(ledger, make_command(ledger), ("repo:project",))
    assert_receipt(same_resource.receipt, "REJECTED", "BUSY")
    unrelated = make_command(ledger)
    assert admit(ledger, unrelated, ("repo:unrelated",)).execute
    full_capacity = admit(ledger, make_command(ledger), ("repo:third",))
    assert_receipt(full_capacity.receipt, "REJECTED", "BUSY")
    ledger.finish(unrelated["request_id"], "COMPLETED")

    restarted = Ledger(database, max_parallel=2)
    restarted.startup()
    persisted = restarted.get(first["request_id"])
    assert persisted["effects"][0]["effect_id"] == effect_id
    assert persisted["effects"][0]["state"] == "UNKNOWN"
    assert persisted["reconcile_required"] == 1
    with pytest.raises(LedgerError) as error:
        restarted.arm()
    assert error.value.code == "NEEDS_RECONCILIATION"


@pytest.mark.parametrize("cleanup_code", [
    "CONTAINER_CLEANUP_FAILED", "CONTAINER_CLEANUP_UNCERTAIN",
])
def test_uncertain_container_cleanup_holds_lock_until_trusted_reconciliation(
    database: Database, cleanup_code: str,
) -> None:
    ledger = Ledger(database, max_parallel=1)
    ledger.startup()
    ledger.arm()
    command = make_command(ledger)
    assert admit(ledger, command, ("workspace:project/area",)).execute
    receipt = ledger.finish(command["request_id"], "FAULT", code=cleanup_code)
    assert_receipt(receipt, "FAULT", cleanup_code)
    retained = ledger.get(command["request_id"])
    assert retained["reconcile_required"] == 1
    assert retained["capacity_held"] == 1
    with closing(database.connect()) as connection:
        assert connection.execute(
            "SELECT scope FROM locks WHERE request_id=?", (command["request_id"],)
        ).fetchall() == [("workspace:project/area",)]
    ledger.disarm()
    with pytest.raises(LedgerError) as blocked:
        ledger.arm()
    assert blocked.value.code == "NEEDS_RECONCILIATION"

    # Only a trusted recovery path may call this after proving the container is gone.
    ledger.confirm_stopped(command["request_id"])
    assert ledger.get(command["request_id"])["reconcile_required"] == 0
    with closing(database.connect()) as connection:
        assert connection.execute(
            "SELECT 1 FROM locks WHERE request_id=?", (command["request_id"],)
        ).fetchone() is None
    assert ledger.arm()["armed"] is True


def test_effect_confirmation_is_idempotent_but_provider_identity_is_immutable(ledger: Ledger) -> None:
    command = make_command(ledger)
    assert admit(ledger, command).execute
    effect = ledger.begin_effect(command["request_id"], "create", "github_repository",
                                 {"name": "disposable-fixture"})
    ledger.confirm_effect(effect, "provider-123")
    before = ledger.get(command["request_id"])["effects"]
    ledger.confirm_effect(effect, "provider-123")
    assert ledger.get(command["request_id"])["effects"] == before
    with pytest.raises(ValueError):
        ledger.confirm_effect(effect, "provider-456")
    assert ledger.get(command["request_id"])["effects"] == before


def test_effect_intent_is_frozen_and_duplicate_intent_has_one_identity(ledger: Ledger) -> None:
    command = make_command(ledger)
    assert admit(ledger, command).execute
    intent = {"name": "disposable-fixture"}
    effect = ledger.begin_effect(command["request_id"], "create", "github_repository", intent)
    event_count = len(ledger.events())
    assert ledger.begin_effect(command["request_id"], "create", "github_repository", intent) == effect
    assert len(ledger.events()) == event_count
    with pytest.raises(ValueError):
        ledger.begin_effect(command["request_id"], "create", "github_repository", {"name": "other"})
    assert len(ledger.get(command["request_id"])["effects"]) == 1
    assert ledger.get(command["request_id"])["effects"][0]["intent"] == intent


def test_rejection_reserves_neither_locks_nor_execution_slots(ledger: Ledger) -> None:
    assert_receipt(admit(ledger, make_command(ledger), rejection_code="EXPIRED").receipt,
                   "REJECTED", "EXPIRED")
    assert admit(ledger, make_command(ledger)).execute
    assert admit(ledger, make_command(ledger), ("workspace:project/second",)).execute


def test_regular_operations_cannot_claim_cancellation_capacity_exemption(ledger: Ledger) -> None:
    command = make_command(ledger)
    with pytest.raises(ValueError):
        admit(ledger, command, (), exempt_capacity=True)
    assert ledger.list_operations()["items"] == []


def test_stop_confirmation_cannot_release_a_running_operation(ledger: Ledger) -> None:
    command = make_command(ledger)
    assert admit(ledger, command).execute
    with pytest.raises(ValueError):
        ledger.confirm_stopped(command["request_id"])
    assert_receipt(admit(ledger, make_command(ledger)).receipt, "REJECTED", "BUSY")


def test_terminal_truth_cannot_be_rewritten(ledger: Ledger) -> None:
    command = make_command(ledger)
    assert admit(ledger, command).execute
    with pytest.raises(ValueError):
        ledger.finish(command["request_id"], "RUNNING")
    assert ledger.get(command["request_id"])["status"] == "RUNNING"
    terminal = ledger.finish(command["request_id"], "FAULT", code="FIXTURE_FAILURE")
    with pytest.raises(ValueError):
        ledger.finish(command["request_id"], "COMPLETED")
    assert ledger.get(command["request_id"])["receipt"] == terminal


def test_database_guards_immutable_command_and_append_only_journal(ledger: Ledger,
                                                                 database: Database) -> None:
    command = make_command(ledger)
    assert admit(ledger, command).execute
    ledger.journal(command["request_id"], "fixture-step", "STARTED", {"attempt": 1})
    events = ledger.events()
    stored = ledger.get(command["request_id"])
    with closing(database.connect()) as connection:
        for statement, parameters in [
            ("UPDATE operations SET command_json='{}' WHERE request_id=?", (command["request_id"],)),
            ("UPDATE operations SET digest=? WHERE request_id=?", ("b" * 64, command["request_id"])),
            ("UPDATE operations SET source_id='forged' WHERE request_id=?", (command["request_id"],)),
            ("UPDATE journal SET detail_json='{}'", ()),
            ("DELETE FROM journal", ()),
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, parameters)
    assert ledger.events() == events
    assert ledger.get(command["request_id"]) == stored


def test_operation_and_event_pagination_do_not_repeat_or_skip(ledger: Ledger) -> None:
    request_ids = []
    for number in range(7):
        command = make_command(ledger)
        request_ids.append(command["request_id"])
        assert admit(ledger, command).execute
        ledger.journal(command["request_id"], "fixture", "OBSERVED", {"number": number})
        ledger.finish(command["request_id"], "COMPLETED")
    observed = []
    cursor = None
    for _ in range(5):
        page = ledger.list_operations(limit=2, cursor=cursor)
        assert len(page["items"]) <= 2
        observed.extend(item["request_id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert len(observed) == len(set(observed)) == len(request_ids)
    assert set(observed) == set(request_ids)
    all_events = ledger.events(limit=100)
    page = ledger.events(limit=2)
    following = ledger.events(after=page[-1]["event_id"], limit=100)
    assert page + following == all_events
