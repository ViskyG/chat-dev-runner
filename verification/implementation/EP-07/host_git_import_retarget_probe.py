"""EP-07 disposable real-Git import/retarget probe against the installed wheel.

Run from outside the source tree with the installed 0.2.6 Python. GitHub REST and
HTTPS transport are replaced by exact-identity responses and a local bare clone;
no network, installed state, or owner project is read or written.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from runner_app.adapters.github import GitHubBranch, GitHubRepository
from runner_app.domain.commands import freeze, validate_shape
from runner_app.git.argv import GitError, run_git, run_git_bytes
from runner_app.git.github_import import ApprovedGitHubImport, GitHubImportService
from runner_app.git.imports import ImportGrants
from runner_app.git.registry import SourceRegistry
from runner_app.git.service import SourceService
from runner_app.git.worktrees import (
    create_repository,
    create_workspace,
    inspect_workspace,
    retarget_workspace,
)
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(path: Path, *arguments: str) -> str:
    return run_git(path, list(arguments)).strip()


def tree_sha(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        assert not path.is_symlink()
        if path.is_file():
            relative = path.relative_to(root).as_posix().encode()
            content = path.read_bytes()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    return digest.hexdigest()


def commit(path: Path, name: str, content: bytes) -> str:
    (path / name).write_bytes(content)
    git(path, "add", "--", name)
    git(path, "-c", "user.name=Probe", "-c", "user.email=probe@localhost.invalid",
        "commit", "-m", "Disposable source")
    return git(path, "rev-parse", "HEAD")


def command(ledger: Ledger, action: str, project: str, workspace: str | None,
            head: str | None, params: dict[str, Any]) -> dict[str, Any]:
    state = ledger.state()
    now = datetime.now(timezone.utc)
    value = {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": state["runner_id"], "epoch": state["epoch"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": action, "project_id": project, "workspace_id": workspace,
        "expected_head": head, "params": params, "intent": "Disposable EP-07 host probe",
    }
    validate_shape(value)
    return value


def admit(ledger: Ledger, value: dict[str, Any], scopes: tuple[str, ...]):
    return ledger.admit(value, freeze(value).digest, "ep07-disposable", scopes)


def source_snapshot(source: Path, remote: Path) -> dict[str, str]:
    # Status may refresh index stat metadata; take the index hash afterwards.
    status = run_git_bytes(source, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    return {
        "head": git(source, "rev-parse", "HEAD"),
        "branch": git(source, "symbolic-ref", "--short", "HEAD"),
        "status_sha256": sha(status),
        "refs_sha256": sha(run_git_bytes(source, ["show-ref"])),
        "index_sha256": sha((source / ".git" / "index").read_bytes()),
        "staged_entries_sha256": sha(run_git_bytes(source, ["ls-files", "-s", "-z"])),
        "config_sha256": sha((source / ".git" / "config").read_bytes()),
        "tracked_sha256": sha((source / "main.py").read_bytes()),
        "untracked_sha256": sha((source / "draft.txt").read_bytes()),
        "remote_refs_sha256": sha(run_git_bytes(remote, ["show-ref"])),
        "remote_config_sha256": sha((remote / "config").read_bytes()),
    }


def fixture(root: Path, name: str) -> tuple[Path, Path, str]:
    source = root / name
    source.mkdir(mode=0o700)
    git(source, "init", "-b", "main")
    head = commit(source, "main.py", b"value = 1\n")
    remote = root / f"{name}-remote.git"
    git(root, "clone", "--bare", "--no-local", str(source), str(remote))
    git(source, "remote", "add", "origin", str(remote))
    (source / "main.py").write_bytes(b"value = 2 staged\n")
    git(source, "add", "main.py")
    (source / "main.py").write_bytes(b"value = 3 unstaged\n")
    # Keep the dirty file outside Git's same-second index stat ambiguity.
    os.utime(source / "main.py", (946684800, 946684800))
    (source / "draft.txt").write_bytes(b"private draft\n")
    return source, remote, head


def state(root: Path) -> tuple[Ledger, SourceRegistry]:
    ledger = Ledger(Database(root / "state" / "runner.sqlite"))
    ledger.startup()
    ledger.arm()
    return ledger, SourceRegistry(ledger.database, root / "managed")


def local_import(root: Path) -> dict[str, Any]:
    source, remote, head = fixture(root, "local-original")
    before = source_snapshot(source, remote)
    original_git_tree = tree_sha(source / ".git")
    original_remote_tree = tree_sha(remote)
    ledger, registry = state(root / "local-state")
    grants = ImportGrants(root / "local-state" / "grants", roots=[root])
    handle = grants.grant(str(source))
    value = command(ledger, "CREATE_PROJECT", "local-fixture", None, None, {
        "name": "Local fixture", "source": {"kind": "local", "import_handle": handle},
        "template_id": "python-basic", "github_identity": "owner", "visibility": "private",
    })
    assert admit(ledger, value, ("repo:local-fixture",)).execute
    service = SourceService(ledger, registry, import_grants=grants)
    project = service.import_local_source(value["request_id"])
    workspace = service.create_initial_workspace_source(value["request_id"])
    ledger.finish(value["request_id"], "COMPLETED")
    repo = Path(project["repo_path"])
    # The import itself must leave index bytes unchanged. A subsequent Git status
    # may refresh stat/cache extensions, so compare that raw byte hash before it.
    index_after_import = sha((source / ".git" / "index").read_bytes())
    assert index_after_import == before["index_sha256"]
    assert tree_sha(source / ".git") == original_git_tree
    assert tree_sha(remote) == original_remote_tree
    after = source_snapshot(source, remote)
    assert {k: v for k, v in before.items() if k != "index_sha256"} == {
        k: v for k, v in after.items() if k != "index_sha256"}
    assert git(repo, "rev-parse", "HEAD") == head
    assert workspace["head"] == head and workspace["branch"] == "work/local-fixture"
    assert (Path(workspace["worktree_path"]) / "main.py").read_bytes() == b"value = 1\n"
    assert not (Path(workspace["worktree_path"]) / "draft.txt").exists()
    assert git(repo, "remote") == ""
    effect_states = [effect["state"] for effect in ledger.get(value["request_id"])["effects"]]
    assert effect_states == ["CONFIRMED", "CONFIRMED"]
    return {"status": "PASS", "source_head": head, "snapshot_before_after": {k: v for k, v in before.items() if k != "index_sha256"},
            "raw_index_sha256_before_after_import": before["index_sha256"],
            "original_git_tree_sha256_before_after_import": original_git_tree,
            "original_remote_tree_sha256_before_after_import": original_remote_tree,
            "managed_head": git(repo, "rev-parse", "HEAD"),
            "workspace_branch": workspace["branch"], "effects": effect_states,
            "source_grant_opaque": len(handle) == 48}


class Rest:
    expected_login = "runner-owner"

    def __init__(self, repository: GitHubRepository, head: str) -> None:
        self.repository = repository
        self.head = head
        self.repository_reads = 0
        self.branch_reads = 0

    def get_repository(self, owner: str, name: str, *,
                       expected_repo_id: int | None = None) -> GitHubRepository:
        assert (owner, name, expected_repo_id) == ("alice", "source", 421)
        self.repository_reads += 1
        return self.repository

    def branch_head(self, repository: GitHubRepository, branch: str) -> GitHubBranch:
        assert repository == self.repository and branch == "main"
        self.branch_reads += 1
        return GitHubBranch("main", self.head, False)


class LocalCloner:
    def __init__(self, source: Path, repository: GitHubRepository) -> None:
        self.source = source
        self.repository = repository
        self.calls = 0

    def clone_bare(self, stage_parent: Path, destination: Path) -> None:
        self.calls += 1
        git(stage_parent, "clone", "--bare", "--no-local", "--no-hardlinks", "--no-tags",
            str(self.source), str(destination))
        git(destination, "remote", "set-url", "origin", self.repository.clone_url)


def github_style_import(root: Path) -> dict[str, Any]:
    source, remote, head = fixture(root, "github-original")
    before = source_snapshot(source, remote)
    original_git_tree = tree_sha(source / ".git")
    original_remote_tree = tree_sha(remote)
    ledger, registry = state(root / "github-state")
    value = command(ledger, "CREATE_PROJECT", "github-fixture", None, None, {
        "name": "GitHub fixture", "source": {"kind": "github", "repository": "alice/source"},
        "template_id": "python-basic", "github_identity": "owner", "visibility": "private",
    })
    assert admit(ledger, value, ("repo:github-fixture", "global:owner",
                                 "global:drive-installation")).execute
    repository = GitHubRepository(
        id=421, node_id="R_421", owner="alice", owner_id=99, name="source",
        full_name="alice/source", html_url="https://github.com/alice/source",
        clone_url="https://github.com/alice/source.git",
        ssh_url="git@github.com:alice/source.git", private=True,
        default_branch="main", description=None,
    )
    rest = Rest(repository, head)
    cloners: list[LocalCloner] = []

    def cloner(observed: GitHubRepository) -> LocalCloner:
        result = LocalCloner(remote, observed)
        cloners.append(result)
        return result

    importer = GitHubImportService(
        ledger, registry, rest, cloner,
        {"alice/source": ApprovedGitHubImport(
            "alice/source", "owner", "runner-owner", 421, 99, "R_421")},
    )
    result = importer.import_source(value["request_id"])
    workspace = SourceService(ledger, registry).create_initial_workspace_source(value["request_id"])
    ledger.finish(value["request_id"], "COMPLETED")
    # The import itself must leave index bytes unchanged. A subsequent Git status
    # may refresh stat/cache extensions, so compare that raw byte hash before it.
    index_after_import = sha((source / ".git" / "index").read_bytes())
    assert index_after_import == before["index_sha256"]
    assert tree_sha(source / ".git") == original_git_tree
    assert tree_sha(remote) == original_remote_tree
    after = source_snapshot(source, remote)
    assert {k: v for k, v in before.items() if k != "index_sha256"} == {
        k: v for k, v in after.items() if k != "index_sha256"}
    repo = Path(result.project["repo_path"])
    assert result.default_head == head and git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "remote") == ""
    assert workspace["branch"] == "work/github-fixture" and workspace["head"] == head
    assert rest.repository_reads == 2 and rest.branch_reads == 2
    assert len(cloners) == 1 and cloners[0].calls == 1
    effect_states = [effect["state"] for effect in ledger.get(value["request_id"])["effects"]]
    assert effect_states == ["CONFIRMED", "CONFIRMED"]
    return {"status": "PASS", "source_head": head, "snapshot_before_after": {k: v for k, v in before.items() if k != "index_sha256"},
            "raw_index_sha256_before_after_import": before["index_sha256"],
            "original_git_tree_sha256_before_after_import": original_git_tree,
            "original_remote_tree_sha256_before_after_import": original_remote_tree,
            "managed_head": git(repo, "rev-parse", "HEAD"),
            "workspace_branch": workspace["branch"], "effects": effect_states,
            "provider_boundary": "fake REST + local bare Git clone; no GitHub network"}


def refusal(repo: Path, workspace: Path, expected: str, **arguments: Any) -> str:
    before = inspect_workspace(repo, workspace)
    try:
        retarget_workspace(repo, workspace, expected_head=expected,
                           expected_branch="feature/first", **arguments)
    except GitError as error:
        after = inspect_workspace(repo, workspace)
        assert after == before
        return error.code
    raise AssertionError("Retarget unexpectedly succeeded")


def retarget(root: Path) -> dict[str, Any]:
    ledger, registry = state(root / "retarget-state")
    managed = registry.managed_root
    repo = create_repository(managed, "retarget-fixture")
    base = git(repo, "rev-parse", "HEAD")
    first = create_workspace(managed, "retarget-fixture", repo, "first", "feature/first", base)
    second = create_workspace(managed, "retarget-fixture", repo, "second", "feature/second", base)
    git(repo, "branch", "feature/free", base)
    registry.register_project("retarget-fixture", "Retarget fixture", repo, origin_kind="new")
    registry.register_workspace("retarget-fixture", "first", "First", first,
                                branch="feature/first", head=base)
    registry.register_workspace("retarget-fixture", "second", "Second", second,
                                branch="feature/second", head=base)
    common = {"target_branch": "feature/free", "target_commit": base}
    busy_low = refusal(repo, first, base, **common, busy=True)
    assert busy_low == "WORKSPACE_BUSY"
    (first / "draft.txt").write_bytes(b"keep private\n")
    dirty_hash = sha((first / "draft.txt").read_bytes())
    dirty = refusal(repo, first, base, **common)
    assert dirty == "DIRTY_WORKSPACE" and sha((first / "draft.txt").read_bytes()) == dirty_hash
    (first / "draft.txt").unlink()
    duplicate = refusal(repo, first, base,
                        target_branch="feature/second", target_commit=base)
    assert duplicate == "BRANCH_IN_USE"
    assert git(second, "symbolic-ref", "--short", "HEAD") == "feature/second"

    params = {"expected_branch": "feature/first", **common}
    first_command = command(ledger, "RETARGET_WORKSPACE", "retarget-fixture", "first", base,
                            params)
    assert admit(ledger, first_command, ("repo:retarget-fixture",)).execute
    competing = command(ledger, "RETARGET_WORKSPACE", "retarget-fixture", "second", base,
                        {"expected_branch": "feature/second", "target_branch": "feature/first",
                         "target_commit": base})
    competing_result = admit(ledger, competing, ("repo:retarget-fixture",))
    assert not competing_result.execute and competing_result.receipt["code"] == "BUSY"
    assert ledger.get(competing["request_id"])["status"] == "REJECTED"
    result = SourceService(ledger, registry).retarget_workspace_source(
        first_command["request_id"], expected_generation=0)
    ledger.finish(first_command["request_id"], "COMPLETED")
    assert result["branch"] == "feature/free" and result["head"] == base
    assert result["generation"] == 1
    assert inspect_workspace(repo, first)["branch"] == "feature/free"
    assert inspect_workspace(repo, second)["branch"] == "feature/second"
    assert registry.get_workspace("retarget-fixture", "second")["generation"] == 0
    assert git(repo, "rev-parse", "refs/heads/feature/first") == base
    effects = ledger.get(first_command["request_id"])["effects"]
    assert len(effects) == 1 and effects[0]["state"] == "CONFIRMED"
    return {"status": "PASS_AT_SOURCE_SERVICE_ONLY", "base_head": base,
            "low_level_refusal_codes": {"busy": busy_low, "dirty": dirty,
                                        "duplicate_checkout": duplicate},
            "admission_busy_code": competing_result.receipt["code"],
            "retarget_branch": result["branch"], "source_generation": result["generation"],
            "other_workspace_branch": "feature/second", "effect_state": effects[0]["state"]}


def installed_module_hashes(source_root: Path) -> dict[str, str]:
    import runner_app

    installed = Path(runner_app.__file__).parent
    paths = ("git/worktrees.py", "git/service.py", "git/github_import.py",
             "git/imports.py", "git/registry.py", "storage/ledger.py",
             "engine/integrations.py", "engine/service.py")
    hashes = {}
    for relative in paths:
        current = sha((source_root / "runner_app" / relative).read_bytes())
        wheel = sha((installed / relative).read_bytes())
        assert current == wheel, f"Source/wheel differs for {relative}"
        hashes[relative] = current
    assert "site-packages" in str(installed)
    return hashes


def installed_runtime_handler_keys() -> list[str]:
    import runner_app

    module_path = Path(runner_app.__file__).parent / "engine" / "integrations.py"
    tree = ast.parse(module_path.read_text())
    candidates = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "RuntimeIntegrations" and node.args
                and isinstance(node.args[-1], ast.Dict)):
            candidates.append(node.args[-1])
    assert len(candidates) == 1
    keys = [item.value for item in candidates[0].keys if isinstance(item, ast.Constant)]
    assert len(keys) == len(candidates[0].keys)
    assert set(keys) == {"CREATE_PROJECT", "CREATE_WORKSPACE", "RECONCILE",
                         "RESUME_PROVISION", "FETCH"}
    return sorted(keys)


def main() -> None:
    source_root = Path(__file__).resolve().parents[3]
    assert importlib.metadata.version("chat-dev-runner") == "0.2.6"
    hashes = installed_module_hashes(source_root)
    with tempfile.TemporaryDirectory(prefix="cdr-ep07-import-retarget-") as directory:
        root = Path(directory)
        assert os.stat(root).st_mode & 0o777 == 0o700
        report = {
            "schema_version": "1.0", "packet": "EP-07", "result": "PASS_WITH_ROUTE_GAP",
            "source_commit": subprocess.check_output(
                ["git", "-C", str(source_root), "rev-parse", "HEAD"], text=True,
            ).strip(),
            "installed_version": importlib.metadata.version("chat-dev-runner"),
            "installed_module_sha256": hashes,
            "installed_runtime_handler_keys": installed_runtime_handler_keys(),
            "observed_at_utc": datetime.now(timezone.utc).isoformat(),
            "environment": "owner Linux host; installed 0.2.6 Python; disposable /tmp Git + SQLite",
            "local_import": local_import(root),
            "github_style_import": github_style_import(root),
            "retarget": retarget(root),
            "release_limit": (
                "Installed runtime integrations register CREATE_PROJECT, CREATE_WORKSPACE, "
                "RECONCILE, RESUME_PROVISION and FETCH, but no RETARGET_WORKSPACE handler. "
                "Engine records terminal ACTION_UNAVAILABLE at admission; the UI "
                "eligibility guard disables the form because the capability is absent. "
                "Low-level and SourceService proof does not close the installed route gate."
            ),
            "network_calls": 0,
            "installed_state_touched": False,
        }
    output = source_root / "verification/implementation/EP-07/host-git-import-retarget-20260929.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"result": report["result"], "local": report["local_import"]["status"],
                      "github_style": report["github_style_import"]["status"],
                      "retarget": report["retarget"]["status"], "evidence": str(output)}))


if __name__ == "__main__":
    main()
