"""Read-only finite task planning and initial language recipes; never install tools here."""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from runner_app.domain.contracts_v2 import canonical_bytes, validate_relative_path
from runner_app.domain.project_v2 import FrozenProject, parse_project, validate_project_shape

_TOOLS = frozenset({"python", "venv_python", "node", "npm", "maven", "dotnet", "vitest", "tsc"})
_PATHS = frozenset({"workspace", "out"})
_MAX_METADATA_BYTES = 2 * 1024 * 1024


class PlanError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ResolvedStep:
    id: str
    phase: str
    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: int


@dataclass(frozen=True)
class PlannedOutput:
    id: str
    path: Path
    source_root: str
    kind: str
    format: str
    required: bool
    publish: str


@dataclass(frozen=True)
class ToolProvenance:
    id: str
    argv_prefix: tuple[str, ...]
    version: str | None
    version_status: str
    version_probe_argv: tuple[str, ...]
    availability: str


@dataclass(frozen=True)
class TaskPlan:
    project_id: str
    task_id: str
    language: str
    profile_id: str
    project_digest: str
    task_digest: str
    steps: tuple[ResolvedStep, ...]
    outputs: tuple[PlannedOutput, ...]
    environment: Mapping[str, str]
    tools: tuple[ToolProvenance, ...]
    lock_hashes: Mapping[str, str]
    quality_gate: Mapping[str, Any]
    preparation_required: bool


def _confined(root: Path, relative: str, *, allow_root: bool = False) -> Path:
    validate_relative_path(relative, allow_root=allow_root)
    path = root if relative == "." else root / relative
    if not path.resolve().is_relative_to(root):
        raise PlanError("UNSAFE_PATH", "A task path escapes its selected run root.")
    return path


def _metadata(path: Path, root: Path) -> bytes:
    if not path.resolve().is_relative_to(root):
        raise PlanError("UNSAFE_PATH", "Project metadata escapes the frozen run workspace.")
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_METADATA_BYTES + 1)
    except OSError:
        raise PlanError("PREPARATION_REQUIRED", "Selected project metadata is unavailable.") from None
    if len(raw) > _MAX_METADATA_BYTES:
        raise PlanError("METADATA_BUDGET_EXCEEDED", "Project metadata exceeds the read budget.")
    return raw


def _tool_file(path: Path, *, executable: bool) -> None:
    if not path.is_absolute() or path.suffix.lower() in {".cmd", ".bat"}:
        raise PlanError("TOOLCHAIN_UNAVAILABLE", "Tool must have an explicit native executable path.")
    if not path.is_file() or (executable and os.name == "posix" and not os.access(path, os.X_OK)):
        raise PlanError("TOOLCHAIN_UNAVAILABLE", "A selected tool is missing or not executable.")
    if {path.name.lower(), path.resolve().name.lower()} & {"npx", "npx-cli.js", "npx.cmd"}:
        raise PlanError("TOOLCHAIN_UNAVAILABLE", "Automatic package fetching is not a tool binding.")


