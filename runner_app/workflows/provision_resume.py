"""Read-only preflight for a fresh RESUME_PROVISION request.

This preview is deliberately not adoption authority. The resume handler must hold
its new repo/global locks, verify every provider identity by exact ID, and record
an atomic cross-request adoption proof before calling a mutating adapter. An
uncertain prior effect is never classified as reusable here.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from runner_app.adapters.github import make_provision_marker
from runner_app.domain.commands import freeze, validate_shape
from runner_app.git.registry import SourceRegistry
from runner_app.storage.ledger import Ledger

_PARTIAL = frozenset({"BLOCKED", "FAULT", "INTERRUPTED", "CANCELLED"})
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SAFE_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z")
_SAFE_ALIAS = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")


class ProvisionResumeError(ValueError):
    """Stable local finding; no provider body or source path in the message."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PriorConfirmedEffect:
    effect_id: str
    step_id: str
    kind: str
    provider_id: str
    intent_json: str

    @property
    def intent(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.intent_json))


@dataclass(frozen=True)
class ProvisionRecoveryPreview:
    """Immutable old-request facts requiring fresh readback before reuse."""

    target_request_id: str
    target_digest: str
    target_status: str
    command_json: str
    installation_id: str
    github_owner: str
    provision_marker: str
    effects: tuple[PriorConfirmedEffect, ...]

    @property
    def command(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.command_json))

    def effect(self, step_id: str) -> PriorConfirmedEffect | None:
        return next((item for item in self.effects if item.step_id == step_id), None)


