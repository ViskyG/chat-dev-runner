"""EP-07 user service rendering and fake systemctl lifecycle; no host service writes."""
from __future__ import annotations

import sys
from importlib.resources import files
from pathlib import Path

import pytest

from runner_app import service_install
from runner_app.cli import parser
from runner_app.service_install import (
    ServiceInstallError,
    dry_run,
    install_user,
    render_plan,
    status_user,
    stop_user,
    uninstall_user,
)
from runner_app.storage.paths import Paths

ROOT = Path(__file__).resolve().parents[1]


class FakeSystemctl:
    def __init__(self) -> None:
        self.active = False
        self.enabled = False
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *arguments: str) -> int:
        self.calls.append(arguments)
        command = arguments[0]
        if command == "is-active":
            return 0 if self.active else 3
        if command == "is-enabled":
            return 0 if self.enabled else 1
        if command == "daemon-reload":
            return 0
        if command == "enable":
            self.enabled = True
            return 0
        if command == "start":
            self.active = True
            return 0
        if command == "stop":
            self.active = False
            return 0
        if command == "disable":
            self.enabled = False
            return 0
        raise AssertionError(arguments)


@pytest.fixture(autouse=True)
def no_loaded_foreign_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_install, "_fragment_path", lambda: None)


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config" / "chat-dev-runner",
        state_dir=tmp_path / "data" / "chat-dev-runner",
        cache_dir=tmp_path / "cache" / "chat-dev-runner",
        runtime_dir=tmp_path / "runtime" / "chat-dev-runner",
    )


def test_serve_auto_arms_by_default_with_explicit_unarmed_override() -> None:
    assert parser().parse_args(["serve"]).start_unarmed is False
    assert parser().parse_args(["serve", "--start-unarmed"]).start_unarmed is True


@pytest.mark.parametrize("unarmed", [False, True])
def test_serve_waits_for_v2_recovery_before_arming(monkeypatch, unarmed):
    from types import SimpleNamespace

    from runner_app import cli

    events = []
    callbacks = []
    runtime = SimpleNamespace(
        integrations=None, app=object(),
        v2=SimpleNamespace(start=lambda *, on_recovered: callbacks.append(on_recovered)),
        arm_on_startup=lambda: events.append("armed"), close=lambda: events.append("closed"))
    monkeypatch.setattr("runner_app.engine.runtime.Runtime", lambda *_a: runtime)

    def serve(*_a, **_kw):
        assert events == [] and len(callbacks) == 1
        events.append("recovery-finished")
        if callbacks[0] is not None:
            callbacks[0]()
    monkeypatch.setattr("uvicorn.run", serve)
    assert cli.main(["serve", *(["--start-unarmed"] if unarmed else [])]) == 0
    assert events == ["recovery-finished", *([] if unarmed else ["armed"]), "closed"]


def test_packaged_template_matches_reviewed_source_and_is_loopback_auto_armed(paths: Paths) -> None:
    assert files("runner_app").joinpath("production.service.in").read_text() == (
        ROOT / "deploy" / "production.service.in"
    ).read_text()
    plan = render_plan(paths, executable=Path(sys.executable), port=9876)
    assert plan.unit_path == paths.config_dir.parent / "systemd/user/chat-dev-runner.service"
    assert plan.argv == [sys.executable, "-m", "runner_app.cli", "serve", "--host",
                         "127.0.0.1", "--port", "9876"]
    assert f"WorkingDirectory={paths.state_dir}" in plan.text
    assert f'ExecStart="{sys.executable}" -m runner_app.cli serve --host 127.0.0.1 ' in plan.text
    assert 'Environment="XDG_CONFIG_HOME=' in plan.text
    assert 'Environment="XDG_DATA_HOME=' in plan.text
    assert 'Environment="XDG_CACHE_HOME=' in plan.text
    assert 'Environment="XDG_RUNTIME_DIR=' in plan.text
    assert "NoNewPrivileges=no" in plan.text
    assert "no-new-privileges:true" in plan.text
    assert "0.0.0.0" not in plan.text
    assert "--start-unarmed" not in plan.text
    assert "google-oauth-client" not in plan.text
    assert "github-token" not in plan.text
    assert "@" not in plan.text


def test_dry_run_is_pure_and_discloses_live_start_state_write(paths: Paths) -> None:
    plan = render_plan(paths)
    assert not plan.unit_path.parent.exists()
    report = dry_run(plan)
    assert report["runtime_package"]["status"] in {"WHEEL", "EDITABLE_SOURCE", "MISSING"}
    assert report["installable"] is (report["runtime_package"]["status"] == "WHEEL")
    assert report["unit_state"] == "ABSENT"
    assert report["service_changes"] is False
    assert report["cloud_writes"] is False
    assert report["would_start_armed"] is None
    assert report["startup_arm_policy"] == "CONFIGURED_RUNTIME"
    assert report["live_start_may_publish_system_state"] is True
    assert report["ready_mailbox_polling_while_disarmed"] is False
    assert not plan.unit_path.parent.exists()
    assert not paths.state_dir.exists()