def plan_task(project: FrozenProject | dict[str, Any], task_id: str, *, workspace: Path,
              out: Path, tool_paths: Mapping[str, Path | tuple[Path, ...]],
              timeout_budget_seconds: int, run_id: str | None = None,
              source_sha: str | None = None,
              tool_versions: Mapping[str, str] | None = None) -> TaskPlan:
    """Resolve whole tokens without executing project code, probes, venvs or installs.

    A multi-token tool binding expands only in executable position. Missing local
    Vitest/tsc entries may be deferred only behind an explicit npm-ci prepare step.
    Version probe commands are returned for a separately supervised invocation;
    caller-supplied observed versions are preserved, never inferred from filenames.
    """
    value = project.project if isinstance(project, FrozenProject) else project
    validate_project_shape(value)
    if task_id not in value["tasks"]:
        raise PlanError("TASK_NOT_FOUND", "Selected task is not defined by this project version.")
    if (not workspace.is_absolute() or not workspace.is_dir() or workspace.resolve() != workspace
            or not out.is_absolute() or out.resolve() != out or out == workspace
            or type(timeout_budget_seconds) is not int or not 1 <= timeout_budget_seconds <= 86400):
        raise PlanError("INVALID_PLAN", "Task roots and finite execution budget must be explicit.")
    task = value["tasks"][task_id]
    outputs = tuple(PlannedOutput(
        item["id"], _confined(out if item["source_root"] == "out" else workspace, item["path"]),
        item["source_root"], item["kind"], item["format"], item["required"], item["publish"])
        for item in task["outputs"])
    output_paths = {item.id: str(item.path) for item in outputs}
    bindings: dict[str, tuple[str, ...]] = {}
    availability: dict[str, str] = {}
    for name, supplied in tool_paths.items():
        if name not in _TOOLS or name == "venv_python":
            raise PlanError("INVALID_TOOL_BINDING", "Tool binding is unknown or runner-managed.")
        paths = supplied if isinstance(supplied, tuple) else (supplied,)
        if not paths or any(not isinstance(path, Path) for path in paths):
            raise PlanError("INVALID_TOOL_BINDING", "Tool bindings contain explicit paths only.")
        for index, path in enumerate(paths):
            _tool_file(path, executable=index == 0)
        bindings[name] = tuple(str(path) for path in paths)
        availability[name] = "PRESENT"
    used: set[str] = set()
    requires_venv = any("{venv_python}" in step["argv"] for step in task["steps"])
    venv_root = out / ".environment/python"
    if requires_venv:
        if "python" not in bindings:
            raise PlanError("TOOLCHAIN_UNAVAILABLE", "Per-run venv creation needs a selected Python.")
        if venv_root.exists():
            raise PlanError("RUN_ENVIRONMENT_EXISTS", "A new run cannot reuse an existing Python venv.")
        bindings["venv_python"] = (str(venv_root / ("Scripts/python.exe" if os.name == "nt"
                                                  else "bin/python")),)
        availability["venv_python"] = "AFTER_PREPARATION"
        used.add("python")
    npm_ci = any(step["phase"] == "prepare" and step["argv"][:2] == ["{npm}", "ci"]
                 for step in task["steps"])
    package_roots = {_confined(workspace, step["cwd"], allow_root=True) for step in task["steps"]
                     if any(token in {"{npm}", "{vitest}", "{tsc}"} for token in step["argv"])}
    if len(package_roots) > 1:
        raise PlanError("AMBIGUOUS_PACKAGE_ROOT", "One task must select one npm package root.")
    package_root = next(iter(package_roots), workspace)
    for name, local in (("vitest", "node_modules/vitest/vitest.mjs"),
                        ("tsc", "node_modules/typescript/bin/tsc")):
        if not any("{" + name + "}" in step["argv"] for step in task["steps"]):
            continue
        if name in tool_paths:
            raise PlanError("INVALID_TOOL_BINDING", "Project test/compiler bindings must be local.")
        if "node" not in bindings:
            raise PlanError("TOOLCHAIN_UNAVAILABLE", "Local JavaScript tools need a selected Node.")
        local_path = _confined(package_root, local)
        if not local_path.is_file() and not npm_ci:
            raise PlanError("TOOLCHAIN_UNAVAILABLE", "Selected project-local package is not installed.")
        bindings[name] = (*bindings["node"], str(local_path))
        availability[name] = "PRESENT" if local_path.is_file() else "AFTER_PREPARATION"
        used.add("node")
    if "maven" in bindings:
        bindings["maven"] = (*bindings["maven"], f"-Dmaven.repo.local={out / '.cache/maven'}")

    def resolve(token: str, position: int) -> tuple[str, ...]:
        if "{" not in token and "}" not in token:
            return (token,)
        if token in {"{" + name + "}" for name in _PATHS}:
            return (str(workspace if token == "{workspace}" else out),)
        if token.startswith("{output:") and token.endswith("}"):
            output_id = token[8:-1]
            if output_id not in output_paths:
                raise PlanError("UNKNOWN_OUTPUT", "Argument references an undeclared output.")
            return (output_paths[output_id],)
        if token.startswith("{") and token.endswith("}") and token[1:-1] in _TOOLS:
            name = token[1:-1]
            if name not in bindings:
                raise PlanError("TOOLCHAIN_UNAVAILABLE", "A required tool has no selected binding.")
            used.add(name)
            if position != 0 and len(bindings[name]) != 1:
                raise PlanError("EMBEDDED_TOOL_PREFIX", "A tool command prefix requires argv position zero.")
            return bindings[name]
        raise PlanError("INVALID_PLACEHOLDER", "Only documented whole argument tokens are supported.")

    steps: list[ResolvedStep] = []
    if requires_venv:
        if any(step["id"] == "cdr-create-venv" for step in task["steps"]):
            raise PlanError("INVALID_PLAN", "Task step uses a reserved preparation ID.")
        steps.append(ResolvedStep("cdr-create-venv", "prepare",
                                  (*bindings["python"], "-m", "venv", "--copies", str(venv_root)),
                                  workspace, 120))
    for step in task["steps"]:
        cwd = _confined(workspace, step["cwd"], allow_root=True)
        if not cwd.is_dir():
            raise PlanError("INVALID_CWD", "Each task working directory must already exist.")
        argv = tuple(part for index, token in enumerate(step["argv"])
                     for part in resolve(token, index))
        allowed_executables = {binding[0] for binding in bindings.values()}
        if not Path(argv[0]).is_absolute() or argv[0] not in allowed_executables:
            raise PlanError("UNRESOLVED_EXECUTABLE", "Task executable must use a selected tool binding.")
        if not step["argv"][0].startswith("{"):
            direct = [name for name, binding in bindings.items() if binding == (argv[0],)]
            if not direct:
                raise PlanError("UNRESOLVED_EXECUTABLE", "Compound tool commands need their whole token.")
            used.update(direct)
        steps.append(ResolvedStep(step["id"], step["phase"], argv, cwd, step["timeout_seconds"]))
    if sum(step.timeout_seconds for step in steps) > timeout_budget_seconds:
        raise PlanError("TASK_BUDGET_EXCEEDED", "Declared phase timeouts exceed the run budget.")
    if npm_ci:
        first_install = next(i for i, step in enumerate(task["steps"])
                             if step["phase"] == "prepare" and step["argv"][:2] == ["{npm}", "ci"])
        for index, step in enumerate(task["steps"]):
            if any(token in {"{vitest}", "{tsc}"} for token in step["argv"]) and index < first_install:
                raise PlanError("PREPARATION_ORDER", "Local tools cannot run before their npm preparation.")

    lock_hashes: dict[str, str] = {}
    roots = {step.cwd for step in steps} | {workspace}
    for root in roots:
        if task["language"] in {"javascript", "typescript"}:
            locks = [root / name for name in ("package-lock.json", "npm-shrinkwrap.json")
                     if (root / name).is_file()]
            if root != package_root:
                continue
            if len(locks) != 1:
                raise PlanError("PREPARATION_REQUIRED", "The npm profile needs one unambiguous lockfile.")
            if any((root / name).is_file() for name in ("yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb")):
                raise PlanError("PREPARATION_REQUIRED", "Another package manager needs its own profile.")
            try:
                package = json.loads(_metadata(root / "package.json", workspace))
                lock = json.loads(_metadata(locks[0], workspace))
                manager = package.get("packageManager")
                if (not isinstance(package, dict) or not isinstance(lock, dict)
                        or type(lock.get("lockfileVersion")) is not int or lock["lockfileVersion"] < 1
                        or (manager is not None and (not isinstance(manager, str)
                            or not manager.startswith("npm@")))):
                    raise ValueError
            except (ValueError, AttributeError):
                raise PlanError("PREPARATION_REQUIRED", "The npm profile needs valid matching package metadata.") from None
        candidates = ("requirements.txt", "pyproject.toml", "requirements.lock", "uv.lock",
                      "poetry.lock", "package-lock.json", "npm-shrinkwrap.json", "package.json",
                      "pom.xml", "mvnw", ".mvn/wrapper/maven-wrapper.properties", "global.json",
                      "packages.lock.json")
        for name in candidates:
            path = root / name
            if path.is_file():
                lock_hashes[str(path.relative_to(workspace))] = hashlib.sha256(
                    _metadata(path, workspace)).hexdigest()
    if task["language"] == "csharp":
        pin = workspace / "global.json"
        try:
            version = json.loads(_metadata(pin, workspace))["sdk"]["version"]
            if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[\w.-]+)?", version):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise PlanError("PREPARATION_REQUIRED", "The .NET profile needs an explicit valid SDK pin.") from None
        if tool_versions and "dotnet" in tool_versions and tool_versions["dotnet"] != version:
            raise PlanError("TOOLCHAIN_UNAVAILABLE", "Selected .NET SDK differs from global.json.")
    versions = tool_versions or {}
    tools: list[ToolProvenance] = []
    for name in sorted(used):
        version = versions.get(name)
        if version is not None and (not isinstance(version, str) or not version
                                    or len(version) > 1024 or any(ord(c) < 32 for c in version)):
            raise PlanError("INVALID_VERSION", "Observed tool version must be bounded single-line text.")
        tools.append(ToolProvenance(name, bindings[name], version,
                                    "SUPPLIED" if version is not None else "NOT_PROBED",
                                    (*bindings[name], "-version" if name == "maven" else "--version"),
                                    availability[name]))
    environment = {"CDR_OUTPUT_DIR": str(out), "NPM_CONFIG_CACHE": str(out / ".cache/npm"),
                   "NUGET_PACKAGES": str(out / ".cache/nuget"),
                   "DOTNET_CLI_HOME": str(out / ".environment/dotnet"),
                   "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1"}
    for key, item in (("CDR_RUN_ID", run_id), ("CDR_SOURCE_SHA", source_sha)):
        if item is not None:
            if (not isinstance(item, str) or not item or len(item) > 128
                    or any(ord(char) < 32 for char in item)):
                raise PlanError("INVALID_ENV", "Run provenance must be bounded explicit text.")
            environment[key] = item
    if source_sha is not None and not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source_sha):
        raise PlanError("INVALID_ENV", "Source identity must be a full Git or workload digest.")
    gate = dict(task["quality_gate"])
    gate["required_outputs"] = tuple(sorted(set(gate["required_outputs"]) |
                                            {item.id for item in outputs if item.required}))
    return TaskPlan(value["project_id"], task_id, task["language"], task["profile_id"],
                    hashlib.sha256(canonical_bytes(value)).hexdigest(),
                    hashlib.sha256(canonical_bytes(task)).hexdigest(), tuple(steps), outputs,
                    MappingProxyType(environment), tuple(tools), MappingProxyType(lock_hashes),
                    MappingProxyType(gate), any(step.phase == "prepare" for step in steps))


