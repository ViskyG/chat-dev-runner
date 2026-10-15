from __future__ import annotations

import json
import sys
from pathlib import Path
from threading import Event

import pytest

from runner_app.domain.project_v2 import parse_project
from runner_app.executors.native_v2 import execute_native
from runner_app.executors.task_plan_v2 import PlanError, plan_task, standard_project


def executable(path: Path) -> Path:
    path.write_bytes(b"tool fixture, not executed")
    path.chmod(0o700)
    return path


def roots(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    return workspace, tmp_path / "artifacts"


@pytest.mark.skipif(sys.platform != "linux", reason="Actual Linux environment/backup boundary")
def test_prepared_venv_keeps_its_interpreters_inside_backup_state(tmp_path):
    workspace, out = roots(tmp_path)
    out.mkdir(mode=0o700)
    plan = plan_task(standard_project("example", "python"), "test", workspace=workspace, out=out,
                     tool_paths={"python": Path(sys.executable)}, timeout_budget_seconds=500)
    prepare = plan.steps[0]
    result = execute_native(list(prepare.argv), cwd=prepare.cwd, env=plan.environment,
                            output_dir=tmp_path / "capture", artifact_dir=out,
                            timeout_seconds=30, cancel=Event(), max_log_bytes=16384)
    assert result.outcome == "PASSED" and result.cleanup_verified
    interpreter = out / ".environment/python/bin/python"
    assert interpreter.is_file() and not interpreter.is_symlink()
    for path in interpreter.parent.iterdir():
        assert not path.is_symlink() or not path.readlink().is_absolute()


def test_python_venv_is_a_finite_preparation_plan_not_a_side_effect(tmp_path):
    workspace, out = roots(tmp_path)
    python = executable(tmp_path / "python")
    project = standard_project("example", "python", requirements_file="requirements.txt")
    (workspace / "requirements.txt").write_text("pytest==9.1.1\n")
    plan = plan_task(project, "test", workspace=workspace, out=out,
                     tool_paths={"python": python}, timeout_budget_seconds=800,
                     run_id="run-1", source_sha="a" * 40, tool_versions={"python": "3.11.0"})
    assert not out.exists()
    assert plan.steps[0].argv == (str(python), "-m", "venv", "--copies", str(out / ".environment/python"))
    assert plan.steps[1].argv[0] == str(out / ".environment/python/bin/python")
    assert plan.steps[2].argv[-2:] == ("--junitxml", str(out / "junit.xml"))
    assert all(step.cwd == workspace for step in plan.steps)
    assert plan.preparation_required
    assert plan.environment["CDR_RUN_ID"] == "run-1"
    assert plan.environment["CDR_OUTPUT_DIR"] == str(out)
    assert "requirements.txt" in plan.lock_hashes
    assert next(tool for tool in plan.tools if tool.id == "python").version == "3.11.0"
    assert next(tool for tool in plan.tools if tool.id == "venv_python").version_status == "NOT_PROBED"


def test_whole_tokens_preserve_unicode_filenames_and_reject_embedded_interpolation(tmp_path):
    workspace, out = roots(tmp_path)
    python = executable(tmp_path / "python")
    value = standard_project("example", "python").project
    task = value["tasks"]["test"]
    task["steps"][0]["argv"] = ["{python}", "-m", "pytest", "{workspace}", "{out}", "{output:junit}"]
    task["outputs"][0]["path"] = "Результат.xml"
    plan = plan_task(value, "test", workspace=workspace, out=out, tool_paths={"python": python},
                     timeout_budget_seconds=300)
    assert plan.steps[0].argv[-3:] == (str(workspace), str(out), str(out / "Результат.xml"))
    for bad in ("--junitxml={output:junit}", "{unknown}", "{output:missing}", "pre{out}post"):
        task["steps"][0]["argv"][-1] = bad
        with pytest.raises(PlanError):
            plan_task(value, "test", workspace=workspace, out=out, tool_paths={"python": python},
                      timeout_budget_seconds=300)


@pytest.mark.parametrize("language", ["javascript", "typescript"])
def test_npm_local_tools_are_deferred_behind_finite_ci_without_npx(tmp_path, language):
    workspace, out = roots(tmp_path)
    (workspace / "package.json").write_text('{}')
    (workspace / "package-lock.json").write_text('{"lockfileVersion":3}')
    node = executable(tmp_path / "node")
    npm_cli = tmp_path / "npm-cli.js"
    npm_cli.write_text("fixture")
    project = standard_project("example", language)
    plan = plan_task(project, "test", workspace=workspace, out=out,
                     tool_paths={"node": node, "npm": (node, npm_cli)}, timeout_budget_seconds=1000)
    assert plan.steps[0].argv == (str(node), str(npm_cli), "ci")
    assert plan.steps[-1].argv[:2] == (str(node), str(workspace / "node_modules/vitest/vitest.mjs"))
    if language == "typescript":
        assert plan.steps[1].phase == "typecheck"
        assert plan.steps[1].argv[-1] == "--noEmit"
    assert all("npx" not in arg for step in plan.steps for arg in step.argv)
    assert next(tool for tool in plan.tools if tool.id == "vitest").availability == "AFTER_PREPARATION"
    assert not (workspace / "node_modules").exists()


def test_npm_monorepo_binding_uses_selected_module_root(tmp_path):
    workspace, out = roots(tmp_path)
    module = workspace / "packages/web"
    module.mkdir(parents=True)
    (module / "package.json").write_text('{}')
    (module / "package-lock.json").write_text('{"lockfileVersion":3}')
    node = executable(tmp_path / "node")
    npm = executable(tmp_path / "npm")
    plan = plan_task(standard_project("example", "typescript", module_root="packages/web"), "test",
                     workspace=workspace, out=out, tool_paths={"node": node, "npm": npm},
                     timeout_budget_seconds=1000)
    assert plan.steps[-1].argv[1] == str(module / "node_modules/vitest/vitest.mjs")
    assert all(step.cwd == module for step in plan.steps)


def test_missing_ambiguous_lock_or_preparation_order_is_explicit(tmp_path):
    workspace, out = roots(tmp_path)
    node = executable(tmp_path / "node")
    npm = executable(tmp_path / "npm")
    tools = {"node": node, "npm": npm}
    project = standard_project("example", "javascript")
    with pytest.raises(PlanError) as exc:
        plan_task(project, "test", workspace=workspace, out=out, tool_paths=tools,
                  timeout_budget_seconds=1000)
    assert exc.value.code == "PREPARATION_REQUIRED"
    (workspace / "package-lock.json").write_text('{}')
    (workspace / "npm-shrinkwrap.json").write_text('{}')
    with pytest.raises(PlanError):
        plan_task(project, "test", workspace=workspace, out=out, tool_paths=tools,
                  timeout_budget_seconds=1000)
    (workspace / "npm-shrinkwrap.json").unlink()
    value = project.project
    value["tasks"]["test"]["steps"].reverse()
    with pytest.raises(PlanError) as exc:
        plan_task(value, "test", workspace=workspace, out=out, tool_paths=tools,
                  timeout_budget_seconds=1000)
    assert exc.value.code == "PREPARATION_ORDER"


def test_maven_wrapper_is_explicit_hashed_and_uses_a_run_local_repository(tmp_path):
    workspace, out = roots(tmp_path)
    wrapper = executable(workspace / "mvnw")
    (workspace / "pom.xml").write_text('<project/>')
    plan = plan_task(standard_project("example", "java", maven_integration=True), "test",
                     workspace=workspace, out=out, tool_paths={"maven": wrapper},
                     timeout_budget_seconds=300)
    assert plan.steps[0].argv[:2] == (str(wrapper), f"-Dmaven.repo.local={out / '.cache/maven'}")
    assert plan.steps[0].argv[-1] == "verify"
    assert {output.format for output in plan.outputs} == {"surefire-xml", "failsafe-xml"}
    assert "mvnw" in plan.lock_hashes and "pom.xml" in plan.lock_hashes


def test_dotnet_requires_explicit_test_host_framework_and_matching_sdk_pin(tmp_path):
    workspace, out = roots(tmp_path)
    dotnet = executable(tmp_path / "dotnet")
    with pytest.raises(PlanError):
        standard_project("example", "csharp")
    project = standard_project("example", "csharp", dotnet_test_platform="vstest",
                               target_framework="net8.0")
    (workspace / "global.json").write_text('{"sdk":{"version":"8.0.403"}}')
    plan = plan_task(project, "test", workspace=workspace, out=out, tool_paths={"dotnet": dotnet},
                     tool_versions={"dotnet": "8.0.403"}, timeout_budget_seconds=600)
    assert "trx;LogFileName=net8.0.trx" in plan.steps[1].argv
    assert plan.outputs[0].path == out / "net8.0.trx"
    with pytest.raises(PlanError) as exc:
        plan_task(project, "test", workspace=workspace, out=out, tool_paths={"dotnet": dotnet},
                  tool_versions={"dotnet": "9.0.100"}, timeout_budget_seconds=600)
    assert exc.value.code == "TOOLCHAIN_UNAVAILABLE"
    with pytest.raises(PlanError):
        standard_project("example", "csharp", dotnet_test_platform="mtp", target_framework="net8.0")
    explicit = ["{dotnet}", "test", "--framework", "net8.0", "--", "--report-trx"]
    mtp = standard_project("example", "csharp", dotnet_test_platform="mtp", target_framework="net8.0",
                           mtp_test_argv=explicit)
    assert mtp.project["tasks"]["test"]["steps"][1]["argv"] == explicit


def test_budget_unselected_executable_symlink_escape_and_npx_fail_before_execution(tmp_path):
    workspace, out = roots(tmp_path)
    python = executable(tmp_path / "python")
    project = standard_project("example", "python")
    with pytest.raises(PlanError) as exc:
        plan_task(project, "test", workspace=workspace, out=out, tool_paths={"python": python},
                  timeout_budget_seconds=419)
    assert exc.value.code == "TASK_BUDGET_EXCEEDED"
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    value = project.project
    value["tasks"]["test"]["steps"][0]["cwd"] = "escape"
    with pytest.raises(PlanError) as exc:
        plan_task(value, "test", workspace=workspace, out=out, tool_paths={"python": python},
                  timeout_budget_seconds=1000)
    assert exc.value.code == "UNSAFE_PATH"
    npx = executable(tmp_path / "npx")
    with pytest.raises(PlanError) as exc:
        plan_task(project, "test", workspace=workspace, out=out, tool_paths={"python": npx},
                  timeout_budget_seconds=1000)
    assert exc.value.code == "TOOLCHAIN_UNAVAILABLE"
    value = project.project
    value["tasks"]["test"]["steps"][0]["argv"][0] = "python"
    with pytest.raises(PlanError) as exc:
        plan_task(value, "test", workspace=workspace, out=out, tool_paths={"python": python},
                  timeout_budget_seconds=1000)
    assert exc.value.code == "UNRESOLVED_EXECUTABLE"


def test_factory_declarations_remain_schema_valid_without_modifying_a_project(tmp_path):
    for language in ("python", "javascript", "typescript", "java"):
        frozen = standard_project("example", language)
        assert parse_project(json.dumps(frozen.project).encode()).digest == frozen.digest
    assert list(tmp_path.iterdir()) == []
