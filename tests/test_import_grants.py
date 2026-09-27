"""Owner-only local import grants against temporary real Git checkouts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from runner_app.api.auth import Sessions
from runner_app.api.runtime import create_runtime_app
from runner_app.config import Settings
from runner_app.domain.commands import DomainError, LocalPolicy, Provenance
from runner_app.engine.service import Engine, OperationResult
from runner_app.git.imports import ImportGrants
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger

ORIGIN = "http://127.0.0.1:8765"


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    (path / "README.md").write_text("Owner's uncommitted work\n")
    (path / "untracked.txt").write_text("Must remain untouched\n")
    return path


def _contents(path: Path) -> dict[str, str]:
    return {
        str(item.relative_to(path)): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in path.rglob("*") if item.is_file()
    }


def _error(grants: ImportGrants, handle: str) -> str:
    with pytest.raises(DomainError) as caught:
        grants.resolve(handle)
    return caught.value.code


def test_grant_is_private_durable_and_original_checkout_is_untouched(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    repo = _repo(root / "source")
    before = _contents(repo)
    store = tmp_path / "state" / "import-handles"
    grants = ImportGrants(store, roots=[root])
    handle = grants.grant(str(repo))
    assert re.fullmatch(r"[0-9a-f]{48}", handle)
    assert grants.resolve(handle) == repo
    with grants.open_source(handle) as pinned:
        assert pinned.path == repo
        assert os.fstat(pinned.directory_fd).st_ino == repo.stat().st_ino
        probe = subprocess.run(["git", "-C", pinned.child_path, "rev-parse", "--git-dir"],
                               check=True, capture_output=True, pass_fds=pinned.pass_fds)
        assert probe.stdout.strip() == b".git"
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    assert stat.S_IMODE((store / handle).stat().st_mode) == 0o600
    assert _contents(repo) == before
    assert ImportGrants(store, roots=[root]).resolve(handle) == repo
    assert _error(ImportGrants(store), handle) == "IMPORT_NOT_CONFIGURED"
    other = tmp_path / "other"
    other.mkdir()
    assert _error(ImportGrants(store, roots=[other]), handle) == "IMPORT_NOT_APPROVED"


def test_grants_refuse_outside_roots_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    outside = _repo(tmp_path / "outside")
    grants = ImportGrants(tmp_path / "state" / "handles", roots=[root])
    with pytest.raises(DomainError, match="разрешённые корни") as caught:
        grants.grant(str(outside))
    assert caught.value.code == "IMPORT_NOT_APPROVED"
    alias = root / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(DomainError) as caught:
        grants.grant(str(alias))
    assert caught.value.code == "INVALID_IMPORT_PATH"
    nested = root / "nested"
    nested.mkdir()
    linked_parent = root / "linked-parent"
    linked_parent.symlink_to(nested, target_is_directory=True)
    _repo(nested / "repo")
    with pytest.raises(DomainError) as caught:
        grants.grant(str(linked_parent / "repo"))
    assert caught.value.code == "INVALID_IMPORT_PATH"
    with pytest.raises(DomainError) as caught:
        ImportGrants(tmp_path / "another-store", roots=[alias])
    assert caught.value.code == "INVALID_IMPORT_ROOT"


def test_changed_git_directory_and_path_swap_invalidate_handle(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    repo = _repo(root / "source")
    grants = ImportGrants(tmp_path / "state" / "handles", roots=[root])
    handle = grants.grant(str(repo))
    original = root / "moved-original"
    repo.rename(original)
    repo.symlink_to(original, target_is_directory=True)
    assert _error(grants, handle) == "IMPORT_SOURCE_CHANGED"
    repo.unlink()
    _repo(repo)
    assert _error(grants, handle) == "IMPORT_SOURCE_CHANGED"
    repo.rename(root / "replacement")
    original.rename(repo)
    git_dir = repo / ".git"
    git_dir.rename(repo / ".git-old")
    git_dir.symlink_to(repo / ".git-old", target_is_directory=True)
    assert _error(grants, handle) == "IMPORT_SOURCE_CHANGED"


def test_pinned_clone_reads_original_after_path_replacement(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    repo = _repo(root / "source")
    (repo / "README.md").write_text("approved source\n")
    environment = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, env=environment)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-qm", "initial"],
                   check=True, env=environment)
    grants = ImportGrants(tmp_path / "state" / "handles", roots=[root])
    handle = grants.grant(str(repo))
    with grants.open_source(handle) as pinned:
        moved = root / "original-moved"
        repo.rename(moved)
        replacement = _repo(repo)
        (replacement / "README.md").write_text("replacement source\n")
        managed = tmp_path / "managed"
        subprocess.run(["git", "clone", "-q", "--no-local", "--no-hardlinks",
                        pinned.child_path, str(managed)],
                       check=True, env=environment, pass_fds=pinned.pass_fds,
                       capture_output=True)
    assert (managed / "README.md").read_text() == "approved source\n"
    assert (moved / "README.md").read_text() == "approved source\n"
    assert _error(grants, handle) == "IMPORT_SOURCE_CHANGED"


def test_unknown_and_unsafe_handles_are_refused_without_path_leak(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    store = tmp_path / "state" / "handles"
    grants = ImportGrants(store, roots=[root])
    assert _error(grants, "unknown") == "IMPORT_NOT_APPROVED"
    assert _error(grants, "../outside") == "IMPORT_NOT_APPROVED"
    assert _error(grants, "0" * 48) == "IMPORT_NOT_APPROVED"
    no_git = root / "ordinary"
    no_git.mkdir()
    with pytest.raises(DomainError) as caught:
        grants.grant(str(no_git))
    assert caught.value.code == "IMPORT_SOURCE_UNSAFE"


def test_local_endpoint_requires_session_origin_and_csrf(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    repo = _repo(root / "source")
    store = tmp_path / "state" / "handles"
    grants = ImportGrants(store, roots=[root])
    ledger = Ledger(Database(tmp_path / "database" / "state.sqlite"))
    ledger.startup()
    engine = Engine(ledger, import_grants=grants)
    sessions = Sessions()
    bootstrap = sessions.issue_bootstrap()
    app = create_runtime_app(Settings(), engine, sessions, import_grants=grants)
    with TestClient(app, base_url=ORIGIN) as client:
        path = "/api/v1/local/import-handles"
        body = {"path": str(repo)}
        assert client.post(path, json=body, headers={"Origin": ORIGIN}).status_code == 403
        session = client.post("/api/v1/local/session", json={"bootstrap": bootstrap},
                              headers={"Origin": ORIGIN})
        assert session.status_code == 200
        csrf = session.json()["csrf_token"]
        bad = [
            ({"Origin": "http://evil.invalid", "X-CSRF-Token": csrf}, "INVALID_ORIGIN"),
            ({"Origin": ORIGIN}, "INVALID_CSRF"),
            ({"Origin": ORIGIN, "X-CSRF-Token": "forged"}, "INVALID_CSRF"),
            ({"Origin": ORIGIN, "X-CSRF-Token": csrf, "Host": "evil.invalid"}, "INVALID_HOST"),
        ]
        for headers, code in bad:
            response = client.post(path, json=body, headers=headers)
            assert response.status_code == 403
            assert response.json()["error"] == code
        assert not list(store.iterdir())
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == 200
        handle = response.json()["import_handle"]
        assert response.json() == {"import_handle": handle}
        assert str(repo) not in response.text
        assert grants.resolve(handle) == repo
        assert client.post(path, json={"path": str(repo), "extra": 1},
                           headers=headers).status_code == 422
    engine.shutdown()


def test_default_unconfigured_api_cannot_grant_a_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "source")
    ledger = Ledger(Database(tmp_path / "database" / "state.sqlite"))
    ledger.startup()
    engine = Engine(ledger)
    sessions = Sessions()
    bootstrap = sessions.issue_bootstrap()
    with TestClient(create_runtime_app(Settings(), engine, sessions), base_url=ORIGIN) as client:
        session = client.post("/api/v1/local/session", json={"bootstrap": bootstrap},
                              headers={"Origin": ORIGIN})
        headers = {"Origin": ORIGIN, "X-CSRF-Token": session.json()["csrf_token"]}
        response = client.post("/api/v1/local/import-handles", json={"path": str(repo)},
                               headers=headers)
        assert response.status_code == 503
        assert response.json()["error"] == "IMPORT_NOT_CONFIGURED"
        assert str(repo) not in response.text
    engine.shutdown()


def _create_project_command(ledger: Ledger, handle: str) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": ledger.state()["runner_id"], "epoch": ledger.state()["epoch"],
        "issued_at": now.isoformat(), "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "CREATE_PROJECT", "project_id": "imported", "workspace_id": None,
        "expected_head": None, "intent": "Import the owner-approved source checkout.",
        "params": {"name": "Imported", "source": {"kind": "local", "import_handle": handle},
                   "template_id": "python", "github_identity": "owner", "visibility": "private"},
    }


def test_remote_command_uses_only_issued_handle_without_policy_bleed(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    repo = _repo(root / "source")
    grants = ImportGrants(tmp_path / "state" / "handles", roots=[root])
    handle = grants.grant(str(repo))
    ledger = Ledger(Database(tmp_path / "database" / "state.sqlite"))
    ledger.startup()
    ledger.arm()
    provenance = Provenance(kind="drive", source_id="registered-root")
    policy = LocalPolicy(mailboxes={provenance.source_id: provenance},
                         approved_templates=frozenset({"python"}),
                         github_identities=frozenset({"owner"}))
    engine = Engine(ledger, policy=policy, import_grants=grants,
                    handlers={"CREATE_PROJECT": lambda command, context: OperationResult()})
    try:
        unknown = _create_project_command(ledger, "0" * 48)
        rejection = engine.submit(json.dumps(unknown).encode(), provenance)
        assert rejection.receipt["code"] == "IMPORT_NOT_APPROVED"
        assert not rejection.execute
        issued = _create_project_command(ledger, handle)
        accepted = engine.submit(json.dumps(issued).encode(), provenance)
        assert accepted.execute
        deadline = time.monotonic() + 3
        while ledger.get(str(issued["request_id"]))["status"] == "RUNNING" and time.monotonic() < deadline:
            time.sleep(.01)
        assert ledger.get(str(issued["request_id"]))["status"] == "COMPLETED"
        assert not engine.policy.import_handles
        forged_path = _create_project_command(ledger, str(repo))
        rejection = engine.submit(json.dumps(forged_path).encode(), provenance)
        assert rejection.receipt["code"] == "INVALID_SCHEMA"
        assert not rejection.execute
    finally:
        engine.shutdown()
