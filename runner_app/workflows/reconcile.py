"""Owner-local, read-only classification of a prior operation's uncertain effects.

A provider port may only read the exact resource named by a durable effect intent.
It returns a positive identity proof or None. This workflow never repeats an
external write or releases the target's retained lock. Ledger confirmation is
an internal proof record, not a cloud mutation.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from runner_app.domain.commands import FrozenCommand, validate_shape
from runner_app.engine.service import Context, OperationResult
from runner_app.git.github_transport import FETCH_REFSPEC
from runner_app.storage.ledger import TERMINAL, Ledger, LedgerError

_NUMERIC_ID = re.compile(r"[1-9][0-9]*\Z")
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{8,256}\Z")
_EXTERNAL_STEPS = {
    "github_repository": frozenset({"github-repository"}),
    "git_ref": frozenset({"baseline-main", "tested-git-push"}),
    "pull_request": frozenset({"draft-pull-request", "attach-run-report-link"}),
    "local_workspace": frozenset({"local-project", "initial-workspace", "git-fetch"}),
}
_PORT_KINDS = frozenset({"github_repository", "git_ref", "pull_request", "drive_file"})
_OPTIONAL_PORT_KINDS = frozenset({"local_workspace"})


@dataclass(frozen=True)
class EffectProof:
    """Positive remote identity readback, bound to one immutable local intent.

    The trusted port must populate observed_intent from values it verified by a
    remote read. A copied request intent alone is not proof of the remote state.
    """

    effect_id: str
    provider_id: str
    observed_intent: Mapping[str, Any]


class ReadOnlyEffectPort(Protocol):
    def __call__(self, target: Mapping[str, Any],
                 effect: Mapping[str, Any]) -> EffectProof | None: ...


def _positive_proof(effect: Mapping[str, Any], proof: EffectProof) -> bool:
    kind, intent = effect["kind"], effect["intent"]
    if (not isinstance(proof, EffectProof) or proof.effect_id != effect["effect_id"]
            or not isinstance(proof.provider_id, str)
            or not isinstance(proof.observed_intent, Mapping)
            or not isinstance(intent, dict)
            or dict(proof.observed_intent) != intent):
        return False
    if kind == "github_repository":
        return (effect["step_id"] == "github-repository"
                and _NUMERIC_ID.fullmatch(proof.provider_id) is not None
                and intent.get("private") is True
                and isinstance(intent.get("owner"), str) and bool(intent["owner"])
                and isinstance(intent.get("name"), str) and bool(intent["name"])
                and isinstance(intent.get("marker"), str)
                and re.fullmatch(r"cdr:v1:[0-9a-f]{64}", intent["marker"]) is not None)
    if kind == "git_ref":
        return (effect["step_id"] in _EXTERNAL_STEPS["git_ref"]
                and _OBJECT_ID.fullmatch(proof.provider_id) is not None
                and proof.provider_id == intent.get("commit")
                and type(intent.get("repository_id")) is int
                and intent["repository_id"] > 0
                and isinstance(intent.get("ref"), str)
                and intent["ref"].startswith("refs/heads/")
                and len(intent["ref"]) > len("refs/heads/"))
    if kind == "pull_request":
        if (_NUMERIC_ID.fullmatch(proof.provider_id) is None
                or type(intent.get("repository_id")) is not int
                or intent["repository_id"] < 1
                or not isinstance(intent.get("commit"), str)
                or _OBJECT_ID.fullmatch(intent["commit"]) is None):
            return False
        if effect["step_id"] == "draft-pull-request":
            return all(isinstance(intent.get(key), str) and bool(intent[key])
                       for key in ("head", "base", "title", "body"))
        if effect["step_id"] == "attach-run-report-link":
            folder = intent.get("run_folder_id")
            return (intent.get("pull_request_id") == int(proof.provider_id)
                    and isinstance(folder, str)
                    and _DRIVE_ID.fullmatch(folder) is not None
                    and intent.get("url") ==
                    f"https://drive.google.com/drive/folders/{folder}"
                    and all(isinstance(intent.get(key), str)
                            for key in ("title", "previous_body", "body"))
                    and intent["body"] == intent["previous_body"]
                    + f"Report: {intent['url']}\n")
        return False
    if kind == "local_workspace":
        project_id = intent.get("project_id")
        if (not isinstance(project_id, str)
                or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", project_id) is None):
            return False
        if effect["step_id"] == "git-fetch":
            advertised = intent.get("advertised_heads")
            return (re.fullmatch(r"[0-9a-f]{64}", proof.provider_id) is not None
                    and set(intent) == {
                        "project_id", "repository_id", "clone_url", "refspec",
                        "advertised_heads", "before_refs_sha256",
                    }
                    and type(intent.get("repository_id")) is int
                    and intent["repository_id"] > 0
                    and isinstance(intent.get("clone_url"), str)
                    and intent["clone_url"].startswith("https://github.com/")
                    and intent.get("refspec") == FETCH_REFSPEC
                    and isinstance(advertised, dict) and len(advertised) <= 256
                    and all(isinstance(branch, str) and isinstance(oid, str)
                            and _OBJECT_ID.fullmatch(oid) is not None
                            for branch, oid in advertised.items())
                    and isinstance(intent.get("before_refs_sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", intent["before_refs_sha256"])
                    is not None)
        if effect["step_id"] == "local-project":
            suffix = f"/{project_id}/repo.git"
            source_kind = intent.get("kind")
            return (proof.provider_id.startswith("/")
                    and proof.provider_id.endswith(suffix)
                    and source_kind in {"new", "local", "github"}
                    and (source_kind != "local"
                         or isinstance(intent.get("import_handle"), str))
                    and (source_kind != "github"
                         or intent.get("repo_path") == proof.provider_id))
        if effect["step_id"] == "initial-workspace":
            return (proof.provider_id.startswith("/")
                    and proof.provider_id.endswith(
                        f"/{project_id}/workspaces/primary"
                    ) and intent.get("workspace_id") == "primary"
                    and intent.get("branch") == f"work/{project_id}"
                    and isinstance(intent.get("base_commit"), str)
                    and _OBJECT_ID.fullmatch(intent["base_commit"]) is not None)
        return False
    if kind == "drive_file":
        if _DRIVE_ID.fullmatch(proof.provider_id) is None:
            return False
        if "parent_id" in intent:
            return (all(isinstance(intent.get(key), str) and intent[key]
                        for key in ("parent_id", "installation_id", "key", "name", "mime_type"))
                    and _DRIVE_ID.fullmatch(intent["parent_id"]) is not None)
        if "file_id" in intent:
            return (proof.provider_id == intent["file_id"]
                    and isinstance(intent.get("key"), str)
                    and isinstance(intent.get("desired_sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", intent["desired_sha256"])
                    is not None)
        if "receipt_doc_id" in intent:
            return (proof.provider_id == intent["receipt_doc_id"]
                    and isinstance(intent.get("command_doc_id"), str)
                    and isinstance(intent.get("desired_sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", intent["desired_sha256"])
                    is not None)
        return False
    return False


class ReconcileWorkflow:
    """Classify recorded uncertainty under a fresh local-owner request ID."""

    def __init__(self, ledger: Ledger, *, owner_source_ids: frozenset[str],
                 ports: Mapping[str, ReadOnlyEffectPort]) -> None:
        if not owner_source_ids or any(not isinstance(value, str) or not value
                                       for value in owner_source_ids):
            raise ValueError("Exact authenticated owner-local source IDs are required")
        if (not _PORT_KINDS <= set(ports)
                or set(ports) - _PORT_KINDS - _OPTIONAL_PORT_KINDS
                or any(not callable(port) for port in ports.values())):
            raise ValueError("Four external ports and an optional local Git port are required")
        self.ledger = ledger
        self.owner_source_ids = frozenset(owner_source_ids)
        self.ports = dict(ports)

    def __call__(self, frozen: FrozenCommand, context: Context) -> OperationResult:
        command = frozen.command
        validate_shape(command)
        if command["action"] != "RECONCILE":
            return OperationResult("BLOCKED", "WRONG_WORKFLOW_ACTION")
        if context.ledger.database.path != self.ledger.database.path:
            return OperationResult("FAULT", "WORKFLOW_LEDGER_MISMATCH")
        request_id = command["request_id"]
        actor = self.ledger.get(request_id)
        if (actor is None or actor["status"] != "RUNNING"
                or actor["command"] != command or actor["digest"] != frozen.digest
                or actor["source_id"] not in self.owner_source_ids):
            return OperationResult("BLOCKED", "RECONCILE_ADMISSION_MISMATCH")
        if context.cancel.is_set():
            return OperationResult("CANCELLED", "CANCEL_REQUESTED")
        target_id = command["params"]["target_request_id"]
        target = self.ledger.get(target_id)
        if (target is None or target_id == request_id
                or target["project_id"] != command["project_id"]
                or target["workspace_id"] != command["workspace_id"]):
            return OperationResult("BLOCKED", "RECONCILE_TARGET_SCOPE_MISMATCH")
        if target["status"] not in TERMINAL or not target["reconcile_required"]:
            return OperationResult("BLOCKED", "RECONCILIATION_NOT_REQUIRED")
        if not any(item["state"] in {"INTENDED", "UNKNOWN"} for item in target["effects"]):
            return OperationResult("BLOCKED", "RECONCILIATION_NO_EXTERNAL_EFFECTS")

        confirmed: list[str] = []
        unresolved: list[dict[str, str]] = []
        for effect in target["effects"]:
            if effect["state"] not in {"INTENDED", "UNKNOWN"}:
                continue
            effect_id = effect["effect_id"]
            kind = effect["kind"]
            step = effect["step_id"]
            if context.cancel.is_set():
                return OperationResult("CANCELLED", "CANCEL_REQUESTED", {
                    "target_request_id": target_id, "confirmed_effect_ids": confirmed,
                    "unresolved_effects": unresolved,
                })
            if kind not in self.ports or (kind in _EXTERNAL_STEPS
                                          and step not in _EXTERNAL_STEPS[kind]):
                unresolved.append({"effect_id": effect_id, "kind": kind,
                                   "step_id": step, "code": "EFFECT_KIND_UNSUPPORTED"})
                continue
            try:
                proof = self.ports[kind](target, effect)
            except Exception:  # noqa: BLE001 -- provider text may contain private data
                unresolved.append({"effect_id": effect_id, "kind": kind,
                                   "step_id": step, "code": "PROVIDER_READ_UNAVAILABLE"})
                continue
            if proof is None:
                unresolved.append({"effect_id": effect_id, "kind": kind,
                                   "step_id": step, "code": "REMOTE_EFFECT_NOT_OBSERVED"})
                continue
            if not _positive_proof(effect, proof):
                unresolved.append({"effect_id": effect_id, "kind": kind,
                                   "step_id": step, "code": "REMOTE_IDENTITY_MISMATCH"})
                continue
            current = self.ledger.get(target_id)
            current_effects = ([] if current is None else [item for item in current["effects"]
                            if item["effect_id"] == effect_id])
            if (current is None or current["status"] != target["status"]
                    or current["project_id"] != target["project_id"]
                    or current["workspace_id"] != target["workspace_id"]
                    or len(current_effects) != 1 or current_effects[0] != effect):
                unresolved.append({"effect_id": effect_id, "kind": kind,
                                   "step_id": step, "code": "EFFECT_CHANGED"})
                continue
            try:
                self.ledger.confirm_effect(effect_id, proof.provider_id)
            except LedgerError:
                unresolved.append({"effect_id": effect_id, "kind": kind,
                                   "step_id": step, "code": "EFFECT_CHANGED"})
                continue
            confirmed.append(effect_id)
            # Later effects can depend on an earlier proof in this same finite
            # reconciliation pass (e.g. the primary worktree on its project).
            # Re-read the durable row; never hand a provider a speculative proof.
            target = self.ledger.get(target_id) or target
        final = self.ledger.get(target_id)
        remaining = ([] if final is None else [item for item in final["effects"]
                      if item["state"] in {"INTENDED", "UNKNOWN"}])
        result: dict[str, Any] = {
            "reconcile_version": "1.0", "target_request_id": target_id,
            "target_status": target["status"], "confirmed_effect_ids": confirmed,
            "unresolved_effects": unresolved,
            "remaining_effect_ids": [item["effect_id"] for item in remaining],
            "ready_for_resume": (target["action"] == "CREATE_PROJECT"
                                 and not remaining and not unresolved),
            "target_lock_retained": bool(final and (
                self.ledger.holds_lock(target_id, f"repo:{target['project_id']}")
                or (target["workspace_id"] is not None and self.ledger.holds_lock(
                    target_id, f"workspace:{target['project_id']}/{target['workspace_id']}"
                ))
            )),
        }
        return OperationResult("COMPLETED", result=result)
