"""EP-03 durable contained-run evidence on real Git and SQLite fixtures."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from runner_app.api.auth import Sessions
from runner_app.api.runtime import create_runtime_app
from runner_app.config import Settings
from runner_app.domain.commands import freeze
from runner_app.engine.service import Engine
from runner_app.git.argv import run_git
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.git.snapshots import capture_clean_verify
from runner_app.git.worktrees import create_repository, create_workspace
from runner_app.storage import Database, ensure_private_directory
from runner_app.storage import database as database_module
from runner_app.storage.ledger import Ledger, LedgerError
from runner_app.storage.run_evidence import EvidenceError, RunEvidenceStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fixture(tmp_path: Path, *, action: str = "VERIFY") -> tuple[
    RunEvidenceStore, SourceRegistry, Ledger, str, str, str, Path,
]:
    managed = tmp_path / "managed"
    repo = create_repository(managed, "fixture")
    head = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()
    workspace = create_workspace(managed, "fixture", repo, "area", "feature/area", head)
    database = Database(tmp_path / "state" / "state.sqlite")
    registry = SourceRegistry(database, managed)
    registry.register_project("fixture", "Fixture", repo, origin_kind="new")
    registry.register_workspace("fixture", "area", "Area", workspace,
                                branch="feature/area", head=head)
    snapshot = capture_clean_verify(repo, workspace, managed / "fixture" / "snapshots",
                                    project_id="fixture", workspace_id="area",
                                    expected_head=head, source_generation=0)
    registry.register_snapshot_record(snapshot)
    ledger = Ledger(database)
    ledger.startup()
    state = ledger.arm()
    request_id = str(uuid4())
    issued = datetime.now(timezone.utc)
    command = {
        "schema_version": "1.0", "request_id": request_id, "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(minutes=5)).isoformat(),
        "action": action, "project_id": "fixture", "workspace_id": "area",
        "expected_head": head,
        "params": ({"profile_id": "python-quick"} if action == "VERIFY" else {
            "changeset_doc_id": "changeset-document-123", "changeset_revision": "rev-1",
            "profile_id": "python-quick", "publication": "never",
        }),
        "intent": "Contained fixture run",
    }
    scope = "workspace:fixture/area" if action == "VERIFY" else "repo:fixture"
    assert ledger.admit(command, freeze(command).digest, "local-owner", (scope,)).execute
    effect = ledger.begin_effect(request_id, "source-snapshot", "git_ref",
                                 {"snapshot_id": snapshot.snapshot_id, "expected_head": head})
    ledger.confirm_effect(effect, snapshot.commit)
    store = RunEvidenceStore(database, registry, tmp_path / "state" / "outputs")
    return store, registry, ledger, request_id, snapshot.snapshot_id, head, workspace


def _evidence(store: RunEvidenceStore, request_id: str, snapshot_id: str,
              registry: SourceRegistry, *, outcome: str = "FAILED") -> tuple[dict, dict, dict[str, Path]]:
    snapshot = registry.get_snapshot(snapshot_id)
    assert snapshot is not None
    run_id = str(uuid4())
    run_dir = store.evidence_root / run_id
    capture = run_dir / "capture"
    ensure_private_directory(capture)
    raw = capture / "stdout.bin"
    raw.write_bytes(b"test failed\n")
    raw.chmod(0o600)
    stderr = capture / "stderr.bin"
    stderr.write_bytes(b"")
    stderr.chmod(0o600)

    def descriptor(name: str, path: Path) -> dict:
        content = path.read_bytes()
        return {
            "output_id": f"{name}-raw", "kind": "console", "format": "raw-bytes-v1",
            "media_type": "application/octet-stream", "relative_path": f"capture/{name}.bin",
            "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
            "complete": True, "truncated_reason": None, "publication": "local_only",
        }

    outputs = [descriptor("stdout", raw), descriptor("stderr", stderr)]
    exit_code = 1 if outcome == "FAILED" else 0 if outcome == "PASSED" else None
    manifest = {
        "schema_version": "1.0", "run_id": run_id, "request_id": request_id,
        "project_id": "fixture", "workspace_id": "area",
        "snapshot": {"commit": snapshot["snapshot_commit"], "tree": snapshot["tree_digest"],
                     "source_dirty_at_capture": False},
        "operation_status": "COMPLETED" if outcome != "CANCELLED" else "CANCELLED",
        "execution": {"outcome": outcome, "exit_code": exit_code, "duration_seconds": 0.01},
        "delivery": {"status": "NOT_ATTEMPTED", "reason": None},
        "outputs": outputs, "created_at": _now(),
    }
    provenance = {
        "backend": "rootless_docker", "run_id": run_id, "snapshot_id": snapshot_id,
        "snapshot_commit": snapshot["snapshot_commit"], "snapshot_tree": snapshot["tree_digest"],
        "profile_id": "python-quick", "profile_revision": "b" * 64,
        "image_digest": "docker.io/library/python@sha256:" + "a" * 64,
        "argv_sha256": "c" * 64, "started_at": _now(), "ended_at": _now(),
        "containment": {"verified": True, "network": "none", "read_only_source": True,
                        "output_collection_complete": True},
        "outcome": outcome, "exit_code": exit_code, "duration_seconds": 0.01,
    }
    return manifest, provenance, {"stdout-raw": raw, "stderr-raw": stderr}


def test_failed_project_run_is_durable_and_bound_to_exact_snapshot(tmp_path: Path) -> None:
    store, registry, ledger, request_id, snapshot_id, _, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, request_id, snapshot_id, registry)
    assert store.record_contained_run(manifest, snapshot_id=snapshot_id,
                                      provenance=provenance, files=files) == manifest
    assert ledger.get_run(manifest["run_id"])["execution"]["outcome"] == "FAILED"
    assert registry.publication_target(manifest["run_id"], project_id="fixture",
                                       workspace_id="area")["commit"] == manifest["snapshot"]["commit"]
    assert ledger.list_outputs(manifest["run_id"]) == sorted(manifest["outputs"], key=lambda item: item["output_id"])
    handle = store.open_output(manifest["run_id"], "stdout-raw")
    try:
        assert os.read(handle.descriptor, 100) == b"test failed\n"
        assert handle.sha256 == manifest["outputs"][0]["sha256"]
    finally:
        handle.close()
    with closing(store.database.connect()) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE outputs SET manifest_json='{}' WHERE output_id='stdout-raw'")
    with closing(store.database.connect()) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM run_executions WHERE run_id=?", (manifest["run_id"],))


def test_cancelled_run_is_preserved_without_publication_authority(tmp_path: Path) -> None:
    store, registry, _, request_id, snapshot_id, _, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, request_id, snapshot_id, registry,
                                            outcome="CANCELLED")
    store.record_contained_run(manifest, snapshot_id=snapshot_id,
                               provenance=provenance, files=files)
    with pytest.raises(SourceError) as error:
        registry.publication_target(manifest["run_id"], project_id="fixture", workspace_id="area")
    assert error.value.code == "RUN_NOT_TESTED"


def test_unverified_broker_or_modified_output_cannot_be_recorded(tmp_path: Path) -> None:
    store, registry, _, request_id, snapshot_id, _, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, request_id, snapshot_id, registry)
    unverified = {**provenance, "containment": {"verified": False}}
    with pytest.raises(EvidenceError) as error:
        store.record_contained_run(manifest, snapshot_id=snapshot_id,
                                   provenance=unverified, files=files)
    assert error.value.code == "CONTAINMENT_UNVERIFIED"
    files["stdout-raw"].write_bytes(b"modified\n")
    with pytest.raises(EvidenceError) as error:
        store.record_contained_run(manifest, snapshot_id=snapshot_id,
                                   provenance=provenance, files=files)
    assert error.value.code == "OUTPUT_CHANGED"
    with closing(store.database.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_approved_registry_port_image_is_accepted_in_evidence(tmp_path: Path) -> None:
    store, registry, _, request_id, snapshot_id, _, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, request_id, snapshot_id, registry)
    provenance["image_digest"] = "registry.example:5000/team/python@sha256:" + "a" * 64
    store.record_contained_run(manifest, snapshot_id=snapshot_id,
                               provenance=provenance, files=files)
    with closing(store.database.connect()) as connection:
        assert connection.execute("SELECT image_digest FROM run_executions").fetchone()[0] == provenance[
            "image_digest"
        ]


def test_faulted_operation_retains_run_without_publication_authority(tmp_path: Path) -> None:
    store, registry, ledger, request_id, snapshot_id, _, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, request_id, snapshot_id, registry)
    fault = {**manifest, "operation_status": "FAULT"}
    store.record_contained_run(fault, snapshot_id=snapshot_id,
                               provenance=provenance, files=files)
    assert ledger.get_run(manifest["run_id"])["operation_status"] == "FAULT"
    with pytest.raises(SourceError) as error:
        registry.publication_target(manifest["run_id"], project_id="fixture",
                                    workspace_id="area")
    assert error.value.code == "RUN_NOT_TESTED"
    with pytest.raises(SourceError) as error:
        registry.bind_tested_run(manifest["run_id"], snapshot_id)
    assert error.value.code == "RUN_NOT_TESTED"


def test_unknown_capture_fault_is_durable_but_cannot_bind_publication(tmp_path: Path) -> None:
    store, registry, ledger, request_id, snapshot_id, _, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, request_id, snapshot_id, registry)
    fault = {
        **manifest, "operation_status": "FAULT",
        "execution": {**manifest["execution"], "outcome": "UNKNOWN"},
        "outputs": [{**item, "complete": False, "truncated_reason": "storage_error"}
                    if item["output_id"] == "stdout-raw" else item
                    for item in manifest["outputs"]],
    }
    observed = {
        **provenance, "outcome": "UNKNOWN", "capture_fault": "STORAGE_WRITE_FAILED:OSError",
        "containment": {**provenance["containment"], "output_collection_complete": False},
    }
    store.record_contained_run(fault, snapshot_id=snapshot_id,
                               provenance=observed, files=files)
    assert ledger.get_run(manifest["run_id"]) == fault
    with pytest.raises(SourceError) as error:
        registry.publication_target(manifest["run_id"], project_id="fixture",
                                    workspace_id="area")
    assert error.value.code == "RUN_NOT_TESTED"


def test_incomplete_capture_cannot_be_recorded_as_completed(tmp_path: Path) -> None:
    store, registry, _, request_id, snapshot_id, _, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, request_id, snapshot_id, registry)
    incomplete = {
        **manifest,
        "outputs": [{**item, "complete": False, "truncated_reason": "storage_error"}
                    if item["output_id"] == "stdout-raw" else item
                    for item in manifest["outputs"]],
    }
    with pytest.raises(EvidenceError) as error:
        store.record_contained_run(incomplete, snapshot_id=snapshot_id,
                                   provenance=provenance, files=files)
    assert error.value.code == "CAPTURE_INCOMPLETE"
    with closing(store.database.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_broker_identity_must_match_recorded_snapshot(tmp_path: Path) -> None:
    store, registry, _, request_id, snapshot_id, _, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, request_id, snapshot_id, registry)
    mismatched = {**provenance, "snapshot_tree": "0" * 40}
    with pytest.raises(EvidenceError) as error:
        store.record_contained_run(manifest, snapshot_id=snapshot_id,
                                   provenance=mismatched, files=files)
    assert error.value.code == "SNAPSHOT_MISMATCH"
    with closing(store.database.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_output_download_rechecks_bytes_and_rejects_symlink(tmp_path: Path) -> None:
    store, registry, _, request_id, snapshot_id, _, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, request_id, snapshot_id, registry)
    store.record_contained_run(manifest, snapshot_id=snapshot_id,
                               provenance=provenance, files=files)
    raw = files["stdout-raw"]
    raw.write_bytes(b"mutated after registration\n")
    with pytest.raises(EvidenceError) as error:
        store.open_output(manifest["run_id"], "stdout-raw")
    assert error.value.code == "OUTPUT_CHANGED"
    raw.unlink()
    raw.symlink_to(tmp_path / "outside")
    with pytest.raises(EvidenceError) as error:
        store.open_output(manifest["run_id"], "stdout-raw")
    assert error.value.code == "UNSAFE_OUTPUT_FILE"


def test_output_route_requires_session_and_serves_only_registered_attachment(tmp_path: Path) -> None:
    store, registry, ledger, request_id, snapshot_id, _, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, request_id, snapshot_id, registry)
    store.record_contained_run(manifest, snapshot_id=snapshot_id,
                               provenance=provenance, files=files)
    sessions = Sessions()
    bootstrap = sessions.issue_bootstrap()
    origin = "http://127.0.0.1:8765"
    path = f"/api/v1/runs/{manifest['run_id']}/outputs/stdout-raw"
    with TestClient(create_runtime_app(Settings(), Engine(ledger), sessions,
                                       run_evidence=store), base_url=origin) as client:
        assert client.get(path).status_code == 403
        exchange = client.post("/api/v1/local/session", json={"bootstrap": bootstrap},
                               headers={"Origin": origin})
        assert exchange.status_code == 200
        response = client.get(path)
        assert response.status_code == 200 and response.content == b"test failed\n"
        assert response.headers["content-disposition"] == 'attachment; filename="stdout-raw.bin"'
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-type"] == "application/octet-stream"
        assert client.get(path, headers={"Host": "attacker.example"}).status_code == 403
        assert client.get(path + "/extra").status_code == 404
        files["stdout-raw"].write_bytes(b"tampered\n")
        changed = client.get(path)
        assert changed.status_code == 409 and changed.json()["error"] == "OUTPUT_CHANGED"


def test_failed_v4_migration_preserves_v3_database(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "state" / "state.sqlite")
    with monkeypatch.context() as scope:
        scope.setattr(database_module, "SCHEMA_VERSION", 3)
        assert database.migrate() == 3
    monkeypatch.setitem(database_module._MIGRATIONS, 4,
                        database_module._MIGRATIONS[4] + ("NOT VALID SQL",))
    with closing(database.connect()) as connection:
        with pytest.raises(sqlite3.OperationalError):
            database.migrate(connection)
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
        assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
            (1,), (2,), (3,),
        ]
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='run_executions'",
        ).fetchone() is None
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='output_files'",
        ).fetchone() is None


def test_failed_v5_run_reservation_migration_preserves_v4_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "state" / "state.sqlite")
    with monkeypatch.context() as scope:
        scope.setattr(database_module, "SCHEMA_VERSION", 4)
        assert database.migrate() == 4
    monkeypatch.setitem(database_module._MIGRATIONS, 5,
                        database_module._MIGRATIONS[5] + ("NOT VALID SQL",))
    with closing(database.connect()) as connection:
        with pytest.raises(sqlite3.OperationalError):
            database.migrate(connection)
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='run_attempts'",
        ).fetchone() is None
        assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
            (1,), (2,), (3,), (4,),
        ]


def test_run_cannot_change_admitted_profile_or_source(tmp_path: Path) -> None:
    store, registry, _, request_id, snapshot_id, _, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, request_id, snapshot_id, registry)
    wrong_profile = {**provenance, "profile_id": "other-approved-profile"}
    with pytest.raises(EvidenceError) as error:
        store.record_contained_run(manifest, snapshot_id=snapshot_id,
                                   provenance=wrong_profile, files=files)
    assert error.value.code == "COMMAND_MISMATCH"
    with closing(store.database.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_missing_raw_streams_and_nonfinite_duration_never_bind(tmp_path: Path) -> None:
    store, registry, _, request_id, snapshot_id, _, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, request_id, snapshot_id, registry)
    incomplete = {**manifest, "outputs": []}
    with pytest.raises(EvidenceError) as error:
        store.record_contained_run(incomplete, snapshot_id=snapshot_id,
                                   provenance=provenance, files={})
    assert error.value.code == "CAPTURE_INCOMPLETE"
    nonfinite = {**manifest, "execution": {**manifest["execution"],
                                           "duration_seconds": float("inf")}}
    matching = {**provenance, "duration_seconds": float("inf")}
    with pytest.raises(EvidenceError) as error:
        store.record_contained_run(nonfinite, snapshot_id=snapshot_id,
                                   provenance=matching, files=files)
    assert error.value.code == "INVALID_RUN_MANIFEST"
    with closing(store.database.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_run_snapshots").fetchone()[0] == 0


def test_binding_failure_rolls_back_run_and_outputs(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    store, registry, _, request_id, snapshot_id, _, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, request_id, snapshot_id, registry)

    def fail_binding(*_args: object) -> None:
        raise SourceError("SNAPSHOT_REF_CHANGED", "Simulated ref change before binding")

    monkeypatch.setattr(registry, "_bind_tested_run_record", fail_binding)
    with pytest.raises(SourceError) as error:
        store.record_contained_run(manifest, snapshot_id=snapshot_id,
                                   provenance=provenance, files=files)
    assert error.value.code == "SNAPSHOT_REF_CHANGED"
    with closing(store.database.connect()) as connection:
        for table in ("runs", "run_executions", "outputs", "output_files", "source_run_snapshots"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert files["stdout-raw"].read_bytes() == b"test failed\n"


def test_open_output_stream_is_sealed_against_postcheck_file_edits(tmp_path: Path) -> None:
    store, registry, _, request_id, snapshot_id, _, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, request_id, snapshot_id, registry)
    store.record_contained_run(manifest, snapshot_id=snapshot_id,
                               provenance=provenance, files=files)
    handle = store.open_output(manifest["run_id"], "stdout-raw")
    try:
        files["stdout-raw"].write_bytes(b"new private bytes\n")
        assert os.read(handle.descriptor, 100) == b"test failed\n"
        assert handle.sha256 == hashlib.sha256(b"test failed\n").hexdigest()
    finally:
        handle.close()
    with pytest.raises(EvidenceError) as error:
        store.open_output(manifest["run_id"], "stdout-raw")
    assert error.value.code == "OUTPUT_CHANGED"


def test_incomplete_collection_preserves_run_without_publication_binding(tmp_path: Path) -> None:
    store, registry, ledger, request_id, snapshot_id, _, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, request_id, snapshot_id, registry)
    incomplete = {**provenance, "containment": {**provenance["containment"],
                                                "output_collection_complete": False}}
    store.record_contained_run(manifest, snapshot_id=snapshot_id,
                               provenance=incomplete, files=files)
    assert ledger.get_run(manifest["run_id"])["execution"]["outcome"] == "FAILED"
    with pytest.raises(SourceError) as error:
        registry.publication_target(manifest["run_id"], project_id="fixture",
                                    workspace_id="area")
    assert error.value.code == "RUN_NOT_TESTED"
    with pytest.raises(SourceError) as error:
        registry.bind_tested_run(manifest["run_id"], snapshot_id)
    assert error.value.code == "RUN_NOT_TESTED"


@pytest.mark.parametrize("action,scope", [
    ("VERIFY", "workspace:fixture/area"),
    ("DELIVER_VERIFY_PUBLISH", "repo:fixture"),
])
def test_delivery_preflight_original_run_is_bound_to_contained_evidence(
    tmp_path: Path, action: str, scope: str,
) -> None:
    store, registry, ledger, request_id, snapshot_id, _, _ = _fixture(tmp_path, action=action)
    manifest, provenance, files = _evidence(store, request_id, snapshot_id, registry)
    with pytest.raises(LedgerError) as missing:
        ledger.require_delivery_preflight(request_id, manifest["run_id"])
    assert missing.value.code == "SOURCE_RUN_MISSING"
    store.record_contained_run(manifest, snapshot_id=snapshot_id,
                               provenance=provenance, files=files)
    preflight = ledger.require_delivery_preflight(request_id, manifest["run_id"])
    assert preflight.delivery_request_id == request_id
    assert preflight.source_request_id == request_id
    assert preflight.run_id == manifest["run_id"]
    assert (preflight.project_id, preflight.workspace_id) == ("fixture", "area")
    assert (preflight.action, preflight.source_action, preflight.source_status) == (
        action, action, "RUNNING",
    )
    assert preflight.lock_scope == scope
    canonical_run = (json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False) + "\n").encode("utf-8")
    assert preflight.run_manifest_sha256 == hashlib.sha256(canonical_run).hexdigest()
    snapshot_record = registry.get_snapshot(snapshot_id)
    assert snapshot_record is not None
    assert preflight.input_manifest_sha256 == snapshot_record["manifest_sha256"]
    assert preflight.source_operation_manifest_sha256 is None
    ledger.finish(request_id, "COMPLETED")
    with pytest.raises(LedgerError) as finished:
        ledger.require_delivery_preflight(request_id, manifest["run_id"])
    assert finished.value.code == "DELIVERY_NOT_RUNNING"


def test_delivery_preflight_report_retry_uses_only_named_stored_run(tmp_path: Path) -> None:
    store, registry, ledger, source_id, snapshot_id, head, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, source_id, snapshot_id, registry)
    store.record_contained_run(manifest, snapshot_id=snapshot_id,
                               provenance=provenance, files=files)
    operation_manifest = {
        "request_id": source_id, "project_id": "fixture", "workspace_id": "area",
        "run_ids": [manifest["run_id"]], "status": "COMPLETED",
    }
    ledger.finish(source_id, "COMPLETED", result={
        "run_id": manifest["run_id"], "operation_manifest": operation_manifest,
    })
    issued = datetime.now(timezone.utc)
    retry_id = str(uuid4())
    retry = {
        "schema_version": "1.0", "request_id": retry_id, "dispatch": "READY",
        "runner_id": "local-runner", "epoch": ledger.state()["epoch"],
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(minutes=5)).isoformat(),
        "action": "PUBLISH_REPORT", "project_id": "fixture", "workspace_id": "area",
        "expected_head": head, "params": {"source_run_id": manifest["run_id"]},
        "intent": "Retry immutable evidence delivery",
    }
    assert ledger.admit(retry, freeze(retry).digest, "local-owner",
                        ("workspace:fixture/area",)).execute
    preflight = ledger.require_delivery_preflight(retry_id, manifest["run_id"])
    assert preflight.delivery_request_id == retry_id
    assert preflight.source_request_id == source_id
    assert preflight.action == "PUBLISH_REPORT"
    assert preflight.source_action == "VERIFY"
    assert preflight.source_status == "COMPLETED"
    assert preflight.lock_scope == "workspace:fixture/area"
    canonical_run = (json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False) + "\n").encode("utf-8")
    canonical_operation = (json.dumps(operation_manifest, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False) + "\n").encode("utf-8")
    assert preflight.run_manifest_sha256 == hashlib.sha256(canonical_run).hexdigest()
    snapshot_record = registry.get_snapshot(snapshot_id)
    assert snapshot_record is not None
    assert preflight.input_manifest_sha256 == snapshot_record["manifest_sha256"]
    assert preflight.source_operation_manifest_sha256 == hashlib.sha256(
        canonical_operation
    ).hexdigest()
    with pytest.raises(LedgerError) as wrong_run:
        ledger.require_delivery_preflight(retry_id, str(uuid4()))
    assert wrong_run.value.code == "SOURCE_RUN_MISSING"


@pytest.mark.parametrize("change,expected", [
    ({"action": "PUBLISH", "params": {"source_run_id": "ignored"}}, "DELIVERY_ACTION_INVALID"),
    ({"params": {"source_run_id": str(uuid4())}}, "DELIVERY_RUN_MISMATCH"),
    ({"project_id": "other"}, "DELIVERY_SOURCE_MISMATCH"),
    ({"workspace_id": "other"}, "DELIVERY_SOURCE_MISMATCH"),
])
def test_delivery_preflight_rejects_wrong_action_or_scope(
    tmp_path: Path, change: dict[str, object], expected: str,
) -> None:
    store, registry, ledger, source_id, snapshot_id, head, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, source_id, snapshot_id, registry)
    store.record_contained_run(manifest, snapshot_id=snapshot_id,
                               provenance=provenance, files=files)
    ledger.finish(source_id, "COMPLETED")
    issued = datetime.now(timezone.utc)
    retry = {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": "local-runner", "epoch": ledger.state()["epoch"],
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(minutes=5)).isoformat(),
        "action": "PUBLISH_REPORT", "project_id": "fixture", "workspace_id": "area",
        "expected_head": head, "params": {"source_run_id": manifest["run_id"]},
        "intent": "Retry immutable evidence delivery",
    }
    retry.update(change)
    assert ledger.admit(retry, freeze(retry).digest, "local-owner",
                        ("workspace:fixture/area",)).execute
    with pytest.raises(LedgerError) as error:
        ledger.require_delivery_preflight(retry["request_id"], manifest["run_id"])
    assert error.value.code == expected


def test_delivery_preflight_requires_exact_held_lock(tmp_path: Path) -> None:
    store, registry, ledger, source_id, snapshot_id, head, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, source_id, snapshot_id, registry)
    store.record_contained_run(manifest, snapshot_id=snapshot_id,
                               provenance=provenance, files=files)
    ledger.finish(source_id, "COMPLETED")
    issued = datetime.now(timezone.utc)
    retry = {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": "local-runner", "epoch": ledger.state()["epoch"],
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(minutes=5)).isoformat(),
        "action": "PUBLISH_REPORT", "project_id": "fixture", "workspace_id": "area",
        "expected_head": head, "params": {"source_run_id": manifest["run_id"]},
        "intent": "Retry immutable evidence delivery",
    }
    assert ledger.admit(retry, freeze(retry).digest, "local-owner", ()).execute
    with pytest.raises(LedgerError) as error:
        ledger.require_delivery_preflight(retry["request_id"], manifest["run_id"])
    assert error.value.code == "DELIVERY_LOCK_MISSING"


def test_input_manifest_is_exact_registered_run_snapshot_bytes(tmp_path: Path) -> None:
    store, registry, _, request_id, snapshot_id, _, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, request_id, snapshot_id, registry)
    store.record_contained_run(manifest, snapshot_id=snapshot_id,
                               provenance=provenance, files=files)
    snapshot = registry.get_snapshot(snapshot_id)
    assert snapshot is not None
    raw = Path(snapshot["manifest_path"]).read_bytes()
    assert store.read_input_manifest(manifest["run_id"]) == raw
    assert hashlib.sha256(raw).hexdigest() == snapshot["manifest_sha256"]
    with pytest.raises(EvidenceError) as missing:
        store.read_input_manifest(str(uuid4()))
    assert missing.value.code == "INPUT_MANIFEST_NOT_FOUND"


def test_input_manifest_tamper_and_symlink_fail_closed(tmp_path: Path) -> None:
    store, registry, _, request_id, snapshot_id, _, _ = _fixture(tmp_path)
    manifest, provenance, files = _evidence(store, request_id, snapshot_id, registry)
    store.record_contained_run(manifest, snapshot_id=snapshot_id,
                               provenance=provenance, files=files)
    snapshot = registry.get_snapshot(snapshot_id)
    assert snapshot is not None
    path = Path(snapshot["manifest_path"])
    original = path.read_bytes()
    path.write_bytes(original + b" ")
    with pytest.raises(EvidenceError) as changed:
        store.read_input_manifest(manifest["run_id"])
    assert changed.value.code == "INPUT_MANIFEST_CHANGED"
    path.unlink()
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(original)
    replacement.chmod(0o600)
    path.symlink_to(replacement)
    with pytest.raises(EvidenceError) as unsafe:
        store.read_input_manifest(manifest["run_id"])
    assert unsafe.value.code == "INPUT_MANIFEST_UNSAFE"


def test_input_manifest_refuses_run_record_with_different_source_scope(tmp_path: Path) -> None:
    store, registry, _, request_id, snapshot_id, _, _ = _fixture(tmp_path)
    manifest, provenance, _ = _evidence(store, request_id, snapshot_id, registry)
    manifest["project_id"] = "another-project"
    with closing(store.database.connect()) as connection:
        connection.execute(
            "INSERT INTO runs(run_id,request_id,project_id,workspace_id,manifest_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (manifest["run_id"], request_id, "fixture", "area", json.dumps(manifest), _now()),
        )
        connection.execute(
            "INSERT INTO run_executions(run_id,snapshot_id,profile_id,profile_revision,"
            "image_digest,backend,argv_sha256,started_at,ended_at,containment_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (manifest["run_id"], snapshot_id, provenance["profile_id"],
             provenance["profile_revision"], provenance["image_digest"],
             "rootless_docker", provenance["argv_sha256"], provenance["started_at"],
             provenance["ended_at"], json.dumps(provenance["containment"]), _now()),
        )
    with pytest.raises(EvidenceError) as mismatch:
        store.read_input_manifest(manifest["run_id"])
    assert mismatch.value.code == "INPUT_MANIFEST_SCOPE_MISMATCH"
