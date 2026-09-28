"""Owner execution wiring stays inert until the fixed quota helper is ready."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from runner_app.engine import execution_assembly
from runner_app.engine.execution_assembly import build_owner_execution_ports
from runner_app.executors.volume_provider import HardQuotaVolumeProvider
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger
from runner_app.storage.paths import Paths


def _fixture(tmp_path: Path) -> tuple[Paths, Ledger]:
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    paths.ensure_private()
    ledger = Ledger(Database(paths.database_path))
    ledger.startup()
    return paths, ledger


def test_owner_execution_remains_inert_without_fixed_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, ledger = _fixture(tmp_path)
    profile_dir = tmp_path / "owner-profiles"
    monkeypatch.setattr(
        execution_assembly, "load_python_quick_grant",
        lambda selected: (object(), object()) if selected == profile_dir else pytest.fail(),
    )
    monkeypatch.setattr(
        HardQuotaVolumeProvider, "_installed_helper",
        staticmethod(lambda: False),
    )
    unavailable = build_owner_execution_ports(
        paths, ledger, cast(Any, object()), cast(Any, object()), profile_dir,
    )
    assert unavailable.ports is None
    assert unavailable.code == "VOLUME_HELPER_UNAVAILABLE"
    assert not (paths.state_dir / "execution-runs").exists()
    assert not (paths.state_dir / "preparation-evidence").exists()


def test_owner_execution_builds_one_request_bound_quota_port_only_when_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, ledger = _fixture(tmp_path)
    profile_dir = tmp_path / "owner-profiles"
    profiles = object()
    grant = SimpleNamespace(execution_profile_id="python-quick")
    monkeypatch.setattr(execution_assembly, "load_python_quick_grant",
                        lambda _selected: (profiles, grant))
    monkeypatch.setattr(HardQuotaVolumeProvider,
                        "_installed_helper", staticmethod(lambda: True))

    class Broker:
        def __init__(self, selected: object, **_kwargs: object) -> None:
            assert selected is profiles

    class Coordinator:
        def __init__(self, selected_ledger: Ledger, selected_profiles: object,
                     selected_broker: Broker, **_kwargs: object) -> None:
            assert selected_ledger is ledger and selected_profiles is profiles
            assert isinstance(selected_broker, Broker)

    class Volumes:
        @staticmethod
        def _installed_helper() -> bool:
            return True

        def __init__(self, selected_ledger: Ledger, selected_profiles: object,
                     selected_preparation: Coordinator, selected_grant: object,
                     **_kwargs: object) -> None:
            assert selected_ledger is ledger and selected_profiles is profiles
            assert isinstance(selected_preparation, Coordinator)
            assert selected_grant is grant

        def readiness(self) -> SimpleNamespace:
            return SimpleNamespace(available=True, code="READY")

        def volume_for_run(self, request_id: str) -> Path:
            return paths.state_dir / request_id

        def preparation_for_run(self, request_id: str, profile_id: str) -> tuple[str, str]:
            return request_id, profile_id

    class Runs:
        def __init__(self, selected_ledger: Ledger, _registry: object,
                     selected_profiles: object, selected_broker: Broker,
                     _evidence: object, **_kwargs: object) -> None:
            assert selected_ledger is ledger and selected_profiles is profiles
            assert isinstance(selected_broker, Broker)

    monkeypatch.setattr(execution_assembly, "RootlessDockerBroker", Broker)
    monkeypatch.setattr(execution_assembly, "TrustedPreparationCoordinator", Coordinator)
    monkeypatch.setattr(execution_assembly, "HardQuotaVolumeProvider", Volumes)
    monkeypatch.setattr(execution_assembly, "RunService", Runs)
    ready = build_owner_execution_ports(
        paths, ledger, cast(Any, object()), cast(Any, object()), profile_dir,
    )
    assert ready.code == "READY"
    assert ready.ports is not None
    assert ready.ports.quota_ready("python-quick") is True
    assert ready.ports.quota_ready("other") is False
    assert ready.ports.volume_for_run("request") == paths.state_dir / "request"
