"""No-write completion of a fully provisioned, interrupted owner-local create.

This is intentionally a narrow RESUME_PROVISION handler. It completes only a
owner-local new project whose entire original resource graph already exists,
is READY locally, and passes fresh exact-ID GitHub, Git, and Drive readback. It
never invokes a provisioning writer. Missing steps remain a truthful partial
result until the adapters support safe continuation under the new request ID.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from typing import Any, Protocol

from runner_app.adapters.drive_registry import WorkspaceDriveRegistration
from runner_app.adapters.drive_upload import DOCUMENT_MIME, FOLDER_MIME
from runner_app.adapters.github import GitHubBranch, GitHubRepository
from runner_app.domain.commands import FrozenCommand, validate_shape
from runner_app.engine.service import Context, OperationResult
from runner_app.git import worktrees
from runner_app.git.argv import GitError, run_git
from runner_app.git.registry import SourceRegistry
from runner_app.storage.ledger import EffectAdoptionProof, Ledger, LedgerError
from runner_app.workflows.provision_resume import ProvisionResumeError, inspect_prior_create
from runner_app.workflows.publish_run import _matches_project_remote
from runner_app.workflows.reconcile import EffectProof, _positive_proof

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_DRIVE_CREATES = (
    ("drive-project-folder", "project_folder_id", "projects", "project:{project}",
     "{project}", FOLDER_MIME, None),
    ("drive-project-state-doc", "project_state_doc_id", "project_folder_id",
     "project-state", "PROJECT STATE", DOCUMENT_MIME, None),
    ("drive-project-command-doc", "project_command_doc_id", "project_folder_id",
     "project-command", "PROJECT COMMAND", DOCUMENT_MIME, None),
    ("drive-project-receipt-doc", "project_receipt_doc_id", "project_folder_id",
     "project-receipt", "PROJECT RECEIPT", DOCUMENT_MIME, None),
    ("drive-workspace-folder", "workspace_folder_id", "project_folder_id",
     "workspace:{project}/primary", "primary", FOLDER_MIME, "primary"),
    ("drive-source-context-doc", "context_doc_id", "workspace_folder_id",
     "source-context", "SOURCE CONTEXT", DOCUMENT_MIME, "primary"),
    ("drive-command-doc", "command_doc_id", "workspace_folder_id",
     "workspace-command", "COMMAND", DOCUMENT_MIME, "primary"),
    ("drive-changeset-doc", "changeset_doc_id", "workspace_folder_id",
     "workspace-changeset", "CHANGESET", DOCUMENT_MIME, "primary"),
    ("drive-receipt-doc", "receipt_doc_id", "workspace_folder_id",
     "workspace-receipt", "RECEIPT", DOCUMENT_MIME, "primary"),
    ("drive-latest-result-doc", "latest_result_doc_id", "workspace_folder_id",
     "latest-result", "LATEST RESULT", DOCUMENT_MIME, "primary"),
)
_CONTENT = (
    ("drive-project-state-content", "project_state_doc_id", "project-state:{project}"),
    ("drive-source-context-content", "context_doc_id", "source-context:{project}/primary"),
)
_STEPS = frozenset({
    "github-repository", "local-project", "baseline-main", "initial-workspace",
    *(item[0] for item in _DRIVE_CREATES), *(item[0] for item in _CONTENT),
})


class GitHubReadPort(Protocol):
    def get_repository(self, owner: str, name: str, *,
                       expected_repo_id: int | None = None) -> GitHubRepository: ...

    def branch_head(self, repository: GitHubRepository, branch: str) -> GitHubBranch | None: ...


class DriveReadPort(Protocol):
    def __call__(self, target: Mapping[str, Any],
                 effect: Mapping[str, Any]) -> EffectProof | None: ...


class DriveRegistrationReadPort(Protocol):
    def get(self, project_id: str, workspace_id: str) -> WorkspaceDriveRegistration: ...


class CompleteProvisionRecoveryWorkflow:
    """Adopt an entirely proven create, with no external or local resource write.

    Register this handler only with owner-local RESUME_PROVISION admission. It is
    deliberately unable to fill absent steps, replay uncertain effects, or emit
    a root Drive receipt for an original Drive command.
    """

    def __init__(self, ledger: Ledger, registry: SourceRegistry,
                 github: GitHubReadPort, drive: DriveReadPort,
                 drive_registry: DriveRegistrationReadPort, *,
                 installation_id: str, projects_folder_id: str,
                 github_owner: str, github_identity: str,
                 template_profiles: Mapping[str, str], owner_source_ids: frozenset[str],
                 root_command_id: str) -> None:
        if (ledger.database.path != registry.database.path or not owner_source_ids
                or not root_command_id or not projects_folder_id or not template_profiles
                or any(not value for value in owner_source_ids)):
            raise ValueError("Exact owner, source, and provision configuration required")
        self.ledger = ledger
        self.registry = registry
        self.github = github
        self.drive = drive
        self.drive_registry = drive_registry
        self.installation_id = installation_id
        self.projects_folder_id = projects_folder_id
        self.github_owner = github_owner
        self.github_identity = github_identity
        self.template_profiles = dict(template_profiles)
        self.owner_source_ids = frozenset(owner_source_ids)
        self.root_command_id = root_command_id

    def __call__(self, frozen: FrozenCommand, context: Context) -> OperationResult:
        command = frozen.command
        validate_shape(command)
        if command["action"] != "RESUME_PROVISION":
            return OperationResult("BLOCKED", "WRONG_WORKFLOW_ACTION")
        request_id, project_id = command["request_id"], command["project_id"]
        if context.ledger.database.path != self.ledger.database.path:
            return OperationResult("FAULT", "WORKFLOW_LEDGER_MISMATCH")
        actor = self.ledger.get(request_id)
        if (actor is None or actor["status"] != "RUNNING"
                or actor["command"] != command or actor["digest"] != frozen.digest
                or actor["source_id"] not in self.owner_source_ids
                or command["params"] != {
                    "resume_version": "1.0",
                    "target_request_id": command["params"].get("target_request_id"),
                } or command["workspace_id"] is not None
                or command["expected_head"] is not None):
            return OperationResult("BLOCKED", "RESUME_ADMISSION_MISMATCH")
        target_id = command["params"]["target_request_id"]
        scopes = (f"repo:{project_id}", f"global:{self.github_identity}",
                  "global:drive-installation")
        if not all(self.ledger.holds_lock(request_id, scope) for scope in scopes):
            return OperationResult("BLOCKED", "PROVISION_LOCK_MISSING")
        if context.cancel.is_set():
            return OperationResult("CANCELLED", "CANCEL_REQUESTED")
        try:
            preview = inspect_prior_create(
                self.ledger, self.registry, target_id, project_id=project_id,
                installation_id=self.installation_id,
                github_identity=self.github_identity, github_owner=self.github_owner,
            )
        except ProvisionResumeError as error:
            return OperationResult("BLOCKED", error.code)
        target = self.ledger.get(target_id)
        if target is None:
            return OperationResult("BLOCKED", "RESUME_TARGET_MISSING")
        if target["source_id"] == self.root_command_id:
            return OperationResult("BLOCKED", "RESUME_ROOT_RECEIPT_UNSUPPORTED")
        original = preview.command
        params = original["params"]
        if params["source"] != {"kind": "new"}:
            return OperationResult("BLOCKED", "RESUME_SOURCE_KIND_UNSUPPORTED")
        profile_id = self.template_profiles.get(params["template_id"])
        if profile_id is None:
            return OperationResult("BLOCKED", "TEMPLATE_BINDING_MISSING")
        old = {effect.step_id: effect for effect in preview.effects}
        if set(old) != _STEPS:
            return OperationResult("BLOCKED", "RESUME_INCOMPLETE_RESOURCE_GRAPH", {
                "target_request_id": target_id,
                "missing_steps": sorted(_STEPS - set(old)),
                "unexpected_steps": sorted(set(old) - _STEPS),
            })
        try:
            repository, head = self._local_and_github(
                project_id, params["name"], params["template_id"], profile_id,
                preview.provision_marker, old,
            )
            registration = self._drive_registration(project_id, old)
            proofs = self._proofs(target, old, repository, head)
        except ValueError:
            return OperationResult("BLOCKED", "RESUME_IDENTITY_MISMATCH")
        except (GitError, LedgerError, OSError, sqlite3.Error):
            return OperationResult("BLOCKED", "RESUME_READBACK_UNAVAILABLE")
        except Exception:  # noqa: BLE001 -- never expose a provider body
            return OperationResult("BLOCKED", "RESUME_READBACK_UNAVAILABLE")
        if proofs is None:
            return OperationResult("BLOCKED", "RESUME_IDENTITY_MISMATCH")
        if context.cancel.is_set():
            return OperationResult("CANCELLED", "CANCEL_REQUESTED")
        # All readbacks happen before the first durable claim. A storage fault
        # after this point retains adopted evidence for manual recovery.
        adopted: list[str] = []
        try:
            for step in sorted(_STEPS):
                adopted.append(self.ledger.adopt_confirmed_effect(request_id, proofs[step]))
        except LedgerError as error:
            return OperationResult("BLOCKED", error.code, {
                "target_request_id": target_id, "adopted_effect_ids": adopted,
            })
        drive_ids = {
            "project_folder_id": registration.project_folder_id,
            "workspace_folder_id": registration.workspace_folder_id,
            "command_doc_id": registration.command_doc_id,
            "changeset_doc_id": registration.changeset_doc_id,
            "receipt_doc_id": registration.receipt_doc_id,
            "latest_result_doc_id": registration.latest_result_doc_id,
            "project_state_doc_id": registration.project_state_doc_id,
            "context_doc_id": registration.context_doc_id,
            "project_command_doc_id": registration.project_command_doc_id,
            "project_receipt_doc_id": registration.project_receipt_doc_id,
        }
        result: dict[str, Any] = {
            "project_id": project_id, "workspace_id": "primary",
            "template_id": params["template_id"], "profile_id": profile_id,
            "github_for_chat": "MANUAL_REQUIRED",
            "github": {
                "repository_id": repository.id, "owner": repository.owner,
                "name": repository.name, "node_id": repository.node_id,
                "full_name": repository.full_name, "html_url": repository.html_url,
                "clone_url": repository.clone_url, "private": True,
                "disposition": "reconciled",
            },
            "local": {
                "repo_path": str(self.registry.managed_root / project_id / "repo.git"),
                "baseline_commit": head, "origin_url": repository.clone_url,
            },
            "baseline": {"ref": "refs/heads/main", "commit": head},
            "workspace": {
                "path": str(self.registry.managed_root / project_id / "workspaces" / "primary"),
                "branch": f"work/{project_id}", "head": head,
            },
            "drive": drive_ids,
            "drive_content": {
                "project_state_sha256": old["drive-project-state-content"].intent[
                    "desired_sha256"],
                "context_sha256": old["drive-source-context-content"].intent[
                    "desired_sha256"],
            },
            "project_state": "READY",
            "recovery": {
                "version": "1.0", "target_request_id": target_id,
                "target_digest": preview.target_digest,
                "adopted_effect_ids": adopted,
            },
        }
        return OperationResult("COMPLETED", result=result)

    def _local_and_github(
        self, project_id: str, project_name: str, template_id: str,
        profile_id: str, marker: str, old: Mapping[str, Any],
    ) -> tuple[GitHubRepository, str]:
        project = self.registry.get_project(project_id)
        workspace = self.registry.get_workspace(project_id, "primary")
        binding = self.registry.get_project_remote(project_id)
        profile = self.registry.get_workspace_profile(project_id, "primary")
        repo = self.registry.managed_root / project_id / "repo.git"
        worktree = self.registry.managed_root / project_id / "workspaces" / "primary"
        if (project is None or workspace is None or binding is None
                or project["display_name"] != project_name
                or project["state"] != "READY" or project["origin_kind"] != "new"
                or project["origin_reference"] is not None
                or project["repo_path"] != str(repo)
                or workspace["worktree_path"] != str(worktree)
                or workspace["branch"] != f"work/{project_id}"
                or workspace["generation"] != 0 or workspace["archived"]
                or profile != {"template_id": template_id, "profile_id": profile_id}
                or old["local-project"].kind != "local_workspace"
                or old["local-project"].provider_id != str(repo)
                or old["local-project"].intent != {
                    "project_id": project_id, "kind": "new",
                } or old["initial-workspace"].kind != "local_workspace"
                or old["initial-workspace"].provider_id != str(worktree)):
            raise ValueError("Local source identity changed")
        github_effect = old["github-repository"]
        if (github_effect.kind != "github_repository"
                or not github_effect.provider_id.isdecimal()):
            raise ValueError("GitHub identity changed")
        repository = self.github.get_repository(
            self.github_owner, project_id,
            expected_repo_id=int(github_effect.provider_id),
        )
        if (repository.id != int(github_effect.provider_id)
                or repository.owner != self.github_owner
                or repository.name != project_id
                or repository.description != marker
                or repository.default_branch != "main"
                or not _matches_project_remote(project, binding, repository)):
            raise ValueError("GitHub identity changed")
        worktrees._inspect_config(repo)
        worktrees._reject_object_alternates(repo)
        observed = worktrees.inspect_workspace(repo, worktree)
        head = run_git(repo, ["rev-parse", "--verify", "refs/heads/main^{commit}"]).strip()
        work_branch = run_git(
            repo, ["rev-parse", "--verify", f"refs/heads/work/{project_id}^{{commit}}"],
        ).strip()
        intent = old["initial-workspace"].intent
        if (not isinstance(head, str) or _SHA.fullmatch(head) is None
                or head != work_branch or head != workspace["head"]
                or observed["head"] != head
                or observed["branch"] != f"work/{project_id}"
                or observed["dirty"]
                or intent != {"project_id": project_id, "workspace_id": "primary",
                              "branch": f"work/{project_id}", "base_commit": head}
                or run_git(repo, ["remote"]).strip() != "origin"
                or run_git(repo, ["remote", "get-url", "origin"]).strip()
                    != repository.clone_url):
            raise ValueError("Git source changed")
        return repository, head

    def _drive_registration(self, project_id: str,
                            old: Mapping[str, Any]) -> WorkspaceDriveRegistration:
        registration = self.drive_registry.get(project_id, "primary")
        if (not isinstance(registration, WorkspaceDriveRegistration)
                or registration.installation_id != self.installation_id
                or registration.project_id != project_id
                or registration.workspace_id != "primary"):
            raise ValueError("Drive registration changed")
        for step, field, parent, key, name, mime, workspace in _DRIVE_CREATES:
            effect = old[step]
            parent_id = (self.projects_folder_id if parent == "projects"
                         else getattr(registration, parent))
            provider_id = getattr(registration, field)
            intent = {
                "parent_id": parent_id, "installation_id": self.installation_id,
                "key": key.format(project=project_id), "name": name.format(project=project_id),
                "mime_type": mime, "project_id": project_id,
                "workspace_id": workspace,
            }
            if (effect.kind != "drive_file" or effect.provider_id != provider_id
                    or effect.intent != intent):
                raise ValueError("Drive resource graph changed")
        for step, field, key in _CONTENT:
            effect = old[step]
            intent = effect.intent
            if (effect.kind != "drive_file"
                    or effect.provider_id != getattr(registration, field)
                    or set(intent) != {"file_id", "key", "base_revision", "desired_sha256"}
                    or intent["file_id"] != effect.provider_id
                    or intent["key"] != key.format(project=project_id)
                    or not isinstance(intent["base_revision"], str)
                    or not intent["base_revision"]
                    or not isinstance(intent["desired_sha256"], str)
                    or _DIGEST.fullmatch(intent["desired_sha256"]) is None):
                raise ValueError("Drive content graph changed")
        return registration

    def _proofs(
        self, target: Mapping[str, Any], old: Mapping[str, Any],
        repository: GitHubRepository, head: str,
    ) -> dict[str, EffectAdoptionProof] | None:
        branch = self.github.branch_head(repository, "main")
        if (branch is None or branch.name != "main" or branch.sha != head
                or old["baseline-main"].kind != "git_ref"
                or old["baseline-main"].provider_id != head
                or old["baseline-main"].intent != {
                    "repository_id": repository.id, "ref": "refs/heads/main",
                    "commit": head,
                }):
            return None
        project_id = target["project_id"]
        local = {
            "local-project": (str(self.registry.managed_root / project_id / "repo.git"),
                              {"project_id": project_id, "kind": "new"}),
            "initial-workspace": (
                str(self.registry.managed_root / project_id / "workspaces" / "primary"),
                {"project_id": project_id, "workspace_id": "primary",
                 "branch": f"work/{project_id}", "base_commit": head}),
        }
        observed: dict[str, tuple[str, Mapping[str, Any]]] = {
            "github-repository": (str(repository.id), {
                "owner": repository.owner, "name": repository.name,
                "private": repository.private, "marker": repository.description,
            }),
            "baseline-main": (head, {
                "repository_id": repository.id, "ref": f"refs/heads/{branch.name}",
                "commit": branch.sha,
            }),
            **local,
        }
        for step in (*[item[0] for item in _DRIVE_CREATES],
                     *[item[0] for item in _CONTENT]):
            effect = old[step]
            synthetic = next((item for item in target["effects"]
                              if item["effect_id"] == effect.effect_id), None)
            if synthetic is None:
                return None
            proof = self.drive(target, {**synthetic, "state": "UNKNOWN"})
            if (proof is None or not _positive_proof(synthetic, proof)
                    or proof.provider_id != effect.provider_id):
                return None
            observed[step] = (proof.provider_id, proof.observed_intent)
        proofs: dict[str, EffectAdoptionProof] = {}
        for step, (provider_id, intent) in observed.items():
            effect = old[step]
            if provider_id != effect.provider_id or dict(intent) != effect.intent:
                return None
            proofs[step] = EffectAdoptionProof(effect.effect_id, provider_id, intent)
        return proofs
