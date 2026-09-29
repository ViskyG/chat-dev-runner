"""Finite EP-02 local source steps behind EP-01 admission and effect intent.

These methods are building blocks for later EP-05 handlers. No handler is installed
by default and no project code is executed here. The caller must submit a valid
one-shot command through Engine first; every method checks its durable lock.
"""
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from runner_app.domain.commands import validate_shape
from runner_app.git import worktrees
from runner_app.git.argv import run_git
from runner_app.git.imports import ImportGrants
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.git.snapshots import (
    Snapshot,
    capture_clean_verify,
    capture_dirty_verify,
    stage_changeset,
)
from runner_app.git.templates import starter_files
from runner_app.storage.ledger import Ledger


class SourceService:
    def __init__(self, ledger: Ledger, registry: SourceRegistry,
                 *, import_grants: ImportGrants | None = None) -> None:
        if ledger.database.path != registry.database.path:
            raise ValueError("Source service and admission ledger must share one database")
        self.ledger = ledger
        self.registry = registry
        self.import_grants = import_grants

    def _command(self, request_id: str, action: str) -> tuple[dict[str, Any], dict[str, Any]]:
        operation = self.ledger.get(request_id)
        if operation is None or operation["status"] != "RUNNING":
            raise SourceError("OPERATION_NOT_RUNNING", "Source step requires an admitted operation")
        command: dict[str, Any] = operation["command"]
        validate_shape(command)
        if command["action"] != action:
            raise SourceError("WRONG_SOURCE_ACTION", "Source step does not match admitted action")
        project = command["project_id"]
        workspace = command["workspace_id"]
        scope = f"workspace:{project}/{workspace}" if action == "VERIFY" else f"repo:{project}"
        with closing(self.registry.database.connect()) as connection:
            held = connection.execute(
                "SELECT 1 FROM locks WHERE request_id=? AND scope=?", (request_id, scope),
            ).fetchone()
        if held is None:
            raise SourceError("SOURCE_LOCK_MISSING", "Required source lock is not held")
        return command, operation

    def _project(self, project_id: str) -> Path:
        project = self.registry.get_project(project_id)
        if project is None:
            raise SourceError("PROJECT_NOT_REGISTERED", "Managed project source is missing")
        return Path(project["repo_path"])

    def _workspace(self, project_id: str, workspace_id: str) -> dict[str, Any]:
        workspace = self.registry.get_workspace(project_id, workspace_id)
        if workspace is None:
            raise SourceError("WORKSPACE_NOT_REGISTERED", "Managed workspace source is missing")
        if workspace["archived"]:
            raise SourceError("WORKSPACE_ARCHIVED", "Archived workspace is inactive")
        return workspace

    def inspect_workspace_source(self, project_id: str, workspace_id: str,
                                 *, remote: str | None = None) -> dict[str, Any]:
        """Read both durable identity and live Git state, including retained archives."""
        current = self.registry.get_workspace(project_id, workspace_id)
        if current is None:
            raise SourceError("WORKSPACE_NOT_REGISTERED", "Managed workspace source is missing")
        repo = self._project(project_id)
        observed = worktrees.inspect_workspace(repo, Path(current["worktree_path"]), remote=remote)
        return {
            **observed,
            "project_id": project_id,
            "workspace_id": workspace_id,
            "source_generation": current["generation"],
            "registered_head": current["head"],
            "registered_branch": current["branch"],
            "source_changed": (observed["head"] != current["head"]
                               or observed["branch"] != current["branch"]),
            "archived": bool(current["archived"]),
        }

    def create_new_source(self, request_id: str) -> dict[str, Any]:
        command, _ = self._command(request_id, "CREATE_PROJECT")
        params = command["params"]
        if params["source"]["kind"] != "new":
            raise SourceError("WRONG_SOURCE_KIND", "Project request is not a new-source request")
        project_id = command["project_id"]
        starter = starter_files(params["template_id"], project_id)
        effect = self.ledger.begin_effect(request_id, "local-project", "local_workspace",
                                          {"project_id": project_id, "kind": "new"})
        repo = worktrees.create_repository(self.registry.managed_root, project_id,
                                           starter_files=starter)
        record = self.registry.register_project(project_id, params["name"], repo, origin_kind="new")
        self.ledger.confirm_effect(effect, str(repo))
        return record

    def import_local_source(self, request_id: str) -> dict[str, Any]:
        command, _ = self._command(request_id, "CREATE_PROJECT")
        params = command["params"]
        source = params["source"]
        if source["kind"] != "local" or self.import_grants is None:
            raise SourceError("IMPORT_NOT_CONFIGURED", "Approved local import is unavailable")
        handle = source["import_handle"]
        project_id = command["project_id"]
        with self.import_grants.open_source(handle) as pinned:
            effect = self.ledger.begin_effect(request_id, "local-project", "local_workspace",
                                              {"project_id": project_id, "kind": "local",
                                               "import_handle": handle})
            repo = worktrees.import_local_granted(
                self.registry.managed_root, project_id, pinned.child_path,
                pass_fds=pinned.pass_fds,
            )
        record = self.registry.register_project(project_id, params["name"], repo,
                                                origin_kind="local", origin_reference=handle)
        self.ledger.confirm_effect(effect, str(repo))
        return record

    def create_initial_workspace_source(self, request_id: str) -> dict[str, Any]:
        """Create the first editable area inside the admitted CREATE_PROJECT operation.

        The IDs are trusted local policy, not values supplied by the root command.
        A prior uncertain effect is retained for explicit reconciliation. Only a
        confirmed effect with the exact registered Git state may be observed again.
        """
        command, operation = self._command(request_id, "CREATE_PROJECT")
        project_id = command["project_id"]
        workspace_id = "primary"
        branch = f"work/{project_id}"
        repo = self._project(project_id)
        project_effects = [item for item in operation["effects"]
                           if item["step_id"] == "local-project"
                           and item["kind"] == "local_workspace"]
        if (len(project_effects) != 1 or project_effects[0]["state"] != "CONFIRMED"
                or project_effects[0]["provider_id"] != str(repo)
                or project_effects[0]["intent"].get("project_id") != project_id):
            raise SourceError("PROJECT_NOT_PROVISIONED",
                              "The admitted operation has no confirmed managed project")
        base_head = run_git(repo, ["rev-parse", "--verify", "HEAD^{commit}"]).strip()
        worktree = self.registry.managed_root / project_id / "workspaces" / workspace_id
        intent = {"project_id": project_id, "workspace_id": workspace_id,
                  "branch": branch, "base_commit": base_head}
        previous = [item for item in operation["effects"]
                    if item["step_id"] == "initial-workspace"
                    and item["kind"] == "local_workspace"]
        if previous:
            if len(previous) != 1 or previous[0]["intent"] != intent:
                raise SourceError("INITIAL_WORKSPACE_INTENT_CHANGED",
                                  "Initial workspace intent differs from its recorded identity")
            if previous[0]["state"] != "CONFIRMED":
                raise SourceError("INITIAL_WORKSPACE_NEEDS_RECONCILIATION",
                                  "Prior initial workspace effect has no confirmed outcome")
            record = self.registry.get_workspace(project_id, workspace_id)
            if (previous[0]["provider_id"] != str(worktree) or record is None
                    or record["worktree_path"] != str(worktree)
                    or record["branch"] != branch or record["head"] != base_head
                    or record["generation"] != 0 or record["archived"]):
                raise SourceError("INITIAL_WORKSPACE_CHANGED",
                                  "Confirmed initial workspace no longer matches its record")
            observed = worktrees.inspect_workspace(repo, worktree)
            if (observed["head"] != base_head or observed["branch"] != branch
                    or observed["dirty"]):
                raise SourceError("INITIAL_WORKSPACE_CHANGED",
                                  "Confirmed initial workspace Git state changed")
            return record
        if self.registry.get_workspace(project_id, workspace_id) is not None:
            raise SourceError("INITIAL_WORKSPACE_EXISTS",
                              "An unowned initial workspace identity already exists")
        effect = self.ledger.begin_effect(request_id, "initial-workspace", "local_workspace", intent)
        path = worktrees.create_workspace(self.registry.managed_root, project_id, repo,
                                          workspace_id, branch, base_head)
        record = self.registry.register_workspace(project_id, workspace_id,
                                                  "Primary workspace", path,
                                                  branch=branch, head=base_head)
        observed = worktrees.inspect_workspace(repo, path)
        if (record["head"] != base_head or record["branch"] != branch
                or observed["head"] != base_head or observed["branch"] != branch
                or observed["dirty"]):
            raise SourceError("INITIAL_WORKSPACE_CHANGED",
                              "Initial workspace Git state differs from its record")
        self.ledger.confirm_effect(effect, str(path))
        current = self.registry.get_workspace(project_id, workspace_id)
        if current is None:
            raise SourceError("INITIAL_WORKSPACE_CHANGED",
                              "Confirmed initial workspace record is unavailable")
        return current

    def create_workspace_source(self, request_id: str) -> dict[str, Any]:
        command, _ = self._command(request_id, "CREATE_WORKSPACE")
        project_id, workspace_id = command["project_id"], command["workspace_id"]
        params = command["params"]
        repo = self._project(project_id)
        effect = self.ledger.begin_effect(request_id, "local-workspace", "local_workspace",
                                          {"project_id": project_id, "workspace_id": workspace_id,
                                           "branch": params["branch"], "base_commit": params["base_commit"]})
        path = worktrees.create_workspace(self.registry.managed_root, project_id, repo,
                                          workspace_id, params["branch"], params["base_commit"])
        record = self.registry.register_workspace(project_id, workspace_id, params["name"], path,
                                                   branch=params["branch"], head=params["base_commit"])
        self.ledger.confirm_effect(effect, str(path))
        return record

    def retarget_workspace_source(self, request_id: str, *, expected_generation: int,
                                  defer_confirmation: bool = False) -> dict[str, Any]:
        command, _ = self._command(request_id, "RETARGET_WORKSPACE")
        project_id, workspace_id = command["project_id"], command["workspace_id"]
        params = command["params"]
        current = self._workspace(project_id, workspace_id)
        if (current["generation"] != expected_generation or current["head"] != command["expected_head"]
                or current["branch"] != params["expected_branch"]):
            raise SourceError("WORKSPACE_CHANGED", "Workspace changed before branch retarget")
        repo = self.registry.checked_project_path(project_id)
        path = self.registry.checked_workspace_path(project_id, workspace_id)
        if params["target_branch"] == current["branch"] and params["target_commit"] == current["head"]:
            raise SourceError("NO_CHANGE", "Workspace already has the requested branch and commit")
        # Ordinary refusal must not leave an uncertain effect and retain the repo lock.
        worktrees.preflight_retarget_workspace(
            repo, path, expected_head=current["head"], expected_branch=current["branch"],
            target_branch=params["target_branch"], target_commit=params["target_commit"],
        )
        effect = self.ledger.begin_effect(request_id, "retarget-workspace", "local_workspace", {
            "project_id": project_id, "workspace_id": workspace_id,
            "from_branch": current["branch"], "from": current["head"],
            "from_generation": expected_generation,
            "to_branch": params["target_branch"], "to": params["target_commit"],
        })
        state = worktrees.retarget_workspace(
            repo, path, expected_head=current["head"],
            expected_branch=current["branch"], target_branch=params["target_branch"],
            target_commit=params["target_commit"],
        )
        updated = self.registry.transition_workspace(
            project_id, workspace_id, expected_generation=expected_generation,
            expected_head=current["head"], branch=params["target_branch"], head=state["head"],
        )
        # The full Drive-backed route keeps this intent open until both
        # association documents have durable readback proof. A crash between
        # the Git switch and those writes must retain the repository lock.
        if not defer_confirmation:
            self.ledger.confirm_effect(effect, state["head"])
        return updated

    def fetch_source(self, request_id: str, *, remote: str,
                     allowed_urls: Sequence[str]) -> str:
        command, _ = self._command(request_id, "FETCH")
        repo = self._project(command["project_id"])
        effect = self.ledger.begin_effect(request_id, "fetch", "git_ref",
                                          {"project_id": command["project_id"], "remote": remote})
        fetched = worktrees.fetch(repo, remote, allowed_urls=allowed_urls)
        self.ledger.confirm_effect(effect, fetched)
        return fetched

    def sync_workspace_source(self, request_id: str, *, expected_generation: int,
                              remote: str, remote_branch: str) -> dict[str, Any]:
        command, _ = self._command(request_id, "SYNC_WORKSPACE")
        project_id, workspace_id = command["project_id"], command["workspace_id"]
        params = command["params"]
        if params["remote_ref"] != f"{remote}/{remote_branch}":
            raise SourceError("REMOTE_NOT_ALLOWED", "Command target differs from approved remote")
        current = self._workspace(project_id, workspace_id)
        if (current["generation"] != expected_generation or current["head"] != command["expected_head"]):
            raise SourceError("WORKSPACE_CHANGED", "Workspace changed before sync")
        repo = self._project(project_id)
        effect = self.ledger.begin_effect(request_id, "sync-workspace", "local_workspace",
                                          {"project_id": project_id, "workspace_id": workspace_id,
                                           "from": current["head"], "to": params["target_commit"]})
        state = worktrees.sync_workspace(
            repo, Path(current["worktree_path"]), expected_head=current["head"],
            expected_branch=current["branch"], remote=remote, remote_branch=remote_branch,
            expected_target_commit=params["target_commit"],
        )
        if state["head"] != params["target_commit"]:
            raise SourceError("REMOTE_CHANGED", "Fetched remote ref changed before sync")
        updated = self.registry.transition_workspace(
            project_id, workspace_id, expected_generation=expected_generation,
            expected_head=current["head"], branch=current["branch"], head=state["head"],
        )
        self.ledger.confirm_effect(effect, state["head"])
        return updated

    def archive_workspace_source(self, request_id: str, *, expected_generation: int) -> dict[str, Any]:
        command, _ = self._command(request_id, "ARCHIVE_WORKSPACE")
        params = command["params"]
        if params["delete_local_clean"] or params["delete_remote_merged"]:
            raise SourceError("DELETION_UNAVAILABLE", "Archive retains source in this packet")
        project_id, workspace_id = command["project_id"], command["workspace_id"]
        current = self._workspace(project_id, workspace_id)
        if (current["generation"] != expected_generation or current["head"] != command["expected_head"]
                or current["branch"] != params["expected_branch"]):
            raise SourceError("WORKSPACE_CHANGED", "Workspace changed before archive")
        repo = self._project(project_id)
        worktrees.archive_workspace(repo, Path(current["worktree_path"]),
                                    expected_head=current["head"], expected_branch=current["branch"])
        return self.registry.transition_workspace(
            project_id, workspace_id, expected_generation=expected_generation,
            expected_head=current["head"], branch=current["branch"], head=current["head"],
            archive=True,
        )

    def _record_snapshot(self, request_id: str, command: dict[str, Any], *,
                         kind: str, construct: Any) -> Snapshot:
        project_id, workspace_id = command["project_id"], command["workspace_id"]
        snapshot_id = str(uuid4())
        stage_root = self.registry.managed_root / project_id / "snapshots"
        effect = self.ledger.begin_effect(request_id, "source-snapshot", "git_ref",
                                          {"snapshot_id": snapshot_id, "project_id": project_id,
                                           "workspace_id": workspace_id, "kind": kind,
                                           "expected_head": command["expected_head"]})
        try:
            snapshot: Snapshot = construct(snapshot_id, stage_root)
            self.registry.register_snapshot_record(snapshot)
        except BaseException:
            # Snapshot functions create this directory before touching Git objects.
            # A refusal before staging is provably no-write; otherwise retain
            # uncertain effect and repository/workspace lock for reconciliation.
            if not os.path.lexists(stage_root / snapshot_id):
                self.ledger.confirm_effect(effect, f"NO_WRITE:{snapshot_id}")
            raise
        self.ledger.confirm_effect(effect, snapshot.commit)
        return snapshot

    def stage_changeset_source(self, request_id: str, change: Mapping[str, Any]) -> Snapshot:
        command, _ = self._command(request_id, "DELIVER_VERIFY_PUBLISH")
        project_id, workspace_id = command["project_id"], command["workspace_id"]
        current = self._workspace(project_id, workspace_id)
        if current["head"] != command["expected_head"]:
            raise SourceError("WORKSPACE_CHANGED", "Workspace HEAD changed before changeset")
        repo = self._project(project_id)
        return self._record_snapshot(
            request_id, command, kind="CHANGESET_V1",
            construct=lambda snapshot_id, stage_root: stage_changeset(
                repo, Path(current["worktree_path"]), stage_root, change,
                project_id=project_id, workspace_id=workspace_id,
                expected_head=current["head"], source_generation=current["generation"],
                snapshot_id=snapshot_id,
            ),
        )

    def capture_clean_verify_source(self, request_id: str) -> Snapshot:
        command, _ = self._command(request_id, "VERIFY")
        project_id, workspace_id = command["project_id"], command["workspace_id"]
        current = self._workspace(project_id, workspace_id)
        if current["head"] != command["expected_head"]:
            raise SourceError("WORKSPACE_CHANGED", "Workspace HEAD changed before verify")
        repo = self._project(project_id)
        return self._record_snapshot(
            request_id, command, kind="CLEAN_VERIFY",
            construct=lambda snapshot_id, stage_root: capture_clean_verify(
                repo, Path(current["worktree_path"]), stage_root,
                project_id=project_id, workspace_id=workspace_id,
                expected_head=current["head"], source_generation=current["generation"],
                snapshot_id=snapshot_id,
            ),
        )

    def capture_dirty_verify_source(self, request_id: str,
                                    *, approved_files: Mapping[str, str | None]) -> Snapshot:
        command, operation = self._command(request_id, "VERIFY")
        if operation["source_id"] != "local-owner":
            raise SourceError("LOCAL_APPROVAL_REQUIRED", "Dirty source capture requires local owner approval")
        project_id, workspace_id = command["project_id"], command["workspace_id"]
        current = self._workspace(project_id, workspace_id)
        if current["head"] != command["expected_head"]:
            raise SourceError("WORKSPACE_CHANGED", "Workspace HEAD changed before verify")
        repo = self._project(project_id)
        return self._record_snapshot(
            request_id, command, kind="LOCAL_DIRTY_VERIFY",
            construct=lambda snapshot_id, stage_root: capture_dirty_verify(
                repo, Path(current["worktree_path"]), stage_root,
                project_id=project_id, workspace_id=workspace_id,
                expected_head=current["head"], source_generation=current["generation"],
                approved_files=approved_files, snapshot_id=snapshot_id,
            ),
        )
