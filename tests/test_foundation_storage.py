"""EP-00 storage security and durable migration behavior on disposable fixtures."""

from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import runner_app.storage.database as database_module
from runner_app.storage import (
    SCHEMA_VERSION,
    Database,
    MigrationError,
    Paths,
    UnsafePathError,
    UnsupportedSchemaVersion,
    ensure_private_directory,
    open_private_file,
)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_xdg_discovery_has_no_side_effects(tmp_path: Path) -> None:
    home = tmp_path / "absent-home"
    paths = Paths.discover({"HOME": str(home)})
    assert paths.config_dir == home / ".config/chat-dev-runner"
    assert paths.state_dir == home / ".local/share/chat-dev-runner"
    assert paths.cache_dir == home / ".cache/chat-dev-runner"
    assert paths.runtime_dir is None
    assert paths.database_path == paths.state_dir / "state.sqlite"
    assert not home.exists()
    with pytest.raises(FrozenInstanceError):
        paths.config_dir = home  # type: ignore[misc]


def test_explicit_xdg_roots_and_architecture_state_location(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path / "home")}
    for key in ("CONFIG", "DATA", "CACHE", "RUNTIME", "STATE"):
        env[f"XDG_{key}_HOME" if key != "RUNTIME" else "XDG_RUNTIME_DIR"] = str(tmp_path / key)
    paths = Paths.discover(env)
    assert paths.state_dir == tmp_path / "DATA/chat-dev-runner"
    assert paths.runtime_dir == tmp_path / "RUNTIME/chat-dev-runner"
    assert paths.config_dir == tmp_path / "CONFIG/chat-dev-runner"
    assert paths.cache_dir == tmp_path / "CACHE/chat-dev-runner"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("key", ["HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"])
def test_relative_paths_are_rejected(tmp_path: Path, key: str) -> None:
    env = {"HOME": str(tmp_path), key: "relative"}
    with pytest.raises(UnsafePathError, match="absolute"):
        Paths.discover(env)


def test_private_roots_do_not_chmod_existing_ancestors(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o755)
    config_parent = home / ".config"
    config_parent.mkdir(mode=0o755)
    paths = Paths.discover({"HOME": str(home)})
    paths.ensure_private()
    for path in (paths.config_dir, paths.state_dir, paths.cache_dir, paths.token_dir, paths.evidence_dir):
        assert mode(path) == 0o700
    assert mode(home) == 0o755
    assert mode(config_parent) == 0o755
    token = paths.token_dir / "fixture-token"
    descriptor = open_private_file(token, create=True)
    try:
        os.write(descriptor, b"temporary fixture")
    finally:
        os.close(descriptor)
    assert mode(token) == 0o600


def test_creation_handles_restrictive_umask(tmp_path: Path) -> None:
    previous = os.umask(0o777)
    try:
        token = tmp_path / "created" / "token"
        os.close(open_private_file(token, create=True))
    finally:
        os.umask(previous)
    assert mode(token.parent) == 0o700
    assert mode(token) == 0o600


@pytest.mark.parametrize("unsafe_mode", [0o755, 0o750, 0o770, 0o1700])
def test_unsafe_existing_app_directory_is_rejected_without_repair(tmp_path: Path, unsafe_mode: int) -> None:
    target = tmp_path / "state"
    target.mkdir()
    target.chmod(unsafe_mode)
    with pytest.raises(UnsafePathError, match="0700"):
        ensure_private_directory(target)
    assert mode(target) == unsafe_mode


@pytest.mark.parametrize("link_position", ["parent", "target"])
def test_symlink_directories_are_rejected(tmp_path: Path, link_position: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    target = link / "child" if link_position == "parent" else link
    with pytest.raises(UnsafePathError):
        ensure_private_directory(target)
    assert list(outside.iterdir()) == []


def test_parent_traversal_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        ensure_private_directory(tmp_path / "x" / ".." / "outside")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("file_kind", ["symlink", "hardlink", "fifo", "public", "directory"])
def test_unsafe_files_are_rejected_without_changing_target(tmp_path: Path, file_kind: str) -> None:
    target = tmp_path / "secret"
    external = tmp_path / "external"
    external.write_bytes(b"unchanged")
    external.chmod(0o600)
    if file_kind == "symlink":
        target.symlink_to(external)
    elif file_kind == "hardlink":
        os.link(external, target)
    elif file_kind == "fifo":
        os.mkfifo(target, 0o600)
    elif file_kind == "directory":
        target.mkdir(mode=0o700)
    else:
        target.write_bytes(b"public")
        target.chmod(0o644)
    with pytest.raises(UnsafePathError):
        open_private_file(target, create=True)
    assert external.read_bytes() == b"unchanged"
    if file_kind == "public":
        assert mode(target) == 0o644


def test_private_storage_rejects_foreign_ownership(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    actual_uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: actual_uid + 1)
    with pytest.raises(UnsafePathError, match="owned"):
        ensure_private_directory(tmp_path)


def test_migration_is_durable_idempotent_and_metadata_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Retain the original EP-00 v1 foundation acceptance after adding later migrations.
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 1)
    database = Database(tmp_path / "state.sqlite")
    assert database.migrate() == 1
    assert database.migrate() == 1
    connection = database.connect()
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (5000,)
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("schema_migrations",)]
        assert connection.execute("SELECT version, name FROM schema_migrations").fetchall() == [(1, "foundation")]
        for path in tmp_path.iterdir():
            assert mode(path) == 0o600
    finally:
        connection.close()


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    connection = Database(tmp_path / "state.sqlite").connect()
    try:
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO child VALUES (17)")
    finally:
        connection.close()


