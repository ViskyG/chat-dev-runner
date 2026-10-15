from __future__ import annotations

import copy
import json

import pytest

from runner_app.domain.commands import DomainError
from runner_app.domain.project_v2 import parse_project, validate_project_shape


def project() -> dict:
    return {"protocol": "cdr-project/2", "project_id": "project-1", "tasks": {
        "test": {"language": "python", "profile_id": "python-local",
                 "steps": [{"id": "test", "phase": "test", "argv": ["python", "-m", "pytest"],
                            "cwd": ".", "timeout_seconds": 300}],
                 "outputs": [{"id": "junit", "source_root": "out", "path": "reports/junit.xml",
                              "kind": "test_result", "format": "junit", "required": True,
                              "publish": "git_if_fits"}],
                 "quality_gate": {"test_required": True, "minimum_executed": 1,
                                  "required_outputs": ["junit"]}}}}


def test_project_capture_preserves_filenames_and_is_detached():
    value = project()
    value["tasks"]["test"]["outputs"][0]["path"] = "reports/Результат.xml"
    frozen = parse_project(json.dumps(value).encode())
    assert frozen.project == value
    view = frozen.project
    view["tasks"]["test"]["steps"].clear()
    assert len(frozen.project["tasks"]["test"]["steps"]) == 1
    assert frozen.canonical_payload == json.dumps(value, sort_keys=True, ensure_ascii=False,
                                                  separators=(",", ":")).encode()


@pytest.mark.parametrize("path", ["../out", "/tmp/out", "a//b", "a/./b", "a\\b", "C:/out",
                                   ".git/config", "x/../../out", "reports/\x00.xml", "."])
def test_output_paths_cannot_escape_declared_root(path):
    value = project()
    value["tasks"]["test"]["outputs"][0]["path"] = path
    with pytest.raises(DomainError) as exc:
        validate_project_shape(value)
    assert exc.value.code in {"UNSAFE_PATH", "PROTECTED_PATH"}


@pytest.mark.parametrize("cwd", ["../src", "/src", "src/..", "src\\tests", ".git"])
def test_step_cwd_is_relative(cwd):
    value = project()
    value["tasks"]["test"]["steps"][0]["cwd"] = cwd
    with pytest.raises(DomainError):
        validate_project_shape(value)


@pytest.mark.parametrize("change", [
    lambda t: t["steps"].append(copy.deepcopy(t["steps"][0])),
    lambda t: t["outputs"].append(copy.deepcopy(t["outputs"][0])),
    lambda t: t["quality_gate"].update(required_outputs=["unknown"]),
    lambda t: t["steps"][0].update(phase="prepare"),
    lambda t: t["steps"][0].update(argv=["python\x00ignored"]),
])
def test_task_references_and_executable_arguments_are_validated(change):
    value = project()
    change(value["tasks"]["test"])
    with pytest.raises(DomainError) as exc:
        validate_project_shape(value)
    assert exc.value.code == "INVALID_TASK"


def test_project_parser_rejects_integral_float_and_duplicate_task_key():
    raw = json.dumps(project()).replace('"timeout_seconds": 300', '"timeout_seconds": 300.0')
    with pytest.raises(DomainError) as exc:
        parse_project(raw.encode())
    assert exc.value.code == "INVALID_JSON"
    with pytest.raises(DomainError) as exc:
        parse_project(b'{"tasks":{"test":{},"test":{}}}')
    assert exc.value.code == "DUPLICATE_JSON_KEY"
