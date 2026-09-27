"""Atomic, provenance-linked adoption of exact prior CREATE_PROJECT effects."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from runner_app.domain.commands import LocalPolicy, Provenance, freeze, validate_shape
from runner_app.engine.service import Context, Engine, OperationResult
from runner_app.storage import Database
from runner_app.storage.ledger import EffectAdoptionProof, Ledger, LedgerError

_OWNER = Provenance("local", "owner-session")


def _command(ledger: Ledger, action: str, *, target_id: str | None = None,
             project_id: str = "fixture") -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    state = ledger.state()
    params = ({
        "name": "Fixture", "source": {"kind": "new"},
        "template_id": "python-basic", "github_identity": "personal",
        "visibility": "private",
    } if action == "CREATE_PROJECT" else {
        "resume_version": "1.0", "target_request_id": target_id,
    })
    command = {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": action, "project_id": project_id, "workspace_id": None,
        "expected_head": None, "params": params,
        "intent": "Recover an exact confirmed resource",
    }
    validate_shape(command)
    return command


def _target(tmp_path: Path) -> tuple[Ledger, dict[str, Any], str, dict[str, Any]]:
    ledger = Ledger(Database(tmp_path / "state" / "ledger.sqlite"))
    ledger.startup()
    ledger.arm()
    command = _command(ledger, "CREATE_PROJECT")
    assert ledger.admit(
        command, freeze(command).digest, "root-command",
        ("repo:fixture", "global:personal", "global:drive-installation"),
    ).execute
    intent = {"owner": "ViskyG", "name": "fixture", "private": True,
              "marker": "cdr:v1:" + "a" * 64}
    effect_id = ledger.begin_effect(
        command["request_id"], "github-repository", "github_repository", intent,
    )
    ledger.confirm_effect(effect_id, "421")
    ledger.finish(command["request_id"], "BLOCKED", code="PROVISION_PARTIAL")
    return ledger, command, effect_id, intent


def _submit(ledger: Ledger, command: dict[str, Any], handler: Any
            ) -> dict[str, Any]:
    engine = Engine(
        ledger, policy=LocalPolicy(), handlers={"RESUME_PROVISION": handler},
    )
    admission = engine.submit(json.dumps(command).encode(), _OWNER)
    assert admission.execute
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        observed = ledger.get(command["request_id"])
        assert observed is not None
        if observed["status"] != "RUNNING":
            return observed
        time.sleep(0.01)
    raise AssertionError("Resume handler did not finish")


def test_exact_readback_adopts_once_with_immutable_source_provenance(
    tmp_path: Path,
) -> None:
    ledger, target, source_effect_id, intent = _target(tmp_path)
    resume = _command(ledger, "RESUME_PROVISION", target_id=target["request_id"])
    proof = EffectAdoptionProof(source_effect_id, "421", intent)
    observed_ids: list[str] = []

    def handler(_frozen: Any, context: Context) -> OperationResult:
        first = context.ledger.adopt_confirmed_effect(resume["request_id"], proof)
        second = context.ledger.adopt_confirmed_effect(resume["request_id"], proof)
        assert first == second
        observed_ids.append(first)
        return OperationResult("COMPLETED", result={"adopted_effect_id": first})

    stored = _submit(ledger, resume, handler)
    assert stored["status"] == "COMPLETED"
    assert len(stored["effects"]) == 1
    adopted = stored["effects"][0]
    assert adopted["effect_id"] == observed_ids[0]
    assert adopted["step_id"] == "github-repository"
    assert adopted["kind"] == "github_repository"
    assert adopted["state"] == "CONFIRMED" and adopted["provider_id"] == "421"
    assert adopted["intent"] == intent
    assert adopted["adoption"]["source_effect_id"] == source_effect_id
    assert len(adopted["adoption"]["proof_sha256"]) == 64
    assert [step["last_event_type"] for step in stored["steps"]
            if step["step_id"] == "github-repository"] == ["EFFECT_ADOPTED"]
    original = ledger.get(target["request_id"])
    assert original is not None and original["effects"][0]["effect_id"] == source_effect_id
    assert "adoption" not in original["effects"][0]
    with ledger.database.connect() as connection:
        claim = connection.execute(
            "SELECT resume_request_id FROM provision_resume_claims "
            "WHERE target_request_id=?", (target["request_id"],),
        ).fetchone()
        assert claim == (resume["request_id"],)
        proof_row = connection.execute(
            "SELECT source_effect_id,proof_json FROM effect_adoptions WHERE effect_id=?",
            (observed_ids[0],),
        ).fetchone()
        assert proof_row is not None and proof_row[0] == source_effect_id
        assert json.loads(proof_row[1]) == {
            "source_effect_id": source_effect_id, "provider_id": "421", "intent": intent,
        }
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "UPDATE effect_adoptions SET proof_sha256=? WHERE effect_id=?",
                ("0" * 64, observed_ids[0]),
            )


@pytest.mark.parametrize("provider_id,observed_intent", [
    ("422", {"owner": "ViskyG", "name": "fixture", "private": True,
             "marker": "cdr:v1:" + "a" * 64}),
    ("421", {"owner": "wrong", "name": "fixture", "private": True,
             "marker": "cdr:v1:" + "a" * 64}),
])
def test_mismatched_readback_never_claims_or_adopts(
    tmp_path: Path, provider_id: str, observed_intent: dict[str, Any],
) -> None:
    ledger, target, source_effect_id, _ = _target(tmp_path)
    resume = _command(ledger, "RESUME_PROVISION", target_id=target["request_id"])
    codes: list[str] = []

    def handler(_frozen: Any, context: Context) -> OperationResult:
        try:
            context.ledger.adopt_confirmed_effect(
                resume["request_id"],
                EffectAdoptionProof(source_effect_id, provider_id, observed_intent),
            )
        except LedgerError as error:
            codes.append(error.code)
            return OperationResult("BLOCKED", error.code)
        raise AssertionError("Mismatched proof was accepted")

    stored = _submit(ledger, resume, handler)
    assert stored["status"] == "BLOCKED" and codes == ["ADOPTION_PROOF_MISMATCH"]
    assert stored["effects"] == []
    with ledger.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM provision_resume_claims").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM effect_adoptions").fetchone()[0] == 0


def test_second_resume_cannot_fork_a_claimed_create(tmp_path: Path) -> None:
    ledger, target, source_effect_id, intent = _target(tmp_path)
    proof = EffectAdoptionProof(source_effect_id, "421", intent)
    first = _command(ledger, "RESUME_PROVISION", target_id=target["request_id"])
    assert _submit(
        ledger, first,
        lambda _frozen, context: OperationResult(
            "BLOCKED", "MORE_WORK_REQUIRED", {
                "effect_id": context.ledger.adopt_confirmed_effect(first["request_id"], proof),
            },
        ),
    )["status"] == "BLOCKED"
    second = _command(ledger, "RESUME_PROVISION", target_id=target["request_id"])
    codes: list[str] = []

    def second_handler(_frozen: Any, context: Context) -> OperationResult:
        try:
            context.ledger.adopt_confirmed_effect(second["request_id"], proof)
        except LedgerError as error:
            codes.append(error.code)
            return OperationResult("BLOCKED", error.code)
        raise AssertionError("A second resume adopted the same original")

    stored = _submit(ledger, second, second_handler)
    assert stored["status"] == "BLOCKED"
    assert codes == ["ADOPTION_TARGET_CLAIMED"]
    assert stored["effects"] == []


def test_adoption_transaction_rolls_back_claim_and_effect_on_journal_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, target, source_effect_id, intent = _target(tmp_path)
    resume = _command(ledger, "RESUME_PROVISION", target_id=target["request_id"])
    original_append = ledger._append

    def fail_adoption(connection: Any, request_id: str, step_id: str | None,
                      event_type: str, detail: dict[str, Any], timestamp: str) -> Any:
        if event_type == "EFFECT_ADOPTED":
            raise RuntimeError("synthetic SQLite journal failure")
        return original_append(connection, request_id, step_id, event_type, detail, timestamp)

    monkeypatch.setattr(ledger, "_append", fail_adoption)

    def handler(_frozen: Any, context: Context) -> OperationResult:
        with pytest.raises(RuntimeError):
            context.ledger.adopt_confirmed_effect(
                resume["request_id"], EffectAdoptionProof(source_effect_id, "421", intent),
            )
        return OperationResult("BLOCKED", "ADOPTION_WRITE_FAILED")

    stored = _submit(ledger, resume, handler)
    assert stored["status"] == "BLOCKED" and stored["effects"] == []
    with ledger.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM provision_resume_claims").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM effect_adoptions").fetchone()[0] == 0


def test_terminal_resume_cannot_adopt_after_its_lock_was_released(tmp_path: Path) -> None:
    ledger, target, source_effect_id, intent = _target(tmp_path)
    resume = _command(ledger, "RESUME_PROVISION", target_id=target["request_id"])
    _submit(ledger, resume, lambda *_: OperationResult("BLOCKED", "PREVIEW_ONLY"))
    with pytest.raises(LedgerError) as error:
        ledger.adopt_confirmed_effect(
            resume["request_id"], EffectAdoptionProof(source_effect_id, "421", intent),
        )
    assert error.value.code == "ADOPTION_NOT_ADMITTED"


def test_v8_to_v9_adoption_migration_preserves_prior_effect_and_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import runner_app.storage.database as database_module

    database = Database(tmp_path / "legacy" / "ledger.sqlite")
    with monkeypatch.context() as scope:
        scope.setattr(database_module, "SCHEMA_VERSION", 8)
        ledger = Ledger(database)
        ledger.startup()
        ledger.arm()
        command = _command(ledger, "CREATE_PROJECT")
        assert ledger.admit(
            command, freeze(command).digest, "root-command",
            ("repo:fixture", "global:personal", "global:drive-installation"),
        ).execute
        effect_id = ledger.begin_effect(
            command["request_id"], "github-repository", "github_repository",
            {"owner": "ViskyG", "name": "fixture", "private": True,
             "marker": "cdr:v1:" + "a" * 64},
        )
        ledger.confirm_effect(effect_id, "421")
        ledger.finish(command["request_id"], "BLOCKED", code="PROVISION_PARTIAL")
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
    with monkeypatch.context() as scope:
        scope.setitem(database_module._MIGRATIONS, 9, (
            "CREATE TABLE migration_must_rollback (id INTEGER PRIMARY KEY)",
            "INVALID ADOPTION MIGRATION SQL",
        ))
        with pytest.raises(sqlite3.DatabaseError):
            database.migrate()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='migration_must_rollback'",
        ).fetchone() is None
    assert database.migrate() == 9
    stored = ledger.get(command["request_id"])
    assert stored is not None and stored["effects"][0]["effect_id"] == effect_id
    assert stored["effects"][0]["provider_id"] == "421"
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='effect_adoptions'",
        ).fetchone() == ("effect_adoptions",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='provision_resume_claims'",
        ).fetchone() == ("provision_resume_claims",)
