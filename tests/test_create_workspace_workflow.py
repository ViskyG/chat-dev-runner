"""Configured CREATE_WORKSPACE executes only one admitted, exact project area."""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from test_drive_provision import FakeStateWriter, _additional_workspace_fixture

from runner_app.domain.commands import freeze
from runner_app.engine.service import Context
from runner_app.git.service import SourceService
from runner_app.workflows.create_workspace import CreateWorkspaceWorkflow


class FakePrivateRepository:
    def get_repository(self, owner: str, name: str, *, expected_repo_id: int
                       ) -> SimpleNamespace:
        assert (owner, name, expected_repo_id) == ("Owner", "fixture", 12345)
        return SimpleNamespace(
            private=True, id=12345, node_id="repo-node-12345",
            owner="Owner", name="fixture",
            html_url="https://github.com/Owner/fixture",
            clone_url="https://github.com/Owner/fixture.git",
        )


def test_configured_workspace_route_creates_registered_independent_area(tmp_path: Path) -> None:
    ledger, api, drive, registry, sources, initial_writer, request_id = (
        _additional_workspace_fixture(tmp_path)
    )
    writers: list[FakeStateWriter] = []

    def populate(selected: str, registration: Any, selected_sources: Any
                 ) -> dict[str, str]:
        assert selected == request_id and selected_sources is sources
        writer = FakeStateWriter(registration)
        writer.contents["project-state:fixture"] = initial_writer.contents["project-state:fixture"]
        writers.append(writer)
        return drive.populate_workspace(
            selected, registration, source_registry=selected_sources,
            writer=cast(Any, writer),
        )

    workflow = CreateWorkspaceWorkflow(
        ledger, SourceService(ledger, sources), cast(Any, FakePrivateRepository()),
        drive, populate, template_profiles={"python-basic": "python-quick"},
    )
    operation = ledger.get(request_id)
    assert operation is not None
    result = workflow(freeze(operation["command"]), Context(threading.Event(), ledger))
    assert result.status == "COMPLETED" and result.code is None
    assert result.result["workspace_state"] == "ACTIVE"
    assert result.result["profile_id"] == "python-quick"
    assert result.result["drive"]["workspace_folder_id"] == (
        registry.get("fixture", "experiment").workspace_folder_id
    )
    assert sources.get_workspace("fixture", "experiment") is not None
    assert len(writers) == 1 and len(api.created) == 16
    ledger.finish(request_id, result.status, result=result.result)
    assert ("fixture", "experiment") in ledger.completed_workspace_pairs()


def test_cancelled_workspace_route_does_not_start_an_effect(tmp_path: Path) -> None:
    ledger, api, drive, _, sources, _, request_id = _additional_workspace_fixture(tmp_path)
    workflow = CreateWorkspaceWorkflow(
        ledger, SourceService(ledger, sources), cast(Any, FakePrivateRepository()),
        drive, lambda *_: {}, template_profiles={"python-basic": "python-quick"},
    )
    operation = ledger.get(request_id)
    assert operation is not None
    cancel = threading.Event()
    cancel.set()
    result = workflow(freeze(operation["command"]), Context(cancel, ledger))
    assert result.status == "CANCELLED" and result.code == "CANCEL_REQUESTED"
    recorded = ledger.get(request_id)
    assert recorded is not None and recorded["effects"] == []
    assert len(api.created) == 10
