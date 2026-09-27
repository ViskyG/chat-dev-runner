"""Owner-only GitHub Git transport tests; no network or real credentials."""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from runner_app.adapters.github import GitHubRepository, make_provision_marker
from runner_app.git.argv import GitError, run_git, run_git_input
from runner_app.git.github_transport import (
    GitHubGitTransport,
    OwnerTokenFile,
    _child_environment,
    _command,
    _credential_fds,
)
from runner_app.git.worktrees import create_repository

TOKEN = "fixture-secret-token"
URL = "https://github.com/alice/notes-lab.git"
MARKER = make_provision_marker("installation", "toy-project", "operation")


def _repository() -> GitHubRepository:
    return GitHubRepository(
        id=101, node_id="R_101", owner="alice", owner_id=1, name="notes-lab",
        full_name="alice/notes-lab", html_url="https://github.com/alice/notes-lab",
        clone_url=URL, ssh_url="git@github.com:alice/notes-lab.git",
        private=True, default_branch="main", description=MARKER,
    )


def _managed_repo(tmp_path: Path) -> Path:
    root = tmp_path / "managed"
    root.mkdir(mode=0o700)
    return create_repository(root, "toy-project")


def test_owner_token_file_requires_owner_only_regular_storage(tmp_path: Path) -> None:
    private = tmp_path / "credentials"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    token_file = private / "github-token"
    token_file.write_text(TOKEN + "\n")
    token_file.chmod(0o600)
    source = OwnerTokenFile(token_file)
    assert source() == TOKEN

    token_file.chmod(0o644)
    with pytest.raises(GitError) as error:
        source()
    assert error.value.code == "GITHUB_AUTH_UNSAFE"
    token_file.chmod(0o600)
    private.chmod(0o755)
    with pytest.raises(GitError) as error:
        source()
    assert error.value.code == "GITHUB_AUTH_UNSAFE"
    private.chmod(0o700)
    link = private / "link"
    link.symlink_to(token_file)
    with pytest.raises(GitError) as error:
        OwnerTokenFile(link)()
    assert error.value.code == "GITHUB_AUTH_NOT_CONFIGURED"


def test_anonymous_askpass_works_with_local_git_credential_fill_only() -> None:
    with _credential_fds(TOKEN.encode()) as (script_fd, token_fd):
        argv = _command(["credential", "fill"])
        env = _child_environment(script_fd, token_fd)
        assert TOKEN not in "\n".join(argv)
        assert all(TOKEN not in value for value in env.values())
        assert "HTTPS_PROXY" not in env and "GIT_TRACE" not in env
        assert env["GIT_ASKPASS"] == f"/proc/self/fd/{script_fd}"
        assert os.readlink(f"/proc/self/fd/{token_fd}").startswith("/memfd:")
        process = subprocess.run(
            argv, input=b"protocol=https\nhost=github.com\npath=alice/notes-lab.git\n\n",
            capture_output=True, env=env, check=False,
            pass_fds=(script_fd, token_fd), timeout=10,
        )
        assert process.returncode == 0
        assert b"username=x-access-token" in process.stdout
        assert b"password=" + TOKEN.encode() in process.stdout
        assert TOKEN.encode() not in process.stderr
        off_host = subprocess.run(
            argv, input=b"protocol=https\nhost=evil.example\n\n",
            capture_output=True, env=env, check=False,
            pass_fds=(script_fd, token_fd), timeout=10,
        )
        assert off_host.returncode != 0
        assert TOKEN.encode() not in off_host.stdout + off_host.stderr


def test_command_fixes_https_no_redirect_proxy_or_force() -> None:
    command = _command(["push", "--porcelain", URL,
                        "a" * 40 + ":refs/heads/work/item"])
    settings = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-c"]
    assert "protocol.allow=never" in settings
    assert "protocol.file.allow=never" in settings
    assert "protocol.https.allow=always" in settings
    assert "credential.helper=" in settings
    assert "http.followRedirects=false" in settings
    assert "http.sslVerify=true" in settings
    assert "http.proxy=" in settings
    assert "http.extraHeader=" in settings
    assert not any("--force" in value or value.startswith("+") for value in command)
    assert TOKEN not in "\n".join(command)


def test_exact_approved_private_url_is_required() -> None:
    with pytest.raises(GitError) as error:
        GitHubGitTransport(replace(_repository(), private=False), lambda: TOKEN)
    assert error.value.code == "GITHUB_REPOSITORY_UNAPPROVED"
    with pytest.raises(GitError) as error:
        GitHubGitTransport(replace(_repository(), clone_url="https://evil.example/repo.git"),
                           lambda: TOKEN)
    assert error.value.code == "GITHUB_REPOSITORY_UNAPPROVED"
    with pytest.raises(GitError) as error:
        GitHubGitTransport(replace(_repository(), full_name="other/notes-lab"), lambda: TOKEN)
    assert error.value.code == "GITHUB_REPOSITORY_UNAPPROVED"


