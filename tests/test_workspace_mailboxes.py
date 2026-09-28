"""Completed project mailboxes become known dynamically without command polling."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from runner_app.adapters.drive_fake import FakeDrive
from runner_app.adapters.drive_registry import WorkspaceDriveRegistration
from runner_app.domain.commands import Provenance, freeze
from runner_app.engine.service import Context, Engine, OperationResult
from runner_app.engine.workspace_mailboxes import WorkspaceMailboxManager
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger

_INSTALLATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class FakeSources:
    def __init__(self, ledger: Ledger) -> None:
        self.database = ledger.database
        self.ready = True

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        return {"state": "READY"} if project_id == "toy" and self.ready else None

    def get_workspace(self, project_id: str, workspace_id: str) -> dict[str, Any] | None:
        if (project_id, workspace_id) == ("toy", "primary") and self.ready:
            return {"state": "ACTIVE", "archived": False}
        return None

    def get_workspace_profile(self, project_id: str, workspace_id: str) -> dict[str, str] | None:
        if (project_id, workspace_id) == ("toy", "primary") and self.ready:
            return {"template_id": "python-basic", "profile_id": "python-quick"}
        return None

    def get_project_remote(self, project_id: str) -> dict[str, Any] | None:
        return {"provider_id": 1} if project_id == "toy" and self.ready else None


class FakeRegistrations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.registration = WorkspaceDriveRegistration(
            _INSTALLATION, "toy", "primary", "projectFolder123", "workspaceFolder123",
            "commandDocument123", "changesetDocument123", "receiptDocument123",
            "latestResultDocument123", "projectStateDocument123", "sourceContextDocument123",
            "projectCommandDocument123", "projectReceiptDocument123",
        )

    def get(self, project_id: str, workspace_id: str) -> WorkspaceDriveRegistration:
        self.calls.append((project_id, workspace_id))
        assert (project_id, workspace_id) == ("toy", "primary")
        return self.registration


def _ledger(tmp_path: Path) -> Ledger:
    ledger = Ledger(Database(tmp_path / "state" / "state.sqlite"))
    ledger.startup()
    return ledger


def _completed_create(ledger: Ledger) -> None:
    ledger.arm()  # disposable fake-backed Ledger only
    now = datetime.now(timezone.utc)
    state = ledger.state()
    command = {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "CREATE_PROJECT", "project_id": "toy",
        "workspace_id": None, "expected_head": None,
        "params": {"name": "Toy", "source": {"kind": "new"},
                   "template_id": "python-basic", "github_identity": "owner",
                   "visibility": "private"},
        "intent": "Create a disposable fake project",
    }
    assert ledger.admit(command, freeze(command).digest, "root-command",
                        ("repo:toy", "global:owner", "global:drive-installation")).execute
    ledger.finish(command["request_id"], "COMPLETED")


def _manager(tmp_path: Path, *, route_factory: Any = None) -> tuple[
    WorkspaceMailboxManager, Ledger, Engine, FakeRegistrations, FakeDrive,
]:
    ledger = _ledger(tmp_path)
    engine = Engine(ledger)
    registry = FakeRegistrations()
    provider = FakeDrive()
    manager = WorkspaceMailboxManager(
        ledger, cast(Any, FakeSources(ledger)), cast(Any, registry), provider, engine,
        tmp_path / "frozen-workspaces", {"python-basic": "python-quick"},
        route_factory=route_factory, refresh_seconds=0.25,
    )
    return manager, ledger, engine, registry, provider


def test_inert_registration_requires_completed_create_and_does_not_poll(tmp_path: Path) -> None:
    manager, ledger, engine, registry, provider = _manager(tmp_path)
    try:
        before = manager.refresh()
        assert before.known == 0 and registry.calls == []
        _completed_create(ledger)
        after = manager.refresh()
        assert after.known == 1 and after.activated == 0
        mailbox = manager.mailboxes()[("toy", "primary")]
        assert mailbox.command_id == registry.registration.command_doc_id
        assert mailbox.changeset_id == registry.registration.changeset_doc_id
        assert engine.capabilities() == []
        assert engine.policy.mailboxes == {}
        assert provider.calls == []
        assert manager.refresh().new_mailboxes == ()
        assert registry.calls == [("toy", "primary")]
    finally:
        manager.close()


def test_background_refresh_finds_new_completed_project_without_restart(tmp_path: Path) -> None:
    manager, ledger, engine, _, provider = _manager(tmp_path)
    manager.start()
    try:
        assert manager.mailboxes() == {}
        _completed_create(ledger)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not manager.mailboxes():
            time.sleep(0.01)
        assert ("toy", "primary") in manager.mailboxes()
        assert manager.active_count() == 0
        assert engine.capabilities() == []
        assert provider.calls == []
    finally:
        manager.close()


def test_route_activation_is_exact_and_stays_disarmed(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(frozen: Any, _context: Context) -> OperationResult:
        calls.append(frozen.command["project_id"])
        return OperationResult("COMPLETED")

    manager, ledger, engine, registry, provider = _manager(
        tmp_path, route_factory=lambda _registration, _profile_id, _slots: handler,
    )
    try:
        _completed_create(ledger)
        ledger.disarm()
        refreshed = manager.refresh()
        assert refreshed.activated == 1
        assert manager.active_count() == 1
        assert "DELIVER_VERIFY_PUBLISH" in engine.capabilities()
        expected = Provenance("drive", registry.registration.command_doc_id, "toy", "primary")
        assert engine.policy.mailboxes[expected.source_id] == expected
        assert "toy" in engine.policy.project_ids
        assert ("toy", "primary") in engine.policy.workspace_pairs
        assert "python-quick" in engine.policy.approved_profiles
        assert ledger.state()["armed"] is False
        assert provider.calls == []
        matching = freeze({"action": "DELIVER_VERIFY_PUBLISH", "project_id": "toy",
                           "workspace_id": "primary"})
        wrong = freeze({"action": "DELIVER_VERIFY_PUBLISH", "project_id": "other",
                        "workspace_id": "primary"})
        context = Context(threading.Event(), ledger)
        assert engine.handlers["DELIVER_VERIFY_PUBLISH"](matching, context).status == "COMPLETED"
        assert engine.handlers["DELIVER_VERIFY_PUBLISH"](wrong, context).code == (
            "WORKSPACE_ROUTE_NOT_REGISTERED"
        )
        assert calls == ["toy"]
    finally:
        manager.close()


def test_route_collision_cannot_replace_registered_workspace(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    engine = Engine(ledger)
    provenance = Provenance("drive", "commandDocument123", "toy", "primary")
    handler = lambda _frozen, _context: OperationResult("COMPLETED")
    engine.register_workspace_route(provenance, "python-quick", handler)
    engine.register_workspace_route(provenance, "python-quick", handler)
    with pytest.raises(ValueError):
        engine.register_workspace_route(provenance, "python-quick",
                                        lambda _frozen, _context: OperationResult("FAULT"))
    with pytest.raises(ValueError):
        engine.register_workspace_route(provenance, "changed-profile", handler)
    with pytest.raises(ValueError):
        engine.register_workspace_route(
            Provenance("drive", "otherCommandDoc123", "toy", "primary"),
            "python-quick", handler,
        )
    with pytest.raises(ValueError):
        engine.register_workspace_route(
            Provenance("drive", provenance.source_id, "other", "primary"),
            "python-quick", handler,
        )
