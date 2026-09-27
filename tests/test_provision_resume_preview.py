"""Read-only classification of a partial CREATE_PROJECT before any adoption."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from runner_app.adapters.github import make_provision_marker
from runner_app.domain.commands import freeze
from runner_app.git.registry import SourceRegistry
from runner_app.git.service import SourceService
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger
from runner_app.workflows.provision_resume import (
    ProvisionResumeError,
    inspect_prior_create,
)

_INSTALLATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _case(tmp_path: Path) -> tuple[Ledger, SourceRegistry, dict[str, Any]]:
    ledger = Ledger(Database(tmp_path / "state" / "ledger.sqlite"))
    ledger.startup()
    ledger.arm()
    registry = SourceRegistry(ledger.database, tmp_path / "managed")
    state = ledger.state()
    now = datetime.now(timezone.utc)
    command = {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "CREATE_PROJECT", "project_id": "fixture",
        "workspace_id": None, "expected_head": None,
        "params": {"name": "Fixture", "source": {"kind": "new"},
                   "template_id": "python-basic", "github_identity": "personal",
                   "visibility": "private"},
        "intent": "Original owner intent stays frozen",
    }
    assert ledger.admit(
        command, freeze(command).digest, "owner-session",
        ("repo:fixture", "global:personal", "global:drive-installation"),
    ).execute
    return ledger, registry, command


def _inspect(ledger: Ledger, registry: SourceRegistry, request_id: str,
             **changes: Any) -> Any:
    values = {
        "project_id": "fixture", "installation_id": _INSTALLATION,
        "github_identity": "personal", "github_owner": "ViskyG",
        **changes,
    }
    return inspect_prior_create(ledger, registry, request_id, **values)


def _confirmed_github(ledger: Ledger, command: dict[str, Any]) -> str:
    marker = make_provision_marker(_INSTALLATION, "fixture", command["request_id"])
    effect = ledger.begin_effect(
        command["request_id"], "github-repository", "github_repository",
        {"owner": "ViskyG", "name": "fixture", "private": True, "marker": marker},
    )
    ledger.confirm_effect(effect, "421")
    return marker


def _code(call: Any, code: str) -> None:
    with pytest.raises(ProvisionResumeError) as error:
        call()
    assert error.value.code == code


def test_confirmed_prior_repo_is_previewed_with_original_marker_and_digest(
    tmp_path: Path,
) -> None:
    ledger, registry, command = _case(tmp_path)
    marker = _confirmed_github(ledger, command)
    ledger.finish(command["request_id"], "BLOCKED", code="DRIVE_QUOTA_EXCEEDED")
    preview = _inspect(ledger, registry, command["request_id"])
    assert preview.target_status == "BLOCKED"
    assert preview.target_digest == freeze(command).digest
    assert preview.provision_marker == marker
    assert preview.command == command
    assert preview.effect("github-repository") is not None
    assert preview.effect("github-repository").provider_id == "421"
    assert preview.effect("local-project") is None
    stored = ledger.get(command["request_id"])
    assert stored is not None and stored["effects"][0]["state"] == "CONFIRMED"
    _code(lambda: _inspect(ledger, registry, command["request_id"],
                          github_identity="other"), "RESUME_TARGET_MISMATCH")
    _code(lambda: _inspect(ledger, registry, command["request_id"],
                          github_owner="someone-else"),
          "RESUME_GITHUB_IDENTITY_MISMATCH")


def test_confirmed_managed_source_is_classified_without_recreating_it(
    tmp_path: Path,
) -> None:
    ledger, registry, command = _case(tmp_path)
    _confirmed_github(ledger, command)
    project = SourceService(ledger, registry).create_new_source(command["request_id"])
    ledger.finish(command["request_id"], "CANCELLED", code="CANCEL_REQUESTED")
    preview = _inspect(ledger, registry, command["request_id"])
    source_effect = preview.effect("local-project")
    assert source_effect is not None
    assert source_effect.provider_id == project["repo_path"]
    assert preview.effect("initial-workspace") is None
    registered = registry.get_project("fixture")
    assert registered is not None and registered["repo_path"] == project["repo_path"]


def test_unknown_prior_effect_requires_reconcile_and_keeps_old_lock(
    tmp_path: Path,
) -> None:
    ledger, registry, command = _case(tmp_path)
    marker = make_provision_marker(_INSTALLATION, "fixture", command["request_id"])
    ledger.begin_effect(
        command["request_id"], "github-repository", "github_repository",
        {"owner": "ViskyG", "name": "fixture", "private": True, "marker": marker},
    )
    ledger.finish(command["request_id"], "BLOCKED", code="REPOSITORY_CREATE_UNKNOWN")
    assert ledger.holds_lock(command["request_id"], "repo:fixture")
    _code(lambda: _inspect(ledger, registry, command["request_id"]),
          "RESUME_RECONCILIATION_REQUIRED")


def test_unjournaled_local_project_is_not_adopted_by_matching_path(
    tmp_path: Path,
) -> None:
    ledger, registry, command = _case(tmp_path)
    _confirmed_github(ledger, command)
    # A path and registry row with the expected name are not an old effect proof.
    repo = registry.managed_root / "fixture" / "repo.git"
    repo.mkdir(parents=True, mode=0o700)
    registry.register_project("fixture", "Fixture", repo, origin_kind="new")
    ledger.finish(command["request_id"], "BLOCKED", code="PROVISION_PARTIAL")
    _code(lambda: _inspect(ledger, registry, command["request_id"]),
          "RESUME_LOCAL_SOURCE_UNVERIFIED")


def test_completed_create_is_not_a_partial_resume_target(tmp_path: Path) -> None:
    ledger, registry, command = _case(tmp_path)
    _confirmed_github(ledger, command)
    ledger.finish(command["request_id"], "COMPLETED")
    _code(lambda: _inspect(ledger, registry, command["request_id"]),
          "RESUME_TARGET_NOT_PARTIAL")