def inspect_prior_create(
    ledger: Ledger, registry: SourceRegistry, target_request_id: str, *,
    project_id: str, installation_id: str, github_identity: str,
    github_owner: str,
) -> ProvisionRecoveryPreview:
    """Classify only prior confirmed local facts; perform no provider write.

    The original CREATE_PROJECT command, not RESUME_PROVISION input, remains the
    source of template/source/name and the stable GitHub provision marker.
    """
    if (ledger.database.path != registry.database.path
            or not isinstance(project_id, str)
            or _SAFE_ALIAS.fullmatch(project_id) is None
            or not isinstance(github_identity, str)
            or _SAFE_ALIAS.fullmatch(github_identity) is None
            or not isinstance(github_owner, str)
            or _SAFE_OWNER.fullmatch(github_owner) is None):
        raise ProvisionResumeError("RESUME_CONFIGURATION_INVALID")
    try:
        valid_installation = str(UUID(installation_id)) == installation_id
    except (ValueError, TypeError, AttributeError):
        valid_installation = False
    if not valid_installation:
        raise ProvisionResumeError("RESUME_CONFIGURATION_INVALID")
    operation = ledger.get(target_request_id)
    if operation is None:
        raise ProvisionResumeError("RESUME_TARGET_MISSING")
    if operation["status"] not in _PARTIAL:
        raise ProvisionResumeError("RESUME_TARGET_NOT_PARTIAL")
    if operation["reconcile_required"]:
        raise ProvisionResumeError("RESUME_RECONCILIATION_REQUIRED")
    command = operation["command"]
    try:
        validate_shape(command)
        valid_command = (
            command["action"] == "CREATE_PROJECT"
            and command["request_id"] == target_request_id
            and command["project_id"] == project_id
            and command["params"]["github_identity"] == github_identity
            and operation["digest"] == freeze(command).digest
        )
    except (KeyError, TypeError, ValueError):
        valid_command = False
    if not valid_command:
        raise ProvisionResumeError("RESUME_TARGET_MISMATCH")
    if any(ledger.holds_lock(target_request_id, scope) for scope in (
        f"repo:{project_id}", f"global:{github_identity}",
        "global:drive-installation",
    )):
        raise ProvisionResumeError("RESUME_TARGET_LOCKED")
    raw_effects = operation["effects"]
    if not raw_effects:
        raise ProvisionResumeError("RESUME_NO_CONFIRMED_EFFECTS")
    if any(effect["state"] != "CONFIRMED" for effect in raw_effects):
        raise ProvisionResumeError("RESUME_RECONCILIATION_REQUIRED")
    steps: set[str] = set()
    effects: list[PriorConfirmedEffect] = []
    for effect in raw_effects:
        step = effect["step_id"]
        provider_id = effect["provider_id"]
        intent = effect["intent"]
        if (not isinstance(step, str) or not step or step in steps
                or not isinstance(provider_id, str) or not provider_id
                or not isinstance(intent, dict)):
            raise ProvisionResumeError("RESUME_EFFECT_INVALID")
        steps.add(step)
        # Content-write intents carry IDs and hashes; create intents pin
        # installation_id. Their exact Drive identity still needs readback.
        if (step.startswith("drive-") and step != "drive-root-create-receipt"
                and "installation_id" in intent
                and intent["installation_id"] != installation_id):
            raise ProvisionResumeError("RESUME_INSTALLATION_MISMATCH")
        effects.append(PriorConfirmedEffect(
            effect["effect_id"], step, effect["kind"], provider_id,
            json.dumps(intent, sort_keys=True, separators=(",", ":")),
        ))

    source = command["params"]["source"]
    source_kind = source["kind"]
    marker = make_provision_marker(installation_id, project_id, target_request_id)
    github_effect = next((item for item in effects
                          if item.step_id == "github-repository"), None)
    if source_kind in {"new", "local"}:
        if github_effect is not None:
            intent = github_effect.intent
            if (github_effect.kind != "github_repository"
                    or intent != {"owner": github_owner, "name": project_id,
                                  "private": True, "marker": marker}
                    or not github_effect.provider_id.isdecimal()
                    or int(github_effect.provider_id) < 1):
                raise ProvisionResumeError("RESUME_GITHUB_IDENTITY_MISMATCH")
    elif github_effect is not None:
        raise ProvisionResumeError("RESUME_EFFECT_INVALID")

    local_effect = next((item for item in effects if item.step_id == "local-project"), None)
    project = registry.get_project(project_id)
    if (local_effect is None) != (project is None):
        raise ProvisionResumeError("RESUME_LOCAL_SOURCE_UNVERIFIED")
    if local_effect is not None and project is not None:
        expected_repo = registry.managed_root / project_id / "repo.git"
        reference = (None if source_kind == "new" else
                     source["import_handle"] if source_kind == "local" else
                     source["repository"])
        if (local_effect.kind != "local_workspace"
                or local_effect.provider_id != str(expected_repo)
                or project["repo_path"] != str(expected_repo)
                or project["origin_kind"] != source_kind
                or project["origin_reference"] != reference
                or local_effect.intent.get("project_id") != project_id
                or local_effect.intent.get("kind") != source_kind
                or (source_kind == "local"
                    and local_effect.intent.get("import_handle") != reference)):
            raise ProvisionResumeError("RESUME_LOCAL_SOURCE_UNVERIFIED")
    workspace_effect = next((item for item in effects
                             if item.step_id == "initial-workspace"), None)
    workspace = registry.get_workspace(project_id, "primary")
    if (workspace_effect is None) != (workspace is None):
        raise ProvisionResumeError("RESUME_WORKSPACE_UNVERIFIED")
    if workspace_effect is not None and workspace is not None:
        expected_path = registry.managed_root / project_id / "workspaces" / "primary"
        intent = workspace_effect.intent
        if (workspace_effect.kind != "local_workspace"
                or workspace_effect.provider_id != str(expected_path)
                or workspace["worktree_path"] != str(expected_path)
                or workspace["branch"] != f"work/{project_id}"
                or workspace["head"] != intent.get("base_commit")
                or workspace["archived"]
                or intent.get("project_id") != project_id
                or intent.get("workspace_id") != "primary"
                or intent.get("branch") != f"work/{project_id}"
                or not isinstance(workspace["head"], str)
                or _OID.fullmatch(workspace["head"]) is None):
            raise ProvisionResumeError("RESUME_WORKSPACE_UNVERIFIED")
    return ProvisionRecoveryPreview(
        target_request_id, operation["digest"], operation["status"],
        json.dumps(command, sort_keys=True, separators=(",", ":")),
        installation_id, github_owner, marker, tuple(effects),
    )
