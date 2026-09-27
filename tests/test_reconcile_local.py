"""Real disposable managed Git proves local reconciliation without replay."""
from __future__ import annotations

from pathlib import Path

from test_reconcile_workflow import _ports, _submit, _target

from runner_app.git import worktrees
from runner_app.git.argv import run_git
from runner_app.git.registry import SourceRegistry
from runner_app.git.templates import starter_files
from runner_app.storage.database import Database
from runner_app.storage.ledger import Ledger
from runner_app.workflows.reconcile_local import ManagedGitEffectReader

PROJECT = "notes-lab"


def _registered_new(tmp_path: Path, ledger: Ledger) -> tuple[SourceRegistry, Path]:
    registry = SourceRegistry(ledger.database, tmp_path / "managed")
    repo = worktrees.create_repository(
        registry.managed_root, PROJECT,
        starter_files=starter_files("python-basic", PROJECT),
    )
    registry.register_project(PROJECT, "Notes Lab", repo, origin_kind="new")
    return registry, repo


def test_lost_local_project_ack_requires_registered_exact_starter(tmp_path: Path) -> None:
    ledger, target, ids = _target(tmp_path, [
        ("local-project", "local_workspace", {"project_id": PROJECT, "kind": "new"}),
    ])
    registry, repo = _registered_new(tmp_path, ledger)
    local = ManagedGitEffectReader(registry)
    _, result = _submit(ledger, target, {**_ports(), "local_workspace": local})
    assert result["status"] == "COMPLETED"
    assert result["result"]["confirmed_effect_ids"] == ids
    assert ledger.get(target["request_id"])["effects"][0]["provider_id"] == str(repo)
    assert ledger.holds_lock(target["request_id"], f"repo:{PROJECT}")


def test_local_project_name_collision_without_registry_remains_unresolved(tmp_path: Path) -> None:
    ledger, target, ids = _target(tmp_path, [
        ("local-project", "local_workspace", {"project_id": PROJECT, "kind": "new"}),
    ])
    registry = SourceRegistry(ledger.database, tmp_path / "managed")
    worktrees.create_repository(
        registry.managed_root, PROJECT,
        starter_files=starter_files("python-basic", PROJECT),
    )
    _, result = _submit(ledger, target, {
        **_ports(), "local_workspace": ManagedGitEffectReader(registry),
    })
    assert result["result"]["confirmed_effect_ids"] == []
    assert result["result"]["remaining_effect_ids"] == ids
    assert ledger.holds_lock(target["request_id"], f"repo:{PROJECT}")


