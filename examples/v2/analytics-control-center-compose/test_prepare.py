"""Focused construction checks; fake release metadata, no Docker or app execution."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from runner_app.domain.contracts_v2 import validate_schema
from runner_app.executors.compose_v2 import _source_config, load_compose_profiles

SPEC = importlib.util.spec_from_file_location("analytics_prepare", Path(__file__).with_name("prepare.py"))
assert SPEC is not None and SPEC.loader is not None
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


def manifest(tmp_path: Path) -> Path:
    result = tmp_path / "release.json"
    result.write_text(json.dumps({
        "schemaVersion": 1, "targetPlatform": "linux/amd64", "releaseVersion": "1.2.3",
        "source": {"repository": "https://example.invalid/application", "revision": "a" * 40,
                   "tag": "v1.2.3"},
        "images": {key: {"reference": f"example.invalid/app@sha256:{digit * 64}",
                         "digest": "sha256:" + digit * 64, "target": target}
                   for key, target, digit in (("runtime", "runtime", "1"),
                                              ("uiContract", "ui-contract", "2"))}}))
    return result


def test_generated_project_passes_real_runner_boundaries(tmp_path: Path) -> None:
    project, profiles = tmp_path / "project", tmp_path / "profiles"
    result = prepare.prepare(manifest(tmp_path), project, profiles, "analytics-test", "analytics-cc-ui")
    declaration = json.loads((project / ".runner/project.json").read_bytes())
    validate_schema(declaration, "v2-project")
    profile = load_compose_profiles(profiles)["analytics-cc-ui"]
    source = _source_config(SimpleNamespace(path=project), profile)
    assert source["services"]["ui-contract"]["command"] == declaration["tasks"]["test"]["steps"][0]["argv"]
    assert set(source["services"]) == {"control-center", "ui-contract"}
    assert result["execution_timeout_seconds"] == (
        profile.timeout_seconds + profile.readiness_seconds + 2 * profile.cleanup_seconds + 120)
    assert len(profile.allowed_images) == 2
    assert (profiles.stat().st_mode & 0o777) == 0o700
    assert ((profiles / "compose-grant.json").stat().st_mode & 0o777) == 0o600
    assert {path.name for path in project.iterdir()} == {
        "compose.json", "run-ui-contract.cjs", "release-provenance.json", ".runner"}


def test_refuses_existing_destination_without_changing_it(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    sentinel = project / "owner.txt"
    sentinel.write_text("keep")
    with pytest.raises(ValueError, match="must be new"):
        prepare.prepare(manifest(tmp_path), project, tmp_path / "profiles", "p", "p")
    assert sentinel.read_text() == "keep"
    assert not (tmp_path / "profiles").exists()


@pytest.mark.parametrize("damage", ["digest", "tagged", "platform", "duplicate"])
def test_rejects_bad_release_before_writing(tmp_path: Path, damage: str) -> None:
    release = manifest(tmp_path)
    value = json.loads(release.read_bytes())
    if damage == "digest":
        value["images"]["runtime"]["digest"] = "sha256:" + "9" * 64
    elif damage == "tagged":
        value["images"]["runtime"]["reference"] = "example.invalid/app:latest"
    elif damage == "platform":
        value["targetPlatform"] = "linux/arm64"
    release.write_text(json.dumps(value) if damage != "duplicate" else '{"schemaVersion":1,"schemaVersion":1}')
    with pytest.raises(ValueError):
        prepare.prepare(release, tmp_path / "project", tmp_path / "profiles", "p", "p")
    assert not (tmp_path / "project").exists()
    assert not (tmp_path / "profiles").exists()


def test_profile_cannot_be_inside_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside"):
        prepare.prepare(manifest(tmp_path), tmp_path / "project", tmp_path / "project/profiles", "p", "p")
