"""The root helper cannot select host paths or unbounded tmpfs sizes."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType
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


def test_allocation_uses_only_fixed_base_and_bounded_mount_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
    result = helper.allocate(RUN_ID, 64)
    assert result == {"status": "ALLOCATED", "run_id": RUN_ID, "size_mb": 64,
                      "path": str(base / RUN_ID)}
    assert len(commands) == 1
    assert commands[0][:4] == ["/usr/bin/mount", "-t", "tmpfs", "-o"]
    assert "size=64m" in commands[0][4]
    assert "nosuid,nodev" in commands[0][4]
    assert commands[0][-2:] == ["cdr-run-quota", str(base / RUN_ID)]