def test_newer_database_is_rejected_without_downgrade(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 99")
    connection.close()
    path.chmod(0o600)
    with pytest.raises(UnsupportedSchemaVersion, match="99"):
        Database(path).migrate()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (99,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    finally:
        connection.close()


def test_migration_statement_failure_rolls_back_schema_and_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "state.sqlite")
    connection = database.connect()
    statements = database_module._MIGRATIONS[1]
    monkeypatch.setitem(database_module._MIGRATIONS, 1, statements + ("THIS IS NOT SQL",))
    try:
        with pytest.raises(sqlite3.OperationalError):
            database.migrate(connection)
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
        assert not connection.in_transaction
    finally:
        connection.close()


def test_unversioned_foreign_database_is_not_adopted(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite")
    connection = database.connect()
    try:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES ('preserve')")
        with pytest.raises(MigrationError, match="unversioned"):
            database.migrate(connection)
        assert connection.execute("SELECT value FROM unrelated").fetchall() == [("preserve",)]
    finally:
        connection.close()


def test_inconsistent_migration_metadata_is_rejected(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite")
    database.migrate()
    connection = database.connect()
    try:
        connection.execute("DELETE FROM schema_migrations")
        with pytest.raises(MigrationError, match="disagrees"):
            database.migrate(connection)
    finally:
        connection.close()


def test_migration_does_not_commit_callers_transaction(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite")
    connection = database.connect()
    try:
        connection.execute("BEGIN")
        with pytest.raises(MigrationError, match="active transaction"):
            database.migrate(connection)
        assert connection.in_transaction
        connection.execute("ROLLBACK")
    finally:
        connection.close()


@pytest.mark.parametrize("filename", ["state.sqlite", "state.sqlite-wal", "state.sqlite-shm", "state.sqlite-journal"])
def test_database_and_sidecar_symlinks_are_rejected(tmp_path: Path, filename: str) -> None:
    target = tmp_path / "external"
    target.write_bytes(b"do not overwrite")
    target.chmod(0o600)
    (tmp_path / filename).symlink_to(target)
    with pytest.raises(UnsafePathError):
        Database(tmp_path / "state.sqlite").connect()
    assert target.read_bytes() == b"do not overwrite"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_sqlite_sidecar_unlinked_between_open_and_fstat_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str,
) -> None:
    database_path = tmp_path / "state.sqlite"
    sidecar = tmp_path / f"state.sqlite{suffix}"
    sidecar.write_bytes(b"")
    sidecar.chmod(0o600)
    original_fstat = os.fstat
    unlinked = False

    def unlink_before_fstat(descriptor: int) -> os.stat_result:
        nonlocal unlinked
        if not unlinked and os.readlink(f"/proc/self/fd/{descriptor}") == str(sidecar):
            sidecar.unlink()
            unlinked = True
        return original_fstat(descriptor)

    monkeypatch.setattr(os, "fstat", unlink_before_fstat)
    connection = Database(database_path).connect()
    try:
        assert unlinked
        assert connection.execute("SELECT 1").fetchone() == (1,)
    finally:
        connection.close()


def test_sqlite_sidecar_replaced_with_symlink_during_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar = tmp_path / "state.sqlite-wal"
    sidecar.write_bytes(b"")
    sidecar.chmod(0o600)
    external = tmp_path / "external"
    external.write_bytes(b"unchanged")
    external.chmod(0o600)
    original_fstat = os.fstat
    replaced = False

    def replace_before_fstat(descriptor: int) -> os.stat_result:
        nonlocal replaced
        if not replaced and os.readlink(f"/proc/self/fd/{descriptor}") == str(sidecar):
            sidecar.unlink()
            sidecar.symlink_to(external)
            replaced = True
        return original_fstat(descriptor)

    monkeypatch.setattr(os, "fstat", replace_before_fstat)
    with pytest.raises(UnsafePathError):
        Database(tmp_path / "state.sqlite").connect()
    assert replaced
    assert sidecar.is_symlink()
    assert external.read_bytes() == b"unchanged"


def test_unlinked_ordinary_private_file_still_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "token"
    target.write_bytes(b"fixture")
    target.chmod(0o600)
    original_fstat = os.fstat
    unlinked = False

    def unlink_before_fstat(descriptor: int) -> os.stat_result:
        nonlocal unlinked
        if not unlinked and os.readlink(f"/proc/self/fd/{descriptor}") == str(target):
            target.unlink()
            unlinked = True
        return original_fstat(descriptor)

    monkeypatch.setattr(os, "fstat", unlink_before_fstat)
    with pytest.raises(UnsafePathError, match="hard link"):
        open_private_file(target)
    assert unlinked


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("file_kind", ["hardlink", "fifo", "public", "directory"])
def test_unsafe_sqlite_sidecars_still_fail(
    tmp_path: Path, suffix: str, file_kind: str,
) -> None:
    sidecar = tmp_path / f"state.sqlite{suffix}"
    external = tmp_path / "external"
    external.write_bytes(b"unchanged")
    external.chmod(0o600)
    if file_kind == "hardlink":
        os.link(external, sidecar)
    elif file_kind == "fifo":
        os.mkfifo(sidecar, 0o600)
    elif file_kind == "directory":
        sidecar.mkdir(mode=0o700)
    else:
        sidecar.write_bytes(b"public")
        sidecar.chmod(0o644)
    with pytest.raises(UnsafePathError):
        Database(tmp_path / "state.sqlite").connect()
    assert external.read_bytes() == b"unchanged"
    if file_kind == "public":
        assert mode(sidecar) == 0o644


def test_sqlite_uri_filename_is_escaped(tmp_path: Path) -> None:
    path = tmp_path / "literal?#%name.sqlite"
    Database(path).migrate()
    assert path.exists()
    assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]


def test_connections_apply_bounded_busy_timeout(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite", busy_timeout_ms=1)
    database.migrate()
    first = database.connect()
    second = database.connect()
    try:
        first.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            second.execute("BEGIN IMMEDIATE")
        assert not second.in_transaction
        first.execute("ROLLBACK")
        assert database.migrate(second) == SCHEMA_VERSION
    finally:
        first.close()
        second.close()


def test_sqlite_parent_descriptor_survives_ancestor_replacement(tmp_path: Path) -> None:
    parent = tmp_path / "ancestor" / "state"
    database = Database(parent / "state.sqlite")
    database.migrate()
    connection = database.connect()
    try:
        (tmp_path / "ancestor").rename(tmp_path / "moved-ancestor")
        parent.mkdir(parents=True, mode=0o700)
        decoy = parent / "state.sqlite"
        decoy.write_bytes(b"replacement must remain untouched")
        connection.execute("UPDATE schema_migrations SET name = 'pinned-parent'")
        assert decoy.read_bytes() == b"replacement must remain untouched"
    finally:
        connection.close()
    moved = Database(tmp_path / "moved-ancestor/state/state.sqlite").connect()
    try:
        assert moved.execute("SELECT name FROM schema_migrations").fetchone() == ("pinned-parent",)
    finally:
        moved.close()


def test_database_close_and_failure_release_descriptors(tmp_path: Path) -> None:
    descriptors_before = len(list(Path("/proc/self/fd").iterdir()))
    database = Database(tmp_path / "state.sqlite")
    for _ in range(5):
        database.migrate()
        connection = database.connect()
        connection.close()
        connection.close()  # Like sqlite3, repeated close is harmless.
    link = tmp_path / "linked.sqlite"
    link.symlink_to(database.path)
    for _ in range(5):
        with pytest.raises(UnsafePathError):
            Database(link).connect()
    assert len(list(Path("/proc/self/fd").iterdir())) == descriptors_before


def test_domain_upgrade_preserves_v1_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "state.sqlite")
    with monkeypatch.context() as context:
        context.setattr(database_module, "SCHEMA_VERSION", 1)
        database.migrate()
    connection = database.connect()
    try:
        original = connection.execute("SELECT * FROM schema_migrations WHERE version=1").fetchone()
        assert database.migrate(connection) == SCHEMA_VERSION
        assert connection.execute("SELECT * FROM schema_migrations WHERE version=1").fetchone() == original
        assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,)]
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_failed_domain_upgrade_preserves_v1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "state.sqlite")
    with monkeypatch.context() as context:
        context.setattr(database_module, "SCHEMA_VERSION", 1)
        database.migrate()
    monkeypatch.setitem(database_module._MIGRATIONS, 2, database_module._MIGRATIONS[2] + ("NOT VALID SQL",))
    connection = database.connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            database.migrate(connection)
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("schema_migrations",)]
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
    finally:
        connection.close()
