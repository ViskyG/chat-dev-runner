"""Owner-local RESUME_PROVISION admission is narrow and has no default handler."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from runner_app.domain.commands import DomainError, LocalPolicy, Provenance, freeze, parse_command
from runner_app.engine.service import Context, Engine, OperationResult
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger

_OWNER = Provenance("local", "owner-session")


def _command(ledger: Ledger, action: str, *, target_id: str | None = None,
             project_id: str = "fixture") -> dict[str, Any]:
    state = ledger.state()
    now = datetime.now(timezone.utc)
    params = ({
        "name": "Fixture", "source": {"kind": "new"},
        "template_id": "python-basic", "github_identity": "personal",
        "visibility": "private",
    } if action == "CREATE_PROJECT" else {
        "resume_version": "1.0", "target_request_id": target_id,
    })
    return {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": action, "project_id": project_id, "workspace_id": None,
        "expected_head": None, "params": params,
        "intent": "Resume only recorded partial provisioning",
    }


def _case(tmp_path: Path, *, effect: str = "confirmed",
          target_status: str = "BLOCKED") -> tuple[Ledger, dict[str, Any]]:
    ledger = Ledger(Database(tmp_path / "state" / "ledger.sqlite"))
    ledger.startup()
    ledger.arm()
    command = _command(ledger, "CREATE_PROJECT")
    assert ledger.admit(
        command, freeze(command).digest, "root-command",
        ("repo:fixture", "global:personal", "global:drive-installation"),
    ).execute
    if effect != "none":
        effect_id = ledger.begin_effect(
            command["request_id"], "github-repository", "github_repository",
            {"owner": "ViskyG", "name": "fixture", "private": True,
             "marker": "cdr:v1:" + "a" * 64},
        )
        if effect == "confirmed":
            ledger.confirm_effect(effect_id, "421")
    ledger.finish(command["request_id"], target_status, code="PROVISION_PARTIAL")
    return ledger, command


def _submit(ledger: Ledger, command: dict[str, Any], *,
            provenance: Provenance = _OWNER,
            handlers: dict[str, Any] | None = None) -> dict[str, Any]:
    engine = Engine(ledger, policy=LocalPolicy(), handlers=handlers)
    engine.submit(json.dumps(command).encode(), provenance)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        observed = ledger.get(command["request_id"])
        assert observed is not None
        if observed["status"] != "RUNNING":
            return observed
        time.sleep(0.01)
    raise AssertionError("Resume handler did not finish")


def test_schema_accepts_only_versioned_target_and_null_workspace_head(tmp_path: Path) -> None:
    ledger, target = _case(tmp_path)
    command = _command(ledger, "RESUME_PROVISION", target_id=target["request_id"])
    assert parse_command(json.dumps(command).encode()).command == command
    for key, value in (
        ("resume_version", "2.0"), ("target_request_id", "invalid"),
        ("unexpected", True),
    ):
        changed = json.loads(json.dumps(command))
        changed["params"][key] = value
        with pytest.raises(DomainError) as error:
            parse_command(json.dumps(changed).encode())
        assert error.value.code == "INVALID_SCHEMA"
    changed = {**command, "workspace_id": "primary"}
    with pytest.raises(DomainError):
        parse_command(json.dumps(changed).encode())
    changed = {**command, "expected_head": "a" * 40}
    with pytest.raises(DomainError):
        parse_command(json.dumps(changed).encode())


def test_no_default_handler_never_reserves_or_writes(tmp_path: Path) -> None:
    ledger, target = _case(tmp_path)
    command = _command(ledger, "RESUME_PROVISION", target_id=target["request_id"])
    engine = Engine(ledger, policy=LocalPolicy())
    assert "RESUME_PROVISION" not in engine.capabilities()
    admission = engine.submit(json.dumps(command).encode(), _OWNER)
    assert not admission.execute and admission.receipt["code"] == "ACTION_UNAVAILABLE"
    result = ledger.get(command["request_id"])
    assert result is not None and result["status"] == "REJECTED"
    assert result["effects"] == []
    assert not ledger.holds_lock(command["request_id"], "repo:fixture")
    assert not ledger.holds_lock(command["request_id"], "global:personal")
    assert not ledger.holds_lock(command["request_id"], "global:drive-installation")


def test_valid_partial_target_gets_original_repo_and_global_locks(
    tmp_path: Path,
) -> None:
    ledger, target = _case(tmp_path, target_status="CANCELLED")
    command = _command(ledger, "RESUME_PROVISION", target_id=target["request_id"])
    observed: list[tuple[bool, bool, bool]] = []

    def handler(_frozen: Any, context: Context) -> OperationResult:
        request_id = command["request_id"]
        observed.append((
            context.ledger.holds_lock(request_id, "repo:fixture"),
            context.ledger.holds_lock(request_id, "global:personal"),
            context.ledger.holds_lock(request_id, "global:drive-installation"),
        ))
        return OperationResult("BLOCKED", "PREVIEW_ONLY")

    result = _submit(ledger, command, handlers={"RESUME_PROVISION": handler})
    assert result["status"] == "BLOCKED" and result["code"] == "PREVIEW_ONLY"
    assert observed == [(True, True, True)]
    assert result["effects"] == []
    assert not ledger.holds_lock(command["request_id"], "repo:fixture")


def test_wrong_owner_scope_target_and_partial_state_are_rejected(
    tmp_path: Path,
) -> None:
    ledger, target = _case(tmp_path)
    handlers = {"RESUME_PROVISION": lambda *_: OperationResult("COMPLETED")}
    drive = _command(ledger, "RESUME_PROVISION", target_id=target["request_id"])
    assert _submit(ledger, drive, provenance=Provenance("drive", "root-command"),
                   handlers=handlers)["code"] == "SOURCE_SCOPE"
    wrong_owner = _command(ledger, "RESUME_PROVISION", target_id=target["request_id"])
    assert _submit(ledger, wrong_owner,
                   provenance=Provenance("local", "owner-session", project_id="other"),
                   handlers=handlers)["code"] == "SOURCE_SCOPE"
    other = _command(ledger, "RESUME_PROVISION", target_id=target["request_id"],
                     project_id="other")
    assert _submit(ledger, other, handlers=handlers)["code"] == "RESUME_TARGET_SCOPE_MISMATCH"
    absent = _command(ledger, "RESUME_PROVISION", target_id=str(uuid4()))
    assert _submit(ledger, absent, handlers=handlers)["code"] == "RESUME_TARGET_MISSING"


def test_unknown_effect_keeps_old_lock_and_cannot_resume(tmp_path: Path) -> None:
    ledger, target = _case(tmp_path, effect="unknown")
    assert ledger.holds_lock(target["request_id"], "repo:fixture")
    command = _command(ledger, "RESUME_PROVISION", target_id=target["request_id"])
    handlers = {"RESUME_PROVISION": lambda *_: OperationResult("COMPLETED")}
    result = _submit(ledger, command, handlers=handlers)
    assert result["status"] == "REJECTED"
    assert result["code"] == "RESUME_RECONCILIATION_REQUIRED"
    assert ledger.holds_lock(target["request_id"], "repo:fixture")
    assert not ledger.holds_lock(command["request_id"], "repo:fixture")


def test_no_confirmed_effect_and_completed_target_cannot_resume(tmp_path: Path) -> None:
    ledger, target = _case(tmp_path, effect="none")
    handlers = {"RESUME_PROVISION": lambda *_: OperationResult("COMPLETED")}
    command = _command(ledger, "RESUME_PROVISION", target_id=target["request_id"])
    assert _submit(ledger, command, handlers=handlers)["code"] == "RESUME_NO_CONFIRMED_EFFECTS"
    other = tmp_path / "other"
    ledger2, completed = _case(other, target_status="COMPLETED")
    command2 = _command(ledger2, "RESUME_PROVISION", target_id=completed["request_id"])
    assert _submit(ledger2, command2, handlers=handlers)["code"] == "RESUME_TARGET_NOT_PARTIAL"