def test_initial_workspace_ack_requires_clean_registered_worktree(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    repo = worktrees.create_repository(
        managed, PROJECT, starter_files=starter_files("python-basic", PROJECT),
    )
    head = run_git(repo, ["rev-parse", "--verify", "HEAD^{commit}"]).strip()
    ledger, target, ids = _target(tmp_path, [
        ("local-project", "local_workspace", {"project_id": PROJECT, "kind": "new"}),
        ("initial-workspace", "local_workspace", {
            "project_id": PROJECT, "workspace_id": "primary",
            "branch": f"work/{PROJECT}", "base_commit": head,
        }),
    ])
    registry = SourceRegistry(ledger.database, managed)
    registry.register_project(PROJECT, "Notes Lab", repo, origin_kind="new")
    path = worktrees.create_workspace(
        managed, PROJECT, repo, "primary", f"work/{PROJECT}", head,
    )
    registry.register_workspace(
        PROJECT, "primary", "Primary workspace", path,
        branch=f"work/{PROJECT}", head=head,
    )
    local = ManagedGitEffectReader(registry)
    _, result = _submit(ledger, target, {**_ports(), "local_workspace": local})
    assert result["result"]["confirmed_effect_ids"] == ids
    confirmed = ledger.get(target["request_id"])["effects"]
    assert confirmed[0]["provider_id"] == str(repo)
    assert confirmed[1]["provider_id"] == str(path)


def test_dirty_initial_workspace_remains_unresolved(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    repo = worktrees.create_repository(
        managed, PROJECT, starter_files=starter_files("python-basic", PROJECT),
    )
    head = run_git(repo, ["rev-parse", "--verify", "HEAD^{commit}"]).strip()
    ledger, target, ids = _target(tmp_path, [
        ("local-project", "local_workspace", {"project_id": PROJECT, "kind": "new"}),
        ("initial-workspace", "local_workspace", {
            "project_id": PROJECT, "workspace_id": "primary",
            "branch": f"work/{PROJECT}", "base_commit": head,
        }),
    ])
    registry = SourceRegistry(ledger.database, managed)
    registry.register_project(PROJECT, "Notes Lab", repo, origin_kind="new")
    path = worktrees.create_workspace(
        managed, PROJECT, repo, "primary", f"work/{PROJECT}", head,
    )
    registry.register_workspace(
        PROJECT, "primary", "Primary workspace", path,
        branch=f"work/{PROJECT}", head=head,
    )
    ledger.confirm_effect(ids[0], str(repo))
    (path / "README.md").write_text("Unreviewed edit\n")
    _, result = _submit(ledger, target, {
        **_ports(), "local_workspace": ManagedGitEffectReader(registry),
    })
    assert result["result"]["confirmed_effect_ids"] == []
    assert result["result"]["remaining_effect_ids"] == [ids[1]]
    assert ledger.holds_lock(target["request_id"], f"repo:{PROJECT}")


def test_registered_local_import_is_proved_by_opaque_origin_handle(tmp_path: Path) -> None:
    source = tmp_path / "approved-source"
    source.mkdir(mode=0o700)
    run_git(source, ["init", "-b", "main"])
    (source / "README.md").write_text("Committed source\n")
    run_git(source, ["add", "README.md"])
    run_git(source, ["-c", "user.name=Fixture", "-c",
                     "user.email=fixture@localhost.invalid", "commit", "-m", "Base"])
    managed = tmp_path / "managed"
    repo = worktrees.import_local_granted(managed, PROJECT, source)
    ledger = Ledger(Database(tmp_path / "state" / "ledger.sqlite"))
    ledger.startup()
    registry = SourceRegistry(ledger.database, managed)
    handle = "a" * 48
    registry.register_project(
        PROJECT, "Notes Lab", repo, origin_kind="local", origin_reference=handle,
    )
    target = {
        "status": "BLOCKED", "action": "CREATE_PROJECT", "project_id": PROJECT,
        "request_id": "old-create-id",
        "command": {"action": "CREATE_PROJECT", "project_id": PROJECT,
                    "params": {"name": "Notes Lab",
                               "source": {"kind": "local", "import_handle": handle}}},
    }
    effect = {
        "effect_id": "old-effect-id", "step_id": "local-project",
        "kind": "local_workspace", "state": "UNKNOWN", "provider_id": None,
        "intent": {"project_id": PROJECT, "kind": "local", "import_handle": handle},
    }
    reader = ManagedGitEffectReader(registry)
    proof = reader(target, effect)
    assert proof is not None and proof.provider_id == str(repo)
    assert proof.observed_intent == effect["intent"]
    effect["intent"] = {**effect["intent"], "import_handle": "b" * 48}
    assert reader(target, effect) is None


def test_registered_github_import_requires_exact_cloned_branch_and_head(tmp_path: Path) -> None:
    source = tmp_path / "clone-fixture"
    source.mkdir(mode=0o700)
    run_git(source, ["init", "-b", "main"])
    (source / "README.md").write_text("Imported source\n")
    run_git(source, ["add", "README.md"])
    run_git(source, ["-c", "user.name=Fixture", "-c",
                     "user.email=fixture@localhost.invalid", "commit", "-m", "Base"])
    managed = tmp_path / "managed"
    repo = worktrees.import_local_granted(managed, PROJECT, source)
    head = run_git(repo, ["rev-parse", "--verify", "HEAD^{commit}"]).strip()
    ledger = Ledger(Database(tmp_path / "state" / "ledger.sqlite"))
    ledger.startup()
    registry = SourceRegistry(ledger.database, managed)
    registry.register_project(
        PROJECT, "Notes Lab", repo, origin_kind="github",
        origin_reference="ViskyG/existing-source",
    )
    request_id = "original-request"
    target = {
        "status": "BLOCKED", "action": "CREATE_PROJECT", "project_id": PROJECT,
        "request_id": request_id,
        "command": {"action": "CREATE_PROJECT", "project_id": PROJECT,
                    "params": {"name": "Notes Lab", "source": {
                        "kind": "github", "repository": "ViskyG/existing-source",
                    }}},
    }
    intent = {
        "project_id": PROJECT, "kind": "github",
        "repository": "ViskyG/existing-source", "repository_id": 42,
        "owner_id": 12452296, "node_id": "R_42",
        "clone_url": "https://github.com/ViskyG/existing-source.git",
        "default_branch": "main", "remote_head": head,
        "stage_path": str(managed / PROJECT / f".github-import-{request_id}"),
        "repo_path": str(repo),
    }
    effect = {
        "effect_id": "original-effect", "step_id": "local-project",
        "kind": "local_workspace", "state": "UNKNOWN",
        "provider_id": None, "intent": intent,
    }
    reader = ManagedGitEffectReader(registry)
    proof = reader(target, effect)
    assert proof is not None and proof.provider_id == str(repo)
    effect["intent"] = {**intent, "remote_head": "b" * 40}
    assert reader(target, effect) is None
