"""Owner-granted execution profiles; tests only parse metadata and run no project code."""
from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from runner_app.executors.profiles import ProfileError, ProfileRegistry

REFERENCE = Path(__file__).resolve().parents[1] / "config" / "profile.python-quick.json"


def _base() -> dict[str, Any]:
    value = json.loads(REFERENCE.read_text(encoding="utf-8"))
    value["installed"] = True
    value["approved"] = True
    return value


def _store(root: Path, value: dict[str, Any] | None = None, *, raw: bytes | None = None
           ) -> tuple[ProfileRegistry, Path, str]:
    directory = root / "owner-profiles"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    path = directory / "python-quick.json"
    if raw is None:
        raw = json.dumps(value or _base(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, raw)
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    digest = hashlib.sha256(raw).hexdigest()
    return ProfileRegistry(directory, {"python-quick": digest}), path, digest


def _refusal(registry: ProfileRegistry, profile_id: str, code: str) -> None:
    with pytest.raises(ProfileError) as raised:
        registry.require(profile_id)
    assert raised.value.code == code


def test_denied_by_default_even_when_reference_profile_is_present(tmp_path: Path) -> None:
    registry = ProfileRegistry(tmp_path)
    assert registry.granted_ids == frozenset()
    assert registry.available_ids() == frozenset()
    _refusal(registry, "python-quick", "PROFILE_UNAVAILABLE")


def test_owner_grant_yields_immutable_bounded_profile(tmp_path: Path) -> None:
    registry, _, digest = _store(tmp_path)
    profile = registry.require("python-quick")
    assert profile.profile_id == "python-quick"
    assert registry.available_ids() == frozenset({"python-quick"})
    assert profile.revision_sha256 == digest
    assert profile.image_digest == "54b4fc9408ea4f5d1b1b9c63c7ef1968d46d3b927e00df8ab1f09364593f979f"
    assert profile.argv == ("python", "-m", "pytest", "-q", "--junitxml=/out/junit.xml")
    assert profile.cwd == "/src"
    assert profile.user == "0:0"
    assert profile.network == "none"
    assert profile.timeout_seconds == 600
    assert profile.mounts[0].source == "snapshot"
    assert profile.mounts[0].destination == "/src"
    assert profile.mounts[0].read_only is True
    assert tuple((m.source, m.destination) for m in profile.mounts[1:]) == (
        ("build", "/build"), ("output", "/out"), ("scratch", "/tmp")
    )
    assert tuple(output.path for output in profile.collect) == ("junit.xml", "app.jsonl")
    with pytest.raises(AttributeError):
        profile.image = "mutable"  # type: ignore[misc]


def test_explicit_virtual_mounts_and_bounded_output_glob(tmp_path: Path) -> None:
    profile = _base()
    profile["user"] = "1000:1000"
    profile["cwd"] = "/src/tests"
    profile["mounts"] = [
        {"source": "snapshot", "destination": "/src", "read_only": True},
        {"source": "build", "destination": "/build", "read_only": False},
        {"source": "output", "destination": "/out", "read_only": False},
        {"source": "scratch", "destination": "/tmp", "read_only": False},
    ]
    profile["collect"] = [
        {"id": "results", "kind": "test_result", "path": "reports/*.xml",
         "format": "junit-xml", "required": True, "upload_policy": "sanitized_only"}
    ]
    registry, _, _ = _store(tmp_path, profile)
    approved = registry.require("python-quick")
    assert approved.user == "1000:1000"
    assert approved.cwd == "/src/tests"
    assert approved.collect[0].path == "reports/*.xml"
    assert approved.collect[0].upload_policy == "sanitized_only"


def test_stock_profile_stays_unavailable_even_if_hash_is_granted(tmp_path: Path) -> None:
    raw = REFERENCE.read_bytes()
    registry, _, _ = _store(tmp_path, raw=raw)
    _refusal(registry, "python-quick", "PROFILE_UNAVAILABLE")
    assert registry.available_ids() == frozenset()


def test_owner_hash_is_immutable_and_file_mutation_revokes_grant(tmp_path: Path) -> None:
    directory = tmp_path / "owner-profiles"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    raw = json.dumps(_base(), sort_keys=True).encode()
    path = directory / "python-quick.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    grants = {"python-quick": hashlib.sha256(raw).hexdigest()}
    registry = ProfileRegistry(directory, grants)
    grants.clear()
    assert registry.require("python-quick").revision_sha256 == hashlib.sha256(raw).hexdigest()
    changed = deepcopy(_base())
    changed["network"] = "host"
    path.write_text(json.dumps(changed), encoding="utf-8")
    _refusal(registry, "python-quick", "PROFILE_CHANGED")


def test_repository_suggestion_cannot_add_or_replace_a_local_grant(tmp_path: Path) -> None:
    registry, path, _ = _store(tmp_path)
    hostile = tmp_path / "repository" / "config"
    hostile.mkdir(parents=True)
    (hostile / "python-quick.json").write_text(
        json.dumps({**_base(), "network": "host"}), encoding="utf-8"
    )
    assert registry.require("python-quick").network == "none"
    _refusal(registry, "host-shell", "PROFILE_UNAVAILABLE")
    assert path.parent != hostile


@pytest.mark.parametrize(
    "change",
    [
        {"execution_backend": "host_subprocess"},
        {"image": "docker.io/library/python:latest"},
        {"image": "python@sha256:" + "a" * 64},
        {"argv": []},
        {"argv": ["python", "bad\nargument"]},
        {"cwd": "/home/owner"},
        {"cwd": "/src/../home"},
        {"user": "root"},
        {"network": "host"},
        {"network": "bridge"},
        {"capabilities": "keep_all"},
        {"no_new_privileges": False},
        {"read_only_root": False},
        {"secrets": ["TOKEN"]},
        {"memory_mb": True},
        {"pids_limit": 0},
        {"cpu_limit": float("nan")},
        {"cpu_limit": 10 ** 400},
        {"timeout_seconds": 0},
        {"output_budget_mb": 0},
        {"mounts": [{"source": "host", "destination": "/home", "read_only": False}]},
        {"mounts": [
            {"source": "snapshot", "destination": "/src", "read_only": False},
            {"source": "build", "destination": "/build", "read_only": False},
            {"source": "output", "destination": "/out", "read_only": False},
            {"source": "scratch", "destination": "/tmp", "read_only": False},
        ]},
        {"collect": [{"id": "host", "kind": "artifact", "path": "../secrets", "format": "bin",
                      "required": True}]},
        {"collect": [{"id": "host", "kind": "artifact", "path": "/home/owner", "format": "bin",
                      "required": True}]},
        {"collect": [{"id": "host", "kind": [], "path": "file", "format": "bin",
                      "required": True}]},
        {"platform": ["linux/amd64"]},
        {"env": {"HOME": "/home/owner"}},
    ],
)
def test_granted_file_cannot_exceed_containment_policy(tmp_path: Path, change: dict[str, Any]
                                                       ) -> None:
    profile = _base()
    profile.update(change)
    registry, _, _ = _store(tmp_path, profile)
    _refusal(registry, "python-quick", "PROFILE_INVALID")


@pytest.mark.parametrize("field", ["approved", "installed"])
def test_approval_and_installation_are_separate_gates(tmp_path: Path, field: str) -> None:
    profile = _base()
    profile[field] = False
    registry, _, _ = _store(tmp_path, profile)
    _refusal(registry, "python-quick", "PROFILE_UNAVAILABLE")


def test_private_file_mode_symlink_and_hardlink_are_required(tmp_path: Path) -> None:
    registry, path, digest = _store(tmp_path)
    path.chmod(0o644)
    _refusal(registry, "python-quick", "PROFILE_UNAVAILABLE")
    path.chmod(0o600)
    second = tmp_path / "second"
    os.link(path, second)
    _refusal(registry, "python-quick", "PROFILE_UNAVAILABLE")
    second.unlink()
    real = tmp_path / "real"
    path.rename(real)
    path.symlink_to(real)
    _refusal(registry, "python-quick", "PROFILE_UNAVAILABLE")
    assert digest == hashlib.sha256(real.read_bytes()).hexdigest()


def test_symlinked_profile_directory_is_not_trusted(tmp_path: Path) -> None:
    registry, path, digest = _store(tmp_path)
    link = tmp_path / "linked"
    link.symlink_to(path.parent, target_is_directory=True)
    linked_registry = ProfileRegistry(link, {"python-quick": digest})
    _refusal(linked_registry, "python-quick", "PROFILE_UNAVAILABLE")
    assert registry.require("python-quick").profile_id == "python-quick"


def test_invalid_grant_and_path_fail_before_file_io(tmp_path: Path) -> None:
    with pytest.raises(ProfileError) as raised:
        ProfileRegistry(Path("relative"), {"python-quick": "a" * 64})
    assert raised.value.code == "PROFILE_CONFIG_UNSAFE"
    with pytest.raises(ProfileError) as raised:
        ProfileRegistry(tmp_path, {"../host": "a" * 64})
    assert raised.value.code == "PROFILE_CONFIG_UNSAFE"
    with pytest.raises(ProfileError) as raised:
        ProfileRegistry(tmp_path, {"python-quick": "not-a-hash"})
    assert raised.value.code == "PROFILE_CONFIG_UNSAFE"


def test_duplicate_json_keys_and_invalid_unicode_are_refused(tmp_path: Path) -> None:
    source = _base()
    raw = json.dumps(source, separators=(",", ":")).encode()
    duplicate = raw.replace(b'"profile_id":"python-quick",',
                            b'"profile_id":"python-quick","profile_id":"python-quick",')
    registry, _, _ = _store(tmp_path, raw=duplicate)
    _refusal(registry, "python-quick", "PROFILE_INVALID")

    second_root = tmp_path / "second-root"
    second_root.mkdir()
    source["argv"] = ["python", "\ud800"]
    second_registry, _, _ = _store(second_root, source)
    _refusal(second_registry, "python-quick", "PROFILE_INVALID")


def test_oversized_json_integer_is_a_profile_refusal(tmp_path: Path) -> None:
    raw = json.dumps(_base(), separators=(",", ":")).encode()
    assert b'"cpu_limit":1,' in raw
    hostile = raw.replace(b'"cpu_limit":1,', b'"cpu_limit":' + b"9" * 5000 + b",")
    registry, _, _ = _store(tmp_path, raw=hostile)
    _refusal(registry, "python-quick", "PROFILE_INVALID")
