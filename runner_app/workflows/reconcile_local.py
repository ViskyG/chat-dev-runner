"""Read-only managed Git identity proofs for a partial CREATE_PROJECT.

Only a registered repository or primary worktree with exact recorded identity can
confirm an uncertain local effect. An orphaned path without a registry record is
left for explicit owner resolution; this reader never imports or creates it.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from runner_app.git import worktrees
from runner_app.git.argv import GitError, run_git, run_git_bytes, validate_branch
from runner_app.git.registry import SourceRegistry
from runner_app.git.templates import starter_files
from runner_app.workflows.reconcile import EffectProof


class ManagedGitEffectReader:
    """Owner-configured exact local Git readbacks; construction is inert."""

    def __init__(self, registry: SourceRegistry) -> None:
        self.registry = registry

    def __call__(self, target: Mapping[str, Any],
                 effect: Mapping[str, Any]) -> EffectProof | None:
        command = target.get("command")
        intent = effect.get("intent")
        project_id = target.get("project_id")
        if (target.get("action") != "CREATE_PROJECT"
                or target.get("status") not in {"BLOCKED", "FAULT", "INTERRUPTED", "CANCELLED"}
                or not isinstance(command, dict)
                or command.get("action") != "CREATE_PROJECT"
                or not isinstance(project_id, str)
                or command.get("project_id") != project_id
                or effect.get("kind") != "local_workspace"
                or effect.get("state") not in {"INTENDED", "UNKNOWN"}
                or not isinstance(effect.get("effect_id"), str)
                or not isinstance(intent, dict)
                or intent.get("project_id") != project_id):
            return None
        step = effect.get("step_id")
        if step == "local-project":
            return self._project(target, effect, command, intent, project_id)
        if step == "initial-workspace":
            return self._workspace(target, effect, intent, project_id)
        return None

    def _registered_repo(self, project_id: str, command: Mapping[str, Any],
                         kind: str) -> Path | None:
        params = command.get("params")
        if not isinstance(params, dict):
            return None
        source = params.get("source")
        if not isinstance(source, dict) or source.get("kind") != kind:
            return None
        record = self.registry.get_project(project_id)
        expected = self.registry.managed_root / project_id / "repo.git"
        reference = (source.get("import_handle") if kind == "local" else
                     source.get("repository") if kind == "github" else None)
        if (record is None or record.get("repo_path") != str(expected)
                or record.get("origin_kind") != kind
                or record.get("origin_reference") != reference
                or record.get("display_name") != params.get("name")):
            return None
        self.registry._managed_path(expected, directory=True, expected=expected)
        worktrees._repo(expected)
        worktrees._inspect_config(expected)
        worktrees._reject_object_alternates(expected)
        if run_git(expected, ["rev-parse", "--is-bare-repository"]).strip() != "true":
            return None
        head = run_git(expected, ["rev-parse", "--verify", "HEAD^{commit}"]).strip()
        worktrees._commit(expected, head)
        worktrees._check_tree_features(expected, head)
        remotes = run_git(expected, ["remote"]).splitlines()
        if remotes:
            if remotes != ["origin"]:
                return None
            binding = self.registry.get_project_remote(project_id)
            if (binding is None or binding.get("clone_url") !=
                    run_git(expected, ["remote", "get-url", "origin"]).strip()):
                return None
        return expected

    def _project(self, target: Mapping[str, Any], effect: Mapping[str, Any],
                 command: Mapping[str, Any], intent: Mapping[str, Any],
                 project_id: str) -> EffectProof | None:
        kind = intent.get("kind")
        if kind not in {"new", "local", "github"}:
            return None
        params = command.get("params")
        if not isinstance(params, dict):
            return None
        source = params.get("source")
        if not isinstance(source, dict) or source.get("kind") != kind:
            return None
        if kind == "new" and dict(intent) != {"project_id": project_id, "kind": kind}:
            return None
        if kind == "local" and dict(intent) != {
            "project_id": project_id, "kind": kind,
            "import_handle": source.get("import_handle"),
        }:
            return None
        expected = self.registry.managed_root / project_id / "repo.git"
        if (effect.get("provider_id") is not None
                and effect["provider_id"] != str(expected)):
            return None
        if kind == "github":
            reference = source.get("repository")
            if (set(intent) != {
                    "project_id", "kind", "repository", "repository_id",
                    "owner_id", "node_id", "clone_url", "default_branch",
                    "remote_head", "stage_path", "repo_path",
                } or not isinstance(reference, str)
                    or intent.get("repository") != reference
                    or intent.get("repo_path") != str(expected)
                    or intent.get("stage_path") != str(
                        self.registry.managed_root / project_id /
                        f".github-import-{target.get('request_id')}"
                    ) or intent.get("clone_url") != f"https://github.com/{reference}.git"
                    or type(intent.get("repository_id")) is not int
                    or intent["repository_id"] < 1
                    or type(intent.get("owner_id")) is not int
                    or intent["owner_id"] < 1
                    or not isinstance(intent.get("node_id"), str)
                    or not intent["node_id"]):
                return None
        repo = self._registered_repo(project_id, command, kind)
        if repo is None:
            return None
        if kind == "github":
            branch = intent.get("default_branch")
            head = intent.get("remote_head")
            if not isinstance(branch, str) or not isinstance(head, str):
                return None
            try:
                validate_branch(branch)
            except GitError:
                return None
            if (run_git(repo, ["symbolic-ref", "--quiet", "HEAD"]).strip()
                    != f"refs/heads/{branch}"
                    or run_git(repo, ["rev-parse", "--verify", "HEAD^{commit}"]).strip()
                    != head):
                return None
        elif kind == "new":
            if (run_git(repo, ["symbolic-ref", "--quiet", "HEAD"]).strip()
                    != "refs/heads/main"):
                return None
            head = run_git(repo, ["rev-parse", "--verify", "HEAD^{commit}"]).strip()
            # The root commit is determined by the checked-in starter, including
            # file bytes; a same-name repository with different source is no proof.
            if len(run_git(repo, ["rev-list", "--parents", "-n", "1", head]).split()) != 1:
                return None
            starter = starter_files(params["template_id"], project_id)
            paths = run_git_bytes(repo, ["ls-tree", "-r", "--name-only", "-z", head])
            observed_paths = {part.decode("utf-8") for part in paths.split(b"\x00") if part}
            if observed_paths != set(starter):
                return None
            for path, content in starter.items():
                if run_git_bytes(repo, ["show", f"{head}:{path}"]) != content:
                    return None
        return EffectProof(effect["effect_id"], str(repo), dict(intent))

    def _workspace(self, target: Mapping[str, Any], effect: Mapping[str, Any],
                   intent: Mapping[str, Any], project_id: str) -> EffectProof | None:
        path = self.registry.managed_root / project_id / "workspaces" / "primary"
        branch = f"work/{project_id}"
        commit = intent.get("base_commit")
        if (set(intent) != {"project_id", "workspace_id", "branch", "base_commit"}
                or intent.get("workspace_id") != "primary"
                or intent.get("branch") != branch
                or not isinstance(commit, str)
                or (effect.get("provider_id") is not None
                    and effect["provider_id"] != str(path))):
            return None
        project_effects = [item for item in target.get("effects", ())
                           if item.get("step_id") == "local-project"
                           and item.get("kind") == "local_workspace"]
        if len(project_effects) != 1 or project_effects[0].get("state") != "CONFIRMED":
            return None
        command = target["command"]
        source = command.get("params", {}).get("source", {})
        kind = source.get("kind") if isinstance(source, dict) else None
        if not isinstance(kind, str):
            return None
        repo = self._registered_repo(project_id, command, kind)
        record = self.registry.get_workspace(project_id, "primary")
        if (repo is None or record is None or record.get("worktree_path") != str(path)
                or record.get("branch") != branch or record.get("head") != commit
                or record.get("generation") != 0 or record.get("archived")):
            return None
        self.registry._managed_path(path, directory=True, expected=path)
        observed = worktrees.inspect_workspace(repo, path)
        if (observed["head"] != commit or observed["branch"] != branch
                or observed["dirty"] or run_git(repo, [
                    "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}",
                ]).strip() != commit):
            return None
        return EffectProof(effect["effect_id"], str(path), dict(intent))
