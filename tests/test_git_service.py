"""EP-02 finite source steps require durable admission and effect intent."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from runner_app.domain.commands import freeze, validate_shape
from runner_app.git.argv import GitError, run_git
from runner_app.git.imports import ImportGrants
from runner_app.git.registry import SourceError, SourceRegistry
from runner_app.git.service import SourceService
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger, LedgerError


def admit(ledger: Ledger, action: str, project_id: str, workspace_id: str | None,
          expected_head: str | None, params: dict, scopes: tuple[str, ...]) -> str:
    state = ledger.state()
    now = datetime.now(timezone.utc)
    command = {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": now.isoformat(), "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": action, "project_id": project_id, "workspace_id": workspace_id,
        "expected_head": expected_head, "params": params, "intent": "Local source fixture",
    }
    validate_shape(command)
    assert ledger.admit(command, freeze(command).digest, "local-owner", scopes).execute
    return command["request_id"]


def make_service(tmp_path: Path) -> tuple[Ledger, SourceRegistry, SourceService]:
    database = Database(tmp_path / "state" / "state.sqlite")
    ledger = Ledger(database)
    ledger.startup()
    ledger.arm()
    registry = SourceRegistry(database, tmp_path / "managed")
    return ledger, registry, SourceService(ledger, registry)


def create_project_workspace(ledger: Ledger, registry: SourceRegistry,
                             service: SourceService) -> tuple[str, Path]:
    create_id = admit(ledger, "CREATE_PROJECT", "fixture", None, None,
                      {"name": "Fixture", "source": {"kind": "new"},
                       "template_id": "python-basic", "github_identity": "owner",
                       "visibility": "private"},
                      ("repo:fixture", "global:owner", "global:drive-installation"))
    project = service.create_new_source(create_id)
    assert project["origin_kind"] == "new"
    assert ledger.get(create_id)["effects"][0]["state"] == "CONFIRMED"
    ledger.finish(create_id, "COMPLETED")
    repo = Path(project["repo_path"])
    head = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()
    workspace_id = admit(ledger, "CREATE_WORKSPACE", "fixture", "area", head,
                         {"name": "Area", "branch": "feature/area", "base_commit": head,
                          "profile_id": "python-quick"}, ("repo:fixture",))
    area = service.create_workspace_source(workspace_id)
    assert area["head"] == head
    assert ledger.get(workspace_id)["effects"][0]["state"] == "CONFIRMED"
    ledger.finish(workspace_id, "COMPLETED")
    return head, repo


def test_service_creates_managed_source_and_clean_snapshot_without_execution(tmp_path: Path) -> None:
    ledger, registry, service = make_service(tmp_path)
    head, repo = create_project_workspace(ledger, registry, service)
    request_id = admit(ledger, "VERIFY", "fixture", "area", head,
                       {"profile_id": "python-quick"}, ("workspace:fixture/area",))
    snapshot = service.capture_clean_verify_source(request_id)
    assert snapshot.base_head == head and snapshot.commit == head
    assert snapshot.tree == run_git(repo, ["rev-parse", f"{head}^{{tree}}"] ).strip()
    assert registry.get_snapshot(snapshot.snapshot_id)["manifest_sha256"] == snapshot.manifest_sha256
    assert ledger.get(request_id)["effects"][0]["state"] == "CONFIRMED"
    ledger.finish(request_id, "COMPLETED")
    assert registry.get_workspace("fixture", "area")["head"] == head


def test_source_step_refuses_operation_without_required_lock(tmp_path: Path) -> None:
    ledger, registry, service = make_service(tmp_path)
    head, _ = create_project_workspace(ledger, registry, service)
    request_id = admit(ledger, "VERIFY", "fixture", "area", head,
                       {"profile_id": "python-quick"}, ("workspace:fixture/other",))
    with pytest.raises(SourceError) as error:
        service.capture_clean_verify_source(request_id)
    assert error.value.code == "SOURCE_LOCK_MISSING"
    assert ledger.get(request_id)["effects"] == []
    ledger.finish(request_id, "BLOCKED", code=error.value.code)


def test_crash_after_snapshot_stage_retains_evidence_and_lock(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    ledger, registry, service = make_service(tmp_path)
    head, repo = create_project_workspace(ledger, registry, service)
    request_id = admit(ledger, "DELIVER_VERIFY_PUBLISH", "fixture", "area", head,
                       {"changeset_doc_id": "fixture-change", "changeset_revision": "1",
                        "profile_id": "python-quick", "publication": "never"},
                       ("repo:fixture",))
    changeset = {
        "schema_version": "1.0", "change_id": str(uuid4()),
        "project_id": "fixture", "workspace_id": "area", "base_head": head,
        "files": [{"op": "upsert", "path": "src/main.py", "expected_sha256": None,
                   "content_utf8": "value = 42\n"}],
    }

    def fail_record(_snapshot: object) -> None:
        raise RuntimeError("simulated crash after Git snapshot")

    monkeypatch.setattr(registry, "register_snapshot_record", fail_record)
    with pytest.raises(RuntimeError, match="simulated crash"):
        service.stage_changeset_source(request_id, changeset)
    effect = ledger.get(request_id)["effects"][0]
    snapshot_id = effect["intent"]["snapshot_id"]
    assert effect["state"] == "INTENDED"
    stage = registry.managed_root / "fixture" / "snapshots" / snapshot_id
    assert (stage / "intent.json").is_file()
    assert (stage / "manifest.json").is_file()
    assert run_git(repo, ["rev-parse", f"refs/cdr/snapshots/{snapshot_id}"]).strip() != head
    assert not (registry.managed_root / "fixture/workspaces/area/src/main.py").exists()
    terminal = ledger.finish(request_id, "FAULT", code="HANDLER_FAILED")
    assert terminal["code"] == "NEEDS_RECONCILIATION"
    assert ledger.get(request_id)["effects"][0]["state"] == "UNKNOWN"
    ledger.disarm()
    with pytest.raises(LedgerError) as error:
        ledger.arm()  # Cannot rearm while the staged ref needs reconciliation.
    assert error.value.code == "NEEDS_RECONCILIATION"
    assert json.loads((stage / "intent.json").read_text())["snapshot_id"] == snapshot_id


def test_service_imports_only_pinned_approved_local_source(tmp_path: Path) -> None:
    allowed = tmp_path / "approved"
    allowed.mkdir(mode=0o700)
    original = allowed / "original"
    original.mkdir(mode=0o700)
    run_git(original, ["init", "-b", "main"])
    (original / "main.py").write_text("value = 1\n")
    run_git(original, ["add", "main.py"])
    run_git(original, ["-c", "user.name=Local Owner", "-c", "user.email=owner@local.invalid",
                       "commit", "-m", "Original work"])
    (original / "draft.txt").write_text("unsaved\n")
    head_before = run_git(original, ["rev-parse", "HEAD"]).strip()
    status_before = run_git(original, ["status", "--porcelain=v1", "-z"])
    ledger, registry, _ = make_service(tmp_path)
    grants = ImportGrants(tmp_path / "state" / "import-handles", roots=[allowed])
    handle = grants.grant(str(original))
    service = SourceService(ledger, registry, import_grants=grants)
    request_id = admit(ledger, "CREATE_PROJECT", "fixture", None, None,
                       {"name": "Fixture", "source": {"kind": "local", "import_handle": handle},
                        "template_id": "python-basic", "github_identity": "owner",
                        "visibility": "private"},
                       ("repo:fixture", "global:owner", "global:drive-installation"))
    project = service.import_local_source(request_id)
    assert project["origin_kind"] == "local" and project["origin_reference"] == handle
    assert run_git(Path(project["repo_path"]), ["rev-parse", "refs/heads/main"]).strip() == head_before
    assert run_git(original, ["rev-parse", "HEAD"]).strip() == head_before
    assert run_git(original, ["status", "--porcelain=v1", "-z"]) == status_before
    assert (original / "draft.txt").read_text() == "unsaved\n"
    ledger.finish(request_id, "COMPLETED")


def test_service_sync_refuses_changed_tracking_ref_before_checkout(tmp_path: Path) -> None:
    ledger, registry, service = make_service(tmp_path)
    base, repo = create_project_workspace(ledger, registry, service)
    workspace = Path(registry.get_workspace("fixture", "area")["worktree_path"])
    remote = tmp_path / "upstream.git"
    run_git(tmp_path, ["clone", "--bare", "--no-local", str(repo), str(remote)])
    developer = tmp_path / "developer"
    run_git(tmp_path, ["clone", str(remote), str(developer)])
    (developer / "first.txt").write_text("first\n")
    run_git(developer, ["add", "first.txt"])
    run_git(developer, ["-c", "user.name=Owner", "-c", "user.email=owner@local.invalid",
                        "commit", "-m", "First remote commit"])
    first = run_git(developer, ["rev-parse", "HEAD"]).strip()
    (developer / "second.txt").write_text("second\n")
    run_git(developer, ["add", "second.txt"])
    run_git(developer, ["-c", "user.name=Owner", "-c", "user.email=owner@local.invalid",
                        "commit", "-m", "Second remote commit"])
    second = run_git(developer, ["rev-parse", "HEAD"]).strip()
    run_git(developer, ["push", "origin", "main"])
    run_git(repo, ["remote", "add", "upstream", str(remote)])
    run_git(repo, ["fetch", "upstream"])
    run_git(repo, ["update-ref", "refs/remotes/upstream/main", first, second])
    request_id = admit(ledger, "SYNC_WORKSPACE", "fixture", "area", base,
                       {"mode": "remote_ff", "remote_ref": "upstream/main",
                        "target_commit": first}, ("repo:fixture",))
    # The fetched ref advances after command acceptance, before the admitted step starts.
    run_git(repo, ["update-ref", "refs/remotes/upstream/main", second, first])
    with pytest.raises(GitError) as error:
        service.sync_workspace_source(request_id, expected_generation=0,
                                      remote="upstream", remote_branch="main")
    assert error.value.code == "STALE_TARGET"
    assert run_git(workspace, ["rev-parse", "HEAD"]).strip() == base
    assert not (workspace / "first.txt").exists()
    assert not (workspace / "second.txt").exists()
    assert registry.get_workspace("fixture", "area")["generation"] == 0
    ledger.finish(request_id, "BLOCKED", code=error.value.code)
    assert ledger.get(request_id)["effects"][0]["state"] == "UNKNOWN"


def test_service_archive_retains_dirty_files_and_reports_status(tmp_path: Path) -> None:
    ledger, registry, service = make_service(tmp_path)
    base, _ = create_project_workspace(ledger, registry, service)
    workspace = Path(registry.get_workspace("fixture", "area")["worktree_path"])
    private = workspace / "draft.txt"
    private.write_bytes(b"unsaved owner work\n")
    before = service.inspect_workspace_source("fixture", "area")
    assert before["dirty"] and not before["archived"] and not before["source_changed"]
    request_id = admit(ledger, "ARCHIVE_WORKSPACE", "fixture", "area", base,
                       {"expected_branch": "feature/area", "delete_local_clean": False,
                        "delete_remote_merged": False}, ("repo:fixture",))
    archive = service.archive_workspace_source(request_id, expected_generation=0)
    assert archive["archived"] and archive["generation"] == 1
    ledger.finish(request_id, "COMPLETED")
    after = service.inspect_workspace_source("fixture", "area")
    assert after["archived"] and after["dirty"] and after["source_generation"] == 1
    assert private.read_bytes() == b"unsaved owner work\n"
    assert workspace.is_dir()
