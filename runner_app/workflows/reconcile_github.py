"""Read-only GitHub proofs for the owner-local RECONCILE workflow.

These callbacks never create a repository, move a ref, or write a pull request.
They re-read the numeric private repository identity before classifying an old
uncertain effect. Missing or changed resources stay unresolved.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from runner_app.adapters.github import GitHubRepository, GitHubREST, make_provision_marker
from runner_app.git.registry import SourceRegistry
from runner_app.workflows.publish_run import _matches_project_remote
from runner_app.workflows.reconcile import EffectProof


class GitHubEffectReader:
    """Owner-configured GitHub readbacks; construction performs no network I/O."""

    def __init__(self, github: GitHubREST, registry: SourceRegistry, *,
                 installation_id: str, github_owner: str) -> None:
        if not installation_id or not github_owner:
            raise ValueError("Approved installation and GitHub owner are required")
        self.github = github
        self.registry = registry
        self.installation_id = installation_id
        self.github_owner = github_owner

    def _repository(self, target: Mapping[str, Any]) -> GitHubRepository | None:
        command = target.get("command")
        project_id = target.get("project_id")
        if not isinstance(command, dict) or not isinstance(project_id, str):
            return None
        if command.get("action") == "CREATE_PROJECT":
            source = command.get("params", {}).get("source")
            if not isinstance(source, dict) or source.get("kind") not in {"new", "local"}:
                return None
            marker = make_provision_marker(
                self.installation_id, project_id, target["request_id"],
            )
            repository = self.github.get_repository(self.github_owner, project_id)
            if (repository.private is not True or repository.owner != self.github_owner
                    or repository.name != project_id or repository.description != marker):
                return None
            return repository
        project = self.registry.get_project(project_id)
        binding = self.registry.get_project_remote(project_id)
        if project is None or binding is None:
            return None
        owner, name, provider_id = (
            binding.get("owner"), binding.get("name"), binding.get("provider_id"),
        )
        if (not isinstance(owner, str) or not isinstance(name, str)
                or type(provider_id) is not int or provider_id < 1):
            return None
        repository = self.github.get_repository(owner, name, expected_repo_id=provider_id)
        return repository if _matches_project_remote(project, binding, repository) else None

    @staticmethod
    def _effect(target: Mapping[str, Any], effect: Mapping[str, Any],
                *, kind: str, step: str) -> Mapping[str, Any] | None:
        if (effect.get("kind") != kind or effect.get("step_id") != step
                or effect.get("state") not in {"INTENDED", "UNKNOWN"}
                or not isinstance(effect.get("intent"), dict)
                or not isinstance(effect.get("effect_id"), str)
                or target.get("status") not in {
                    "BLOCKED", "FAULT", "INTERRUPTED", "CANCELLED"
                }):
            return None
        return cast(Mapping[str, Any], effect["intent"])

    def repository(self, target: Mapping[str, Any],
                   effect: Mapping[str, Any]) -> EffectProof | None:
        intent = self._effect(
            target, effect, kind="github_repository", step="github-repository",
        )
        if (intent is None or target.get("action") != "CREATE_PROJECT"
                or intent.get("owner") != self.github_owner
                or intent.get("name") != target.get("project_id")
                or intent.get("private") is not True):
            return None
        repository = self._repository(target)
        if (repository is None or intent.get("marker") != repository.description
                or (effect.get("provider_id") is not None
                    and effect["provider_id"] != str(repository.id))):
            return None
        observed = {
            "owner": repository.owner, "name": repository.name,
            "private": repository.private, "marker": repository.description,
        }
        return EffectProof(effect["effect_id"], str(repository.id), observed)

    def ref(self, target: Mapping[str, Any],
            effect: Mapping[str, Any]) -> EffectProof | None:
        step = effect.get("step_id")
        if step not in {"baseline-main", "tested-git-push"}:
            return None
        intent = self._effect(target, effect, kind="git_ref", step=step)
        if intent is None:
            return None
        ref = intent.get("ref")
        commit = intent.get("commit")
        if (not isinstance(ref, str) or not ref.startswith("refs/heads/")
                or not isinstance(commit, str)):
            return None
        repository = self._repository(target)
        if repository is None or intent.get("repository_id") != repository.id:
            return None
        branch = ref.removeprefix("refs/heads/")
        if (step == "baseline-main"
                and (target.get("action") != "CREATE_PROJECT"
                     or branch != "main" or repository.default_branch != "main")):
            return None
        if (step == "tested-git-push"
                and (target.get("action") not in {"PUBLISH", "DELIVER_VERIFY_PUBLISH"}
                     or branch == repository.default_branch)):
            return None
        observed = self.github.branch_head(repository, branch)
        if (observed is None or observed.sha != commit
                or (effect.get("provider_id") is not None
                    and effect["provider_id"] != observed.sha)):
            return None
        # The remaining fields are immutable local attestation identifiers, not
        # remote values. Only the ref and commit are inferred from GitHub.
        observed_intent = dict(intent)
        observed_intent["repository_id"] = repository.id
        observed_intent["ref"] = f"refs/heads/{observed.name}"
        observed_intent["commit"] = observed.sha
        return EffectProof(effect["effect_id"], observed.sha, observed_intent)

    def pull_request(self, target: Mapping[str, Any],
                     effect: Mapping[str, Any]) -> EffectProof | None:
        step = effect.get("step_id")
        if step not in {"draft-pull-request", "attach-run-report-link"}:
            return None
        intent = self._effect(target, effect, kind="pull_request", step=step)
        if intent is None:
            return None
        repository = self._repository(target)
        if repository is None or intent.get("repository_id") != repository.id:
            return None
        head, base, commit = (
            intent.get("head"), intent.get("base"), intent.get("commit"),
        )
        if step == "attach-run-report-link":
            original = next((
                item for item in target.get("effects", ())
                if item.get("step_id") == "draft-pull-request"
                and item.get("kind") == "pull_request"
                and item.get("state") == "CONFIRMED"
            ), None)
            if not isinstance(original, dict):
                return None
            original_id = original.get("provider_id")
            original_intent = original.get("intent")
            if (not isinstance(original_id, str) or not original_id.isdecimal()
                    or intent.get("pull_request_id") != int(original_id)
                    or not isinstance(original_intent, dict)
                    or original_intent.get("repository_id") != repository.id
                    or original_intent.get("run_id") != intent.get("run_id")
                    or original_intent.get("commit") != commit
                    or original_intent.get("title") != intent.get("title")
                    or original_intent.get("body") != intent.get("previous_body")):
                return None
            head = original_intent.get("head")
            base = original_intent.get("base")
        if (not isinstance(head, str) or not isinstance(base, str)
                or not isinstance(commit, str) or base != repository.default_branch
                or head == base):
            return None
        pr_id = intent.get("pull_request_id") if step == "attach-run-report-link" else None
        if pr_id is not None and (type(pr_id) is not int or pr_id < 1):
            return None
        found = self.github.reconcile_draft_pr(
            repository, head_branch=head, base_branch=base,
            expected_head_sha=commit, expected_pr_id=pr_id,
        ).pull_request
        if (found.title != intent.get("title") or found.body != intent.get("body")
                or (effect.get("provider_id") is not None
                    and effect["provider_id"] != str(found.id))
                or (step == "draft-pull-request"
                    and (found.head_branch != head or found.base_branch != base))):
            return None
        observed_intent = dict(intent)
        observed_intent["repository_id"] = repository.id
        observed_intent["commit"] = found.head_sha
        observed_intent["title"] = found.title
        observed_intent["body"] = found.body
        if step == "draft-pull-request":
            observed_intent["head"] = found.head_branch
            observed_intent["base"] = found.base_branch
        else:
            observed_intent["pull_request_id"] = found.id
        return EffectProof(effect["effect_id"], str(found.id), observed_intent)