def test_exact_remote_read_and_normal_push_leave_repo_config_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _managed_repo(tmp_path)
    initial_config = run_git(repo, ["config", "--local", "--null", "--list"])
    commit = run_git(repo, ["rev-parse", "--verify", "refs/heads/main"]).strip()
    seen: list[tuple[list[str], bytes]] = []

    def fake_network(path: Path, operation: list[str], token: bytes, *, timeout: float) -> bytes:
        assert path == repo and timeout in {30, 120}
        seen.append((operation, token))
        if operation[0] == "ls-remote":
            return (commit + "\trefs/heads/work/item\n").encode()
        return b""

    monkeypatch.setattr("runner_app.git.github_transport._run_network_git", fake_network)
    transport = GitHubGitTransport(_repository(), lambda: TOKEN)
    assert transport.read_head(repo, "work/item") == commit
    transport.push_commit(repo, commit, "work/item")
    assert seen[0][0] == ["ls-remote", "--refs", "--heads", URL,
                          "refs/heads/work/item"]
    assert seen[1][0] == [
        "push", "--porcelain", "--no-verify", "--no-follow-tags",
        "--recurse-submodules=no", URL, f"{commit}:refs/heads/work/item",
    ]
    assert all(token == TOKEN.encode() for _, token in seen)
    assert run_git(repo, ["config", "--local", "--null", "--list"]) == initial_config


def test_default_branch_and_unsafe_local_network_config_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _managed_repo(tmp_path)
    commit = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()
    monkeypatch.setattr("runner_app.git.github_transport._run_network_git",
                        lambda *_args, **_kwargs: pytest.fail("network must not start"))
    transport = GitHubGitTransport(_repository(), lambda: TOKEN)
    with pytest.raises(GitError) as error:
        transport.push_commit(repo, commit, "main")
    assert error.value.code == "PROTECTED_BRANCH"
    run_git(repo, ["config", "--local", "http.proxy", "https://evil.example"])
    with pytest.raises(GitError) as error:
        transport.read_head(repo, "work/item")
    assert error.value.code == "UNSAFE_GIT_CONFIG"


def test_token_inside_managed_source_and_provider_exception_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _managed_repo(tmp_path)
    inside = repo.parent / "token"
    inside.write_text(TOKEN)
    inside.chmod(0o600)
    transport = GitHubGitTransport(_repository(), OwnerTokenFile(inside))
    with pytest.raises(GitError) as error:
        transport.read_head(repo, "work/item")
    assert error.value.code == "GITHUB_AUTH_UNSAFE"

    def bad_provider() -> str:
        raise RuntimeError(TOKEN)

    transport = GitHubGitTransport(_repository(), bad_provider)
    monkeypatch.setattr("runner_app.git.github_transport._run_network_git",
                        lambda *_args, **_kwargs: pytest.fail("network must not start"))
    with pytest.raises(GitError) as error:
        transport.read_head(repo, "work/item")
    assert error.value.code == "GITHUB_AUTH_NOT_CONFIGURED"
    assert TOKEN not in str(error.value)



class FakeInspector:
    def __init__(self, current: GitHubRepository | None = None) -> None:
        self.current = current or _repository()
        self.calls: list[tuple[str, str, int | None]] = []

    def get_repository(self, owner: str, name: str, *,
                       expected_repo_id: int | None = None) -> GitHubRepository:
        self.calls.append((owner, name, expected_repo_id))
        return self.current


def test_baseline_main_bootstrap_uses_fresh_identity_empty_remote_and_exact_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _managed_repo(tmp_path)
    commit = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()
    inspector = FakeInspector()
    calls: list[list[str]] = []
    remote: dict[str, str] = {}

    def fake_network(path: Path, operation: list[str], token: bytes, *, timeout: float) -> bytes:
        assert path == repo and token == TOKEN.encode() and timeout in {30, 120}
        calls.append(operation)
        if operation[0] == "ls-remote":
            return b"".join(f"{oid}\t{ref}\n".encode() for ref, oid in remote.items())
        assert operation == [
            "push", "--porcelain", "--no-verify", "--no-follow-tags",
            "--recurse-submodules=no", URL, f"{commit}:refs/heads/main",
        ]
        remote["refs/heads/main"] = commit
        return b""

    monkeypatch.setattr("runner_app.git.github_transport._run_network_git", fake_network)
    result = GitHubGitTransport(_repository(), lambda: TOKEN).bootstrap_main(
        repo, commit, inspector=inspector, provision_marker=MARKER,
    )
    assert result.disposition == "created" and result.commit == commit
    assert inspector.calls == [("alice", "notes-lab", 101)]
    assert [call[0] for call in calls] == ["ls-remote", "push", "ls-remote"]
    assert not any("--force" in item or item.startswith("+") for call in calls for item in call)


