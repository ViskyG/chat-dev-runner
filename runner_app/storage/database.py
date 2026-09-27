"""SQLite foundation and transactional, monotonically versioned migrations.

Connections use explicit transactions (autocommit outside BEGIN). Call migrate()
when opening the application, then close every connection. The foundation schema
starts with metadata in v1 and durable domain tables in v2.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .paths import UnsafePathError, _absolute, _check_private, _open_directory, _open_file_at

SCHEMA_VERSION = 6
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        (
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL, "
            "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
        ),
        "INSERT INTO schema_migrations(version, name) VALUES (1, 'foundation')",
    ),
    2: (
        (
            "CREATE TABLE runner_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
            "runner_id TEXT NOT NULL, epoch TEXT NOT NULL, armed INTEGER NOT NULL CHECK(armed IN (0,1)), "
            "started_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        ),
        (
            "CREATE TABLE projects (project_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, "
            "state TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        ),
        (
            "CREATE TABLE workspaces (project_id TEXT NOT NULL REFERENCES projects(project_id), workspace_id TEXT NOT NULL, "
            "branch TEXT, state TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, PRIMARY KEY(project_id,workspace_id))"
        ),
        (
            "CREATE TABLE operations (sequence INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL UNIQUE, "
            "digest TEXT NOT NULL, command_json TEXT NOT NULL, source_id TEXT NOT NULL, project_id TEXT, workspace_id TEXT, "
            "action TEXT, status TEXT NOT NULL CHECK(status IN ('RUNNING','COMPLETED','REJECTED','BLOCKED','FAULT','INTERRUPTED','CANCELLED')), "
            "code TEXT, received_at TEXT NOT NULL, updated_at TEXT NOT NULL, receipt_json TEXT NOT NULL, result_json TEXT, "
            "capacity_held INTEGER NOT NULL CHECK(capacity_held IN (0,1)), "
            "reconcile_required INTEGER NOT NULL DEFAULT 0 CHECK(reconcile_required IN (0,1)), "
            "delivery_status TEXT NOT NULL DEFAULT 'NOT_ATTEMPTED')"
        ),
        "CREATE TABLE locks (scope TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES operations(request_id), created_at TEXT NOT NULL)",
        "CREATE INDEX locks_by_request ON locks(request_id)",
        (
            "CREATE TABLE steps (request_id TEXT NOT NULL REFERENCES operations(request_id), step_id TEXT NOT NULL, "
            "last_event_type TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(request_id,step_id))"
        ),
        (
            "CREATE TABLE effects (effect_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES operations(request_id), "
            "step_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('github_repository','git_ref','pull_request','drive_file','local_workspace')), "
            "intent_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('INTENDED','CONFIRMED','UNKNOWN')), provider_id TEXT, url TEXT, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(request_id,step_id,kind), "
            "FOREIGN KEY(request_id,step_id) REFERENCES steps(request_id,step_id))"
        ),
        (
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES operations(request_id), "
            "project_id TEXT NOT NULL, workspace_id TEXT NOT NULL, manifest_json TEXT NOT NULL, created_at TEXT NOT NULL, "
            "FOREIGN KEY(project_id,workspace_id) REFERENCES workspaces(project_id,workspace_id))"
        ),
        (
            "CREATE TABLE outputs (output_id TEXT NOT NULL, request_id TEXT NOT NULL REFERENCES operations(request_id), "
            "run_id TEXT REFERENCES runs(run_id), manifest_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(request_id,output_id))"
        ),
        (
            "CREATE TABLE remote_resources (resource_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES operations(request_id), "
            "effect_id TEXT REFERENCES effects(effect_id), provider TEXT NOT NULL, provider_id TEXT NOT NULL, "
            "metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, UNIQUE(provider,provider_id))"
        ),
        (
            "CREATE TABLE faults (fault_id TEXT PRIMARY KEY, request_id TEXT REFERENCES operations(request_id), "
            "code TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL)"
        ),
        (
            "CREATE TABLE journal (event_id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT REFERENCES operations(request_id), "
            "step_id TEXT, timestamp TEXT NOT NULL, event_type TEXT NOT NULL, detail_json TEXT NOT NULL)"
        ),
        "CREATE INDEX journal_by_request ON journal(request_id,event_id)",
        "CREATE TRIGGER journal_no_update BEFORE UPDATE ON journal BEGIN SELECT RAISE(ABORT,'journal is append-only'); END",
        "CREATE TRIGGER journal_no_delete BEFORE DELETE ON journal BEGIN SELECT RAISE(ABORT,'journal is append-only'); END",
        (
            "CREATE TRIGGER operations_frozen BEFORE UPDATE OF request_id,digest,command_json,source_id,project_id,workspace_id,action,received_at "
            "ON operations BEGIN SELECT RAISE(ABORT,'operation intent is immutable'); END"
        ),
        "CREATE TRIGGER operations_no_delete BEFORE DELETE ON operations BEGIN SELECT RAISE(ABORT,'operation history is immutable'); END",
        (
            "CREATE TRIGGER effects_frozen BEFORE UPDATE OF effect_id,request_id,step_id,kind,intent_json,created_at "
            "ON effects BEGIN SELECT RAISE(ABORT,'effect intent is immutable'); END"
        ),
        "CREATE TRIGGER effects_no_delete BEFORE DELETE ON effects BEGIN SELECT RAISE(ABORT,'effect history is immutable'); END",
        "INSERT INTO schema_migrations(version, name) VALUES (2, 'durable-domain')",
    ),
    3: (
        "ALTER TABLE workspaces ADD COLUMN display_name TEXT NOT NULL DEFAULT ''",
        (
            "CREATE TABLE source_projects (project_id TEXT PRIMARY KEY REFERENCES projects(project_id), "
            "repo_path TEXT NOT NULL UNIQUE, origin_kind TEXT NOT NULL "
            "CHECK(origin_kind IN ('new','local','github')), origin_reference TEXT, "
            "created_at TEXT NOT NULL)"
        ),
        (
            "CREATE TABLE source_workspaces (project_id TEXT NOT NULL, workspace_id TEXT NOT NULL, "
            "worktree_path TEXT NOT NULL UNIQUE, head TEXT NOT NULL, generation INTEGER NOT NULL "
            "CHECK(generation>=0), capture_fingerprint TEXT, archived INTEGER NOT NULL DEFAULT 0 "
            "CHECK(archived IN (0,1)), updated_at TEXT NOT NULL, "
            "PRIMARY KEY(project_id,workspace_id), "
            "FOREIGN KEY(project_id,workspace_id) REFERENCES workspaces(project_id,workspace_id))"
        ),
        (
            "CREATE TABLE source_snapshots (snapshot_id TEXT PRIMARY KEY, "
            "project_id TEXT NOT NULL, workspace_id TEXT NOT NULL, base_head TEXT NOT NULL, "
            "snapshot_commit TEXT NOT NULL, tree_digest TEXT NOT NULL, source_generation INTEGER NOT NULL, "
            "capture_fingerprint TEXT NOT NULL, provenance TEXT NOT NULL, dirty_at_capture INTEGER NOT NULL "
            "CHECK(dirty_at_capture IN (0,1)), included_paths_json TEXT NOT NULL, "
            "manifest_path TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, "
            "FOREIGN KEY(project_id,workspace_id) REFERENCES source_workspaces(project_id,workspace_id))"
        ),
        "CREATE INDEX source_snapshots_by_workspace ON source_snapshots(project_id,workspace_id,created_at)",
        "CREATE TRIGGER source_snapshots_no_update BEFORE UPDATE ON source_snapshots BEGIN SELECT RAISE(ABORT,'source snapshot is immutable'); END",
        "CREATE TRIGGER source_snapshots_no_delete BEFORE DELETE ON source_snapshots BEGIN SELECT RAISE(ABORT,'source snapshot is immutable'); END",
        (
            "CREATE TABLE source_run_snapshots (run_id TEXT PRIMARY KEY REFERENCES runs(run_id), "
            "snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id), created_at TEXT NOT NULL)"
        ),
        "CREATE TRIGGER source_run_snapshots_no_update BEFORE UPDATE ON source_run_snapshots BEGIN SELECT RAISE(ABORT,'run snapshot binding is immutable'); END",
        "CREATE TRIGGER source_run_snapshots_no_delete BEFORE DELETE ON source_run_snapshots BEGIN SELECT RAISE(ABORT,'run snapshot binding is immutable'); END",
        "CREATE TRIGGER runs_no_update BEFORE UPDATE ON runs BEGIN SELECT RAISE(ABORT,'run manifest is immutable'); END",
        "CREATE TRIGGER runs_no_delete BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT,'run manifest is immutable'); END",
        "INSERT INTO schema_migrations(version, name) VALUES (3, 'managed-source-snapshots')",
    ),
    4: (
        (
            "CREATE TABLE run_executions (run_id TEXT PRIMARY KEY REFERENCES runs(run_id), "
            "snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id), "
            "profile_id TEXT NOT NULL, profile_revision TEXT NOT NULL, image_digest TEXT NOT NULL, "
            "backend TEXT NOT NULL CHECK(backend='rootless_docker'), argv_sha256 TEXT NOT NULL, "
            "started_at TEXT NOT NULL, ended_at TEXT NOT NULL, containment_json TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        ),
        "CREATE TRIGGER run_executions_no_update BEFORE UPDATE ON run_executions BEGIN SELECT RAISE(ABORT,'run execution is immutable'); END",
        "CREATE TRIGGER run_executions_no_delete BEFORE DELETE ON run_executions BEGIN SELECT RAISE(ABORT,'run execution is immutable'); END",
        (
            "CREATE TABLE output_files (request_id TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(run_id), "
            "output_id TEXT NOT NULL, private_path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, "
            "size_bytes INTEGER NOT NULL CHECK(size_bytes>=0), created_at TEXT NOT NULL, "
            "PRIMARY KEY(request_id,output_id), "
            "FOREIGN KEY(request_id,output_id) REFERENCES outputs(request_id,output_id))"
        ),
        "CREATE INDEX output_files_by_run ON output_files(run_id,output_id)",
        "CREATE TRIGGER output_files_no_update BEFORE UPDATE ON output_files BEGIN SELECT RAISE(ABORT,'output file record is immutable'); END",
        "CREATE TRIGGER output_files_no_delete BEFORE DELETE ON output_files BEGIN SELECT RAISE(ABORT,'output file record is immutable'); END",
        "CREATE TRIGGER outputs_no_update BEFORE UPDATE ON outputs BEGIN SELECT RAISE(ABORT,'output record is immutable'); END",
        "CREATE TRIGGER outputs_no_delete BEFORE DELETE ON outputs BEGIN SELECT RAISE(ABORT,'output record is immutable'); END",
        "INSERT INTO schema_migrations(version, name) VALUES (4, 'contained-run-evidence')",
    ),
    5: (
        (
            "CREATE TABLE run_attempts (request_id TEXT PRIMARY KEY REFERENCES operations(request_id), "
            "run_id TEXT NOT NULL UNIQUE, snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id), "
            "created_at TEXT NOT NULL)"
        ),
        (
            "CREATE TRIGGER run_attempts_no_update BEFORE UPDATE ON run_attempts "
            "BEGIN SELECT RAISE(ABORT,'run attempt is immutable'); END"
        ),
        (
            "CREATE TRIGGER run_attempts_no_delete BEFORE DELETE ON run_attempts "
            "BEGIN SELECT RAISE(ABORT,'run attempt is immutable'); END"
        ),
        "INSERT INTO schema_migrations(version, name) VALUES (5, 'single-run-attempt')",
    ),
    6: (
        (
            "CREATE TABLE run_delivery_claims (delivery_request_id TEXT PRIMARY KEY "
            "REFERENCES operations(request_id), run_id TEXT NOT NULL REFERENCES runs(run_id), "
            "created_at TEXT NOT NULL)"
        ),
        "CREATE INDEX run_delivery_claims_by_run ON run_delivery_claims(run_id)",
        (
            "INSERT INTO run_delivery_claims(delivery_request_id,run_id,created_at) "
            "SELECT o.request_id,r.run_id,o.received_at FROM operations o "
            "JOIN runs r ON r.run_id=json_extract(o.command_json,'$.params.source_run_id') "
            "JOIN run_executions e ON e.run_id=r.run_id "
            "JOIN operations s ON s.request_id=r.request_id "
            "WHERE o.action='PUBLISH_REPORT' AND o.status!='REJECTED' "
            "AND s.action IN ('VERIFY','DELIVER_VERIFY_PUBLISH') "
            "AND s.status IN ('COMPLETED','BLOCKED','FAULT','INTERRUPTED','CANCELLED') "
            "AND o.project_id=r.project_id AND o.workspace_id=r.workspace_id "
            "AND s.project_id=r.project_id AND s.workspace_id=r.workspace_id"
        ),
        (
            "CREATE TRIGGER run_delivery_claims_no_update BEFORE UPDATE ON run_delivery_claims "
            "BEGIN SELECT RAISE(ABORT,'delivery claim is immutable'); END"
        ),
        (
            "CREATE TRIGGER run_delivery_claims_no_delete BEFORE DELETE ON run_delivery_claims "
            "BEGIN SELECT RAISE(ABORT,'delivery claim is immutable'); END"
        ),
        (
            "CREATE TABLE run_delivery_claim_releases (delivery_request_id TEXT PRIMARY KEY "
            "REFERENCES run_delivery_claims(delivery_request_id), released_at TEXT NOT NULL)"
        ),
        (
            "INSERT INTO run_delivery_claim_releases(delivery_request_id,released_at) "
            "SELECT c.delivery_request_id,o.updated_at FROM run_delivery_claims c "
            "JOIN operations o ON o.request_id=c.delivery_request_id "
            "WHERE o.status='COMPLETED' AND o.delivery_status='DELIVERED'"
        ),
        (
            "CREATE TRIGGER run_delivery_claim_releases_no_update BEFORE UPDATE ON run_delivery_claim_releases "
            "BEGIN SELECT RAISE(ABORT,'delivery claim release is immutable'); END"
        ),
        (
            "CREATE TRIGGER run_delivery_claim_releases_no_delete BEFORE DELETE ON run_delivery_claim_releases "
            "BEGIN SELECT RAISE(ABORT,'delivery claim release is immutable'); END"
        ),
        (
            "CREATE TABLE run_retention_pin_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "run_id TEXT NOT NULL REFERENCES runs(run_id), action TEXT NOT NULL "
            "CHECK(action IN ('PIN','UNPIN')), created_at TEXT NOT NULL)"
        ),
        "CREATE INDEX run_retention_pin_events_by_run ON run_retention_pin_events(run_id,event_id)",
        (
            "CREATE TRIGGER run_retention_pin_events_no_update BEFORE UPDATE ON run_retention_pin_events "
            "BEGIN SELECT RAISE(ABORT,'retention pin event is immutable'); END"
        ),
        (
            "CREATE TRIGGER run_retention_pin_events_no_delete BEFORE DELETE ON run_retention_pin_events "
            "BEGIN SELECT RAISE(ABORT,'retention pin event is immutable'); END"
        ),
        (
            "CREATE TABLE output_tombstones (run_id TEXT NOT NULL REFERENCES runs(run_id), "
            "output_id TEXT NOT NULL, reason TEXT NOT NULL CHECK(reason='RETENTION_EXPIRED'), "
            "created_at TEXT NOT NULL, PRIMARY KEY(run_id,output_id))"
        ),
        (
            "CREATE TRIGGER output_tombstones_no_update BEFORE UPDATE ON output_tombstones "
            "BEGIN SELECT RAISE(ABORT,'output tombstone is immutable'); END"
        ),
        (
            "CREATE TRIGGER output_tombstones_no_delete BEFORE DELETE ON output_tombstones "
            "BEGIN SELECT RAISE(ABORT,'output tombstone is immutable'); END"
        ),
        "INSERT INTO schema_migrations(version, name) VALUES (6, 'output-retention')",
    ),
}


class UnsupportedSchemaVersion(ValueError):
    """This runner cannot safely use a newer database schema."""


class MigrationError(ValueError):
    """Database metadata does not match a supported migration state."""


class _Connection(sqlite3.Connection):
    _directory_fd: int = -1

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._directory_fd != -1:
                os.close(self._directory_fd)
                self._directory_fd = -1

    def __del__(self) -> None:
        # Destructors cannot report close failures to a caller. Explicit close()
        # remains the supported lifecycle and propagates errors normally.
        with suppress(sqlite3.Error, OSError):
            self.close()


def _validate_sidecars(directory_fd: int, filename: str) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        name = filename + suffix
        try:
            descriptor = _open_file_at(directory_fd, name, create=False, allow_unlinked=True)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise UnsafePathError("Unsafe SQLite sidecar file") from error
        try:
            # SQLite may replace the name after our open. Check the current
            # directory entry as well as the opened object; a vanished entry
            # is normal when SQLite closes its final WAL connection.
            try:
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                _check_private(current, directory=False)
        except OSError as error:
            raise UnsafePathError("Unsafe SQLite sidecar file") from error
        finally:
            os.close(descriptor)


def _version(connection: sqlite3.Connection) -> int:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"Database schema {version} is newer than supported version {SCHEMA_VERSION}"
        )
    if version < 0:
        raise MigrationError("Negative SQLite schema version")
    return version


@dataclass(frozen=True)
class Database:
    path: Path
    busy_timeout_ms: int = 5000

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _absolute(self.path))
        if not 0 <= self.busy_timeout_ms <= 60_000:
            raise ValueError("SQLite busy timeout must be between 0 and 60000 milliseconds")

    def connect(self) -> sqlite3.Connection:
        """Open a private WAL database with FK checks and bounded lock waiting.

        An open parent directory descriptor pins every SQLite/sidecar pathname
        beneath the validated private directory even if an ancestor is renamed.
        This Linux implementation deliberately has no path-following fallback.
        """
        directory_fd = _open_directory(self.path.parent)
        connection: _Connection | None = None
        try:
            try:
                file_fd = _open_file_at(directory_fd, self.path.name, create=True)
            except OSError as error:
                raise UnsafePathError("Unsafe SQLite database file") from error
            os.close(file_fd)
            _validate_sidecars(directory_fd, self.path.name)
            uri = f"file:/proc/self/fd/{directory_fd}/{quote(self.path.name, safe='')}?mode=rw"
            connection = sqlite3.connect(
                uri, uri=True, timeout=self.busy_timeout_ms / 1000,
                isolation_level=None, factory=_Connection,
            )
            connection._directory_fd = directory_fd
            _version(connection)  # Reject a future schema before changing journal mode.
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            if connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
                raise MigrationError("SQLite could not enable WAL journaling")
            # Admission claims and cleanup intents must survive a power loss.
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA trusted_schema = OFF")
            _validate_sidecars(directory_fd, self.path.name)
            return connection
        except BaseException:
            if connection is not None:
                connection.close()
            else:
                os.close(directory_fd)
            raise

    def migrate(self, connection: sqlite3.Connection | None = None) -> int:
        """Apply pending migrations atomically; never implicitly downgrade.

        PRAGMA user_version and the metadata row are committed together. A
        failed statement rolls back the entire upgrade. No executescript() is
        used because it can implicitly commit an existing transaction.
        """
        owns_connection = connection is None
        current = self.connect() if connection is None else connection
        try:
            if current.in_transaction:
                raise MigrationError("Migrations require a connection without an active transaction")
            current.execute("BEGIN IMMEDIATE")
            try:
                version = _version(current)
                tables = {
                    row[0] for row in current.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if version == 0 and tables:
                    raise MigrationError("Refusing an unversioned database containing existing tables")
                if version > 0:
                    if "schema_migrations" not in tables:
                        raise MigrationError("Database is missing migration metadata")
                    applied = [row[0] for row in current.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )]
                    if applied != list(range(1, version + 1)):
                        raise MigrationError("Migration metadata disagrees with SQLite schema version")
                for next_version in range(version + 1, SCHEMA_VERSION + 1):
                    for statement in _MIGRATIONS[next_version]:
                        current.execute(statement)
                    current.execute(f"PRAGMA user_version = {next_version}")
                current.execute("COMMIT")
            except BaseException:
                current.execute("ROLLBACK")
                raise
            return SCHEMA_VERSION
        finally:
            if owns_connection:
                current.close()
