"""Owner-local retarget continuation at the exact pre-Drive-intent crash cut."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from test_project_mailbox_receipt import _attached
from test_retarget_route import (
    COMMAND_ID,
    CONTEXT_ID,
    PROJECT,
    STATE_ID,
    WORKSPACE,
    _command,
    _setup,
    _terminal,
    _text,
)

from runner_app.adapters.drive_writer import DocsCASWriter
from runner_app.domain.commands import Provenance
from runner_app.engine.service import Engine
from runner_app.git.registry import SourceRegistry
from runner_app.git.worktrees import inspect_workspace
from runner_app.storage.ledger import Ledger
from runner_app.workflows.reconcile import ReconcileWorkflow
from runner_app.workflows.reconcile_drive import DriveEffectReader
from runner_app.workflows.reconcile_local import ManagedGitEffectReader
from runner_app.workflows.retarget_resume import ResumeRetargetWorkflow


def _reconcile_handler(ledger: Ledger, registry: SourceRegistry,
                       provider: Any, registration: Any) -> ReconcileWorkflow:
    writer = DocsCASWriter(cast(Any, provider), _attached(),
                           extra_docs=registration.state_docs())
    return ReconcileWorkflow(
        ledger, owner_source_ids=frozenset({"local-owner"}), ports={
            "github_repository": lambda *_: None,
            "git_ref": lambda *_: None,
            "pull_request": lambda *_: None,
            "drive_file": DriveEffectReader(_attached(), cast(Any, object()), writer),
            "local_workspace": ManagedGitEffectReader(registry),
        },
    )


def _owner_command(ledger: Ledger, head: str, action: str,
                   target_id: str) -> dict[str, Any]:
    command = _command(ledger, head)
    command.update(
        request_id=str(uuid4()), action=action, expected_head=None,
        params={
            ("reconcile_version" if action == "RECONCILE" else "resume_version"): "1.0",
            "target_request_id": target_id,
        },
    )
    return command


def _submit_owner(engine: Engine, ledger: Ledger, head: str,
                  action: str, target_id: str) -> dict[str, Any]:
    command = _owner_command(ledger, head, action, target_id)
    engine.submit(json.dumps(command).encode(), Provenance("local", "local-owner"))
    return _terminal(ledger, command["request_id"])


def _switched_before_drive(
    ledger: Ledger, registry: SourceRegistry, provider: Any, registration: Any,
    engine: Engine, head: str, monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    workflow = engine.handlers["RETARGET_WORKSPACE"]

    def fail_before_drive(*_: Any, **__: Any) -> None:
        raise RuntimeError("Simulated crash after Git and before Drive intents")

    monkeypatch.setattr(workflow.drive_state, "publish", fail_before_drive)
    original = _command(ledger, head)
    engine.submit(json.dumps(original).encode(), Provenance("local", "local-owner"))
    target = _terminal(ledger, original["request_id"])
    assert target["code"] == "NEEDS_RECONCILIATION"
    assert target["reconcile_required"] == 1
    assert [effect["step_id"] for effect in target["effects"]] == ["retarget-workspace"]
    engine.handlers["RECONCILE"] = _reconcile_handler(
        ledger, registry, provider, registration,
    )
    reconciled = _submit_owner(engine, ledger, head, "RECONCILE", original["request_id"])
    assert reconciled["status"] == "COMPLETED"
    assert reconciled["result"]["release_code"] == "RETARGET_STATE_INCOMPLETE"
    assert ledger.get(original["request_id"])["effects"][0]["state"] == "CONFIRMED"
    assert ledger.holds_lock(original["request_id"], f"repo:{PROJECT}")
    return original


def _install_resume(ledger: Ledger, registry: SourceRegistry, engine: Engine) -> None:
    workflow = engine.handlers["RETARGET_WORKSPACE"]
    engine.handlers["RESUME_RETARGET"] = ResumeRetargetWorkflow(
        ledger, registry, workflow.drive_state,
    )


def test_owner_resume_repairs_pre_drive_gap_once_while_unarmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, registry, repo, area, other, head, provider, registration, engine = _setup(tmp_path)
    try:
        original = _switched_before_drive(
            ledger, registry, provider, registration, engine, head, monkeypatch,
        )
        _install_resume(ledger, registry, engine)
        before_other = inspect_workspace(repo, other)
        ledger.disarm()
        actor = _owner_command(ledger, head, "RESUME_RETARGET", original["request_id"])
        denied = engine.submit(
            json.dumps(actor).encode(),
            Provenance("drive", COMMAND_ID, PROJECT, WORKSPACE),
        )
        assert denied.receipt["status"] == "REJECTED"
        assert denied.receipt["code"] == "SOURCE_SCOPE"
        # A rejected UUID cannot be reused with a different source.
        actor["request_id"] = str(uuid4())
        admitted = engine.submit(json.dumps(actor).encode(), Provenance("local", "local-owner"))
        assert admitted.execute
        completed = _terminal(ledger, actor["request_id"])
        assert completed["status"] == "COMPLETED"
        assert completed["result"]["target_request_id"] == original["request_id"]
        assert ledger.get(original["request_id"])["reconcile_required"] == 0
        assert not ledger.holds_lock(original["request_id"], f"repo:{PROJECT}")
        assert inspect_workspace(repo, area)["branch"] == "feature/free"
        assert inspect_workspace(repo, other) == before_other
        state = json.loads(_text(provider, STATE_ID).removeprefix("PROJECT STATE\n"))
        assert state["workspaces"][0]["branch"] == "feature/free"
        assert "Branch: `feature/free`\n" in _text(provider, CONTEXT_ID)
        effects = ledger.get(original["request_id"])["effects"]
        assert {effect["step_id"] for effect in effects} == {
            "retarget-workspace", "drive-retarget-project-state",
            "drive-retarget-source-context",
        }
        assert all(effect["state"] == "CONFIRMED" for effect in effects)
        writes = list(provider.batches)
        duplicate = engine.submit(json.dumps(actor).encode(), Provenance("local", "local-owner"))
        assert duplicate.duplicate
        assert duplicate.receipt == completed["receipt"]
        assert provider.batches == writes
        second = _submit_owner(engine, ledger, head, "RESUME_RETARGET",
                               original["request_id"])
        assert second["status"] == "REJECTED"
        assert second["code"] == "RETARGET_RESUME_NOT_PARTIAL"
    finally:
        engine.shutdown()


def test_existing_original_drive_intents_are_never_resubmitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, registry, _, _, _, head, provider, registration, engine = _setup(tmp_path)
    workflow = engine.handlers["RETARGET_WORKSPACE"]

    def intents_without_write(request_id: str, plan: Any) -> None:
        for step, intent in workflow.drive_state._intents(plan).items():
            ledger.begin_effect(request_id, step, "drive_file", intent)
        raise RuntimeError("Simulated crash after original intents")

    monkeypatch.setattr(workflow.drive_state, "publish", intents_without_write)
    try:
        original = _command(ledger, head)
        engine.submit(json.dumps(original).encode(), Provenance("local", "local-owner"))
        assert _terminal(ledger, original["request_id"])["reconcile_required"] == 1
        engine.handlers["RECONCILE"] = _reconcile_handler(
            ledger, registry, provider, registration,
        )
        _submit_owner(engine, ledger, head, "RECONCILE", original["request_id"])
        _install_resume(ledger, registry, engine)
        writes = list(provider.batches)
        actor = _submit_owner(engine, ledger, head, "RESUME_RETARGET",
                              original["request_id"])
        assert actor["status"] == "BLOCKED"
        assert actor["code"] == "RETARGET_RESUME_EFFECT_UNSETTLED"
        assert provider.batches == writes == []
        assert ledger.holds_lock(original["request_id"], f"repo:{PROJECT}")
        assert not any(event["type"] == "RETARGET_RESUME_CLAIMED"
                       and event["request_id"] == original["request_id"]
                       for event in ledger.events(limit=1000))
    finally:
        engine.shutdown()


def test_resume_refuses_dirty_git_and_stale_drive_before_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, registry, _, area, _, head, provider, registration, engine = _setup(tmp_path)
    try:
        original = _switched_before_drive(
            ledger, registry, provider, registration, engine, head, monkeypatch,
        )
        _install_resume(ledger, registry, engine)
        dirty = area / "do-not-touch.txt"
        dirty.write_text("owner data")
        actor = _submit_owner(engine, ledger, head, "RESUME_RETARGET",
                              original["request_id"])
        assert actor["status"] == "BLOCKED"
        assert actor["code"] == "RETARGET_RESUME_SOURCE_CHANGED"
        assert dirty.read_text() == "owner data"
        dirty.unlink()
        provider.put_document(STATE_ID, "other writer\n")
        actor = _submit_owner(engine, ledger, head, "RESUME_RETARGET",
                              original["request_id"])
        assert actor["status"] == "BLOCKED"
        assert actor["code"] == "RETARGET_PROJECT_STATE_INVALID"
        assert provider.batches == []
        assert ledger.holds_lock(original["request_id"], f"repo:{PROJECT}")
    finally:
        engine.shutdown()


def test_crash_after_atomic_claim_retains_original_lock_and_never_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, registry, _, _, _, head, provider, registration, engine = _setup(tmp_path)
    try:
        original = _switched_before_drive(
            ledger, registry, provider, registration, engine, head, monkeypatch,
        )
        _install_resume(ledger, registry, engine)
        recovery = engine.handlers["RESUME_RETARGET"]

        def crash_before_cas(*_: Any, **__: Any) -> None:
            raise RuntimeError("Simulated crash after both durable intents")

        monkeypatch.setattr(recovery.drive_state, "_write", crash_before_cas)
        actor = _submit_owner(engine, ledger, head, "RESUME_RETARGET",
                              original["request_id"])
        assert actor["status"] == "FAULT"
        assert actor["code"] == "RETARGET_RESUME_FAILED"
        assert provider.batches == []
        target = ledger.get(original["request_id"])
        assert {effect["step_id"] for effect in target["effects"]} == {
            "retarget-workspace", "drive-retarget-project-state",
            "drive-retarget-source-context",
        }
        assert [effect["state"] for effect in target["effects"]].count("INTENDED") == 2
        assert ledger.holds_lock(original["request_id"], f"repo:{PROJECT}")
        second = _submit_owner(engine, ledger, head, "RESUME_RETARGET",
                               original["request_id"])
        assert second["status"] == "REJECTED"
        assert second["code"] == "RETARGET_RESUME_CLAIMED"
        assert provider.batches == []
        ledger.startup()
        assert [effect["state"] for effect in ledger.get(original["request_id"])["effects"]].count(
            "UNKNOWN",
        ) == 2
    finally:
        engine.shutdown()


def test_partial_recovery_write_needs_manual_exact_repair_then_read_only_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, registry, _, _, _, head, provider, registration, engine = _setup(tmp_path)
    try:
        original = _switched_before_drive(
            ledger, registry, provider, registration, engine, head, monkeypatch,
        )
        _install_resume(ledger, registry, engine)
        recovery = engine.handlers["RESUME_RETARGET"]
        plan = recovery.drive_state.preflight(
            original, generation=0,
            digest=ledger.get(original["request_id"])["digest"],
        )
        intended_context = plan.documents[1].desired
        original_batch = provider.batch_update_document

        def fail_context(file_id: str, *, requests: Any, required_revision_id: str) -> Any:
            if file_id == CONTEXT_ID:
                raise RuntimeError("Simulated non-applied second Docs CAS")
            return original_batch(file_id, requests=requests,
                                  required_revision_id=required_revision_id)

        monkeypatch.setattr(provider, "batch_update_document", fail_context)
        actor = _submit_owner(engine, ledger, head, "RESUME_RETARGET",
                              original["request_id"])
        assert actor["status"] == "BLOCKED"
        assert actor["code"] == "RETARGET_STATE_WRITE_UNCERTAIN"
        effects = {effect["step_id"]: effect for effect in
                   ledger.get(original["request_id"])["effects"]}
        assert effects["drive-retarget-project-state"]["state"] == "CONFIRMED"
        assert effects["drive-retarget-source-context"]["state"] == "INTENDED"
        assert ledger.holds_lock(original["request_id"], f"repo:{PROJECT}")
        writes = list(provider.batches)
        second = _submit_owner(engine, ledger, head, "RESUME_RETARGET",
                               original["request_id"])
        assert second["status"] == "REJECTED"
        assert second["code"] == "RETARGET_RESUME_CLAIMED"
        assert provider.batches == writes
        # A separate explicit owner repair sets precisely the originally
        # intended bytes; RECONCILE only reads and confirms the old intent.
        monkeypatch.setattr(provider, "batch_update_document", original_batch)
        provider.put_document(CONTEXT_ID, intended_context)
        reconciled = _submit_owner(engine, ledger, head, "RECONCILE",
                                   original["request_id"])
        assert reconciled["status"] == "COMPLETED"
        assert reconciled["result"]["target_lock_retained"] is False
        assert ledger.get(original["request_id"])["reconcile_required"] == 0
        assert not ledger.holds_lock(original["request_id"], f"repo:{PROJECT}")
    finally:
        engine.shutdown()


def test_crash_after_original_release_clears_only_empty_resume_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, registry, _, _, _, head, provider, registration, engine = _setup(tmp_path)
    try:
        original = _switched_before_drive(
            ledger, registry, provider, registration, engine, head, monkeypatch,
        )
        _install_resume(ledger, registry, engine)
        actor = _owner_command(ledger, head, "RESUME_RETARGET", original["request_id"])
        old_finish = ledger.finish

        def lose_terminal_ack(request_id: str, *args: Any, **kwargs: Any) -> Any:
            if request_id == actor["request_id"]:
                raise sqlite3.OperationalError("Simulated lost SQLite terminal acknowledgement")
            return old_finish(request_id, *args, **kwargs)

        monkeypatch.setattr(ledger, "finish", lose_terminal_ack)
        engine.submit(json.dumps(actor).encode(), Provenance("local", "local-owner"))
        deadline = time.monotonic() + 5
        while ledger.holds_lock(original["request_id"], f"repo:{PROJECT}"):
            if time.monotonic() >= deadline:
                pytest.fail("Recovery did not reach the post-release crash cut")
            time.sleep(0.01)
        engine.shutdown()
        monkeypatch.setattr(ledger, "finish", old_finish)
        ledger.startup()
        orphan = ledger.get(actor["request_id"])
        assert orphan["status"] == "INTERRUPTED"
        assert orphan["reconcile_required"] == 1
        replacement = Engine(
            ledger, policy=engine.policy,
            handlers={"RECONCILE": _reconcile_handler(
                ledger, registry, provider, registration,
            )},
        )
        try:
            cleared = _submit_owner(replacement, ledger, head, "RECONCILE",
                                    actor["request_id"])
            assert cleared["status"] == "COMPLETED"
            assert ledger.get(actor["request_id"])["reconcile_required"] == 0
            assert ledger.get(original["request_id"])["reconcile_required"] == 0
        finally:
            replacement.shutdown()
    finally:
        engine.shutdown()


def test_crash_after_both_doc_readbacks_keeps_lock_until_owner_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runner_app.storage.ledger import LedgerError

    ledger, registry, _, _, _, head, provider, registration, engine = _setup(tmp_path)
    try:
        original = _switched_before_drive(
            ledger, registry, provider, registration, engine, head, monkeypatch,
        )
        _install_resume(ledger, registry, engine)
        old_release = ledger.release_reconciled_retarget

        def lose_release(target_id: str, *, actor_id: str) -> None:
            actor = ledger.get(actor_id)
            if actor is not None and actor["action"] == "RESUME_RETARGET":
                raise LedgerError("SIMULATED_RELEASE_FAULT", "Simulated crash before lock release")
            old_release(target_id, actor_id=actor_id)

        monkeypatch.setattr(ledger, "release_reconciled_retarget", lose_release)
        actor = _submit_owner(engine, ledger, head, "RESUME_RETARGET",
                              original["request_id"])
        assert actor["status"] == "BLOCKED"
        assert actor["code"] == "SIMULATED_RELEASE_FAULT"
        assert ledger.holds_lock(original["request_id"], f"repo:{PROJECT}")
        assert all(effect["state"] == "CONFIRMED"
                   for effect in ledger.get(original["request_id"])["effects"])
        assert provider.batches.count(STATE_ID) == 1
        assert provider.batches.count(CONTEXT_ID) == 1
        monkeypatch.setattr(ledger, "release_reconciled_retarget", old_release)
        reconciled = _submit_owner(engine, ledger, head, "RECONCILE",
                                   original["request_id"])
        assert reconciled["status"] == "COMPLETED"
        assert reconciled["result"]["target_lock_retained"] is False
        assert not ledger.holds_lock(original["request_id"], f"repo:{PROJECT}")
        assert provider.batches.count(STATE_ID) == 1
        assert provider.batches.count(CONTEXT_ID) == 1
    finally:
        engine.shutdown()


def test_competing_resume_gets_immediate_terminal_busy_without_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, registry, _, _, _, head, provider, registration, engine = _setup(tmp_path)
    try:
        original = _switched_before_drive(
            ledger, registry, provider, registration, engine, head, monkeypatch,
        )
        _install_resume(ledger, registry, engine)
        recovery = engine.handlers["RESUME_RETARGET"]
        entered = threading.Event()
        proceed = threading.Event()
        old_publish = recovery.drive_state.publish_recovery

        def hold_before_claim(target_id: str, actor_id: str, plan: Any) -> Any:
            entered.set()
            assert proceed.wait(5)
            return old_publish(target_id, actor_id, plan)

        monkeypatch.setattr(recovery.drive_state, "publish_recovery", hold_before_claim)
        first = _owner_command(ledger, head, "RESUME_RETARGET", original["request_id"])
        second = _owner_command(ledger, head, "RESUME_RETARGET", original["request_id"])
        engine.submit(json.dumps(first).encode(), Provenance("local", "local-owner"))
        assert entered.wait(5)
        before = time.monotonic()
        refused = engine.submit(json.dumps(second).encode(), Provenance("local", "local-owner"))
        assert time.monotonic() - before < 0.5
        assert refused.receipt["status"] == "REJECTED"
        assert refused.receipt["code"] == "BUSY"
        assert provider.batches == []
        proceed.set()
        assert _terminal(ledger, first["request_id"])["status"] == "COMPLETED"
        assert ledger.get(second["request_id"])["status"] == "REJECTED"
        assert provider.batches.count(STATE_ID) == 1
        assert provider.batches.count(CONTEXT_ID) == 1
    finally:
        proceed.set()
        engine.shutdown()
