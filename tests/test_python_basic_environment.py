"""The shipped starter and profile pair can prepare offline pytest, then emit JUnit."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from runner_app.git.registry import SourceError
from runner_app.git.templates import _PYTHON_QUICK_WHEELS, starter_files

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "docker.io/library/python@sha256:4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111"


def test_python_starter_contains_only_hash_locked_offline_wheels() -> None:
    starter = starter_files("python-basic", "sample-project")
    lock = starter["requirements.lock"].decode("ascii")
    assert len(_PYTHON_QUICK_WHEELS) == 5
    assert "https://" not in lock and "http://" not in lock
    for name, digest in _PYTHON_QUICK_WHEELS.items():
        package, version, *_ = name.split("-")
        assert hashlib.sha256(starter[f"vendor/wheels/{name}"]).hexdigest() == digest
        assert f"{package}=={version} --hash=sha256:{digest}\n" in lock
    assert len([name for name in starter if name.startswith("vendor/wheels/")]) == 5
    assert sum(len(content) for content in starter.values()) < 4_194_304


def test_starter_refuses_tampered_packaged_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runner_app.git import templates

    source = ROOT / "runner_app/git/python_quick_wheels"
    copy = tmp_path / "python_quick_wheels"
    copy.mkdir()
    for name in _PYTHON_QUICK_WHEELS:
        (copy / name).write_bytes((source / name).read_bytes())
    first = next(iter(_PYTHON_QUICK_WHEELS))
    (copy / first).write_bytes((copy / first).read_bytes() + b"tamper")
    monkeypatch.setattr(templates, "files", lambda _name: tmp_path)
    with pytest.raises(SourceError) as raised:
        starter_files("python-basic", "sample-project")
    assert raised.value.code == "TEMPLATE_UNAVAILABLE"


def test_reference_profiles_pin_same_rootless_image_and_prepared_junit() -> None:
    prepare = json.loads((ROOT / "config/profile.python-prepare.json").read_text())
    test = json.loads((ROOT / "config/profile.python-quick.json").read_text())
    assert prepare["profile_id"] == "python-prepare"
    assert test["profile_id"] == "python-quick"
    for profile in (prepare, test):
        assert profile["image"] == IMAGE
        assert profile["network"] == "none"
        assert profile["read_only_root"] is True
        assert profile["installed"] is False and profile["approved"] is False
    assert "--no-index" in prepare["argv"]
    assert "--require-hashes" in prepare["argv"]
    assert "--target=/build" in prepare["argv"]
    assert "-r" in prepare["argv"] and "/src/requirements.lock" in prepare["argv"]
    assert "/build" in test["argv"][2]
    assert "--junitxml=/out/junit.xml" in test["argv"]
    assert any(item["id"] == "junit" and item["required"] for item in test["collect"])