def test_install_is_idempotent_and_uninstall_preserves_state(
    paths: Paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSystemctl()
    monkeypatch.setattr(service_install, "_systemctl", fake)
    plan = render_plan(paths)
    first = install_user(plan)
    assert first["unit_changed"] is True and first["started"] is True
    assert first["startup_arm_policy"] == "CONFIGURED_RUNTIME"
    assert plan.unit_path.read_text() == plan.text
    assert plan.unit_path.stat().st_mode & 0o777 == 0o600
    assert paths.state_dir.stat().st_mode & 0o777 == 0o700
    assert ("daemon-reload",) in fake.calls
    assert ("enable", service_install.UNIT_NAME) in fake.calls
    assert ("start", service_install.UNIT_NAME) in fake.calls
    inode = plan.unit_path.stat().st_ino
    fake.calls.clear()
    second = install_user(plan)
    assert second["unit_changed"] is False and second["started"] is False
    assert plan.unit_path.stat().st_ino == inode
    assert fake.calls == [
        ("is-active", "--quiet", service_install.UNIT_NAME),
        ("is-enabled", "--quiet", service_install.UNIT_NAME),
    ]
    status = status_user(plan)
    assert status["installed"] is True and status["active"] is True
    sentinel = paths.state_dir / "owner-state"
    sentinel.write_text("preserve")
    stopped = stop_user(plan)
    assert stopped["stopped"] is True and fake.active is False
    result = uninstall_user(plan)
    assert result["unit_removed"] is True
    assert result["state_preserved"] is True
    assert not plan.unit_path.exists()
    assert sentinel.read_text() == "preserve"
    assert fake.enabled is False
    assert status_user(plan)["installed"] is False


def test_live_upgrade_requires_explicit_stop(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSystemctl()
    monkeypatch.setattr(service_install, "_systemctl", fake)
    first = render_plan(paths, port=8765)
    install_user(first)
    with pytest.raises(ServiceInstallError, match="ACTIVE_SERVICE_UPGRADE_REQUIRES_STOP"):
        install_user(render_plan(paths, port=8766))
    assert first.unit_path.read_text() == first.text
    stop_user(first)
    changed = install_user(render_plan(paths, port=8766))
    assert changed["unit_changed"] is True
    assert "--port 8766" in first.unit_path.read_text()
    assert "--start-unarmed" not in first.unit_path.read_text()


def test_foreign_unit_and_symlink_are_never_overwritten(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSystemctl()
    monkeypatch.setattr(service_install, "_systemctl", fake)
    plan = render_plan(paths)
    plan.unit_path.parent.mkdir(parents=True)
    plan.unit_path.parent.chmod(0o700)
    plan.unit_path.write_text("[Service]\nExecStart=/bin/true\n")
    plan.unit_path.chmod(0o600)
    assert dry_run(plan)["installable"] is False
    with pytest.raises(ServiceInstallError, match="UNMANAGED_UNIT_EXISTS"):
        install_user(plan)
    with pytest.raises(ServiceInstallError, match="UNMANAGED_UNIT_EXISTS"):
        uninstall_user(plan)
    assert fake.calls == []
    plan.unit_path.unlink()
    external = tmp_path / "other-unit"
    external.write_text("do not touch")
    plan.unit_path.symlink_to(external)
    assert dry_run(plan)["installable"] is False
    with pytest.raises(OSError):
        install_user(plan)
    assert external.read_text() == "do not touch"
    assert fake.calls == []


def test_systemd_escaping_and_no_shell_for_paths_with_space_or_percent(tmp_path: Path) -> None:
    root = tmp_path / "path with space%"
    paths = Paths(root / "config/chat-dev-runner", root / "data/chat-dev-runner",
                  root / "cache/chat-dev-runner")
    plan = render_plan(paths)
    assert "WorkingDirectory=" in plan.text
    assert "path\\x20with\\x20space%%" in plan.text
    assert "path with space%%" in plan.text
    assert "Environment=\"XDG_RUNTIME_DIR=" not in plan.text
    assert "/bin/sh" not in plan.text
    assert "ExecStart=\"" in plan.text
    assert "--host 127.0.0.1" in plan.text


def test_missing_unit_stop_is_idempotent_and_does_not_call_systemctl(
    paths: Paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSystemctl()
    monkeypatch.setattr(service_install, "_systemctl", fake)
    plan = render_plan(paths)
    assert stop_user(plan)["installed"] is False
    assert fake.calls == []
    assert not paths.state_dir.exists()


def test_nonloopback_cannot_be_rendered(paths: Paths) -> None:
    with pytest.raises(TypeError):
        render_plan(paths, host="0.0.0.0")  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        render_plan(paths, port=0)
    assert not paths.state_dir.exists()

@pytest.mark.parametrize("failed_stage", ["daemon-reload", "enable", "start"])
def test_partial_install_keeps_exact_unit_and_can_be_retried(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, failed_stage: str,
) -> None:
    fake = FakeSystemctl()
    attempts = 0

    def fail_once(*arguments: str) -> int:
        nonlocal attempts
        if arguments[0] == failed_stage and attempts == 0:
            attempts += 1
            fake.calls.append(arguments)
            return 1
        return fake(*arguments)

    monkeypatch.setattr(service_install, "_systemctl", fail_once)
    plan = render_plan(paths)
    with pytest.raises(ServiceInstallError) as raised:
        install_user(plan)
    error = raised.value
    stage_code = "RELOAD" if failed_stage == "daemon-reload" else failed_stage.upper()
    assert error.code == f"SERVICE_INSTALL_{stage_code}_FAILED"
    assert error.details["unit_path"] == str(plan.unit_path)
    assert error.details["recovery"] == "RUN_STATUS_THEN_RETRY_INSTALL"
    assert error.details["unit_file_written"] is True
    assert plan.unit_path.read_text() == plan.text
    assert fake.active is False
    if failed_stage in {"enable", "start"}:
        assert error.details["disabled_after_failure"] is True
        assert fake.enabled is False
    monkeypatch.setattr(service_install, "_systemctl", fake)
    retry = install_user(plan)
    assert retry["unit_changed"] is False
    assert fake.active is True and fake.enabled is True
    if failed_stage == "daemon-reload":
        assert fake.calls.count(("daemon-reload",)) >= 2


def test_existing_managed_unit_refuses_lost_state_root(
    paths: Paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSystemctl()
    monkeypatch.setattr(service_install, "_systemctl", fake)
    plan = render_plan(paths)
    install_user(plan)
    stop_user(plan)
    displaced = paths.state_dir.with_name("displaced-state")
    paths.state_dir.rename(displaced)
    fake.calls.clear()
    with pytest.raises(ServiceInstallError, match="SERVICE_STATE_ROOT_MISSING"):
        install_user(plan)
    assert not paths.state_dir.exists()
    assert displaced.is_dir()
    assert plan.unit_path.read_text() == plan.text
    assert fake.calls == []


def test_foreign_loaded_fragment_refuses_all_lifecycle_mutations(
    paths: Paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSystemctl()
    monkeypatch.setattr(service_install, "_systemctl", fake)
    monkeypatch.setattr(service_install, "_fragment_path",
                        lambda: "/etc/systemd/user/chat-dev-runner.service")
    plan = render_plan(paths)
    for action in (install_user, stop_user, uninstall_user):
        with pytest.raises(ServiceInstallError, match="FOREIGN_SERVICE_FRAGMENT"):
            action(plan)
    assert fake.calls == []
    assert not plan.unit_path.exists()
    assert not paths.state_dir.exists()


def test_lifecycle_lock_refuses_overlapping_install(
    paths: Paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSystemctl()
    monkeypatch.setattr(service_install, "_systemctl", fake)
    plan = render_plan(paths)
    with (service_install._lifecycle_lock(plan),
          pytest.raises(ServiceInstallError, match="SERVICE_LIFECYCLE_BUSY")):
        install_user(plan)
    assert fake.calls == []
    assert not plan.unit_path.exists()


def test_failed_disable_reports_uncertain_login_effect(
    paths: Paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSystemctl()

    def fail_enable_and_disable(*arguments: str) -> int:
        if arguments[0] in {"enable", "disable"}:
            fake.calls.append(arguments)
            return 1
        return fake(*arguments)

    monkeypatch.setattr(service_install, "_systemctl", fail_enable_and_disable)
    plan = render_plan(paths)
    with pytest.raises(ServiceInstallError) as raised:
        install_user(plan)
    assert raised.value.code == "SERVICE_INSTALL_ENABLE_PARTIAL_UNCERTAIN"
    assert raised.value.details["disabled_after_failure"] is False
    assert raised.value.details["recovery"] == "RUN_STATUS_THEN_RETRY_INSTALL"
    assert plan.unit_path.read_text() == plan.text
    assert fake.calls[-1] == ("disable", service_install.UNIT_NAME)
