"""Durable owner-managed Git source identities and immutable snapshot records.

This registry only records local facts. Git transitions and provider effects are
separate finite steps; a database row cannot itself prove a worktree mutation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

from bridge_reference.protocol import Rejected, safe_relative_path, validate_branch
from runner_app.git.argv import GitError, run_git
from runner_app.resources import load_schema
from runner_app.storage.database import Database
from runner_app.storage.paths import _absolute, ensure_private_directory, open_private_file

if TYPE_CHECKING:
    from runner_app.git.snapshots import Snapshot

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_OBJECT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_RUN_VALIDATOR = Draft202012Validator(load_schema("run-manifest"), format_checker=FormatChecker())


class SourceError(ValueError):
    """Stable local source refusal without leaking paths or Git output."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    if not isinstance(value, str) or _SLUG.fullmatch(value) is None:
        raise SourceError("INVALID_SOURCE_ID", "Managed source identity must be a canonical slug")
    return value


def _object_id(value: str) -> str:
    if not isinstance(value, str) or _OBJECT.fullmatch(value) is None:
        raise SourceError("INVALID_GIT_OBJECT", "A full lowercase Git object ID is required")
    return value


def _digest(value: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise SourceError("INVALID_FINGERPRINT", "A complete SHA-256 fingerprint is required")
    return value


def _uuid(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError("Noncanonical")
    except (TypeError, ValueError, AttributeError):
        raise SourceError("INVALID_SNAPSHOT_ID", "A canonical snapshot UUID is required") from None
    return value


class SourceRegistry:
    def __init__(self, database: Database, managed_root: Path) -> None:
        self.database = database
        self.managed_root = _absolute(managed_root)
        ensure_private_directory(self.managed_root)
        database.migrate()

    def _managed_path(self, path: Path, *, directory: bool, expected: Path | None = None) -> Path:
        path = _absolute(path)
        if expected is not None and path != expected:
            raise SourceError("OUTSIDE_MANAGED_ROOT", "Source path is outside its managed slot")
        try:
            parts = path.relative_to(self.managed_root).parts
        except ValueError:
            raise SourceError("OUTSIDE_MANAGED_ROOT", "Source path is outside the managed root") from None
        if not parts:
            raise SourceError("INVALID_SOURCE_PATH", "A source cannot be the managed root itself")
        current = self.managed_root
        for part in parts:
            current /= part
            try:
                info = current.lstat()
            except OSError:
                raise SourceError("SOURCE_MISSING", "Managed source path is missing") from None
            if info.st_uid != os.geteuid() or stat.S_ISLNK(info.st_mode):
                raise SourceError("UNSAFE_SOURCE_PATH", "Managed source path has unsafe ownership or link")
            if current != path and not stat.S_ISDIR(info.st_mode):
                raise SourceError("UNSAFE_SOURCE_PATH", "Managed source parent is not a directory")
        info = path.lstat()
        if directory and not stat.S_ISDIR(info.st_mode):
            raise SourceError("UNSAFE_SOURCE_PATH", "Managed source must be a directory")
        if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                              or stat.S_IMODE(info.st_mode) != 0o600):
            raise SourceError("UNSAFE_SOURCE_PATH", "Snapshot manifest must be a private regular file")
        return path

    @staticmethod
    def _manifest_sha256(path: Path) -> str:
        descriptor = open_private_file(path)
        with os.fdopen(descriptor, "rb") as stream:
            content = stream.read(1_048_577)
        if len(content) > 1_048_576:
            raise SourceError("SNAPSHOT_MANIFEST_TOO_LARGE", "Snapshot manifest exceeds the local limit")
        return hashlib.sha256(content).hexdigest()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.database.connect()
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

    def register_project(self, project_id: str, display_name: str, repo_path: Path,
                         *, origin_kind: str, origin_reference: str | None = None) -> dict[str, Any]:
        project_id = _slug(project_id)
        if not isinstance(display_name, str) or not 1 <= len(display_name) <= 80:
            raise SourceError("INVALID_DISPLAY_NAME", "Project display name is required")
        if origin_kind not in {"new", "local", "github"}:
            raise SourceError("INVALID_ORIGIN", "Unsupported source origin")
        if origin_reference is not None and (not isinstance(origin_reference, str)
                                             or not 1 <= len(origin_reference) <= 1000
                                             or (origin_kind == "local" and "/" in origin_reference)):
            raise SourceError("INVALID_ORIGIN", "Source reference must be a local handle or approved identity")
        repo_path = self._managed_path(repo_path, directory=True,
                                       expected=self.managed_root / project_id / "repo.git")
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT p.display_name,s.repo_path,s.origin_kind,s.origin_reference FROM projects p "
                "JOIN source_projects s USING(project_id) WHERE p.project_id=?", (project_id,),
            ).fetchone()
            if existing is not None:
                if (existing["display_name"] != display_name or existing["repo_path"] != str(repo_path)
                        or existing["origin_kind"] != origin_kind
                        or existing["origin_reference"] != origin_reference):
                    raise SourceError("PROJECT_ID_REUSED", "Project identity already has a different source")
                return dict(existing)
            timestamp = _timestamp()
            connection.execute(
                "INSERT INTO projects(project_id,display_name,state,metadata_json,created_at,updated_at) "
                "VALUES(?,?, 'LOCAL_READY','{}',?,?)",
                (project_id, display_name, timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO source_projects(project_id,repo_path,origin_kind,origin_reference,created_at) "
                "VALUES(?,?,?,?,?)", (project_id, str(repo_path), origin_kind, origin_reference, timestamp),
            )
            return {"display_name": display_name, "repo_path": str(repo_path),
                    "origin_kind": origin_kind, "origin_reference": origin_reference}

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        project_id = _slug(project_id)
        connection = self.database.connect()
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT p.display_name,p.state,s.repo_path,s.origin_kind,s.origin_reference "
                "FROM projects p JOIN source_projects s USING(project_id) WHERE p.project_id=?",
                (project_id,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def register_workspace(self, project_id: str, workspace_id: str, display_name: str,
                           worktree_path: Path, *, branch: str, head: str) -> dict[str, Any]:
        project_id, workspace_id = _slug(project_id), _slug(workspace_id)
        if not isinstance(display_name, str) or not 1 <= len(display_name) <= 80:
            raise SourceError("INVALID_DISPLAY_NAME", "Workspace display name is required")
        try:
            validate_branch(branch)
        except Rejected:
            raise SourceError("INVALID_BRANCH", "Workspace branch is invalid") from None
        head = _object_id(head)
        path = self._managed_path(worktree_path, directory=True,
                                  expected=self.managed_root / project_id / "workspaces" / workspace_id)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT w.display_name,w.branch,w.state,s.worktree_path,s.head,s.generation,s.archived "
                "FROM workspaces w JOIN source_workspaces s USING(project_id,workspace_id) "
                "WHERE w.project_id=? AND w.workspace_id=?", (project_id, workspace_id),
            ).fetchone()
            if existing is not None:
                if (existing["display_name"] != display_name or existing["branch"] != branch
                        or existing["worktree_path"] != str(path) or existing["head"] != head):
                    raise SourceError("WORKSPACE_ID_REUSED", "Workspace identity already has different source")
                return dict(existing)
            timestamp = _timestamp()
            metadata = json.dumps({"source_generation": 0, "head": head}, sort_keys=True)
            connection.execute(
                "INSERT INTO workspaces(project_id,workspace_id,branch,state,metadata_json,created_at,updated_at,display_name) "
                "VALUES(?,?,?,'ACTIVE',?,?,?,?)",
                (project_id, workspace_id, branch, metadata, timestamp, timestamp, display_name),
            )
            connection.execute(
                "INSERT INTO source_workspaces(project_id,workspace_id,worktree_path,head,generation,updated_at) "
                "VALUES(?,?,?,?,0,?)", (project_id, workspace_id, str(path), head, timestamp),
            )
            return {"display_name": display_name, "branch": branch, "state": "ACTIVE",
                    "worktree_path": str(path), "head": head, "generation": 0, "archived": 0}

    def get_workspace(self, project_id: str, workspace_id: str) -> dict[str, Any] | None:
        project_id, workspace_id = _slug(project_id), _slug(workspace_id)
        connection = self.database.connect()
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT w.display_name,w.branch,w.state,s.worktree_path,s.head,s.generation,"
                "s.capture_fingerprint,s.archived FROM workspaces w "
                "JOIN source_workspaces s USING(project_id,workspace_id) "
                "WHERE w.project_id=? AND w.workspace_id=?", (project_id, workspace_id),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def transition_workspace(self, project_id: str, workspace_id: str, *, expected_generation: int,
                             expected_head: str, branch: str, head: str,
                             capture_fingerprint: str | None = None,
                             archive: bool = False) -> dict[str, Any]:
        project_id, workspace_id = _slug(project_id), _slug(workspace_id)
        _object_id(expected_head)
        head = _object_id(head)
        try:
            validate_branch(branch)
        except Rejected:
            raise SourceError("INVALID_BRANCH", "Workspace branch is invalid") from None
        if capture_fingerprint is not None:
            _digest(capture_fingerprint)
        if isinstance(expected_generation, bool) or not isinstance(expected_generation, int) or expected_generation < 0:
            raise SourceError("INVALID_GENERATION", "Expected source generation is invalid")
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT head,generation,archived FROM source_workspaces WHERE project_id=? AND workspace_id=?",
                (project_id, workspace_id),
            ).fetchone()
            if current is None:
                raise SourceError("WORKSPACE_NOT_REGISTERED", "Workspace source is not registered")
            if current["head"] != expected_head or current["generation"] != expected_generation:
                raise SourceError("WORKSPACE_CHANGED", "Workspace source changed before transition")
            if current["archived"]:
                raise SourceError("WORKSPACE_ARCHIVED", "Archived workspace cannot transition")
            generation = expected_generation + 1
            timestamp = _timestamp()
            state = "ARCHIVED" if archive else "ACTIVE"
            metadata = json.dumps({"source_generation": generation, "head": head}, sort_keys=True)
            connection.execute(
                "UPDATE source_workspaces SET head=?,generation=?,capture_fingerprint=?,archived=?,updated_at=? "
                "WHERE project_id=? AND workspace_id=?",
                (head, generation, capture_fingerprint, int(archive), timestamp, project_id, workspace_id),
            )
            connection.execute(
                "UPDATE workspaces SET branch=?,state=?,metadata_json=?,updated_at=? "
                "WHERE project_id=? AND workspace_id=?",
                (branch, state, metadata, timestamp, project_id, workspace_id),
            )
            return {"project_id": project_id, "workspace_id": workspace_id, "branch": branch,
                    "head": head, "generation": generation, "state": state, "archived": archive}

    def register_snapshot(self, snapshot_id: str, project_id: str, workspace_id: str, *,
                          base_head: str, commit: str, tree: str, source_generation: int,
                          capture_fingerprint: str, provenance: str, dirty_at_capture: bool,
                          included_paths: Sequence[str], manifest_path: Path) -> dict[str, Any]:
        snapshot_id, project_id, workspace_id = _uuid(snapshot_id), _slug(project_id), _slug(workspace_id)
        for value in (base_head, commit, tree):
            _object_id(value)
        _digest(capture_fingerprint)
        if isinstance(source_generation, bool) or not isinstance(source_generation, int) or source_generation < 0:
            raise SourceError("INVALID_GENERATION", "Source generation must be nonnegative")
        if provenance not in {"CHANGESET_V1", "LOCAL_DIRTY_VERIFY", "CLEAN_VERIFY"}:
            raise SourceError("INVALID_PROVENANCE", "Source snapshot provenance is not recognized")
        if not isinstance(dirty_at_capture, bool):
            raise SourceError("INVALID_SNAPSHOT", "Dirty-at-capture must be boolean")
        if not isinstance(included_paths, (tuple, list)) or len(included_paths) > 1000:
            raise SourceError("INVALID_SNAPSHOT", "Included file list is invalid")
        try:
            for item in included_paths:
                if not isinstance(item, str):
                    raise TypeError("Non-string path")
                safe_relative_path(item)
        except (Rejected, TypeError):
            raise SourceError("INVALID_SNAPSHOT", "Included file list is invalid") from None
        manifest_path = self._managed_path(manifest_path, directory=False,
                                           expected=self.managed_root / project_id / "snapshots"
                                           / snapshot_id / "manifest.json")
        manifest_sha256 = self._manifest_sha256(manifest_path)
        payload = (snapshot_id, project_id, workspace_id, base_head, commit, tree,
                   source_generation, capture_fingerprint, provenance, int(dirty_at_capture),
                   json.dumps(list(included_paths), separators=(",", ":"), ensure_ascii=False),
                   str(manifest_path), manifest_sha256)
        with self._transaction() as connection:
            previous = connection.execute("SELECT * FROM source_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
            if previous is not None:
                if tuple(previous[key] for key in (
                    "snapshot_id", "project_id", "workspace_id", "base_head", "snapshot_commit",
                    "tree_digest", "source_generation", "capture_fingerprint", "provenance",
                    "dirty_at_capture", "included_paths_json", "manifest_path", "manifest_sha256",
                )) != payload:
                    raise SourceError("SNAPSHOT_ID_REUSED", "Snapshot identity already has different content")
                return self._snapshot(previous)
            current = connection.execute(
                "SELECT generation FROM source_workspaces WHERE project_id=? AND workspace_id=?",
                (project_id, workspace_id),
            ).fetchone()
            if current is None or current["generation"] != source_generation:
                raise SourceError("WORKSPACE_CHANGED", "Source generation changed before snapshot record")
            timestamp = _timestamp()
            connection.execute(
                "INSERT INTO source_snapshots(snapshot_id,project_id,workspace_id,base_head,snapshot_commit,"
                "tree_digest,source_generation,capture_fingerprint,provenance,dirty_at_capture,"
                "included_paths_json,manifest_path,manifest_sha256,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*payload, timestamp),
            )
            row = connection.execute("SELECT * FROM source_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
            assert row is not None
            return self._snapshot(row)

    def _verify_snapshot_ref(self, snapshot_id: str, project_id: str,
                             commit: str, tree: str) -> None:
        project = self.get_project(project_id)
        if project is None:
            raise SourceError("PROJECT_NOT_REGISTERED", "Snapshot project is not registered")
        repo = self._managed_path(Path(project["repo_path"]), directory=True,
                                  expected=self.managed_root / project_id / "repo.git")
        ref = f"refs/cdr/snapshots/{snapshot_id}"
        try:
            actual_commit = run_git(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"] ).strip()
            actual_tree = run_git(repo, ["rev-parse", "--verify", f"{commit}^{{tree}}"] ).strip()
        except GitError:
            raise SourceError("SNAPSHOT_REF_MISSING", "Retained Git snapshot ref is unavailable") from None
        if actual_commit != commit or actual_tree != tree:
            raise SourceError("SNAPSHOT_REF_CHANGED", "Retained Git snapshot differs from manifest")

    def register_snapshot_record(self, snapshot: Snapshot) -> dict[str, Any]:
        """Persist an immutable Git snapshot object after its manifest is fsynced."""
        if self._manifest_sha256(snapshot.manifest_path) != snapshot.manifest_sha256:
            raise SourceError("SNAPSHOT_MANIFEST_CHANGED", "Snapshot manifest changed before record")
        self._verify_snapshot_ref(snapshot.snapshot_id, snapshot.project_id,
                                  snapshot.commit, snapshot.tree)
        recorded = self.register_snapshot(
            snapshot.snapshot_id, snapshot.project_id, snapshot.workspace_id,
            base_head=snapshot.base_head, commit=snapshot.commit, tree=snapshot.tree,
            source_generation=snapshot.source_generation,
            capture_fingerprint=snapshot.capture_fingerprint,
            provenance=snapshot.provenance, dirty_at_capture=snapshot.dirty_at_capture,
            included_paths=snapshot.included_paths, manifest_path=snapshot.manifest_path,
        )
        if recorded["manifest_sha256"] != snapshot.manifest_sha256:
            raise SourceError("SNAPSHOT_MANIFEST_CHANGED", "Snapshot manifest changed before record")
        return recorded

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["included_paths"] = json.loads(value.pop("included_paths_json"))
        value["dirty_at_capture"] = bool(value["dirty_at_capture"])
        return value

    def _bind_tested_run_record(self, connection: sqlite3.Connection, run_id: str,
                                snapshot_id: str, snapshot: dict[str, Any], *,
                                allow_insert: bool = True) -> dict[str, str]:
        """Insert the binding using the caller's active transaction, including run creation."""
        self._verify_snapshot_ref(snapshot_id, snapshot["project_id"],
                                  snapshot["snapshot_commit"], snapshot["tree_digest"])
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT run_id,request_id,project_id,workspace_id,manifest_json FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None or row["project_id"] != snapshot["project_id"] or row["workspace_id"] != snapshot["workspace_id"]:
            raise SourceError("RUN_NOT_TESTED", "No run for this source snapshot")
        effect = connection.execute(
            "SELECT intent_json,state,provider_id FROM effects WHERE request_id=? "
            "AND step_id='source-snapshot' AND kind='git_ref'", (row["request_id"],),
        ).fetchone()
        try:
            intent = json.loads(effect["intent_json"]) if effect else None
        except (TypeError, ValueError):
            intent = None
        if (effect is None or effect["state"] != "CONFIRMED"
                or effect["provider_id"] != snapshot["snapshot_commit"]
                or not isinstance(intent, dict)
                or intent.get("snapshot_id") != snapshot_id
                or intent.get("expected_head") != snapshot["base_head"]):
            raise SourceError("RUN_NOT_TESTED", "Run snapshot was not captured by its operation")
        execution = connection.execute(
            "SELECT snapshot_id,backend,containment_json FROM run_executions WHERE run_id=?",
            (run_id,),
        ).fetchone()
        try:
            containment = json.loads(execution["containment_json"]) if execution else None
        except (TypeError, ValueError):
            containment = None
        if (execution is None or execution["snapshot_id"] != snapshot_id
                or execution["backend"] != "rootless_docker"
                or not isinstance(containment, dict)
                or containment.get("verified") is not True
                or containment.get("output_collection_complete") is not True):
            raise SourceError("RUN_NOT_TESTED", "Run lacks contained execution for this snapshot")
        try:
            manifest = json.loads(row["manifest_json"])
        except (TypeError, ValueError):
            raise SourceError("RUN_NOT_TESTED", "Run manifest is invalid") from None
        if (not isinstance(manifest, dict) or not _RUN_VALIDATOR.is_valid(manifest)
                or manifest["run_id"] != run_id or manifest["request_id"] != row["request_id"]
                or manifest["project_id"] != snapshot["project_id"]
                or manifest["workspace_id"] != snapshot["workspace_id"]
                or manifest["snapshot"]["commit"] != snapshot["snapshot_commit"]
                or manifest["snapshot"]["tree"] != snapshot["tree_digest"]
                or manifest["snapshot"]["source_dirty_at_capture"] != snapshot["dirty_at_capture"]
                or manifest["operation_status"] != "COMPLETED"
                or manifest["execution"]["outcome"] not in {"PASSED", "FAILED", "TIMED_OUT"}):
            raise SourceError("RUN_NOT_TESTED", "Run did not test this exact source snapshot")
        existing = connection.execute(
            "SELECT snapshot_id FROM source_run_snapshots WHERE run_id=?", (run_id,),
        ).fetchone()
        if existing is not None:
            if existing["snapshot_id"] != snapshot_id:
                raise SourceError("RUN_SNAPSHOT_CHANGED", "Run already binds another snapshot")
            return {"run_id": run_id, "snapshot_id": snapshot_id}
        if not allow_insert:
            raise SourceError("RUN_NOT_TESTED", "Run has no tested source snapshot")
        connection.execute(
            "INSERT INTO source_run_snapshots(run_id,snapshot_id,created_at) VALUES(?,?,?)",
            (run_id, snapshot_id, _timestamp()),
        )
        return {"run_id": run_id, "snapshot_id": snapshot_id}

    def bind_tested_run(self, run_id: str, snapshot_id: str) -> dict[str, str]:
        """Bind only an attested contained run to its exact immutable source snapshot."""
        run_id, snapshot_id = _uuid(run_id), _uuid(snapshot_id)
        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None:
            raise SourceError("SNAPSHOT_NOT_FOUND", "Source snapshot is not registered")
        with self._transaction() as connection:
            return self._bind_tested_run_record(connection, run_id, snapshot_id, snapshot)

    def publication_target(self, run_id: str, *, project_id: str,
                           workspace_id: str) -> dict[str, str]:
        """Select only a run-bound tested commit, never a newly staged or moving checkout."""
        run_id, project_id, workspace_id = _uuid(run_id), _slug(project_id), _slug(workspace_id)
        connection = self.database.connect()
        connection.row_factory = sqlite3.Row
        try:
            binding = connection.execute(
                "SELECT snapshot_id FROM source_run_snapshots WHERE run_id=?", (run_id,),
            ).fetchone()
        finally:
            connection.close()
        if binding is None:
            raise SourceError("RUN_NOT_TESTED", "Run has no tested source snapshot")
        snapshot_id = str(binding["snapshot_id"])
        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None or snapshot["project_id"] != project_id or snapshot["workspace_id"] != workspace_id:
            raise SourceError("SNAPSHOT_SCOPE_MISMATCH", "Run snapshot belongs to another workspace")
        # A legacy binding alone is insufficient after migration 4. Recheck the
        # immutable run, admission effect and contained collection attestation.
        connection = self.database.connect()
        try:
            self._bind_tested_run_record(connection, run_id, snapshot_id, snapshot,
                                         allow_insert=False)
        finally:
            connection.close()
        return {"snapshot_id": snapshot_id, "commit": str(snapshot["snapshot_commit"]),
                "tree": str(snapshot["tree_digest"])}

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        snapshot_id = _uuid(snapshot_id)
        connection = self.database.connect()
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute("SELECT * FROM source_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
            if row is None:
                return None
            path = self._managed_path(Path(row["manifest_path"]), directory=False)
            if self._manifest_sha256(path) != row["manifest_sha256"]:
                raise SourceError("SNAPSHOT_MANIFEST_CHANGED", "Immutable snapshot manifest changed")
            return self._snapshot(row)
        finally:
            connection.close()