def test_baseline_main_existing_exact_ref_attaches_without_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _managed_repo(tmp_path)
    commit = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()
    calls: list[list[str]] = []

    def fake_network(_path: Path, operation: list[str], _token: bytes, *,
                     timeout: float) -> bytes:
        calls.append(operation)
        assert timeout == 30 and operation[0] == "ls-remote"
        return f"{commit}\trefs/heads/main\n".encode()

    monkeypatch.setattr("runner_app.git.github_transport._run_network_git", fake_network)
    transport = GitHubGitTransport(_repository(), lambda: TOKEN)
    assert transport.bootstrap_main(repo, commit, inspector=FakeInspector(), provision_marker=MARKER).disposition == "attached"
    assert transport.reconcile_initial_main(repo, commit,
                                            inspector=FakeInspector(), provision_marker=MARKER).disposition == "reconciled"
    assert all(call[0] == "ls-remote" for call in calls)


@pytest.mark.parametrize("remote_refs", [
    {"refs/heads/main": "c" * 40},
    {"refs/heads/other": "c" * 40},
    {"refs/tags/v1": "c" * 40},
])
def test_baseline_main_refuses_divergent_or_additional_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_refs: dict[str, str],
) -> None:
    repo = _managed_repo(tmp_path)
    commit = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()

    def fake_network(_path: Path, operation: list[str], _token: bytes, *,
                     timeout: float) -> bytes:
        assert timeout == 30 and operation[0] == "ls-remote"
        return b"".join(f"{oid}\t{ref}\n".encode() for ref, oid in remote_refs.items())

    monkeypatch.setattr("runner_app.git.github_transport._run_network_git", fake_network)
    with pytest.raises(GitError) as error:
        GitHubGitTransport(_repository(), lambda: TOKEN).bootstrap_main(
            repo, commit, inspector=FakeInspector(), provision_marker=MARKER,
        )
    assert error.value.code == "GITHUB_BASELINE_CONFLICT"


def test_baseline_push_lost_response_reconciles_without_second_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _managed_repo(tmp_path)
    commit = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()
    remote: dict[str, str] = {}
    push_count = 0

    def fake_network(_path: Path, operation: list[str], _token: bytes, *,
                     timeout: float) -> bytes:
        nonlocal push_count
        if operation[0] == "ls-remote":
            return b"".join(f"{oid}\t{ref}\n".encode() for ref, oid in remote.items())
        push_count += 1
        remote["refs/heads/main"] = commit
        raise GitError("GITHUB_GIT_TIMEOUT", "Synthetic lost response")

    monkeypatch.setattr("runner_app.git.github_transport._run_network_git", fake_network)
    transport = GitHubGitTransport(_repository(), lambda: TOKEN)
    result = transport.bootstrap_main(repo, commit, inspector=FakeInspector(), provision_marker=MARKER)
    assert result.disposition == "reconciled" and push_count == 1
    assert transport.reconcile_initial_main(repo, commit,
                                            inspector=FakeInspector(), provision_marker=MARKER).disposition == "reconciled"
    assert push_count == 1


def test_baseline_unknown_does_not_retry_and_reconciliation_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _managed_repo(tmp_path)
    commit = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()
    push_count = 0

    def fake_network(_path: Path, operation: list[str], _token: bytes, *,
                     timeout: float) -> bytes:
        nonlocal push_count
        if operation[0] == "ls-remote":
            return b""
        push_count += 1
        raise GitError("GITHUB_GIT_TIMEOUT", "Synthetic lost response")

    monkeypatch.setattr("runner_app.git.github_transport._run_network_git", fake_network)
    transport = GitHubGitTransport(_repository(), lambda: TOKEN)
    with pytest.raises(GitError) as error:
        transport.bootstrap_main(repo, commit, inspector=FakeInspector(), provision_marker=MARKER)
    assert error.value.code == "GITHUB_BASELINE_PUSH_UNKNOWN"
    with pytest.raises(GitError) as error:
        transport.reconcile_initial_main(repo, commit, inspector=FakeInspector(), provision_marker=MARKER)
    assert error.value.code == "GITHUB_BASELINE_PUSH_UNKNOWN"
    assert push_count == 1


