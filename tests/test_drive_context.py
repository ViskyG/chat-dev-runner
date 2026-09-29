"""Real disposable Git fixtures for the read-only EP-04 context exporter."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from runner_app.adapters.drive_context import ContextError, ContextExporter
from runner_app.domain.commands import freeze, validate_shape
from runner_app.git.argv import run_git
from runner_app.git.registry import SourceRegistry
from runner_app.git.worktrees import create_repository, create_workspace
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger


def _commit(workspace: Path) -> str:
    run_git(workspace, ["add", "--all", "--"])
    run_git(workspace, ["-c", "user.name=Context Fixture",
                        "-c", "user.email=context@local.invalid", "commit", "-m", "Fixture"])
    return run_git(workspace, ["rev-parse", "HEAD"]).strip()


@pytest.fixture
def source(tmp_path: Path) -> tuple[Ledger, SourceRegistry, Path, Path, str]:
    database = Database(tmp_path / "state" / "state.sqlite")
    ledger = Ledger(database)
    ledger.startup()
    ledger.arm()
    registry = SourceRegistry(database, tmp_path / "managed")
    repo = create_repository(registry.managed_root, "project")
    initial = run_git(repo, ["rev-parse", "HEAD"]).strip()
    workspace = create_workspace(registry.managed_root, "project", repo,
                                 "area", "feature/area", initial)
    (workspace / "app.py").write_text("value = 'committed'\n")
    (workspace / "data.bin").write_bytes(b"\x00\xff binary")
    (workspace / "large.txt").write_text("x" * 70_000)
    (workspace / ".env").write_text("SECRET=committed-private\n")
    (workspace / "link.txt").symlink_to("app.py")
    head = _commit(workspace)
    registry.register_project("project", "Project", repo, origin_kind="new")
    registry.register_workspace("project", "area", "Area", workspace,
                                branch="feature/area", head=head)
    return ledger, registry, repo, workspace, head


def _admit(ledger: Ledger, head: str, *, paths: list[str] | None = None,
           include_diff: bool = False, action: str = "EXPORT_CONTEXT",
           scopes: tuple[str, ...] = ("workspace:project/area",)) -> str:
    now = datetime.now(timezone.utc)
    command = {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": ledger.state()["runner_id"], "epoch": ledger.state()["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": action, "project_id": "project", "workspace_id": "area",
        "expected_head": head,
        "params": ({"paths": paths or [], "include_diff": include_diff}
                   if action == "EXPORT_CONTEXT" else {"profile_id": "python-quick"}),
        "intent": "One local context fixture",
    }
    validate_shape(command)
    admission = ledger.admit(command, freeze(command).digest, "local-owner", scopes)
    assert admission.execute
    return command["request_id"]


def test_dirty_untracked_and_protected_bytes_never_enter_committed_export(source) -> None:
    ledger, registry, _, workspace, head = source
    original_index = Path(run_git(workspace, ["rev-parse", "--git-path", "index"]).strip())
    index_before = original_index.read_bytes()
    (workspace / "app.py").write_text("value = 'unsaved-private-token'\n")
    (workspace / "private-untracked.txt").write_text("UNTRACKED_SECRET=never-export\n")
    request = _admit(ledger, head, paths=["app.py", "data.bin", "large.txt", "link.txt",
                                          "missing.py"])
    report = ContextExporter(ledger, registry).export(request)
    encoded = json.dumps(report)
    inventory = {entry["path"]: entry for entry in report["inventory"]}
    selection = {entry["path"]: entry for entry in report["selection"]}
    assert report["base_commit"] == head
    assert report["source"] == "COMMITTED_TREE" and report["dirty_at_export"]
    assert report["delivery_status"] == "LOCAL_ONLY" and report["privacy_review_required"]
    assert selection["app.py"]["status"] == "INCLUDED"
    assert selection["app.py"]["text"] == "value = 'committed'\n"
    assert inventory["app.py"]["sha256"] == hashlib.sha256(
        b"value = 'committed'\n").hexdigest()
    assert inventory["data.bin"]["classification"] == "BINARY"
    assert inventory["large.txt"]["classification"] == "LARGE"
    assert inventory["large.txt"]["sha256"] is None
    assert inventory[".env"]["classification"] == "PROTECTED"
    assert inventory[".env"]["sha256"] is None
    assert inventory["link.txt"]["classification"] == "SYMLINK"
    assert inventory["link.txt"]["sha256"] is None
    assert selection["missing.py"]["status"] == "MISSING"
    assert "unsaved-private-token" not in encoded
    assert "UNTRACKED_SECRET" not in encoded
    assert "committed-private" not in encoded
    assert "private-untracked.txt" not in encoded
    assert original_index.read_bytes() == index_before


def test_explicit_diff_request_has_no_implicit_dirty_or_previous_commit_basis(source) -> None:
    ledger, registry, _, workspace, head = source
    (workspace / "app.py").write_text("unsaved source\n")
    request = _admit(ledger, head, paths=["app.py"], include_diff=True)
    with pytest.raises(ContextError) as error:
        ContextExporter(ledger, registry).export(request)
    assert error.value.code == "COMMITTED_DIFF_BASIS_REQUIRED"


def test_only_running_export_with_exact_workspace_lock_may_read(source) -> None:
    ledger, registry, _, _, head = source
    exporter = ContextExporter(ledger, registry)
    with pytest.raises(ContextError) as missing:
        exporter.export(str(uuid4()))
    assert missing.value.code == "OPERATION_NOT_RUNNING"
    wrong = _admit(ledger, head, action="VERIFY")
    with pytest.raises(ContextError) as action:
        exporter.export(wrong)
    assert action.value.code == "WRONG_CONTEXT_ACTION"
    ledger.finish(wrong, "COMPLETED")
    unscoped = _admit(ledger, head, scopes=("workspace:project/other",))
    with pytest.raises(ContextError) as lock:
        exporter.export(unscoped)
    assert lock.value.code == "CONTEXT_LOCK_MISSING"


def test_commit_head_change_refuses_instead_of_reading_live_checkout(source) -> None:
    ledger, registry, _, workspace, head = source
    request = _admit(ledger, head, paths=["app.py"])
    (workspace / "app.py").write_text("changed commit\n")
    _commit(workspace)
    with pytest.raises(ContextError) as error:
        ContextExporter(ledger, registry).export(request)
    assert error.value.code == "WORKSPACE_CHANGED"


def test_hostile_local_git_config_is_refused_before_blob_reads(source) -> None:
    ledger, registry, repo, _, head = source
    request = _admit(ledger, head, paths=["app.py"])
    run_git(repo, ["config", "filter.hijack.smudge", "/bin/true"])
    with pytest.raises(ContextError) as error:
        ContextExporter(ledger, registry).export(request)
    assert error.value.code == "UNSAFE_GIT_CONFIG"


def test_replacement_refs_cannot_change_the_reported_committed_tree(source) -> None:
    ledger, registry, repo, _, head = source
    request = _admit(ledger, head, paths=["app.py"])
    other = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()
    run_git(repo, ["update-ref", f"refs/replace/{head}", other])
    with pytest.raises(ContextError) as error:
        ContextExporter(ledger, registry).export(request)
    assert error.value.code == "REPLACEMENT_OBJECTS_UNSUPPORTED"


def test_engine_export_context_keeps_excerpts_owner_local(source) -> None:
    import time

    from runner_app.domain.commands import LocalPolicy, Provenance
    from runner_app.engine.context_route import ExportContextRoute
    from runner_app.engine.service import Engine

    ledger, registry, _, workspace, head = source
    (workspace / "app.py").write_text("value = 'unsaved-private'\n")
    now = datetime.now(timezone.utc)
    command = {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": ledger.state()["runner_id"], "epoch": ledger.state()["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "EXPORT_CONTEXT", "project_id": "project", "workspace_id": "area",
        "expected_head": head, "params": {"paths": ["app.py"], "include_diff": False},
        "intent": "Read one committed local fixture",
    }
    engine = Engine(
        ledger,
        policy=LocalPolicy(project_ids=frozenset({"project"}),
                           workspace_pairs=frozenset({("project", "area")})),
        handlers={"EXPORT_CONTEXT": ExportContextRoute(ledger, registry)},
    )
    try:
        admission = engine.submit(json.dumps(command).encode(),
                                  Provenance("local", "local-owner"))
        assert admission.execute
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            operation = ledger.get(command["request_id"])
            if operation is not None and operation["status"] != "RUNNING":
                break
            time.sleep(0.01)
        assert operation is not None and operation["status"] == "COMPLETED"
        report = operation["result"]["context"]
        assert report["delivery_status"] == "LOCAL_ONLY"
        assert report["privacy_review_required"] is True
        assert report["dirty_at_export"] is True
        assert report["selection"][0]["text"] == "value = 'committed'\n"
        assert "unsaved-private" not in json.dumps(operation)
        assert "value = 'committed'" not in json.dumps(operation["receipt"])
        assert operation["effects"] == []
        duplicate = engine.submit(json.dumps(command).encode(),
                                  Provenance("local", "local-owner"))
        assert duplicate.duplicate and duplicate.receipt == operation["receipt"]
    finally:
        engine.shutdown()


def test_context_route_rejects_unpinned_diff_without_source_result(source) -> None:
    from threading import Event

    from runner_app.engine.context_route import ExportContextRoute
    from runner_app.engine.service import Context

    ledger, registry, _, _, head = source
    request = _admit(ledger, head, paths=["app.py"], include_diff=True)
    operation = ledger.get(request)
    assert operation is not None
    result = ExportContextRoute(ledger, registry)(
        freeze(operation["command"]), Context(Event(), ledger),
    )
    assert result.status == "BLOCKED"
    assert result.code == "COMMITTED_DIFF_BASIS_REQUIRED"
    assert result.result == {}
