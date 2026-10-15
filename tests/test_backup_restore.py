"""EP-07 stopped-runner backup and restore on disposable private state only."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest

import runner_app.storage.backup as backup_module
from runner_app.domain.mailbox_v2 import acknowledge_mailbox, parse_mailbox
from runner_app.engine.singleton import Singleton
from runner_app.git.argv import run_git
from runner_app.git.source_v2 import LocalWorkspace, SourceProject, SourceResolver
from runner_app.git.worktrees import create_repository, create_workspace
from runner_app.outputs.bundle_v2 import collect_bundle_outputs
from runner_app.storage.backup import BackupError, _rename_new, create_backup, restore_backup
from runner_app.storage.database import SCHEMA_VERSION, Database
from runner_app.storage.intake_v2 import AckProof, IntakeStore
from runner_app.storage.paths import Paths, ensure_private_directory


def _fixture(tmp_path: Path) -> tuple[Paths, Path]:
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    paths.ensure_private()
    Database(paths.database_path).migrate()
    archive_parent = tmp_path / "backups"
    ensure_private_directory(archive_parent)
    return paths, archive_parent / "snapshot"


def _project_row(path: Path) -> tuple[str, str] | None:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT project_id,display_name FROM projects WHERE project_id='demo'"
        ).fetchone()


def test_backup_restores_wal_database_evidence_and_safe_relative_git_link(tmp_path: Path) -> None:
    paths, archive = _fixture(tmp_path)
    fake_token = paths.token_dir / "oauth-fixture"
    fake_token.write_bytes(b"fixture-oauth-secret")
    fake_token.chmod(0o600)
    fake_github = paths.config_dir / "github-token"
    fake_github.write_bytes(b"fixture-github-secret")
    fake_github.chmod(0o600)
    project = paths.state_dir / "managed" / "demo"
    workspace = project / "workspaces" / "primary"
    workspace.mkdir(parents=True)
    (project / "shared.txt").write_text("owner source\n")
    (workspace / "link.txt").symlink_to("../../shared.txt")
    proof = paths.evidence_dir / "run-1" / "stdout.txt"
    proof.parent.mkdir()
    proof.write_bytes(b"contained output\n")
    with Database(paths.database_path).connect() as connection:
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "INSERT INTO projects(project_id,display_name,state,metadata_json,created_at,updated_at) "
            "VALUES('demo','Demo','READY','{}','2026-01-01','2026-01-01')"
        )
        connection.commit()
        assert (paths.database_path.parent / "state.sqlite-wal").exists()
        result = create_backup(paths, archive)
        assert result["database_schema_version"] == SCHEMA_VERSION
        assert result["symlinks"] == 1
    assert archive.stat().st_mode & 0o777 == 0o700
    assert (archive / "manifest.json").stat().st_mode & 0o777 == 0o600
    assert not (archive / "state" / "runner.lock").exists()
    assert not (archive / "state" / "state.sqlite-wal").exists()
    assert not (archive / "config").exists()
    for archived_file in archive.rglob("*"):
        if archived_file.is_file():
            assert b"fixture-oauth-secret" not in archived_file.read_bytes()
            assert b"fixture-github-secret" not in archived_file.read_bytes()
    assert not (archive / "state" / "managed" / "demo" / "workspaces" / "primary" / "link.txt").exists()
    shutil.rmtree(paths.state_dir)  # Disposable fixture only; mimic owner moving damaged state aside.
    restored = restore_backup(archive, paths.state_dir)
    assert restored["manifest_sha256"] == result["manifest_sha256"]
    assert _project_row(paths.database_path) == ("demo", "Demo")
    assert (paths.evidence_dir / "run-1" / "stdout.txt").read_bytes() == b"contained output\n"
    link = paths.state_dir / "managed" / "demo" / "workspaces" / "primary" / "link.txt"
    assert link.is_symlink() and os.readlink(link) == "../../shared.txt"
    assert link.read_text() == "owner source\n"
    assert fake_token.read_bytes() == b"fixture-oauth-secret"
    assert fake_github.read_bytes() == b"fixture-github-secret"


def test_backup_refuses_running_singleton_without_creating_archive(tmp_path: Path) -> None:
    paths, archive = _fixture(tmp_path)
    running = Singleton(paths.state_dir / "runner.lock")
    try:
        with pytest.raises(BackupError) as error:
            create_backup(paths, archive)
        assert error.value.code == "RUNNER_RUNNING"
        assert not archive.exists()
    finally:
        running.close()


@pytest.mark.parametrize("target", ["/etc/passwd", "../../../../../outside"])
def test_backup_refuses_out_of_root_source_link(tmp_path: Path, target: str) -> None:
    paths, archive = _fixture(tmp_path)
    workspace = paths.state_dir / "managed" / "demo" / "workspaces" / "primary"
    workspace.mkdir(parents=True)
    (workspace / "escape").symlink_to(target)
    with pytest.raises(BackupError) as error:
        create_backup(paths, archive)
    assert error.value.code == "UNSAFE_STATE_LINK"
    assert not archive.exists()
    assert not list(archive.parent.glob(".snapshot.partial-*"))


def test_restore_detects_tamper_before_touching_state(tmp_path: Path) -> None:
    paths, archive = _fixture(tmp_path)
    (paths.evidence_dir / "proof").write_bytes(b"original")
    create_backup(paths, archive)
    shutil.rmtree(paths.state_dir)
    (archive / "state" / "outputs" / "proof").write_bytes(b"changed!")
    with pytest.raises(BackupError) as error:
        restore_backup(archive, paths.state_dir)
    assert error.value.code == "BACKUP_HASH_MISMATCH"
    assert not paths.state_dir.exists()


def test_restore_refuses_existing_or_different_state_path(tmp_path: Path) -> None:
    paths, archive = _fixture(tmp_path)
    create_backup(paths, archive)
    with pytest.raises(BackupError) as existing:
        restore_backup(archive, paths.state_dir)
    assert existing.value.code == "RESTORE_TARGET_EXISTS"
    assert paths.database_path.exists()
    different = tmp_path / "different"
    with pytest.raises(BackupError) as mismatch:
        restore_backup(archive, different)
    assert mismatch.value.code == "TARGET_PATH_MISMATCH"
    assert not different.exists()


def test_backup_refuses_unclassified_state_entry(tmp_path: Path) -> None:
    paths, archive = _fixture(tmp_path)
    (paths.state_dir / "unexpected-secret").write_text("private")
    with pytest.raises(BackupError) as error:
        create_backup(paths, archive)
    assert error.value.code == "UNKNOWN_STATE_ENTRY"
    assert not archive.exists()


def test_real_managed_git_worktree_survives_same_path_restore(tmp_path: Path) -> None:
    paths, archive = _fixture(tmp_path)
    managed = paths.state_dir / "managed"
    ensure_private_directory(managed)
    repo = create_repository(managed, "actual")
    head = run_git(repo, ["rev-parse", "HEAD"]).strip()
    workspace = create_workspace(managed, "actual", repo, "primary", "feature", head)
    (workspace / "note.txt").write_text("review me\n")
    create_backup(paths, archive)
    shutil.rmtree(paths.state_dir)
    restore_backup(archive, paths.state_dir)
    assert run_git(workspace, ["rev-parse", "HEAD"]).strip() == head
    assert run_git(workspace, ["status", "--porcelain"]).strip() == "?? note.txt"
    assert (workspace / "note.txt").read_text() == "review me\n"


def test_atomic_promotion_never_replaces_existing_archive(tmp_path: Path) -> None:
    source = tmp_path / "new"
    target = tmp_path / "existing"
    ensure_private_directory(source)
    ensure_private_directory(target)
    (target / "sentinel").write_text("preserved")
    with pytest.raises(BackupError) as error:
        _rename_new(source, target)
    assert error.value.code == "BACKUP_TARGET_EXISTS"
    assert (target / "sentinel").read_text() == "preserved"
    assert source.exists()


def test_restore_refuses_archive_symlink_without_following_it(tmp_path: Path) -> None:
    paths, archive = _fixture(tmp_path)
    (paths.evidence_dir / "proof").write_bytes(b"evidence")
    create_backup(paths, archive)
    shutil.rmtree(paths.state_dir)
    outside = tmp_path / "outside"
    outside.write_bytes(b"external")
    member = archive / "state" / "outputs" / "proof"
    member.unlink()
    member.symlink_to(outside)
    with pytest.raises(BackupError) as error:
        restore_backup(archive, paths.state_dir)
    assert error.value.code == "BACKUP_MANIFEST_INVALID"
    assert outside.read_bytes() == b"external"
    assert not paths.state_dir.exists()


def test_backup_refuses_configuration_root_overlapping_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    paths = Paths(state / "managed", state, tmp_path / "cache")
    paths.ensure_private()
    Database(paths.database_path).migrate()
    parent = tmp_path / "backups"
    ensure_private_directory(parent)
    with pytest.raises(BackupError) as error:
        create_backup(paths, parent / "snapshot")
    assert error.value.code == "BACKUP_PATH_OVERLAP"
    assert not (parent / "snapshot").exists()


def test_backup_detects_new_top_level_entry_during_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, archive = _fixture(tmp_path)
    (paths.evidence_dir / "proof").write_bytes(b"evidence")
    original = backup_module._copy_state_tree

    def mutate_after_scan(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)  # type: ignore[arg-type]
        (paths.state_dir / "new-entry").write_bytes(b"concurrent")

    monkeypatch.setattr(backup_module, "_copy_state_tree", mutate_after_scan)
    with pytest.raises(BackupError) as error:
        create_backup(paths, archive)
    assert error.value.code == "STATE_CHANGED_DURING_BACKUP"
    assert not archive.exists()


def _database_hashes(path: Path) -> dict[str, dict[str, int | str]]:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
        return {name: {"rows": len(rows), "sha256": hashlib.sha256(repr(sorted(rows, key=repr)).encode()).hexdigest()}
                for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                for rows in [connection.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall()]}


def _backup_candidate(store: IntakeStore, session: str, binding: str, *, unknown: bool) -> str:
    token = str(uuid4())
    store.bind(binding, runner_id="backup-runner", project_id="demo", provider="github",
               coordinates={"repository": "fixture/project", "branch": "commands", "path": f"{binding}.json"},
               session_id=session)
    store.establish_baseline(binding, session_id=session, slot_token=token, revision="before")
    raw = json.dumps({"protocol": "cdr-mailbox/2", "runner_id": "backup-runner", "project_id": "demo",
                      "slot_token": token, "dispatch": "READY", "receipt": None,
                      "request": {"request_id": None, "action": "RUN_TASK", "task": "test",
                                  "source": {"kind": "local_workspace", "branch": "backup-proof", "workspace_id": "local"},
                                  "execution": {"mode": "native", "expected_os": "linux", "timeout_seconds": 10}}}).encode()
    frozen = parse_mailbox(raw, installation_id=store.installation_id, mailbox_id=binding)
    ack = acknowledge_mailbox(frozen)
    reserved = store.reserve(binding, frozen, revision="ready", observed_sha256=hashlib.sha256(raw).hexdigest(),
                             ack_payload=ack)
    if unknown:
        store.ack_failed(reserved.candidate_id, code="ACK_RESPONSE_LOST", uncertain=True)
    else:
        assert store.finalize_ack(reserved.candidate_id,
                                  AckProof("accepted", hashlib.sha256(ack).hexdigest(), binding)) == 1
    return reserved.request_id


def test_schema10_v2_backup_roundtrip_retains_sealed_source_large_artifact_and_no_replay(tmp_path: Path) -> None:
    paths, archive = _fixture(tmp_path)
    references = paths.config_dir / "v2-runtime.json"
    references.write_text(json.dumps({"github_token_file": str(paths.token_dir / "not-created"),
                                      "google_config_file": str(paths.config_dir / "not-created.json")}))
    references.chmod(0o600)
    assert list(paths.token_dir.iterdir()) == []
    store = IntakeStore(Database(paths.database_path))
    session = store.start_session("backup-runner")
    queued = _backup_candidate(store, session, "queued", unknown=False)
    unknown = _backup_candidate(store, session, "unknown", unknown=True)
    assert store.get(queued)["status"] == "QUEUED"
    assert store.get(unknown)["status"] == "ACK_PENDING"

    original = tmp_path / "original-project"
    original.mkdir(mode=0o700)
    run_git(original, ["init", "-b", "backup-proof"])
    (original / "app.py").write_bytes(b"print('backup source fixture')\n")
    executable = original / "run.sh"
    executable.write_bytes(b"#!/bin/sh\nprintf 'backup fixture\\n'\n")
    executable.chmod(0o700)
    run_git(original, ["add", "--all"])
    run_git(original, ["-c", "user.name=Backup proof", "-c", "user.email=fixture@localhost",
                       "commit", "-m", "Disposable backup fixture"])
    project = paths.state_dir / "projects-v2" / "demo"
    project.mkdir(parents=True, mode=0o700)
    repository = project / "source.git"
    run_git(project, ["clone", "--bare", "--no-local", str(original), str(repository)])
    snapshot = SourceResolver({"demo": SourceProject(repository, project / "runs",
        {"local": LocalWorkspace(original)})}).resolve_current(
            "demo", queued, {"kind": "local_workspace", "branch": "backup-proof", "workspace_id": "local"})
    assert stat.S_IMODE(snapshot.manifest_path.stat().st_mode) == 0o400

    artifact_source = tmp_path / "artifact-source"
    artifact_source.mkdir(mode=0o700)
    large = artifact_source / "retained.bin"
    with large.open("wb") as stream:
        for _ in range(11):
            stream.write(b"v2-output-bytes!" * 65536)  # 11 MiB; exceeds Git publication per-file limit.
    result_root = project / "runner-results" / "backup-runner" / queued
    outputs = collect_bundle_outputs({"out": artifact_source}, [{"id": "large", "kind": "artifact",
        "path": "retained.bin", "format": "binary", "required": True, "publish": "approved_upload",
        "source_root": "out"}], result_root)
    artifact = outputs.artifacts[0]
    assert artifact["reason"] == "POLICY_SIZE_LIMIT" and artifact["remote_path"] is None
    retained = next(iter(outputs.originals["large"].values()))
    (result_root / "bundle" / "artifact-metadata.json").write_text(json.dumps(artifact, sort_keys=True))
    (result_root / "bundle" / "artifact-metadata.json").chmod(0o600)
    environment = snapshot.path.parent / "out" / ".environment" / "python"
    (environment / "lib").mkdir(parents=True, mode=0o700)
    (environment / "lib64").symlink_to("lib")
    (environment / "bin").mkdir(mode=0o700)
    copied_interpreter = environment / "bin" / "python"
    copied_interpreter.write_bytes(b"copied interpreter fixture; not executed\n")
    copied_interpreter.chmod(0o700)

    evidence = paths.state_dir / "executions-v2" / "rootless-v2" / "evidence" / queued
    preparation = evidence.parents[1] / "preparation-evidence" / str(uuid4())
    for root, filename, payload in ((evidence, "backend.json", b'{"fixture":"rootless retained evidence"}\n'),
                                    (evidence / "capture", "stdout.bin", b"rootless fixture output\x00\xff"),
                                    (preparation, "preparation.json", b'{"fixture":"preparation retained evidence"}\n')):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / filename
        path.write_bytes(payload)
        path.chmod(0o600)
    expected = {str(path.relative_to(paths.state_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (snapshot.path / "app.py", snapshot.path / "run.sh", snapshot.manifest_path,
                             retained, copied_interpreter, evidence / "backend.json", evidence / "capture/stdout.bin",
                             preparation / "preparation.json")}
    database_before = _database_hashes(paths.database_path)
    result = create_backup(paths, archive)
    manifest = json.loads((archive / "manifest.json").read_bytes())
    assert manifest["version"] == 2 and manifest["database_schema_version"] == 10
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in (archive / "state").rglob("*") if path.is_file())
    moved = paths.state_dir.with_name("saved-before-restore")
    paths.state_dir.rename(moved)
    restored = restore_backup(archive, paths.state_dir)
    assert restored["manifest_sha256"] == result["manifest_sha256"]
    assert _database_hashes(paths.database_path) == database_before
    actual = {name: hashlib.sha256((paths.state_dir / name).read_bytes()).hexdigest() for name in expected}
    assert actual == expected
    source = snapshot.path
    assert stat.S_IMODE(source.stat().st_mode) == 0o500
    assert stat.S_IMODE((source / "app.py").stat().st_mode) == 0o400
    assert stat.S_IMODE((source / "run.sh").stat().st_mode) == 0o500
    assert stat.S_IMODE(snapshot.manifest_path.stat().st_mode) == 0o400
    assert stat.S_IMODE(copied_interpreter.stat().st_mode) == 0o700
    assert os.readlink(environment / "lib64") == "lib"
    assert all(not path.lstat().st_mode & 0o7077 for path in paths.state_dir.rglob("*") if not path.is_symlink())
    assert not (archive / "config").exists() and not (archive / "state/tokens").exists()
    assert not any(path.name == "v2-runtime.json" for path in archive.rglob("*"))
    restored_store = IntakeStore(Database(paths.database_path))
    assert restored_store.get(queued)["status"] == "QUEUED"
    assert restored_store.get(unknown)["status"] == "ACK_PENDING"
    pending = restored_store.pending_candidate("unknown")
    assert pending is not None and pending["state"] == "ACK_UNKNOWN"
    new_session = restored_store.start_session("backup-runner")
    assert restored_store.get(queued)["status"] == "SUSPENDED_RESTART"
    assert restored_store.get(unknown)["status"] == "SUSPENDED_RESTART"
    assert restored_store.candidate(pending["candidate_id"])["state"] == "ACK_UNKNOWN"
    assert restored_store.claim_next(new_session) is None
    with closing(Database(paths.database_path).connect()) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    proof = {"scope": "stopped disposable schema10 v2 state; no service/provider/container execution",
             "archive_version": 2, "schema_version": 10, "files": result["files"],
             "manifest_sha256": result["manifest_sha256"], "retained_large_bytes": artifact["bytes"],
             "retained_large_reason": artifact["reason"], "verified_file_hashes": actual,
             "all_database_tables_before_restore": database_before, "all_database_rows_preserved": True,
             "restored_owner_modes": {"source_directory": "0500", "source_file": "0400",
                                      "source_executable": "0500", "source_manifest": "0400", "copied_interpreter": "0700"},
             "relative_environment_link_preserved": True, "config_and_tokens_excluded": True,
             "state_before_restart": {"queued": "QUEUED", "uncertain_request": "ACK_PENDING", "candidate": "ACK_UNKNOWN"},
             "state_after_restart": {"queued": "SUSPENDED_RESTART", "uncertain_request": "SUSPENDED_RESTART", "candidate": "ACK_UNKNOWN"},
             "replayed_requests": 0, "integrity_check": "ok", "foreign_key_violations": 0}
    (tmp_path / "roundtrip-proof.json").write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n")
    for root in (moved, paths.state_dir):
        for directory, _, _ in os.walk(root, followlinks=False):
            os.chmod(directory, 0o700, follow_symlinks=False)  # Disposable fixture cleanup only.


def test_restore_reads_historical_revision1_archive(tmp_path: Path) -> None:
    paths, archive = _fixture(tmp_path)
    (paths.evidence_dir / "historical").write_bytes(b"revision1 fixture")
    create_backup(paths, archive)
    manifest_path = archive / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["version"] = 1
    for entry in manifest["entries"]:
        entry.pop("read_only", None)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    shutil.rmtree(paths.state_dir)
    restore_backup(archive, paths.state_dir)
    assert (paths.evidence_dir / "historical").read_bytes() == b"revision1 fixture"


@pytest.mark.parametrize("root", ["projects-v2", "executions-v2"])
@pytest.mark.parametrize("unsafe", ["absolute-link", "escaping-link", "hardlink", "fifo"])
def test_v2_backup_refuses_unsafe_entries_without_following_them(tmp_path: Path, root: str, unsafe: str) -> None:
    paths, archive = _fixture(tmp_path)
    source = paths.state_dir / root
    source.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_bytes(b"external fixture; never archived")
    entry = source / "unsafe"
    if unsafe == "absolute-link":
        entry.symlink_to(outside)
    elif unsafe == "escaping-link":
        entry.symlink_to("../../../outside")
    elif unsafe == "hardlink":
        os.link(outside, entry)
    else:
        os.mkfifo(entry, 0o600)
    with pytest.raises(BackupError) as error:
        create_backup(paths, archive)
    assert error.value.code == ("UNSAFE_STATE_LINK" if unsafe in {"absolute-link", "escaping-link"} else "UNSAFE_STATE_ENTRY")
    assert outside.read_bytes() == b"external fixture; never archived"
    assert not archive.exists() and not list(archive.parent.glob(".snapshot.partial-*"))


def test_v2_backup_refuses_cross_device_mount_before_reading_members(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, archive = _fixture(tmp_path)
    root = paths.state_dir / "executions-v2"
    root.mkdir(mode=0o700)
    (root / "never-read").write_bytes(b"quota-mounted fixture")
    owned = backup_module._owned

    def different_device(info: os.stat_result, *, root_dev: int, directory: bool = False, file: bool = False) -> None:
        # No mount privilege is needed to exercise the existing device boundary.
        owned(info, root_dev=root_dev + 1, directory=directory, file=file)

    monkeypatch.setattr(backup_module, "_owned", different_device)
    with pytest.raises(BackupError) as error:
        create_backup(paths, archive)
    assert error.value.code == "UNSAFE_STATE_ENTRY"
    assert not archive.exists()


def test_failed_sealed_v2_restore_cleans_only_its_temporary_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, archive = _fixture(tmp_path)
    source = paths.state_dir / "projects-v2" / "demo" / "source"
    source.mkdir(parents=True, mode=0o700)
    (source / "input").write_bytes(b"sealed input")
    (source / "input").chmod(0o400)
    source.chmod(0o500)
    create_backup(paths, archive)
    paths.state_dir.rename(tmp_path / "saved-original")

    def refuse_promotion(stage: Path, target: Path) -> None:
        assert (stage / "projects-v2/demo/source").stat().st_mode & 0o777 == 0o500
        raise BackupError("BACKUP_PROMOTION_FAILED")

    monkeypatch.setattr(backup_module, "_rename_new", refuse_promotion)
    with pytest.raises(BackupError) as error:
        restore_backup(archive, paths.state_dir)
    assert error.value.code == "BACKUP_PROMOTION_FAILED"
    assert not paths.state_dir.exists() and not list(tmp_path.glob(".state.restore-*"))
    assert (archive / "state/projects-v2/demo/source/input").read_bytes() == b"sealed input"
    (tmp_path / "saved-original/projects-v2/demo/source").chmod(0o700)


@pytest.mark.parametrize("path,value", [("projects-v2", "false"), ("state.sqlite", True)])
def test_restore_rejects_invalid_readonly_metadata_before_creating_state(tmp_path: Path, path: str, value: object) -> None:
    paths, archive = _fixture(tmp_path)
    (paths.state_dir / "projects-v2").mkdir(mode=0o700)
    create_backup(paths, archive)
    manifest_path = archive / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    next(entry for entry in manifest["entries"] if entry["path"] == path)["read_only"] = value
    manifest_path.write_text(json.dumps(manifest))
    shutil.rmtree(paths.state_dir)
    with pytest.raises(BackupError) as error:
        restore_backup(archive, paths.state_dir)
    assert error.value.code == "BACKUP_MANIFEST_INVALID"
    assert not paths.state_dir.exists()
