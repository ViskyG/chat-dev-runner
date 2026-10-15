"""Qualify a copied installed ledger without exposing its contents or changing it.

Only the generated unit's ExecStart executable path is selected. All database
copies stay under one fresh owner-private /tmp directory, outside the repository.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import struct
import subprocess
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import runner_app
import runner_app.storage.database as current_database
from runner_app.storage.database import Database

REPO = Path(__file__).resolve().parents[4]
LIVE_DATABASE = Path("/home/visky/Work/GPTRunner/.runner-data/chat-dev-runner/state.sqlite")
SYSTEM_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/home/visky",
              "XDG_RUNTIME_DIR": "/run/user/1000",
              "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
OLD_PROBE = r'''
import hashlib,json,sqlite3,sys
from pathlib import Path
import runner_app
import runner_app.storage.database as database
path=Path(sys.argv[1])
root=Path(sys.argv[2])
assert path.is_absolute() and path.is_relative_to(root) and not path.is_symlink()
report={"version":runner_app.__version__,"supported_schema":database.SCHEMA_VERSION,
        "module_path":str(Path(database.__file__).resolve()),
        "module_sha256":hashlib.sha256(Path(database.__file__).read_bytes()).hexdigest()}
try:
    with __import__('contextlib').closing(database.Database(path).connect()) as connection:
        report.update(opened=True,schema=int(connection.execute('PRAGMA user_version').fetchone()[0]),
                      migrate_return=database.Database(path).migrate(connection),
                      integrity_ok=connection.execute('PRAGMA integrity_check').fetchone()==('ok',),
                      foreign_key_violations=len(connection.execute('PRAGMA foreign_key_check').fetchall()))
except database.UnsupportedSchemaVersion:
    report.update(opened=False,error='UnsupportedSchemaVersion')
print(json.dumps(report,sort_keys=True))
'''


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def identity(path: Path) -> dict[str, Any]:
    info = path.lstat()
    return {"uid": info.st_uid, "gid": info.st_gid,
            "mode": f"{stat.S_IMODE(info.st_mode):04o}", "bytes": info.st_size,
            "inode": info.st_ino, "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns, "nlink": info.st_nlink}


def private_file(path: Path) -> None:
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode) and not path.is_symlink()
    assert info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_nlink == 1


def sidecar_snapshot() -> dict[str, Any]:
    selected: dict[str, Any] = {}
    for suffix in ("-wal", "-shm", "-journal"):
        path = Path(str(LIVE_DATABASE) + suffix)
        if path.exists():
            private_file(path)
            info = identity(path)
            # An empty WAL has no pending frames. Existing SHM is observed only;
            # immutable read-only access never updates its lock/index bytes.
            assert suffix == "-shm" or info["bytes"] == 0, "PENDING_WAL_OR_JOURNAL_REQUIRES_REVIEW"
            selected[suffix] = {"file": info, "sha256": digest(path)}
        else:
            selected[suffix] = None
    return selected


def service() -> dict[str, str]:
    result = subprocess.run(
        ["/usr/bin/systemctl", "--user", "show", "chat-dev-runner.service", "--no-pager",
         "--property=LoadState,ActiveState,SubState,MainPID,ExecStart"], env=SYSTEM_ENV,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=5, check=True)
    allowed = {"LoadState", "ActiveState", "SubState", "MainPID"}
    selected: dict[str, str] = {}
    for line in result.stdout.decode().splitlines():
        key, _, value = line.partition("=")
        if key in allowed:
            selected[key] = value
        elif key == "ExecStart":
            match = re.search(r"(?:^|[ {])path=([^ ;}]+)", value)
            if match:
                selected["ExecStart_path"] = match.group(1)
    assert selected.get("ActiveState") == "inactive" and selected.get("MainPID") == "0"
    executable = Path(selected["ExecStart_path"])
    assert executable.is_absolute() and ".." not in executable.parts and executable.is_file()
    return selected


def readonly(path: Path) -> sqlite3.Connection:
    # A stopped ledger with no pending WAL is immutable for this probe. This
    # prevents SQLite from creating or updating installed SHM/WAL sidecars.
    connection = sqlite3.connect(f"file:{quote(str(path), safe='/')}?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def idle(connection: sqlite3.Connection) -> dict[str, Any]:
    counts = {
        "operations_running": connection.execute("SELECT COUNT(*) FROM operations WHERE status='RUNNING'").fetchone()[0],
        "capacity_held": connection.execute("SELECT COUNT(*) FROM operations WHERE capacity_held=1").fetchone()[0],
        "reconcile_required": connection.execute("SELECT COUNT(*) FROM operations WHERE reconcile_required=1").fetchone()[0],
        "pending_effects": connection.execute("SELECT COUNT(*) FROM effects WHERE state IN ('INTENDED','UNKNOWN')").fetchone()[0],
    }
    assert not any(counts.values())
    return {**counts, "armed": bool(connection.execute("SELECT armed FROM runner_state WHERE singleton=1").fetchone()[0])}


def tagged(value: Any) -> list[Any]:
    if value is None:
        return ["null"]
    if isinstance(value, int):
        return ["integer", str(value)]
    if isinstance(value, float):
        return ["real-ieee754-big-endian", struct.pack(">d", value).hex()]
    if isinstance(value, bytes):
        return ["blob-hex", value.hex()]
    assert isinstance(value, str)
    return ["text", value]


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def inventory(connection: sqlite3.Connection, *, original_schema: int = 9) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        columns = [list(row) for row in connection.execute(f"PRAGMA table_info({identifier(name)})")]
        predicate = " WHERE version <= ?" if name == "schema_migrations" else ""
        parameters = (original_schema,) if predicate else ()
        encoded = sorted(canonical([tagged(value) for value in row]) for row in
                         connection.execute(f"SELECT * FROM {identifier(name)}{predicate}", parameters))
        row_digest = hashlib.sha256()
        for row in encoded:
            row_digest.update(len(row).to_bytes(8, "big"))
            row_digest.update(row)
        tables[name] = {"rows": len(encoded), "columns_sha256": hashlib.sha256(canonical(columns)).hexdigest(),
                        "all_rows_sha256": row_digest.hexdigest()}
    objects = {f"{kind}:{name}": hashlib.sha256(canonical([table, sql])).hexdigest()
               for kind, name, table, sql in connection.execute(
                   "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")}
    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert integrity == [("ok",)] and not violations
    return {"schema": connection.execute("PRAGMA user_version").fetchone()[0],
            "integrity_check": "ok", "foreign_key_violations": 0,
            "tables": tables, "schema_objects": objects}


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    changed_tables = [name for name, value in before["tables"].items()
                      if after["tables"].get(name) != value]
    changed_objects = [name for name, value in before["schema_objects"].items()
                       if after["schema_objects"].get(name) != value]
    assert not changed_tables and not changed_objects
    return {"all_preexisting_tables_and_rows_identical": True,
            "all_preexisting_schema_objects_identical": True,
            "changed_preexisting_tables": changed_tables, "changed_preexisting_schema_objects": changed_objects,
            "added_tables": sorted(set(after["tables"]) - set(before["tables"])),
            "added_schema_objects": sorted(set(after["schema_objects"]) - set(before["schema_objects"]))}


def copy_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with closing(readonly(source)) as original, closing(sqlite3.connect(destination)) as target:
        original.backup(target)
        target.commit()
    private_file(destination)
    descriptor = os.open(destination, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def old_probe(executable: Path, database: Path, scratch: Path) -> dict[str, Any]:
    assert database != LIVE_DATABASE and database.is_relative_to(scratch)
    result = subprocess.run([str(executable), "-I", "-c", OLD_PROBE, str(database), str(scratch)],
        cwd=scratch, env={"PATH": "/usr/bin:/bin", "HOME": str(scratch)},
        stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=False)
    assert result.returncode == 0
    value = json.loads(result.stdout)
    assert value["version"] == "0.2.7" and value["supported_schema"] == 9
    return {"exit_code": result.returncode, **value}


def run() -> dict[str, Any]:
    os.umask(0o077)
    started = datetime.now(timezone.utc).isoformat()
    initial_service = service()
    private_file(LIVE_DATABASE)
    initial_sidecars = sidecar_snapshot()
    live_before = {"file": identity(LIVE_DATABASE), "sha256": digest(LIVE_DATABASE)}
    with closing(readonly(LIVE_DATABASE)) as original:
        original.execute("BEGIN")
        initial_idle = idle(original)
        before = inventory(original)
        assert before["schema"] == 9
        scratch = Path(tempfile.mkdtemp(prefix="cdr-gp07-migration-", dir="/tmp"))
        pristine = scratch / "original-backup/state.sqlite"
        pristine.parent.mkdir(mode=0o700)
        descriptor = os.open(pristine, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with closing(sqlite3.connect(pristine)) as target:
            original.backup(target)
            target.commit()
    private_file(pristine)
    executable = Path(initial_service["ExecStart_path"])
    installed_version = subprocess.run([str(executable), "-I", "-m", "runner_app.cli", "--version"],
        cwd=scratch, env={"PATH": "/usr/bin:/bin", "HOME": str(scratch)},
        stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)
    assert installed_version.returncode == 0 and installed_version.stdout.strip() == b"0.2.7"
    original_backup_probe = old_probe(executable, pristine, scratch)
    assert original_backup_probe["opened"] and original_backup_probe["migrate_return"] == 9
    with closing(readonly(pristine)) as connection:
        backup_inventory = inventory(connection)
    backup_comparison = compare(before, backup_inventory)
    migrated = scratch / "schema10-copy/state.sqlite"
    copy_database(pristine, migrated)
    migrated_version = Database(migrated).migrate()
    assert migrated_version == current_database.SCHEMA_VERSION == 10
    with closing(readonly(migrated)) as connection:
        after = inventory(connection)
        migration_versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
        migration_name = connection.execute("SELECT name FROM schema_migrations WHERE version=10").fetchone()[0]
    migration_comparison = compare(before, after)
    assert migration_versions == list(range(1, 11))
    assert all(after["tables"][name]["rows"] == 0 for name in migration_comparison["added_tables"])
    migrated_before_refusal = digest(migrated)
    refusal = old_probe(executable, migrated, scratch)
    assert not refusal["opened"] and refusal["error"] == "UnsupportedSchemaVersion"
    assert digest(migrated) == migrated_before_refusal
    restored = scratch / "rollback-restored-copy/state.sqlite"
    copy_database(pristine, restored)
    rollback = old_probe(executable, restored, scratch)
    assert rollback["opened"] and rollback["migrate_return"] == 9
    with closing(readonly(restored)) as connection:
        restored_inventory = inventory(connection)
    rollback_comparison = compare(before, restored_inventory)
    final_service = service()
    with closing(readonly(LIVE_DATABASE)) as connection:
        final_idle = idle(connection)
        live_after_inventory = inventory(connection)
    live_after = {"file": identity(LIVE_DATABASE), "sha256": digest(LIVE_DATABASE)}
    assert live_before == live_after and before == live_after_inventory
    final_sidecars = sidecar_snapshot()
    assert initial_sidecars == final_sidecars
    assert initial_service == final_service and initial_idle == final_idle
    copies = {}
    for name, path in (("original_backup", pristine), ("migrated_copy", migrated), ("rollback_restored_copy", restored)):
        private_file(path)
        copies[name] = {"path": str(path), "sha256": digest(path), **identity(path)}
    return {"schema": "cdr-installed-ledger-migration-proof/1", "status": "PASSED",
        "started_at": started, "ended_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Actual installed schema9 ledger copied read-only; migration and rollback only on private copies",
        "uid": os.getuid(), "euid": os.geteuid(), "scratch": str(scratch),
        "scratch_mode": f"{stat.S_IMODE(scratch.stat().st_mode):04o}",
        "source": {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
                   "package_version": runner_app.__version__, "schema_version": current_database.SCHEMA_VERSION,
                   "database_module_sha256": digest(Path(current_database.__file__)),
                   "script_sha256": digest(Path(__file__))},
        "installed_binary": {"path": str(executable), "sha256": digest(executable),
                             "cli_version": installed_version.stdout.decode().strip(),
                             "unit_fields_consumed": ["LoadState", "ActiveState", "SubState", "MainPID", "ExecStart.path"],
                             "environment_fields_consumed": []},
        "guards": {"initial_service": initial_service, "final_service": final_service,
                   "initial_idle": initial_idle, "final_idle": final_idle},
        "installed_ledger": {"path": str(LIVE_DATABASE), "before": live_before, "after": live_after,
                             "sidecars_before": initial_sidecars, "sidecars_after": final_sidecars,
                             "unchanged_bytes_metadata_and_all_rows": True},
        "hash_format": "Typed SQLite values; null, decimal integers, IEEE754 real bytes, UTF8 text, hex blobs. Compact sorted-key JSON rows sorted bytewise, each length-prefixed uint64 big-endian before SHA256. Full ordered column metadata hashed separately.",
        "migration_bookkeeping_exception": "Only additive schema_migrations version10 excluded from pre-existing-row comparison; versions1..9, their timestamps and sqlite_sequence rows compared exactly.",
        "before": before, "after": after,
        "backup_comparison": backup_comparison, "migration_comparison": migration_comparison,
        "added_migration": {"version": 10, "name": migration_name},
        "original_backup_old_binary_open": original_backup_probe,
        "schema10_copy_old_binary_refusal": {**refusal, "database_bytes_unchanged": True},
        "rollback_restored_old_binary_open": rollback, "rollback_comparison": rollback_comparison,
        "private_copies": copies,
        "not_performed": ["live database migration", "installed binary replacement", "service start or stop", "provider/network calls", "configuration/credential copy", "raw database/command rows in repository"],
        "rollback_plan": ["Keep service stopped and idle; retain matching original installed binary and complete original durable state backup outside repository.",
                          "If rollback is required after an approved live upgrade, stop intake/service, reconcile active effects, and restore the pre-upgrade durable-state backup with its matching schema9 binary.",
                          "Never point the old schema9 binary at the upgraded live schema10 ledger; its copied-schema refusal is proven here.",
                          "This proof restores a copied ledger only; a live rollback must preserve matching managed sources, artifacts and external identities from the complete stopped-state backup."],
        "live_upgrade_prerequisites": ["Explicit rollout authorization with service stopped and all active/reconcile/pending-effect counts zero.",
                                      "Complete stopped-state backup including historical artifacts and managed repositories, with matching installed0.2.7 binary retained and restore location checked.",
                                      "Build and inspect a pinned installable v2 wheel; identify it by artifact hash and schema10 because current source still reports package version0.2.7.",
                                      "Record approved optional provider/native/rootless configuration and preserve existing credentials/Drive IDs outside repository.",
                                      "After approved installation and schema10 migration, perform bounded installed-runtime/readback checks before intake is armed; retain rollback materials."]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        record = run()
    except (AssertionError, OSError, sqlite3.Error, subprocess.SubprocessError,
            UnicodeError, ValueError, TypeError, KeyError) as error:
        frame = error.__traceback__
        while frame is not None and frame.tb_next is not None:
            frame = frame.tb_next
        record = {"schema": "cdr-installed-ledger-migration-proof/1", "status": "FAILED",
                  "error_type": type(error).__name__, "raw_error_suppressed": True,
                  "error_function": frame.tb_frame.f_code.co_name if frame else None,
                  "error_line": frame.tb_lineno if frame else None}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
        print(json.dumps(record, sort_keys=True))
        raise SystemExit(1) from None
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"status": record["status"], "scratch": record["scratch"],
                      "original_tables": len(record["before"]["tables"]),
                      "original_rows": sum(table["rows"] for table in record["before"]["tables"].values()),
                      "added_tables": record["migration_comparison"]["added_tables"],
                      "old_binary_schema10": record["schema10_copy_old_binary_refusal"]["error"],
                      "live_database_unchanged": True, "service_active_state": record["guards"]["final_service"]["ActiveState"]}, sort_keys=True))


if __name__ == "__main__":
    main()