def standard_project(project_id: str, language: str, *, task_id: str = "test",
                     module_root: str = ".", requirements_file: str | None = None,
                     dotnet_test_platform: str | None = None, target_framework: str | None = None,
                     mtp_test_argv: list[str] | None = None,
                     maven_integration: bool = False) -> FrozenProject:
    """Return a proposed validated declaration; do not edit or install in any project.

    MTP reporter switches are version-specific, so that recipe requires the exact
    enrolled test command. No migration or third-party reporter is silently added.
    """
    validate_relative_path(module_root, allow_root=True)
    steps: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []

    def step(name: str, phase: str, argv: list[str]) -> None:
        steps.append({"id": name, "phase": phase, "argv": argv, "cwd": module_root,
                      "timeout_seconds": 300})

    def output(name: str, root: str, path: str, format_name: str) -> None:
        outputs.append({"id": name, "source_root": root, "path": path, "kind": "test_result",
                        "format": format_name, "required": True, "publish": "git_if_fits"})

    if language == "python":
        if requirements_file is not None:
            validate_relative_path(requirements_file)
            step("install", "prepare", ["{venv_python}", "-m", "pip", "install", "--requirement",
                                         requirements_file])
        step("test", "test", ["{venv_python}", "-m", "pytest", "--junitxml", "{output:junit}"])
        output("junit", "out", "junit.xml", "junit-xml")
        profile = "python-native"
    elif language in {"javascript", "typescript"}:
        step("install", "prepare", ["{npm}", "ci"])
        if language == "typescript":
            step("typecheck", "typecheck", ["{tsc}", "--noEmit"])
        step("test", "test", ["{vitest}", "run", "--reporter", "junit", "--outputFile", "{output:junit}"])
        output("junit", "out", "junit.xml", "junit-xml")
        profile = "node-native"
    elif language == "java":
        step("test", "test", ["{maven}", "--batch-mode", "--no-transfer-progress",
                               "verify" if maven_integration else "test"])
        prefix = "" if module_root == "." else module_root + "/"
        output("surefire", "workspace", prefix + "target/surefire-reports", "surefire-xml")
        if maven_integration:
            output("failsafe", "workspace", prefix + "target/failsafe-reports", "failsafe-xml")
        profile = "maven-native"
    elif language == "csharp":
        if dotnet_test_platform not in {"vstest", "mtp"} or not target_framework or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}", target_framework):
            raise PlanError("EXPLICIT_TEST_PLATFORM_REQUIRED", "Select a .NET test platform and framework.")
        step("restore", "prepare", ["{dotnet}", "restore"])
        if dotnet_test_platform == "vstest":
            step("test", "test", ["{dotnet}", "test", "--no-restore", "--framework", target_framework,
                                   "--logger", f"trx;LogFileName={target_framework}.trx",
                                   "--results-directory", "{out}"])
        else:
            if not mtp_test_argv or mtp_test_argv[0] != "{dotnet}":
                raise PlanError("EXPLICIT_TEST_PLATFORM_REQUIRED", "MTP needs an enrolled exact reporter recipe.")
            step("test", "test", list(mtp_test_argv))
        output("trx", "out", f"{target_framework}.trx", "trx")
        profile = f"dotnet-{dotnet_test_platform}-native"
    else:
        raise PlanError("UNQUALIFIED_LANGUAGE", "No initial profile exists for this language.")
    declaration = {"protocol": "cdr-project/2", "project_id": project_id, "tasks": {
        task_id: {"language": language, "profile_id": profile, "steps": steps, "outputs": outputs,
                  "quality_gate": {"test_required": True, "minimum_executed": 1,
                                   "required_outputs": [item["id"] for item in outputs]}}}}
    return parse_project(canonical_bytes(declaration))
