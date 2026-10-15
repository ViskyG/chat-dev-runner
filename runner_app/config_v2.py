"""Explicit owner-local enrollment for the additive v2 runtime.

Loading is offline. Repository identities are bindings, not repository-creation
instructions. Secret paths are references only; credentials are read by their
transport when it actually performs an authorized request.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runner_app.adapters.github import GitHubRepository
from runner_app.domain.commands import DomainError
from runner_app.domain.contracts_v2 import parse_contract_json, validate_relative_path
from runner_app.git.argv import GitError, validate_branch
from runner_app.storage.paths import Paths, UnsafePathError, open_private_file

_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")


class V2ConfigError(ValueError):
    def __init__(self, code: str = "V2_CONFIG_INVALID") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class NativeProfile:
    tools: Mapping[str, tuple[Path, ...]]
    shell_dialects: tuple[str, ...]
    environment: Mapping[str, str]
    timeout_seconds: int
    max_log_bytes: int


@dataclass(frozen=True)
class WorkspaceConfig:
    path: Path
    include_dirty: bool
    include_untracked: tuple[str, ...]


@dataclass(frozen=True)
class ProjectConfig:
    project_id: str
    repository: GitHubRepository | None
    workspaces: Mapping[str, WorkspaceConfig]
    native_profiles: tuple[str, ...]
    container_profiles: tuple[str, ...] = ()


@dataclass(frozen=True)
class MailboxConfig:
    binding_id: str
    provider: str
    project_id: str
    coordinates: Mapping[str, Any]


@dataclass(frozen=True)
class V2RuntimeConfig:
    runner_id: str
    enable_v1: bool
    github_token_file: Path | None
    google_config_file: Path | None
    projects: Mapping[str, ProjectConfig]
    native_profiles: Mapping[str, NativeProfile]
    mailboxes: tuple[MailboxConfig, ...]
    queue_limit: int
    poll_seconds: int
    rootless_profile_dir: Path | None = None


def _object(value: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if (not isinstance(value, dict) or not required <= value.keys()
            or value.keys() - required - (optional or set())):
        raise V2ConfigError()
    return value


def _id(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise V2ConfigError()
    return value


def _path(value: Any) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise V2ConfigError()
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise V2ConfigError()
    return path


def _integer(value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise V2ConfigError()
    return value


def _strings(value: Any) -> tuple[str, ...]:
    if (not isinstance(value, list) or len(value) > 128
            or any(not isinstance(item, str) or "\x00" in item for item in value)
            or len(set(value)) != len(value)):
        raise V2ConfigError()
    return tuple(value)


def parse_v2_config(raw: bytes) -> V2RuntimeConfig:
    try:
        value = _object(parse_contract_json(raw), {"protocol", "runner_id", "projects", "mailboxes"},
                        {"enable_v1", "github_token_file", "google_config_file", "native_profiles",
                         "queue_limit", "poll_seconds", "rootless_profile_dir"})
        if value["protocol"] != "cdr-runtime/2" or type(value.get("enable_v1", False)) is not bool:
            raise V2ConfigError()
        profiles: dict[str, NativeProfile] = {}
        for name, candidate in _object(value.get("native_profiles", {}), set(),
                                       set(value.get("native_profiles", {}))).items():
            name = _id(name)
            item = _object(candidate, {"tools"}, {"shell_dialects", "environment", "timeout_seconds",
                                                "max_log_bytes"})
            if not isinstance(item["tools"], dict):
                raise V2ConfigError()
            tools: dict[str, tuple[Path, ...]] = {}
            for tool, paths in item["tools"].items():
                if tool not in {"python", "node", "npm", "maven", "dotnet", "vitest", "tsc"}:
                    raise V2ConfigError()
                parts = paths if isinstance(paths, list) else [paths]
                if not 1 <= len(parts) <= 4:
                    raise V2ConfigError()
                tools[tool] = tuple(_path(part) for part in parts)
            dialects = _strings(item.get("shell_dialects", []))
            if set(dialects) - {"bash", "sh", "pwsh", "cmd"}:
                raise V2ConfigError()
            environment = item.get("environment", {})
            if (not isinstance(environment, dict) or len(environment) > 64
                    or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                           or not isinstance(text, str) or "\x00" in text
                           for key, text in environment.items())):
                raise V2ConfigError()
            profiles[name] = NativeProfile(tools, dialects, environment,
                                          _integer(item.get("timeout_seconds", 900), 1, 86400),
                                          _integer(item.get("max_log_bytes", 1048576), 1024,
                                                   64 * 1024 * 1024))
        project_values = value["projects"]
        if not isinstance(project_values, list) or not 1 <= len(project_values) <= 100:
            raise V2ConfigError()
        projects: dict[str, ProjectConfig] = {}
        for candidate in project_values:
            item = _object(candidate, {"project_id"}, {"github", "workspaces", "native_profiles",
                                                     "container_profiles"})
            project_id = _id(item["project_id"])
            if project_id in projects:
                raise V2ConfigError()
            repository = None
            if item.get("github") is not None:
                git = _object(item["github"], {"id", "node_id", "owner", "owner_id", "name"})
                if (not isinstance(git["owner"], str)
                        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", git["owner"])
                        or not isinstance(git["name"], str)
                        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", git["name"])
                        or git["name"] in {".", ".."}
                        or not isinstance(git["node_id"], str) or not git["node_id"]):
                    raise V2ConfigError()
                full_name = f"{git['owner']}/{git['name']}"
                repository = GitHubRepository(
                    _integer(git["id"], 1, 2**63-1), git["node_id"], git["owner"],
                    _integer(git["owner_id"], 1, 2**63-1), git["name"], full_name,
                    f"https://github.com/{full_name}", f"https://github.com/{full_name}.git",
                    f"git@github.com:{full_name}.git", True, None, None,
                )
            workspaces: dict[str, WorkspaceConfig] = {}
            workspace_values = item.get("workspaces", {})
            if not isinstance(workspace_values, dict):
                raise V2ConfigError()
            for workspace_id, workspace in workspace_values.items():
                workspace = _object(workspace, {"path"}, {"include_dirty", "include_untracked"})
                if type(workspace.get("include_dirty", False)) is not bool:
                    raise V2ConfigError()
                includes = _strings(workspace.get("include_untracked", []))
                for include in includes:
                    validate_relative_path(include.removesuffix("/"))
                workspaces[_id(workspace_id)] = WorkspaceConfig(
                    _path(workspace["path"]), workspace.get("include_dirty", False), includes)
            selected = _strings(item.get("native_profiles", []))
            if set(selected) - profiles.keys() or repository is None and not workspaces:
                raise V2ConfigError()
            containers = _strings(item.get("container_profiles", []))
            if set(containers) - {"python-quick"}:
                raise V2ConfigError()
            projects[project_id] = ProjectConfig(project_id, repository, workspaces, selected, containers)
        mailboxes: list[MailboxConfig] = []
        if not isinstance(value["mailboxes"], list) or len(value["mailboxes"]) > 200:
            raise V2ConfigError()
        seen: set[tuple[str, str, str, str]] = set()
        for candidate in value["mailboxes"]:
            item = _object(candidate, {"id", "provider", "project_id"},
                           {"branch", "path", "document_id", "tab_id"})
            binding_id, project_id = _id(item["id"]), _id(item["project_id"])
            if project_id not in projects or binding_id in {box.binding_id for box in mailboxes}:
                raise V2ConfigError()
            if item["provider"] == "github":
                if set(item) != {"id", "provider", "project_id", "branch", "path"}:
                    raise V2ConfigError()
                validate_branch(item["branch"])
                validate_relative_path(item["path"])
                if not item["path"].startswith(".runner/commands/") or projects[project_id].repository is None:
                    raise V2ConfigError()
                coordinates = {"branch": item["branch"], "path": item["path"]}
                key = ("github", project_id, item["branch"], item["path"])
            elif item["provider"] == "drive":
                if (set(item) != {"id", "provider", "project_id", "document_id", "tab_id"}
                        or any(not isinstance(item[name], str)
                               or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", item[name])
                               for name in ("document_id", "tab_id"))):
                    raise V2ConfigError()
                coordinates = {"document_id": item["document_id"], "tab_id": item["tab_id"]}
                key = ("drive", "", item["document_id"], item["tab_id"])
            else:
                raise V2ConfigError()
            if key in seen:
                raise V2ConfigError()
            seen.add(key)
            mailboxes.append(MailboxConfig(binding_id, item["provider"], project_id, coordinates))
        token = _path(value["github_token_file"]) if value.get("github_token_file") else None
        if any(box.provider == "github" for box in mailboxes) and token is None:
            raise V2ConfigError("V2_GITHUB_TOKEN_REFERENCE_REQUIRED")
        google = _path(value["google_config_file"]) if value.get("google_config_file") else None
        return V2RuntimeConfig(_id(value["runner_id"]), value.get("enable_v1", False), token,
                               google, projects, profiles, tuple(mailboxes),
                               _integer(value.get("queue_limit", 100), 1, 10000),
                               _integer(value.get("poll_seconds", 2), 1, 60),
                               _path(value["rootless_profile_dir"]) if value.get("rootless_profile_dir") else None)
    except (DomainError, GitError, TypeError, KeyError, AttributeError) as error:
        raise V2ConfigError() from error


def load_v2_config(paths: Paths) -> V2RuntimeConfig | None:
    path = paths.config_dir / "runtime-v2.json"
    if not path.exists() and not path.is_symlink():
        return None
    try:
        descriptor = open_private_file(path)
        with os.fdopen(descriptor, "rb") as stream:
            return parse_v2_config(stream.read(262145))
    except (OSError, UnsafePathError) as error:
        raise V2ConfigError("V2_CONFIG_UNREADABLE_OR_UNSAFE") from error