def test_baseline_stale_rest_identity_and_non_root_commit_stop_before_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _managed_repo(tmp_path)
    commit = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()
    monkeypatch.setattr("runner_app.git.github_transport._run_network_git",
                        lambda *_args, **_kwargs: pytest.fail("remote must not be contacted"))
    transport = GitHubGitTransport(_repository(), lambda: TOKEN)
    stale = FakeInspector(replace(_repository(), id=999))
    with pytest.raises(GitError) as error:
        transport.bootstrap_main(repo, commit, inspector=stale, provision_marker=MARKER)
    assert error.value.code == "GITHUB_REPOSITORY_UNVERIFIED"
    tree = run_git(repo, ["rev-parse", f"{commit}^{{tree}}"]).strip()
    child = run_git_input(
        repo,
        ["-c", "user.name=Fixture", "-c", "user.email=fixture@localhost.invalid",
         "commit-tree", tree, "-p", commit],
        b"Child commit\n",
    ).strip()
    run_git(repo, ["update-ref", "refs/heads/main", child, commit])
    with pytest.raises(GitError) as error:
        transport.bootstrap_main(repo, child, inspector=FakeInspector(), provision_marker=MARKER)
    assert error.value.code == "INVALID_COMMIT"


def test_baseline_requires_the_persisted_marker_before_remote_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _managed_repo(tmp_path)
    commit = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()
    monkeypatch.setattr("runner_app.git.github_transport._run_network_git",
                        lambda *_args, **_kwargs: pytest.fail("remote must not be contacted"))
    with pytest.raises(GitError) as error:
        GitHubGitTransport(_repository(), lambda: TOKEN).bootstrap_main(
            repo, commit, inspector=FakeInspector(), provision_marker="cdr:v1:" + "0" * 64,
        )
    assert error.value.code == "GITHUB_REPOSITORY_UNVERIFIED"


def test_bare_clone_uses_exact_url_and_never_places_token_in_process_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import runner_app.git.github_transport as transport_module

    stage = tmp_path / "managed" / "toy-project" / ".github-import-fixture"
    stage.mkdir(mode=0o700, parents=True)
    destination = stage / "repo.git"
    observed: list[tuple[Path, list[str], bytes, float]] = []

    def fake_network(cwd: Path, operation: list[str], token: bytes, *,
                     timeout: float) -> bytes:
        observed.append((cwd, operation, token, timeout))
        return b""

    monkeypatch.setattr(transport_module, "_run_network_git", fake_network)
    transport = GitHubGitTransport(_repository(), lambda: TOKEN)
    transport.clone_bare(stage, destination)
    assert len(observed) == 1
    cwd, operation, secret, timeout = observed[0]
    assert cwd == stage and timeout == 300
    assert operation == [
        "clone", "--bare", "--no-local", "--no-hardlinks", "--no-tags",
        URL, str(destination),
    ]
    assert TOKEN not in " ".join(operation)
    assert secret == TOKEN.encode()
    assert not destination.exists()


def test_bare_clone_refuses_path_adoption_and_credential_inside_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import runner_app.git.github_transport as transport_module

    project = tmp_path / "managed" / "toy-project"
    stage = project / ".github-import-fixture"
    stage.mkdir(mode=0o700, parents=True)
    transport = GitHubGitTransport(_repository(), lambda: TOKEN)
    monkeypatch.setattr(
        transport_module, "_run_network_git",
        lambda *_args, **_kwargs: pytest.fail("unapproved clone must not run"),
    )
    with pytest.raises(GitError) as error:
        transport.clone_bare(stage, project / "other.git")
    assert error.value.code == "INVALID_IMPORT_STAGE"
    destination = stage / "repo.git"
    destination.mkdir(mode=0o700)
    with pytest.raises(GitError) as error:
        transport.clone_bare(stage, destination)
    assert error.value.code == "INVALID_IMPORT_STAGE"
    destination.rmdir()
    token = project / "token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    local_token_transport = GitHubGitTransport(_repository(), OwnerTokenFile(token))
    with pytest.raises(GitError) as error:
        local_token_transport.clone_bare(stage, destination)
    assert error.value.code == "GITHUB_AUTH_UNSAFE"


@pytest.mark.parametrize("stored_default,observed_default", [
    ("develop", "develop"), (None, None), ("main", "develop"), ("main", None),
])
def test_baseline_reports_unsupported_default_branch_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    stored_default: str | None, observed_default: str | None,
) -> None:
    repo = _managed_repo(tmp_path)
    commit = run_git(repo, ["rev-parse", "refs/heads/main"]).strip()
    stored = replace(_repository(), default_branch=stored_default)
    observed = replace(_repository(), default_branch=observed_default)
    monkeypatch.setattr("runner_app.git.github_transport._run_network_git",
                        lambda *_args, **_kwargs: pytest.fail("network must not start"))
    transport = GitHubGitTransport(stored, lambda: TOKEN)
    with pytest.raises(GitError) as error:
        transport.bootstrap_main(
            repo, commit, inspector=FakeInspector(observed), provision_marker=MARKER,
        )
    assert error.value.code == "DEFAULT_BRANCH_UNSUPPORTED"
