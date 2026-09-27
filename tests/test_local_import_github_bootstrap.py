"""Separate owner-approved LOCAL-import baseline push, with no real GitHub calls."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_github_git_transport import MARKER, TOKEN, URL, FakeInspector, _repository

from runner_app.git.argv import GitError, run_git, run_git_input
from runner_app.git.github_transport import GitHubGitTransport
from runner_app.git.registry import SourceRegistry
from runner_app.git.worktrees import create_repository
from runner_app.storage import Database


@dataclass
class Case:
    repo: Path
    registry: SourceRegistry
    root_commit: str
    imported_head: str
    remote: Path


def _case(tmp_path: Path, *, origin_kind: str = "local") -> Case:
    managed = tmp_path / "managed"
    repo = create_repository(managed, "toy-project")
    root = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()
    tree = run_git(repo, ["rev-parse", f"{root}^{{tree}}"] ).strip()
    child = run_git_input(
        repo,
        ["-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
         "commit-tree", tree, "-p", root],
        b"Imported second commit\n",
    ).strip()
    run_git(repo, ["update-ref", "refs/heads/feature/imported", child])
    run_git(repo, ["symbolic-ref", "HEAD", "refs/heads/feature/imported"])
    registry = SourceRegistry(Database(tmp_path / "state" / "state.sqlite"), managed)
    registry.register_project("toy-project", "Toy project", repo,
                              origin_kind=origin_kind, origin_reference="owner-handle")
    remote = tmp_path / "github-receiver.git"
    run_git(tmp_path, ["init", "--bare", "-b", "main", str(remote)])
    return Case(repo, registry, root, child, remote)


class FakeNetwork:
    def __init__(self, case: Case) -> None:
        self.case = case
        self.calls: list[list[str]] = []
        self.push_count = 0
        self.timeout_after_push = False
        self.timeout_before_push = False
        self.read_unavailable_after_push = False

    def __call__(self, cwd: Path, operation: list[str], token: bytes, *,
                 timeout: float) -> bytes:
        assert cwd == self.case.repo and token == TOKEN.encode()
        assert URL in operation and timeout in {30, 120}
        self.calls.append(operation)
        if operation[0] == "ls-remote":
            if self.read_unavailable_after_push and self.push_count:
                raise GitError("GITHUB_GIT_TIMEOUT", "Synthetic read outage")
            return run_git(cwd, ["ls-remote", "--refs", str(self.case.remote)]).encode()
        assert operation[0] == "push" and timeout == 120
        assert operation[-1] == f"{self.case.imported_head}:refs/heads/main"
        assert all("--force" not in part and not part.startswith("+") for part in operation)
        self.push_count += 1
        if self.timeout_before_push:
            raise GitError("GITHUB_GIT_TIMEOUT", "Synthetic no-write timeout")
        run_git(cwd, ["push", "--porcelain", "--no-verify", "--no-follow-tags",
                      str(self.case.remote), operation[-1]])
        if self.timeout_after_push:
            raise GitError("GITHUB_GIT_TIMEOUT", "Synthetic lost response")
        return b""


def _bootstrap(case: Case) -> Any:
    return GitHubGitTransport(_repository(), lambda: TOKEN).bootstrap_imported_main(
        case.repo, case.imported_head, registry=case.registry,
        project_id="toy-project", expected_source_branch="feature/imported",
        inspector=FakeInspector(), provision_marker=MARKER,
    )


def _reconcile(case: Case) -> Any:
    return GitHubGitTransport(_repository(), lambda: TOKEN).reconcile_imported_main(
        case.repo, case.imported_head, registry=case.registry,
        project_id="toy-project", expected_source_branch="feature/imported",
        inspector=FakeInspector(), provision_marker=MARKER,
    )


def test_non_root_imported_head_pushes_full_history_to_empty_main_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path)
    network = FakeNetwork(case)
    monkeypatch.setattr("runner_app.git.github_transport._run_network_git", network)
    first = _bootstrap(case)
    assert first.disposition == "created" and first.commit == case.imported_head
    assert first.repository_id == 101
    assert network.push_count == 1
    assert run_git(case.remote, ["rev-parse", "refs/heads/main"]).strip() == case.imported_head
    assert run_git(case.remote, ["rev-list", "--count", "refs/heads/main"]).strip() == "2"
    assert run_git(case.remote, ["merge-base", "--is-ancestor", case.root_commit,
                                "refs/heads/main"]) == ""
    assert run_git(case.repo, ["symbolic-ref", "HEAD"]).strip() == "refs/heads/feature/imported"
    assert _bootstrap(case).disposition == "attached"
    assert _reconcile(case).disposition == "reconciled"
    assert network.push_count == 1


def test_imported_push_lost_response_reconciles_without_repush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path)
    network = FakeNetwork(case)
    network.timeout_after_push = True
    monkeypatch.setattr("runner_app.git.github_transport._run_network_git", network)
    result = _bootstrap(case)
    assert result.disposition == "reconciled"
    assert _reconcile(case).disposition == "reconciled"
    assert network.push_count == 1


def test_imported_push_unknown_requires_explicit_read_only_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path)
    network = FakeNetwork(case)
    network.timeout_before_push = True
    monkeypatch.setattr("runner_app.git.github_transport._run_network_git", network)
    with pytest.raises(GitError) as error:
        _bootstrap(case)
    assert error.value.code == "GITHUB_IMPORT_PUSH_UNKNOWN"
    with pytest.raises(GitError) as error:
        _reconcile(case)
    assert error.value.code == "GITHUB_IMPORT_PUSH_UNKNOWN"
    assert network.push_count == 1


def test_imported_successful_push_with_lost_readback_keeps_unknown_until_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path)
    network = FakeNetwork(case)
    network.read_unavailable_after_push = True
    monkeypatch.setattr("runner_app.git.github_transport._run_network_git", network)
    with pytest.raises(GitError) as error:
        _bootstrap(case)
    assert error.value.code == "GITHUB_IMPORT_PUSH_UNKNOWN"
    assert network.push_count == 1
    network.read_unavailable_after_push = False
    assert _reconcile(case).disposition == "reconciled"
    assert network.push_count == 1


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/other", "refs/tags/v1"])
def test_imported_main_refuses_nonempty_or_divergent_receiver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ref: str,
) -> None:
    case = _case(tmp_path)
    run_git(case.repo, ["push", str(case.remote), f"{case.root_commit}:{ref}"])
    network = FakeNetwork(case)
    monkeypatch.setattr("runner_app.git.github_transport._run_network_git", network)
    with pytest.raises(GitError) as error:
        _bootstrap(case)
    assert error.value.code == "GITHUB_IMPORT_BASELINE_CONFLICT"
    assert network.push_count == 0


def test_imported_main_refuses_wrong_origin_or_changed_source_head_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong = _case(tmp_path / "wrong", origin_kind="new")
    monkeypatch.setattr("runner_app.git.github_transport._run_network_git",
                        lambda *_args, **_kwargs: pytest.fail("network must not start"))
    with pytest.raises(GitError) as error:
        _bootstrap(wrong)
    assert error.value.code == "GITHUB_IMPORT_SOURCE_INVALID"

    changed = _case(tmp_path / "changed")
    run_git(changed.repo, ["symbolic-ref", "HEAD", "refs/heads/main"])
    with pytest.raises(GitError) as error:
        _bootstrap(changed)
    assert error.value.code == "GITHUB_IMPORT_HEAD_CHANGED"


@pytest.mark.parametrize("path,expected_code", [
    (".gitmodules", "SUBMODULE_UNSUPPORTED"),
    (".lfsconfig", "LFS_UNSUPPORTED"),
])
def test_imported_main_refuses_unsafe_head_tree_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    path: str, expected_code: str,
) -> None:
    case = _case(tmp_path)
    blob = run_git_input(case.repo, ["hash-object", "-w", "--stdin"], b"unsafe source\n").strip()
    tree = run_git_input(case.repo, ["mktree"],
                         f"100644 blob {blob}\t{path}\n".encode()).strip()
    unsafe = run_git_input(
        case.repo,
        ["-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
         "commit-tree", tree, "-p", case.imported_head],
        b"Unsafe imported HEAD\n",
    ).strip()
    run_git(case.repo, ["update-ref", "refs/heads/feature/imported", unsafe,
                        case.imported_head])
    monkeypatch.setattr("runner_app.git.github_transport._run_network_git",
                        lambda *_args, **_kwargs: pytest.fail("network must not start"))
    with pytest.raises(GitError) as error:
        GitHubGitTransport(_repository(), lambda: TOKEN).bootstrap_imported_main(
            case.repo, unsafe, registry=case.registry, project_id="toy-project",
            expected_source_branch="feature/imported", inspector=FakeInspector(),
            provision_marker=MARKER,
        )
    assert error.value.code == expected_code


def test_imported_main_refuses_replace_history_and_stale_marker_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path)
    monkeypatch.setattr("runner_app.git.github_transport._run_network_git",
                        lambda *_args, **_kwargs: pytest.fail("network must not start"))
    with pytest.raises(GitError) as error:
        GitHubGitTransport(_repository(), lambda: TOKEN).bootstrap_imported_main(
            case.repo, case.imported_head, registry=case.registry,
            project_id="toy-project", expected_source_branch="feature/imported",
            inspector=FakeInspector(), provision_marker="cdr:v1:" + "0" * 64,
        )
    assert error.value.code == "GITHUB_REPOSITORY_UNVERIFIED"
    run_git(case.repo, ["update-ref", f"refs/replace/{case.root_commit}", case.imported_head])
    with pytest.raises(GitError) as error:
        _bootstrap(case)
    assert error.value.code == "GITHUB_IMPORT_HISTORY_UNSAFE"


def test_imported_main_rechecks_remote_emptiness_immediately_before_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path)
    network = FakeNetwork(case)
    reads = 0

    def concurrent_ref(cwd: Path, operation: list[str], token: bytes, *,
                       timeout: float) -> bytes:
        nonlocal reads
        if operation[0] == "ls-remote":
            reads += 1
            if reads == 2:
                run_git(case.repo, ["push", str(case.remote),
                                    f"{case.root_commit}:refs/tags/concurrent"])
        return network(cwd, operation, token, timeout=timeout)

    monkeypatch.setattr("runner_app.git.github_transport._run_network_git", concurrent_ref)
    with pytest.raises(GitError) as error:
        _bootstrap(case)
    assert error.value.code == "GITHUB_IMPORT_BASELINE_CONFLICT"
    assert network.push_count == 0
    assert reads == 2
