"""The root helper cannot select host paths or unbounded tmpfs sizes."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import stat
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

HELPER = Path(__file__).resolve().parents[1] / "scripts/cdr_volume_helper.py"
RUN_ID = "00000000-0000-4000-8000-000000000001"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cdr_volume_helper_test", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("run_id", "size_mb"),
    [
        ("../../etc", 64),
        ("AAAAAAAA-0000-4000-8000-000000000001", 64),
        (RUN_ID, 0),
        (RUN_ID, 16),
        (RUN_ID, 128),
        (RUN_ID, 1023),
        (RUN_ID, 1025),
        (RUN_ID, 4097),
    ],
)
def test_invalid_identity_or_size_refuses_before_mount(
    monkeypatch: pytest.MonkeyPatch, run_id: str, size_mb: int,
) -> None:
    helper = _module()
    monkeypatch.setattr(helper, "_base", lambda **_kwargs: pytest.fail("base touched"))
    with pytest.raises(ValueError):
        helper.allocate(run_id, size_mb)


def test_helper_broker_and_provider_share_daemon_visible_root_base() -> None:
    from runner_app.executors.broker import _ROOT_CONTROLLED_VOLUME_BASE
    from runner_app.executors.volume_provider import VOLUME_BASE

    helper = _module()
    assert helper.BASE == VOLUME_BASE == _ROOT_CONTROLLED_VOLUME_BASE
    assert helper.BASE == Path("/var/lib/chat-dev-runner/volumes")


@pytest.mark.parametrize("size_mb", [64, 256, 1024])
def test_allocation_uses_only_fixed_base_and_bounded_mount_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size_mb: int,
) -> None:
    helper = _module()
    base = tmp_path / "volumes"
    base.mkdir()
    monkeypatch.setattr(helper, "BASE", base)
    monkeypatch.setattr(helper, "_base", lambda **_kwargs: None)
    monkeypatch.setattr(helper, "_check_root_directory", lambda *_args: None)
    monkeypatch.setattr(helper, "_mounted", lambda *_args, **_kwargs: True)
    commands: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    result = helper.allocate(RUN_ID, size_mb)
    assert result == {"status": "ALLOCATED", "run_id": RUN_ID, "size_mb": size_mb,
                      "path": str(base / RUN_ID)}
    assert len(commands) == 1
    assert commands[0][:4] == ["/usr/bin/mount", "-t", "tmpfs", "-o"]
    assert commands[0][4] == f"size={size_mb}m,nr_inodes=16384,uid=1000,gid=1000,mode=0700,nosuid,nodev"
    assert commands[0][-2:] == ["cdr-run-quota", str(base / RUN_ID)]


@pytest.mark.parametrize("size_mb", [64, 256, 1024])
def test_mount_verifier_accepts_only_exact_existing_or_browser_quota(
    monkeypatch: pytest.MonkeyPatch, size_mb: int,
) -> None:
    helper = _module()
    path = SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=1000, st_gid=1000,
                                                       st_mode=stat.S_IFDIR | 0o700))
    monkeypatch.setattr(helper, "_mount_record", lambda _path: ("tmpfs", helper.SOURCE))
    monkeypatch.setattr(helper.os, "statvfs", lambda _path: SimpleNamespace(
        f_blocks=size_mb * 256, f_frsize=4096, f_files=16384))
    assert helper._mounted(path, size_mb)
    assert helper._mounted(path)
    with pytest.raises(ValueError, match="quota differs"):
        helper._mounted(path, size_mb + 1)


@pytest.mark.parametrize(("size_mb", "uid", "source"), [
    (1025, 1000, "cdr-run-quota"), (1024, 999, "cdr-run-quota"), (1024, 1000, "foreign"),
])
def test_browser_size_does_not_relax_mount_identity_or_owner(
    monkeypatch: pytest.MonkeyPatch, size_mb: int, uid: int, source: str,
) -> None:
    helper = _module()
    path = SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=uid, st_gid=1000,
                                                       st_mode=stat.S_IFDIR | 0o700))
    monkeypatch.setattr(helper, "_mount_record", lambda _path: ("tmpfs", source))
    monkeypatch.setattr(helper.os, "statvfs", lambda _path: SimpleNamespace(
        f_blocks=size_mb * 256, f_frsize=4096, f_files=16384))
    with pytest.raises(ValueError):
        helper._mounted(path)


def test_browser_slot_does_not_expand_eight_slot_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _module()
    base = tmp_path / "volumes"
    base.mkdir()
    for index in range(8):
        (base / str(index)).mkdir()
    monkeypatch.setattr(helper, "BASE", base)
    monkeypatch.setattr(helper, "_base", lambda **_kwargs: None)
    monkeypatch.setattr(helper, "_mounted", lambda _path: True)
    monkeypatch.setattr(helper.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("mount attempted"))
    with pytest.raises(ValueError, match="capacity reached"):
        helper.allocate(RUN_ID, 1024)
    assert not (base / RUN_ID).exists()


@pytest.mark.parametrize("nonempty", [False, True])
def test_browser_release_requires_verified_empty_exact_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nonempty: bool,
) -> None:
    helper = _module()
    base = tmp_path / "volumes"
    path = base / RUN_ID
    path.mkdir(parents=True)
    if nonempty:
        (path / "evidence.json").write_text("preserve")
    monkeypatch.setattr(helper, "BASE", base)
    monkeypatch.setattr(helper, "_base", lambda **_kwargs: None)
    monkeypatch.setattr(helper, "_mounted", lambda candidate: candidate == path)
    checked = []
    monkeypatch.setattr(helper, "_check_root_directory", lambda candidate, mode: checked.append((candidate, mode)))
    commands = []
    monkeypatch.setattr(helper.subprocess, "run", lambda argv, **_kwargs: commands.append(argv))
    if nonempty:
        with pytest.raises(ValueError, match="not empty"):
            helper.release(RUN_ID)
        assert (path / "evidence.json").read_text() == "preserve"
        assert commands == checked == []
    else:
        assert helper.release(RUN_ID) == {"status": "RELEASED", "run_id": RUN_ID}
        assert commands == [["/usr/bin/umount", str(path)]]
        assert checked == [(path, 0o700)]
        assert not path.exists()


def test_probe_wire_contract_and_capacity_remain_compatible(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    helper = _module()
    monkeypatch.setattr(helper.os, "geteuid", lambda: 0)
    monkeypatch.setattr(helper, "_base", lambda **_kwargs: None)
    monkeypatch.setattr(helper.sys, "argv", [str(HELPER), "probe"])
    assert helper.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "READY", "base": "/var/lib/chat-dev-runner/volumes", "max_slots": 8}


def test_installer_pin_matches_reviewed_helper_without_executing_installer() -> None:
    installer = HELPER.with_name("install_volume_helper.sh")
    text = installer.read_text()
    pin = re.search(r"^expected_sha256=([a-f0-9]{64})$", text, flags=re.MULTILINE)
    assert pin is not None
    assert pin.group(1) == hashlib.sha256(HELPER.read_bytes()).hexdigest()
    # Syntax checking does not evaluate the administrative installer.
    subprocess.run(["bash", "-n", str(installer)], check=True, timeout=5)
